"""Runtime observer and narrowly scoped local AUTH-01 recovery workflow."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from core.unified_core import Incident, IncidentState, SQLiteEvidenceStore, transition
from core.unified_core import resolve_verified as resolve_incident_verified
from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult

AUTH_ALERTS = {
    "AuthLabOpenLdapUnavailable",
    "AuthLabSyntheticLdapLoginFailed",
}
AUTH_PROBES = {
    "ldap_ldaps": 'probe_success{job="openldap_ldaps"}',
    "moodle_health": 'probe_success{job="moodle"}',
    "valid_login": "auth_lab_valid_login_success",
}


def is_auth_alert(alert: dict[str, Any]) -> bool:
    labels = alert.get("labels") or {}
    return str(labels.get("scenario", "")).startswith("AUTH-")


def auth_incident_id(alert: dict[str, Any]) -> str:
    """Use one stable AUTH-01 incident identity across its related alerts."""
    labels = alert.get("labels") or {}
    scenario = str(labels.get("scenario") or "unknown")
    name = str(labels.get("alertname") or "unknown")
    fingerprint = str(alert.get("fingerprint") or "")
    identity = f"{scenario}:authlab-openldap" if scenario == "AUTH-01" else (
        fingerprint or f"{scenario}:{name}:{alert.get('startsAt', '')}"
    )
    return "auth-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def query_prometheus(query: str, *, base_url: str | None = None) -> float | None:
    """Fetch one numeric sample for a fixed, code-owned PromQL expression."""
    origin = (base_url or os.getenv("PROMETHEUS_URL", "http://prometheus:9090")).rstrip("/")
    try:
        response = requests.get(
            f"{origin}/api/v1/query",
            params={"query": query},
            timeout=5,
        )
        response.raise_for_status()
        result = response.json().get("data", {}).get("result", [])
        if len(result) != 1:
            return None
        return float(result[0]["value"][1])
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None


def process_auth_alert(
    alert: dict[str, Any],
    *,
    metric_reader: Callable[[str], float | None] = query_prometheus,
    evidence_path: str | Path | None = None,
) -> dict[str, Any]:
    """Collect live signals, persist a redacted snapshot and deny remediation."""
    labels = alert.get("labels") or {}
    scenario = str(labels.get("scenario") or "unknown")
    alert_name = str(labels.get("alertname") or "unknown")
    status = str(alert.get("status") or "unknown")
    incident_id = auth_incident_id(alert)

    samples = {
        name: metric_reader(query)
        for name, query in AUTH_PROBES.items()
    }
    if alert_name not in AUTH_ALERTS or scenario != "AUTH-01":
        diagnosis = "AUTH scenario is not in the observed local AUTH-01 contract"
        diagnosis_basis = "unsupported_alert_contract"
    elif any(value is None for value in samples.values()):
        diagnosis = "Required live authentication probes are missing; cause is undetermined"
        diagnosis_basis = "incomplete_runtime_evidence"
    elif samples["ldap_ldaps"] == 0 and samples["valid_login"] == 0:
        diagnosis = "LDAPS endpoint and synthetic login probes both fail; directory-path failure is consistent with live evidence"
        diagnosis_basis = "ldaps_and_login_probes_failed"
    elif samples["valid_login"] == 0 and samples["ldap_ldaps"] == 1:
        diagnosis = "Synthetic login fails while LDAPS TCP probe succeeds; cause is undetermined"
        diagnosis_basis = "login_failed_endpoint_reachable"
    elif samples["valid_login"] == 1 and samples["moodle_health"] == 1:
        diagnosis = "Current probes pass; the alert may be stale, but recovery still requires the independent verifier"
        diagnosis_basis = "current_probes_pass"
    else:
        diagnosis = "Observed probes do not support a specific root cause; escalate for operator review"
        diagnosis_basis = "ambiguous_runtime_evidence"

    if status == "resolved":
        disposition = "RECOVERY_SIGNAL_ONLY"
        diagnosis = "Alertmanager sent a recovery signal; run the independent login and stability verifier"
    else:
        disposition = "ESCALATE"

    snapshot = {
        "scenario": scenario,
        "alert_name": alert_name if alert_name in AUTH_ALERTS else "unsupported-auth-alert",
        "alert_status": status if status in {"firing", "resolved"} else "unknown",
        "probe_values": samples,
        "diagnosis_basis": diagnosis_basis,
        "disposition": disposition,
        "action_decision": "DENY",
        "action_decision_reason": "No allowlisted, safety-gated AUTH remediation is registered",
        "resolution_eligible": False,
    }

    database_path = evidence_path or os.getenv(
        "AIOPS_EVIDENCE_DB_PATH", "/app/data/evidence.db"
    )
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteEvidenceStore(database_path)
    store.append(
        incident_id=incident_id,
        source="alertmanager",
        resource_id="authlab-openldap",
        kind="auth_alert_received",
        payload={"scenario": scenario, "alert_name": snapshot["alert_name"], "status": snapshot["alert_status"]},
        observed_at=datetime.now(timezone.utc),
    )
    store.append(
        incident_id=incident_id,
        source="prometheus",
        resource_id="authlab-openldap",
        kind="auth_probe_snapshot",
        payload=snapshot,
        observed_at=datetime.now(timezone.utc),
    )

    return {
        "incident_id": incident_id,
        "scenario": scenario,
        "alert_name": snapshot["alert_name"],
        "status": disposition,
        "diagnosis": diagnosis,
        "action_decision": "DENY",
        "resolution_eligible": False,
    }


def recovery_gate_reason(
    alert: dict[str, Any], samples: dict[str, float | None], *, enabled: bool
) -> str | None:
    """Return None only for the exact, live AUTH-01 local fault contract."""
    labels = alert.get("labels") or {}
    name = str(labels.get("alertname") or "")
    expected_resource = {
        "AuthLabOpenLdapUnavailable": "authlab-openldap",
        "AuthLabSyntheticLdapLoginFailed": "authlab-moodle-login",
    }.get(name)
    checks = (
        (enabled, "automatic local recovery is disabled"),
        (alert.get("status") == "firing", "alert is not firing"),
        (labels.get("scenario") == "AUTH-01", "scenario is outside AUTH-01"),
        (labels.get("environment") == "local-auth-lab", "environment is not the local auth lab"),
        (labels.get("evidence_class") == "runtime", "alert is not marked as runtime evidence"),
        (expected_resource is not None, "alert name is not allowlisted"),
        (labels.get("resource_id") == expected_resource, "resource is outside the fixed local scope"),
        (samples.get("ldap_ldaps") == 0, "LDAPS probe is not failing"),
        (samples.get("valid_login") == 0, "synthetic login probe is not failing"),
        (samples.get("moodle_health") == 1, "Moodle health is not passing"),
    )
    return next((reason for passed, reason in checks if not passed), None)


def request_openldap_restart() -> dict[str, Any]:
    """Request only the fixed actuator route; the agent has no Docker socket."""
    token = os.environ.get("AUTH_LAB_RECOVERY_TOKEN", "")
    origin = os.environ.get("AUTH_LAB_ACTUATOR_URL", "http://recovery-actuator:9191")
    if not token:
        raise RuntimeError("local recovery actuator token is not configured")
    response = requests.post(
        origin.rstrip("/") + "/v1/auth-01/openldap/restart",
        headers={"Authorization": f"Bearer {token}"},
        timeout=70,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("status") != "succeeded" or result.get("target") != "local-openldap":
        raise RuntimeError("local actuator did not confirm the fixed OpenLDAP action")
    return {"target": "local-openldap", "operation": result.get("operation"), "health": result.get("health")}


def request_independent_verifier() -> dict[str, Any]:
    """Ask the isolated credential-holding probe to run its fixed 120s contract."""
    token = os.environ.get("AUTH_LAB_RECOVERY_TOKEN", "")
    origin = os.environ.get("AUTH_LAB_VERIFIER_URL", "http://auth-probe:9105/verify")
    if not token:
        raise RuntimeError("independent verifier token is not configured")
    response = requests.post(
        origin,
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("authority") != "independent_verifier":
        raise RuntimeError("verification response did not identify its independent authority")
    return result


def attempt_auth01_recovery(
    alert: dict[str, Any],
    *,
    metric_reader: Callable[[str], float | None] = query_prometheus,
    actuator: Callable[[], dict[str, Any]] = request_openldap_restart,
    verifier: Callable[[], dict[str, Any]] = request_independent_verifier,
    evidence_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Execute one opt-in local action and require independent 120s proof.

    Returns None when the live gate does not authorize the action. All other
    failures append evidence and escalate without retry or rollback guessing.
    """
    if os.getenv("AUTH_LAB_AUTO_RECOVERY_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return None
    labels = alert.get("labels") or {}
    scenario = str(labels.get("scenario") or "unknown")
    name = str(labels.get("alertname") or "unknown")
    if scenario != "AUTH-01" or name not in AUTH_ALERTS:
        return None

    samples = {key: metric_reader(query) for key, query in AUTH_PROBES.items()}
    reason = recovery_gate_reason(alert, samples, enabled=True)
    if reason:
        return None

    identity = f"{scenario}:authlab-openldap"
    incident_id = "auth-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    database_path = evidence_path or os.getenv("AIOPS_EVIDENCE_DB_PATH", "/app/data/evidence.db")
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteEvidenceStore(database_path)
    incident = Incident(
        incident_id=incident_id,
        fingerprint=identity,
        environment="local-auth-lab",
        resource_ids=["authlab-openldap", "authlab-moodle-login", "authlab-moodle"],
        state=IncidentState.OPEN,
    )
    evidence_refs: list[str] = []

    def record(kind: str, source: str, payload: dict[str, Any]) -> str:
        event = store.append(
            incident_id=incident_id,
            source=source,
            resource_id="authlab-openldap",
            kind=kind,
            payload=payload,
            observed_at=datetime.now(timezone.utc),
        )
        evidence_refs.append(event.evidence_id)
        return event.evidence_id

    def advance(new_state: IncidentState, actor: str, reason_text: str) -> None:
        nonlocal incident
        old_state = incident.state.value
        incident = transition(incident, new_state, actor=actor)
        record("auth_incident_transition", actor, {
            "old_state": old_state,
            "new_state": new_state.value,
            "reason": reason_text,
            "authority": actor,
        })

    def advance_verified(verification: VerificationResult) -> None:
        nonlocal incident
        old_state = incident.state.value
        incident = resolve_incident_verified(incident, verification)
        record("auth_incident_transition", "verifier", {
            "old_state": old_state,
            "new_state": incident.state.value,
            "reason": "Independent verifier passed the typed AUTH-01 contract",
            "authority": "independent_verifier",
        })

    record("auth_alert_received", "alertmanager", {
        "scenario": scenario,
        "alert_name": name,
        "status": "firing",
        "environment": "local-auth-lab",
    })
    record("auth_probe_snapshot", "prometheus", {
        "scenario": scenario,
        "probe_values": samples,
        "action_decision": "ALLOW",
        "action_decision_reason": "Exact local AUTH-01 runtime gate passed",
        "resolution_eligible": False,
    })
    advance(IncidentState.OBSERVED, "observer", "Allowlisted local AUTH-01 firing alert received")
    advance(IncidentState.DIAGNOSED, "diagnosis", "LDAPS and login fail while Moodle health passes")
    advance(IncidentState.PLANNED, "planner", "Request the fixed local OpenLDAP restart action")
    advance(IncidentState.GATED, "safety_gate", "All runtime, environment, resource and scenario checks passed")
    advance(IncidentState.EXECUTING, "executor", "Invoke the fixed local recovery actuator")

    try:
        action_result = actuator()
        record("auth_action_result", "auth_lab_actuator", {
            "status": "SUCCEEDED",
            "target": action_result.get("target"),
            "operation": action_result.get("operation"),
            "health": action_result.get("health"),
        })
        advance(IncidentState.VERIFYING, "executor", "Actuator reports OpenLDAP healthy")
        verdict = verifier()
        raw_observations = verdict.get("stability_observations")
        try:
            observations = [
                StabilityObservation.model_validate(item)
                for item in raw_observations
            ] if isinstance(raw_observations, list) else []
        except (TypeError, ValueError):
            observations = []
        raw_related_probes = verdict.get("related_probes")
        try:
            related_probes = [
                ProbeResult.model_validate(item)
                for item in raw_related_probes
            ] if isinstance(raw_related_probes, list) else []
        except (TypeError, ValueError):
            related_probes = []
        related_probes_valid = bool(related_probes) and all(
            item.passed and item.simulated is False for item in related_probes
        )
        ordered_observations = sorted(item.observed_at for item in observations)
        observations_in_order = [item.observed_at for item in observations] == ordered_observations
        measured_stability = (
            int((ordered_observations[-1] - ordered_observations[0]).total_seconds())
            if len(ordered_observations) >= 2
            else 0
        )
        stability_ok = (
            len(observations) >= 9
            and observations_in_order
            and measured_stability >= 120
            and all(item.healthy for item in observations)
            and -30 <= (datetime.now(timezone.utc) - ordered_observations[-1].astimezone(timezone.utc)).total_seconds() <= 60
            and all(
                0 < (later - earlier).total_seconds() <= 60
                for earlier, later in zip(ordered_observations, ordered_observations[1:])
            )
        )
        proof_passed = (
            verdict.get("authority") == "independent_verifier"
            and verdict.get("simulated") is False
            and verdict.get("valid_login_passed") is True
            and verdict.get("invalid_login_denied") is True
            and verdict.get("health_passed") is True
            and verdict.get("ldaps_passed") is True
            and all(item.simulated is False for item in observations)
            and stability_ok
            and related_probes_valid
            and verdict.get("resolution_eligible") is True
        )
        safe_verdict = {
            "authority": "independent_verifier" if verdict.get("authority") == "independent_verifier" else "unknown",
            "simulated": verdict.get("simulated") is not False,
            "valid_login_passed": verdict.get("valid_login_passed") is True,
            "invalid_login_denied": verdict.get("invalid_login_denied") is True,
            "health_passed": verdict.get("health_passed") is True,
            "ldaps_passed": verdict.get("ldaps_passed") is True,
            "stability_seconds": measured_stability,
            "observations": len(observations),
            "stability_observations": [
                item.model_dump(mode="json") for item in observations
            ],
            "related_probes": [
                item.model_dump(mode="json") for item in related_probes
            ],
            "resolution_eligible": proof_passed,
        }
        record("auth_verifier_result", "independent_verifier", safe_verdict)
        typed_verification = VerificationResult(
            verification_id="auth-verify-" + hashlib.sha256(incident_id.encode()).hexdigest()[:16],
            incident_id=incident_id,
            health_passed=verdict.get("health_passed") is True and verdict.get("ldaps_passed") is True,
            communication_contract_passed=proof_passed,
            stability_seconds=measured_stability,
            stability_observations=observations,
            resolution_eligible=proof_passed,
            allowed_probes=[
                ProbeResult(name="valid_login", passed=verdict.get("valid_login_passed") is True, simulated=verdict.get("simulated") is not False, details="synthetic valid-account login"),
                ProbeResult(name="moodle_health", passed=verdict.get("health_passed") is True, simulated=verdict.get("simulated") is not False, details="Moodle health probe"),
                ProbeResult(name="ldaps_certificate", passed=verdict.get("ldaps_passed") is True, simulated=verdict.get("simulated") is not False, details="LDAPS certificate probe"),
            ],
            forbidden_probes=[
                ProbeResult(name="invalid_login", passed=verdict.get("invalid_login_denied") is True, simulated=verdict.get("simulated") is not False, details="synthetic invalid-account denial")
            ],
            related_probes=related_probes,
            verdict="resolved" if proof_passed else "not_resolved",
            simulated=verdict.get("simulated") is not False,
        )
        if proof_passed:
            advance_verified(typed_verification)
            status = "RESOLVED"
        else:
            advance(IncidentState.ESCALATED, "operator_gate", "Independent verification did not satisfy the full recovery contract")
            status = "ESCALATED"
        return {
            "incident_id": incident_id,
            "scenario": scenario,
            "alert_name": name,
            "status": status,
            "diagnosis": "Local OpenLDAP restart executed; independent verifier " + ("passed" if proof_passed else "did not authorize resolution"),
            "action_decision": "ALLOW",
            "action_status": "SUCCEEDED",
            "resolution_eligible": proof_passed,
            "resolution_authority": "independent_verifier" if proof_passed else None,
            "verifier": safe_verdict,
            "evidence_refs": evidence_refs,
        }
    except Exception as error:
        record("auth_action_or_verification_failure", "auth_lab_recovery", {
            "failure_type": type(error).__name__,
            "failure_summary": str(error)[:180],
            "resolution_eligible": False,
        })
        advance(IncidentState.FAILED, "recovery_controller", "Action or verification failed; operator intervention required")
        return {
            "incident_id": incident_id,
            "scenario": scenario,
            "alert_name": name,
            "status": "ESCALATED",
            "diagnosis": "Recovery action or independent verification failed; incident remains open for operator review",
            "action_decision": "ALLOW",
            "action_status": "FAILED",
            "resolution_eligible": False,
            "resolution_authority": None,
            "evidence_refs": evidence_refs,
        }
