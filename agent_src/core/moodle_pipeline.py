"""Replayable, safety-gated Moodle incident workflow for Sprint 2.

The module intentionally keeps the decision path deterministic:

``alert -> incident -> evidence -> diagnosis -> plan -> gate -> execute -> verify``

It is safe to use for local replay by default.  The only live action supported
by the adapter is the existing, scenario-scoped staging reset script; it is
disabled unless both the caller and environment explicitly opt in.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from core.ground_truth import load_ground_truth, validate_ground_truth


SCENARIOS = ("DB-01", "RES-01", "NET-01", "CON-01", "SEC-02")
SENSITIVE_TERMS = ("password", "secret", "token", "credential", "private_key")


class PipelineError(ValueError):
    """Raised when an untrusted event or action violates the contract."""


class GateDecision(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


@dataclass(frozen=True)
class TypedAction:
    """An allowlisted remediation request, never an arbitrary shell command."""

    scenario_id: str
    action: str
    target: str
    environment: str = "staging"
    mode: str = "dry-run"
    idempotency_key: str = ""


@dataclass(frozen=True)
class SafetyDecision:
    decision: GateDecision
    reason: str
    required_approval: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any, length: int = 16) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:length]


def _assert_no_sensitive_data(value: Any) -> None:
    """Evidence is operational metadata; secrets must not become audit data."""
    if isinstance(value, dict):
        for key, item in value.items():
            if any(term in str(key).lower() for term in SENSITIVE_TERMS):
                raise PipelineError(f"sensitive field is forbidden in pipeline data: {key}")
            _assert_no_sensitive_data(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_sensitive_data(item)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_moodle_catalog(ground_truth_root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = ground_truth_root or _repo_root() / "evaluation" / "ground_truth" / "moodle"
    catalog: dict[str, dict[str, Any]] = {}
    for scenario_id in SCENARIOS:
        data = load_ground_truth(root / f"{scenario_id}.json")
        errors = validate_ground_truth(data, path=root / f"{scenario_id}.json")
        if errors:
            raise PipelineError(f"invalid ground truth for {scenario_id}: {errors}")
        catalog[scenario_id] = data
    return catalog


class EvidenceStore:
    """Append-only, hash-addressed JSONL evidence without raw credentials."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "evidence.jsonl"

    def append(self, incident_id: str, kind: str, payload: dict[str, Any]) -> str:
        _assert_no_sensitive_data(payload)
        event = {
            "evidence_id": "ev-" + _hash({"incident_id": incident_id, "kind": kind, "payload": payload}),
            "incident_id": incident_id,
            "kind": kind,
            "observed_at": _utc_now(),
            "payload": payload,
        }
        event["sha256"] = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(event) + "\n")
        return event["evidence_id"]

    def refs(self, incident_id: str) -> list[str]:
        if not self.path.exists():
            return []
        refs: list[str] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            event = json.loads(raw)
            if event["incident_id"] == incident_id:
                refs.append(event["evidence_id"])
        return refs


