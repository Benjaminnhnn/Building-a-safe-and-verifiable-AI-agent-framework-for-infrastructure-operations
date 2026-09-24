"""Safety Policy Engine — deterministic gate with YAML-configurable rules.

Decisions: ALLOW, REQUIRE_APPROVAL, DENY, HUMAN_ONLY
RBac roles: observer, diagnosis, planner, safety, executor, verifier, approver
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BlastRadius(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"
    HUMAN_ONLY = "HUMAN_ONLY"


# ---------------------------------------------------------------------------
# Policy models
# ---------------------------------------------------------------------------


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    description: str
    conditions: dict[str, Any]
    decision: PolicyDecision
    requires_rollback_ready: bool = False


class RBACPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    allowed_stages: list[str]
    can_execute: bool = False
    can_approve: bool = False
    read_only: bool = False


# ---------------------------------------------------------------------------
# Audit record
# ---------------------------------------------------------------------------


def _sha256_of(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    incident_id: str
    action_id: str
    actor_role: str
    decision: PolicyDecision
    reason: str
    evidence_count: int
    confidence: float
    blast_radius: BlastRadius
    rollback_ready: bool
    timestamp: datetime
    sha256: str

    @classmethod
    def create(
        cls,
        *,
        incident_id: str,
        action_id: str,
        actor_role: str,
        decision: PolicyDecision,
        reason: str,
        evidence_count: int,
        confidence: float,
        blast_radius: BlastRadius,
        rollback_ready: bool,
    ) -> AuditRecord:
        audit_id = str(uuid.uuid4())
        timestamp = datetime.now(tz=timezone.utc)
        payload: dict[str, Any] = {
            "audit_id": audit_id,
            "incident_id": incident_id,
            "action_id": action_id,
            "actor_role": actor_role,
            "decision": decision.value,
            "reason": reason,
            "evidence_count": evidence_count,
            "confidence": confidence,
            "blast_radius": blast_radius.value,
            "rollback_ready": rollback_ready,
            "timestamp": timestamp.isoformat(),
        }
        sha = _sha256_of(payload)
        return cls(
            audit_id=audit_id,
            incident_id=incident_id,
            action_id=action_id,
            actor_role=actor_role,
            decision=decision,
            reason=reason,
            evidence_count=evidence_count,
            confidence=confidence,
            blast_radius=blast_radius,
            rollback_ready=rollback_ready,
            timestamp=timestamp,
            sha256=sha,
        )


# ---------------------------------------------------------------------------
# Forbidden-action patterns (applied before any rule)
# ---------------------------------------------------------------------------

_FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bdelete[_\s]database",  # matches delete_database, delete_database_schema, etc.
        r"\bdrop\b",
        r"\btruncate\b",
        r"\bdelete[_\s]volume\b",
        r"\bunrestricted[_\s]shell\b",
        r"\biam\b",
        r"\bsecurity[_\s]group\b",
        r"\bbusiness[_\s]data[_\s]rollback\b",
        r"\brollback[_\s]business[_\s]data\b",
    ]
]


def _is_forbidden_action(action: str) -> bool:
    return any(p.search(action) for p in _FORBIDDEN_PATTERNS)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SafetyPolicyEngine:
    """Deterministic policy engine — fail-closed (unknown → DENY)."""

    def __init__(self, rules: list[PolicyRule], rbac: list[RBACPolicy]) -> None:
        self._rules = rules
        self._rbac: dict[str, RBACPolicy] = {r.role: r for r in rbac}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        *,
        action: str,
        environment: str,
        blast_radius: BlastRadius,
        confidence: float,
        evidence_count: int,
        rollback_ready: bool,
        actor_role: str,
        is_reversible: bool,
    ) -> tuple[PolicyDecision, str]:
        """Evaluate an action against the policy matrix.

        Returns (decision, reason).  Fail-closed: unmatched → DENY.
        """
        # Hard DENY: forbidden action patterns
        if _is_forbidden_action(action):
            return (
                PolicyDecision.DENY,
                f"Action '{action}' matches a forbidden-action pattern (IAM/security group/drop/truncate/delete-volume/unrestricted-shell).",
            )

        # RBAC gate: executor role required for non-observer stages
        rbac_policy = self._rbac.get(actor_role)
        if rbac_policy is None:
            return PolicyDecision.DENY, f"Unknown role '{actor_role}' — denied by fail-closed policy."

        # Evaluate ordered rules
        for rule in self._rules:
            decision, reason = self._match_rule(
                rule,
                action=action,
                environment=environment,
                blast_radius=blast_radius,
                confidence=confidence,
                evidence_count=evidence_count,
                rollback_ready=rollback_ready,
                actor_role=actor_role,
                is_reversible=is_reversible,
            )
            if decision is not None:
                return decision, reason

        # Fail-closed
        return PolicyDecision.DENY, "No matching policy rule — denied by fail-closed default."

    def check_rbac(self, role: str, stage: str) -> bool:
        """Return True if *role* is allowed in *stage*."""
        policy = self._rbac.get(role)
        if policy is None:
            return False
        return stage in policy.allowed_stages

    # ------------------------------------------------------------------
    # Rule matching (internal)
    # ------------------------------------------------------------------

    def _match_rule(
        self,
        rule: PolicyRule,
        *,
        action: str,
        environment: str,
        blast_radius: BlastRadius,
        confidence: float,
        evidence_count: int,
        rollback_ready: bool,
        actor_role: str,
        is_reversible: bool,
    ) -> tuple[PolicyDecision | None, str]:
        cond = rule.conditions

        # action_pattern
        ap = cond.get("action_pattern")
        if ap is not None and not re.search(ap, action, re.IGNORECASE):
            return None, ""

        # environment
        env_cond = cond.get("environment")
        if env_cond is not None and environment != env_cond:
            return None, ""

        # environment_not
        env_not = cond.get("environment_not")
        if env_not is not None and environment == env_not:
            return None, ""

        # blast_radius
        br_cond = cond.get("blast_radius")
        if br_cond is not None and blast_radius.value != br_cond:
            return None, ""

        # blast_radius_in
        br_in = cond.get("blast_radius_in")
        if br_in is not None and blast_radius.value not in br_in:
            return None, ""

        # blast_radius_not — exclude specific blast radius values
        br_not = cond.get("blast_radius_not")
        if br_not is not None and blast_radius.value == br_not:
            return None, ""

        # confidence_min
        conf_min = cond.get("confidence_min")
        if conf_min is not None and confidence < conf_min:
            return None, ""

        # confidence_max (exclusive upper bound for range matching)
        conf_max = cond.get("confidence_max")
        if conf_max is not None and confidence >= conf_max:
            return None, ""

        # confidence_below
        conf_below = cond.get("confidence_below")
        if conf_below is not None and confidence >= conf_below:
            return None, ""

        # evidence_min
        ev_min = cond.get("evidence_min")
        if ev_min is not None and evidence_count < ev_min:
            return None, ""

        # reversible
        rev_cond = cond.get("reversible")
        if rev_cond is not None and is_reversible != rev_cond:
            return None, ""

        # rollback_ready
        rr_cond = cond.get("rollback_ready")
        if rr_cond is not None and rollback_ready != rr_cond:
            return None, ""

        # role_requires_can_execute
        if cond.get("role_requires_can_execute"):
            rp = self._rbac.get(actor_role)
            if rp is None or not rp.can_execute:
                return None, ""

        # All conditions matched
        if rule.requires_rollback_ready and not rollback_ready:
            return (
                PolicyDecision.DENY,
                f"Rule '{rule.rule_id}' requires rollback_ready=True but rollback is not ready.",
            )

        return rule.decision, f"Matched rule '{rule.rule_id}': {rule.description}"

    # ------------------------------------------------------------------
    # Default rules factory
    # ------------------------------------------------------------------

    @classmethod
    def load_default_rules(cls) -> SafetyPolicyEngine:
        """Return an engine pre-loaded with the default policy matrix."""
        rules: list[PolicyRule] = [
            # R01 – read-only probe on allowlisted target
            PolicyRule(
                rule_id="R01",
                description="Read-only probe on allowlisted target with read-only credential → ALLOW",
                conditions={
                    "action_pattern": r"^(verify|probe|check|inspect|read)_",
                    "reversible": True,
                },
                decision=PolicyDecision.ALLOW,
            ),
            # R02 – staging + high confidence + sufficient evidence + reversible + rollback ready
            PolicyRule(
                rule_id="R02",
                description="Staging, confidence ≥0.80, evidence ≥3, reversible, rollback ready → ALLOW",
                conditions={
                    "environment": "staging",
                    "confidence_min": 0.80,
                    "evidence_min": 3,
                    "reversible": True,
                    "rollback_ready": True,
                    "role_requires_can_execute": True,
                    "blast_radius_not": "HIGH",
                },
                decision=PolicyDecision.ALLOW,
                requires_rollback_ready=True,
            ),
            # R03 – medium confidence range (0.65–0.79) + reversible → REQUIRE_APPROVAL
            PolicyRule(
                rule_id="R03",
                description="Confidence 0.65–0.79, action reversible → REQUIRE_APPROVAL",
                conditions={
                    "confidence_min": 0.65,
                    "confidence_max": 0.80,
                    "reversible": True,
                },
                decision=PolicyDecision.REQUIRE_APPROVAL,
            ),
            # R04 – MEDIUM blast radius + reversible → REQUIRE_APPROVAL
            PolicyRule(
                rule_id="R04",
                description="Blast radius MEDIUM and action reversible → REQUIRE_APPROVAL",
                conditions={
                    "blast_radius": "MEDIUM",
                    "reversible": True,
                },
                decision=PolicyDecision.REQUIRE_APPROVAL,
            ),
            # R05 – HIGH blast radius → REQUIRE_APPROVAL (at minimum)
            PolicyRule(
                rule_id="R05",
                description="Blast radius HIGH → REQUIRE_APPROVAL",
                conditions={
                    "blast_radius": "HIGH",
                },
                decision=PolicyDecision.REQUIRE_APPROVAL,
            ),
            # R06 – production environment → REQUIRE_APPROVAL
            PolicyRule(
                rule_id="R06",
                description="Production environment with shared dependency or host restart → REQUIRE_APPROVAL",
                conditions={
                    "environment": "production",
                },
                decision=PolicyDecision.REQUIRE_APPROVAL,
            ),
            # R07 – low confidence (below 0.65) → DENY
            PolicyRule(
                rule_id="R07",
                description="Confidence below 0.65 → DENY",
                conditions={
                    "confidence_below": 0.65,
                },
                decision=PolicyDecision.DENY,
            ),
        ]

        rbac: list[RBACPolicy] = [
            RBACPolicy(
                role="observer",
                allowed_stages=["observation"],
                can_execute=False,
                can_approve=False,
                read_only=True,
            ),
            RBACPolicy(
                role="diagnosis",
                allowed_stages=["observation", "diagnosis"],
                can_execute=False,
                can_approve=False,
                read_only=True,
            ),
            RBACPolicy(
                role="planner",
                allowed_stages=["observation", "diagnosis", "planning"],
                can_execute=False,
                can_approve=False,
                read_only=False,
            ),
            RBACPolicy(
                role="safety",
                allowed_stages=["observation", "diagnosis", "planning", "gate"],
                can_execute=False,
                can_approve=False,
                read_only=False,
            ),
            RBACPolicy(
                role="executor",
                allowed_stages=["observation", "diagnosis", "planning", "gate", "execution"],
                can_execute=True,
                can_approve=False,
                read_only=False,
            ),
            RBACPolicy(
                role="verifier",
                allowed_stages=["observation", "verification"],
                can_execute=False,
                can_approve=False,
                read_only=True,
            ),
            RBACPolicy(
                role="approver",
                allowed_stages=["observation", "diagnosis", "planning", "gate", "approval"],
                can_execute=False,
                can_approve=True,
                read_only=False,
            ),
        ]

        return cls(rules=rules, rbac=rbac)
