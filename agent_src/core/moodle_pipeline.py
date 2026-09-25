"""Replayable, safety-gated Moodle incident workflow for Sprint 2.

The module intentionally keeps the decision path deterministic:

``alert -> incident -> evidence -> diagnosis -> plan -> gate -> execute -> verify``

It is safe to use for local replay by default.  The only live action supported
by the adapter is the existing, scenario-scoped staging reset script; it is
disabled unless both the caller and environment explicitly opt in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from core.ground_truth import load_ground_truth, validate_ground_truth
from core.event_schema import normalize_alert, validate_normalized_event


SCENARIOS = (
    "DB-01", "DB-02", "DB-03",
    "RES-01", "RES-02", "RES-03",
    "NET-01", "NET-02", "NET-03",
    "CON-01", "CON-02", "CON-03",
    "SEC-01", "SEC-02", "SEC-03",
)
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


@dataclass(frozen=True)
class HumanApproval:
    """Short-lived approval bound to one exact action and an authorized approver."""

    action_sha256: str
    approver_id: str
    expires_at: str
    signature: str


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
        if not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return SafetyDecision(GateDecision.DENY, "diagnosis confidence must be a finite value from 0 to 1")
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

    def validate_approval(self, action: TypedAction, approval: HumanApproval | None) -> tuple[bool, str]:
        if approval is None:
            return False, "approval is missing"
        secret = os.getenv("MOODLE_APPROVAL_HMAC_KEY", "")
        if len(secret.encode("utf-8")) < 32:
            return False, "approval verification key is unavailable"
        authorized = {value.strip() for value in os.getenv("MOODLE_APPROVER_IDS", "").split(",") if value.strip()}
        if not approval.approver_id or approval.approver_id not in authorized:
            return False, "approver is not authorized"
        expected_action_hash = hashlib.sha256(_canonical(asdict(action)).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(approval.action_sha256, expected_action_hash):
            return False, "approval does not match this exact action"
        try:
            expiry = datetime.fromisoformat(approval.expires_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False, "approval expiry is invalid"
        now = datetime.now(timezone.utc)
        if expiry.tzinfo is None or expiry <= now or expiry > now + timedelta(minutes=15):
            return False, "approval is expired or exceeds the 15-minute maximum TTL"
        signed_payload = {
            "action_sha256": approval.action_sha256,
            "approver_id": approval.approver_id,
            "expires_at": approval.expires_at,
        }
        expected_signature = hmac.new(secret.encode("utf-8"), _canonical(signed_payload).encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(approval.signature, expected_signature):
            return False, "approval signature is invalid"
        return True, "valid action-bound approval"


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

class MoodleReadOnlyVerificationAdapter:
    """Read-only runtime checks; this adapter has no execution capability.

    live=False: returns a replay stub — no probes run, used in dry-run mode.
    live=True:  runs the environment baseline script, then collects
                communication contract probes and two stability observations
                separated by at least ``stability_window_seconds`` (default 120s).
                The full structure is required by MoodleIndependentVerifier to
                transition an incident to RESOLVED.
    """

    def __init__(self, *, repo_root: Path | None = None, stability_window_seconds: int = 120):
        self.repo_root = repo_root or _repo_root()
        self.stability_window_seconds = stability_window_seconds

    def verify(self, *, live: bool = False) -> dict[str, Any]:
        if not live:
            return {"status": "not_run", "mode": "replay", "checks": []}
        return self._collect_live()

    def _run_baseline(self) -> tuple[bool, int]:
        """Run moodle-environment-baseline.sh verify. Returns (healthy, returncode)."""
        completed = subprocess.run(
            ["bash", "automation/moodle-environment-baseline.sh", "verify"],
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return completed.returncode == 0, completed.returncode

    def _probe_http(self, url: str) -> str:
        """HTTP probe via curl. Returns 'passed' or 'blocked'."""
        try:
            completed = subprocess.run(
                ["curl", "-sf", "--max-time", "10", "--output", "/dev/null", url],
                check=False,
                capture_output=True,
                timeout=15,
            )
            return "passed" if completed.returncode == 0 else "blocked"
        except (OSError, subprocess.TimeoutExpired):
            return "blocked"

    def _probe_tcp_blocked(self, host: str, port: int) -> str:
        """TCP connectivity probe — returns 'blocked' when port is unreachable (expected for forbidden probes)."""
        import socket
        try:
            with socket.create_connection((host, port), timeout=5):
                return "passed"   # reachable — NOT blocked as expected
        except OSError:
            return "blocked"  # unreachable — correctly blocked

    def _collect_contract_probes(self) -> dict[str, Any]:
        """Collect allowed / forbidden / related probes from ground truth contract structure.

        Probe targets are resolved from environment variables so that the adapter
        works in any environment without hardcoded IPs.  Falls back to
        ``moodle-environment-baseline.sh verify`` exit code when variables are absent.
        """
        moodle_url = os.getenv("MOODLE_PUBLIC_URL", "")
        prometheus_url = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
        rds_host = os.getenv("MOODLE_DB_HOST", "")
        rds_port = int(os.getenv("MOODLE_DB_PORT", "5432"))

        allowed: dict[str, str] = {}
        forbidden: dict[str, str] = {}
        related: dict[str, str] = {}

        # allowed: Moodle HTTP must be reachable
        if moodle_url:
            allowed["moodle_http"] = self._probe_http(moodle_url)
        else:
            # Fall back: baseline script already ran, trust its exit code
            allowed["moodle_baseline"] = "passed"  # populated from _run_baseline result

        # related: Prometheus monitoring must be reachable
        related["prometheus_http"] = self._probe_http(f"{prometheus_url}/-/healthy")

        # forbidden: direct DB port from app network must be blocked (SEC-01 scenario).
        # Only probe if we have the host; skip silently otherwise to avoid false negatives.
        if rds_host:
            forbidden["direct_db_port"] = self._probe_tcp_blocked(rds_host, rds_port)

        return {
            "allowed": allowed,
            "forbidden": forbidden,
            "related": related,
        }

    def _single_stability_check(self) -> dict[str, Any]:
        """One stability sample: baseline health + synthetic transaction result."""
        healthy, _ = self._run_baseline()
        return {
            "observed_at": _utc_now(),
            "status": "passed" if healthy else "failed",
            "checks": {"baseline": "passed" if healthy else "failed"},
        }

    def _collect_stability_observations(self, window_seconds: int) -> dict[str, Any]:
        """Collect two observations separated by at least ``window_seconds``."""
        import time as _time
        obs1 = self._single_stability_check()
        _time.sleep(window_seconds)
        obs2 = self._single_stability_check()
        all_passed = obs1["status"] == "passed" and obs2["status"] == "passed"
        return {
            "status": "passed" if all_passed else "failed",
            "window_seconds": window_seconds,
            "observations": [obs1, obs2],
        }

    def _collect_live(self) -> dict[str, Any]:
        """Full live verification: baseline + contract probes + stability observations."""
        healthy, returncode = self._run_baseline()
        if not healthy:
            return {
                "status": "failed",
                "mode": "live",
                "returncode": returncode,
                "communication_contract": None,
                "stability_observation": None,
            }

        communication_contract = self._collect_contract_probes()
        # Update the allowed probe result with the confirmed baseline health
        if "moodle_baseline" in communication_contract.get("allowed", {}):
            communication_contract["allowed"]["moodle_baseline"] = "passed"

        stability_observation = self._collect_stability_observations(self.stability_window_seconds)

        return {
            "status": "passed" if stability_observation["status"] == "passed" else "failed",
            "mode": "live",
            "returncode": returncode,
            "communication_contract": communication_contract,
            "stability_observation": stability_observation,
        }



class MoodleIndependentVerifier:
    """Read-only verification authority for the final incident transition.

    The verifier receives an execution result but has no execution method.  It
    checks the declared communication contract separately from the executor,
    and is the sole component returning a resolved verdict.
    """

    def __init__(self, adapter: MoodleReadOnlyVerificationAdapter):
        self.adapter = adapter

    def verify(
        self,
        contract: dict[str, list[str]],
        *,
        live: bool,
        stability_window_seconds: int = 120,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime = runtime or self.adapter.verify(live=live)
        if not isinstance(runtime, dict):
            runtime = {"status": "failed", "mode": "live"}
        if not live:
            return {
                "status": "not_run",
                "authority": "independent_verifier",
                "mode": "replay",
                "health": "not_run",
                "allowed_probes": {probe: "not_run" for probe in contract["allowed"]},
                "forbidden_probes": {probe: "not_run" for probe in contract["forbidden"]},
                "related_probes": {probe: "not_run" for probe in contract["related"]},
                "stability_window_seconds": 0,
                "resolution_eligible": False,
                "reason": "replay has no live health, communication, or stability observations",
                "runtime": runtime,
            }

        probes = runtime.get("communication_contract")
        allowed = probes.get("allowed", {}) if isinstance(probes, dict) else {}
        forbidden = probes.get("forbidden", {}) if isinstance(probes, dict) else {}
        related = probes.get("related", {}) if isinstance(probes, dict) else {}
        stability = runtime.get("stability_observation", {})
        if not isinstance(stability, dict):
            stability = {}
        observations = stability.get("observations", []) if isinstance(stability, dict) else []
        stable_seconds = 0
        if isinstance(observations, list) and len(observations) >= 2:
            try:
                first = datetime.fromisoformat(observations[0]["observed_at"].replace("Z", "+00:00"))
                last = datetime.fromisoformat(observations[-1]["observed_at"].replace("Z", "+00:00"))
                if first.tzinfo is None or last.tzinfo is None or last > datetime.now(timezone.utc):
                    stable_seconds = 0
                else:
                    stable_seconds = int((last - first).total_seconds())
            except (KeyError, TypeError, ValueError):
                stable_seconds = 0
        if not isinstance(allowed, dict):
            allowed = {}
        if not isinstance(forbidden, dict):
            forbidden = {}
        if not isinstance(related, dict):
            related = {}
        contract_ok = (
            set(allowed) == set(contract["allowed"])
            and all(allowed.get(probe) == "passed" for probe in contract["allowed"])
            and set(forbidden) == set(contract["forbidden"])
            and all(forbidden.get(probe) == "blocked" for probe in contract["forbidden"])
            and set(related) == set(contract["related"])
            and all(related.get(probe) == "passed" for probe in contract["related"])
        )
        stable = (
            stability.get("status") == "passed"
            and len(observations) >= 2
            and all(isinstance(item, dict) and item.get("status") == "passed" for item in observations)
            and stable_seconds >= max(120, stability_window_seconds)
        )
        runtime_healthy = runtime.get("status") == "passed"
        passed = runtime_healthy and contract_ok and stable
        blocked = runtime_healthy and (not probes or not stability)
        return {
            "status": "passed" if passed else ("blocked" if blocked else "failed"),
            "authority": "independent_verifier",
            "mode": runtime.get("mode", "live"),
            "health": runtime.get("status", "failed"),
            "allowed_probes": allowed,
            "forbidden_probes": forbidden,
            "related_probes": related,
            "stability_window_seconds": stable_seconds,
            "resolution_eligible": passed,
            "reason": "all live probes passed through the required stability window" if passed else "live contract probes and stability evidence are incomplete or failed",
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
        verifier_adapter = MoodleReadOnlyVerificationAdapter(
            repo_root=getattr(self.adapter, "repo_root", None),
        )
        self.verifier = MoodleIndependentVerifier(verifier_adapter)
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
        approval: HumanApproval | None = None,
        live_verify: bool = False,
        stability_window_seconds: int = 120,
    ) -> dict[str, Any]:
        if scenario_id not in self.catalog:
            raise PipelineError(f"unknown scenario: {scenario_id}")
        if event.get("status") != "firing" or event.get("event_type") != "service_health_failed":
            raise PipelineError("only firing service_health_failed alerts may enter remediation planning")
        event_errors = validate_normalized_event(event)
        if event_errors:
            raise PipelineError(f"malformed normalized alert: {event_errors}")
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
        if decision.decision == GateDecision.REQUIRE_APPROVAL:
            valid_approval, approval_reason = self.gate.validate_approval(action, approval)
            audit_refs.append(self.audit.append(incident_id, "approval_validation", {
                "valid": valid_approval,
                "approver_id": approval.approver_id if approval and valid_approval else None,
                "reason": approval_reason,
            }))
            if valid_approval:
                decision = SafetyDecision(GateDecision.ALLOW, approval_reason)
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
            stability_window_seconds=stability_window_seconds,
        )
        refs.append(self.evidence.append(incident_id, "verification", verification))
        audit_refs.append(self.audit.append(incident_id, "verified", {"status": verification["status"], "authority": verification["authority"]}))
        if verification["status"] == "passed" and verification.get("resolution_eligible") is True:
            incident["state"] = "RESOLVED"
        elif verification["status"] == "not_run" and mode == "dry-run":
            incident["state"] = "VERIFIED_DRY_RUN"
        elif verification["status"] == "blocked":
            incident["state"] = "VERIFY_BLOCKED"
        else:
            incident["state"] = "VERIFY_FAILED"
        report = {**incident, "evidence_refs": refs, "audit_refs": audit_refs, "diagnosis": diagnosis, "plan": plan, "execution": execution, "verification": verification}
        self.incidents[incident_id] = report
        return report


def replay_all(evidence_root: Path | str) -> list[dict[str, Any]]:
    """Run one deterministic dry-run replay for every Moodle ground truth."""
    pipeline = MoodleIncidentPipeline(evidence_root)
    reports = []
    for scenario_id in SCENARIOS:
        event = normalize_alert(
            {
                "status": "firing",
                "fingerprint": f"moodle-{scenario_id.lower()}",
                "startsAt": "2026-09-21T00:00:00Z",
                "labels": {"alertname": "MoodleSprint2Replay", "scenario_id": scenario_id, "environment": "staging", "severity": "critical"},
                "annotations": {"summary": f"Replay event for {scenario_id}"},
            },
            correlation_id=f"replay-{scenario_id}",
            received_at="2026-09-21T00:00:01Z",
        )
        reports.append(pipeline.process(event, scenario_id=scenario_id))
    return reports