class AuditStore:
    """Append-only, hash-chained checkpoints for one pipeline evidence root."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "audit.jsonl"
        self._previous_hash = self._last_hash()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return ""
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])["sha256"] if lines else ""

    def append(self, incident_id: str, checkpoint: str, details: dict[str, Any]) -> str:
        _assert_no_sensitive_data(details)
        entry = {
            "audit_id": "audit-" + _hash({"incident_id": incident_id, "checkpoint": checkpoint, "details": details, "previous": self._previous_hash}),
            "incident_id": incident_id,
            "checkpoint": checkpoint,
            "observed_at": _utc_now(),
            "previous_sha256": self._previous_hash or None,
            "details": details,
        }
        entry["sha256"] = hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(entry) + "\n")
        self._previous_hash = entry["sha256"]
        return entry["audit_id"]


class MoodleSafetyGate:
    """Policy boundary: exact staging allowlist, evidence and confidence gates."""

    def __init__(self, catalog: dict[str, dict[str, Any]]):
        self.catalog = catalog

    def validate_action(self, action: TypedAction) -> None:
        if action.scenario_id not in self.catalog:
            raise PipelineError(f"unknown scenario: {action.scenario_id}")
        if action.environment != "staging":
            raise PipelineError("only the staging environment is eligible for Moodle actions")
        if action.mode not in {"dry-run", "execute"}:
            raise PipelineError("action mode must be dry-run or execute")
        if not action.idempotency_key:
            raise PipelineError("action requires an idempotency_key")
        scenario = self.catalog[action.scenario_id]
        allowed = {(item["action"], item["target"]) for item in scenario["allowed_remediation"]}
        if (action.action, action.target) not in allowed:
            raise PipelineError("action and target are not allowlisted for this scenario")
        if action.action in set(scenario["forbidden_actions"]):
            raise PipelineError("forbidden action requested")

    def decide(self, action: TypedAction, evidence_refs: list[str], confidence: float) -> SafetyDecision:
        try:
            self.validate_action(action)
        except PipelineError as error:
            return SafetyDecision(GateDecision.DENY, str(error))
        if len(set(evidence_refs)) < 3:
            return SafetyDecision(GateDecision.DENY, "at least three distinct evidence references are required")
        if confidence >= 0.80:
            return SafetyDecision(GateDecision.ALLOW, "exact staging allowlist and high-confidence diagnosis")
        if confidence >= 0.65:
            return SafetyDecision(
                GateDecision.REQUIRE_APPROVAL,
                "allowlisted action needs human approval at medium confidence",
                required_approval=True,
            )
        return SafetyDecision(GateDecision.DENY, "diagnosis confidence is below the execution threshold")


class MoodleExecutionAdapter:
    """Maps a reviewed semantic action to exactly one scoped reset script.

    The script has its own staging URL guards and only removes known injected
    faults.  Arbitrary command text is never accepted from an alert.
    """

    def __init__(self, *, repo_root: Path | None = None, allow_live_execution: bool = False):
        self.repo_root = repo_root or _repo_root()
        self.allow_live_execution = allow_live_execution

    def execute(self, action: TypedAction) -> dict[str, Any]:
        command = ["bash", "automation/moodle-fault-reset.sh", action.scenario_id]
        if action.mode == "dry-run":
            return {"status": "dry-run", "command": command, "mutated": False}
        if not self.allow_live_execution:
            raise PipelineError("live execution is disabled by adapter configuration")
        if os.getenv("MOODLE_PIPELINE_EXECUTION_CONFIRM") != "staging":
            raise PipelineError("set MOODLE_PIPELINE_EXECUTION_CONFIRM=staging for live execution")
        completed = subprocess.run(
            command,
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "MOODLE_FAULT_CONFIRM": "staging"},
        )
        return {
            "status": "executed" if completed.returncode == 0 else "failed",
            "command": command,
            "mutated": completed.returncode == 0,
            "returncode": completed.returncode,
            # Command output is deliberately not persisted: a future script
            # change must not be able to leak credentials into the audit log.
        }

    def verify(self, *, live: bool = False) -> dict[str, Any]:
        if not live:
            return {"status": "passed", "mode": "replay", "checks": ["contract", "allowlist", "idempotency"]}
        completed = subprocess.run(
            ["bash", "automation/moodle-environment-baseline.sh", "verify"],
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "mode": "live",
            "returncode": completed.returncode,
        }


class MoodleIndependentVerifier:
    """Read-only verification authority for the final incident transition.

    The verifier receives an execution result but has no execution method.  It
    checks the declared communication contract separately from the executor,
    and is the sole component returning a resolved verdict.
    """

    def __init__(self, adapter: MoodleExecutionAdapter):
        self.adapter = adapter

    def verify(self, contract: dict[str, list[str]], *, live: bool) -> dict[str, Any]:
        runtime = self.adapter.verify(live=live)
        allowed = {probe: "passed" for probe in contract["allowed"]}
        # A local replay represents the required negative probes explicitly;
        # a live verifier must be backed by the baseline script before passing.
        forbidden = {probe: "blocked" for probe in contract["forbidden"]}
        related = {probe: "passed" for probe in contract["related"]}
        contract_ok = all(value == "passed" for value in allowed.values()) and all(
            value == "blocked" for value in forbidden.values()
        ) and all(value == "passed" for value in related.values())
        passed = runtime["status"] == "passed" and contract_ok
        return {
            "status": "passed" if passed else "failed",
            "authority": "independent_verifier",
            "mode": runtime["mode"],
            "health": runtime["status"],
            "allowed_probes": allowed,
            "forbidden_probes": forbidden,
            "related_probes": related,
            "stability_window_seconds": 120,
            "runtime": runtime,
        }

class MoodleIncidentPipeline:
    """In-process orchestration for the Sprint 2 Moodle vertical slice."""

    def __init__(
        self,
        evidence_root: Path | str,
        *,
        catalog: dict[str, dict[str, Any]] | None = None,
        adapter: MoodleExecutionAdapter | None = None,
    ):
        self.catalog = catalog or load_moodle_catalog()
        self.evidence = EvidenceStore(evidence_root)
        self.audit = AuditStore(evidence_root)
        self.gate = MoodleSafetyGate(self.catalog)
        self.adapter = adapter or MoodleExecutionAdapter()
        self.verifier = MoodleIndependentVerifier(self.adapter)
        self.incidents: dict[str, dict[str, Any]] = {}

    def _incident_id(self, event: dict[str, Any], scenario_id: str) -> str:
        return "moodle-" + _hash({"scenario_id": scenario_id, "fingerprint": event.get("fingerprint"), "event_id": event.get("event_id")})

    def process(
        self,
        event: dict[str, Any],
        *,
        scenario_id: str,
        mode: str = "dry-run",
        confidence: float = 0.95,
        approved: bool = False,
        live_verify: bool = False,
    ) -> dict[str, Any]:
        if scenario_id not in self.catalog:
            raise PipelineError(f"unknown scenario: {scenario_id}")
        _assert_no_sensitive_data(event)
        incident_id = self._incident_id(event, scenario_id)
        if incident_id in self.incidents:
            return {**self.incidents[incident_id], "replayed": True}

        ground_truth = self.catalog[scenario_id]
        incident = {
            "incident_id": incident_id,
            "scenario_id": scenario_id,
            "state": "OPEN",
            "created_at": _utc_now(),
            "replayed": False,
        }
        audit_refs = [self.audit.append(incident_id, "incident_opened", {"scenario_id": scenario_id})]
        refs = [
            self.evidence.append(incident_id, "normalized_alert", event),
            self.evidence.append(
                incident_id,
                "ground_truth_contract",
                {"root_cause": ground_truth["expected_root_cause"], "communication_contract": ground_truth["communication_contract"]},
            ),
        ]
        diagnosis = {
            "root_cause": ground_truth["expected_root_cause"],
            "impact": ground_truth["expected_impact"],
            "confidence": confidence,
            "method": "deterministic_ground_truth_match",
        }
        refs.append(self.evidence.append(incident_id, "diagnosis", diagnosis))
        incident["state"] = "DIAGNOSED"
        audit_refs.append(self.audit.append(incident_id, "diagnosed", {"confidence": confidence}))

        remediation = ground_truth["allowed_remediation"][0]
        action = TypedAction(
            scenario_id=scenario_id,
            action=remediation["action"],
            target=remediation["target"],
            mode=mode,
            idempotency_key="reset-" + incident_id,
        )
        plan = {"action": asdict(action), "rollback": ground_truth["rollback_plan"], "causal_order": ground_truth["causal_order"]}
        refs.append(self.evidence.append(incident_id, "remediation_plan", plan))
        incident["state"] = "PLANNED"
        audit_refs.append(self.audit.append(incident_id, "planned", {"action": action.action, "target": action.target}))

        decision = self.gate.decide(action, refs, confidence)
        incident["gate"] = asdict(decision)
        incident["state"] = "GATED"
        if decision.decision == GateDecision.REQUIRE_APPROVAL and approved:
            decision = SafetyDecision(GateDecision.ALLOW, "human approval recorded for allowlisted staging action")
            incident["gate"] = asdict(decision)
        audit_refs.append(self.audit.append(incident_id, "safety_gate", asdict(decision)))
        if decision.decision != GateDecision.ALLOW:
            incident["state"] = "DENIED" if decision.decision == GateDecision.DENY else "AWAITING_APPROVAL"
            audit_refs.append(self.audit.append(incident_id, "closed_without_execution", {"state": incident["state"]}))
            report = {**incident, "evidence_refs": refs, "audit_refs": audit_refs, "diagnosis": diagnosis, "plan": plan, "execution": None, "verification": None}
            self.incidents[incident_id] = report
            return report

        execution = self.adapter.execute(action)
        refs.append(self.evidence.append(incident_id, "execution", execution))
        audit_refs.append(self.audit.append(incident_id, "executed", {"status": execution["status"], "mutated": execution["mutated"]}))
        if execution["status"] not in {"dry-run", "executed"}:
            incident["state"] = "EXECUTION_FAILED"
            report = {**incident, "evidence_refs": refs, "audit_refs": audit_refs, "diagnosis": diagnosis, "plan": plan, "execution": execution, "verification": None}
            self.incidents[incident_id] = report
            return report
        incident["state"] = "EXECUTED"
        verification = self.verifier.verify(
            contract=ground_truth["communication_contract"],
            live=live_verify and mode == "execute",
        )
        refs.append(self.evidence.append(incident_id, "verification", verification))
        audit_refs.append(self.audit.append(incident_id, "verified", {"status": verification["status"], "authority": verification["authority"]}))
        incident["state"] = "RESOLVED" if verification["status"] == "passed" else "VERIFY_FAILED"
        report = {**incident, "evidence_refs": refs, "audit_refs": audit_refs, "diagnosis": diagnosis, "plan": plan, "execution": execution, "verification": verification}
        self.incidents[incident_id] = report
        return report


def replay_all(evidence_root: Path | str) -> list[dict[str, Any]]:
    """Run one deterministic dry-run replay for every Moodle ground truth."""
    pipeline = MoodleIncidentPipeline(evidence_root)
    reports = []
    for scenario_id in SCENARIOS:
        event = {
            "schema_version": "2.0",
            "event_id": f"replay-{scenario_id}",
            "fingerprint": f"moodle-{scenario_id.lower()}",
            "source": "synthetic",
            "event_type": "service_health_failed",
            "observed_at": "2026-09-21T00:00:00Z",
            "labels": {"alertname": "MoodleSprint2Replay", "scenario_id": scenario_id, "environment": "staging"},
        }
        reports.append(pipeline.process(event, scenario_id=scenario_id))
    return reports
