#!/usr/bin/env python3
"""Operator-side stability verifier for an executed Moodle safe action.

The verifier reads the append-only action audit and repeatedly runs the
reviewed, read-only Moodle scenario probe. It has no executor credentials.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.safety.safe_action_audit import SafeActionAuditStore  # noqa: E402
from core.schema.common import IncidentStatus  # noqa: E402
from core.schema.incident import Incident  # noqa: E402
from core.schema.verification import ProbeResult, StabilityObservation  # noqa: E402
from core.safety.verifier_attestation import VerifierAttestationPayload, canonical_payload  # noqa: E402
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS  # noqa: E402
from core.verification.verification_agent import VerificationAgent  # noqa: E402


_SCENARIO_RELATED_CHECKS = {
    "DB-01": ["moodle-app-a_database_tls", "moodle-app-b_database_tls", "both_alb_targets_healthy"],
    "DB-02": ["app_role_quota_and_tagged_sessions_restored", "independent_monitor_rds_probe_healthy", "both_alb_targets_healthy"],
    "DB-03": ["db03_backup_removed", "app_b_approved_database_endpoint", "app_b_database_tls", "both_alb_targets_healthy"],
    "RES-01": ["named_cpu_fixture_absent"],
    "RES-02": ["named_memory_fixture_absent", "memory_fixture_metric_absent", "app_b_web_container_healthy", "both_alb_targets_healthy"],
    "RES-03": ["scratch_mount_absent", "scratch_enospc_metric_cleared", "app_b_efs_remains_mounted", "both_alb_targets_healthy"],
    "NET-01": ["moodle-app-a_database_dns", "moodle-app-b_database_dns", "both_alb_targets_healthy"],
    "NET-02": ["tagged_port_block_absent", "moodle-app-a_rds_tcp_healthy", "moodle-app-b_rds_tcp_healthy", "monitor_rds_probe_healthy", "both_alb_targets_healthy"],
    "NET-03": ["net03_fixture_removed", "net03_qdisc_restored", "moodle-app-a_rds_tcp_latency_normal", "moodle-app-b_rds_tcp_latency_normal", "both_alb_targets_healthy"],
    "CON-01": ["moodle_app_b_container_healthy", "both_alb_targets_healthy"],
    "CON-02": ["moodle_app_b_router_enabled", "moodle_app_b_router_fallback_healthy", "both_alb_targets_healthy"],
    "CON-03": ["con03_backup_removed", "app_b_wwwroot_approved", "app_b_web_container_healthy", "both_alb_targets_healthy"],
    "SEC-02": ["fixture_directory_mode_restored", "efs_mount_metric_is_one"],
    "SEC-03": ["sec03_backup_removed", "app_b_reverse_proxy_setting_approved", "app_b_reverse_proxy_metric_approved", "both_alb_targets_healthy"],
}
_PROBE_SCRIPT = REPO_ROOT / "automation" / "moodle-scenario-verify.sh"


def _contract_for_scenario(scenario_id: str) -> dict[str, list[str]]:
    if scenario_id in MOODLE_LIVE_CONTRACTS:
        return MOODLE_LIVE_CONTRACTS[scenario_id]
    related = _SCENARIO_RELATED_CHECKS.get(scenario_id)
    if related is None:
        raise ValueError("scenario has no reviewed live verification contract")
    return {
        "allowed": ["common_synthetic_transaction"],
        "forbidden": ["internet_to_postgresql_blocked"],
        "related": ["scenario_alert_resolved", *related],
    }


def _audit_receipt(audit_db: Path, incident_id: str, request_id: str, scenario_id: str) -> dict[str, Any]:
    with SafeActionAuditStore(audit_db, read_only=True) as audit:
        events = audit.list_for_incident(incident_id)
        if not audit.verify_chain(incident_id):
            raise ValueError("safe-action audit chain is invalid")
    receipts = [
        event for event in events
        if event.get("event_type") == "live_executor_result"
        and event.get("payload", {}).get("request_id") == request_id
        and event.get("payload", {}).get("scenario_id") == scenario_id
        and event.get("payload", {}).get("status") == "executed"
        and event.get("payload", {}).get("mutated") is True
    ]
    if len(receipts) != 1:
        raise ValueError("no unique executed, mutated receipt matches incident/request/scenario")
    return receipts[0]


def _run_probe(scenario_id: str, request_id: str) -> dict[str, bool]:
    if not _PROBE_SCRIPT.is_file():
        raise ValueError("reviewed Moodle scenario probe is missing")
    completed = subprocess.run(
        ["bash", str(_PROBE_SCRIPT), scenario_id, request_id],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        timeout=180,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("probe command did not emit one JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("probe command returned a non-object result")
    if payload.get("scenario_id") != scenario_id or payload.get("drill_id") != request_id:
        raise ValueError("probe output is not bound to the requested scenario and request ID")
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("probe output has no check list")
    normalized: dict[str, bool] = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str):
            raise ValueError("probe output contains a malformed check")
        name = check["id"]
        if name in normalized or check.get("status") not in {"passed", "failed"}:
            raise ValueError("probe output contains duplicate or invalid check status")
        normalized[name] = check["status"] == "passed"
    all_passed = all(normalized.values())
    expected_status = "passed" if all_passed else "failed"
    if payload.get("status") != expected_status or (completed.returncode == 0) != all_passed:
        raise ValueError("probe exit status, overall status, and individual checks conflict")
    return normalized


def verify_window(
    *, audit_db: Path, incident_id: str, request_id: str, scenario_id: str,
    signing_key: Path,
    contract: dict[str, list[str]], window_seconds: int = 120,
    interval_seconds: int = 30, sleep=time.sleep, monotonic=time.monotonic,
    now=lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    if window_seconds < 120 or not 1 <= interval_seconds <= 60:
        raise ValueError("verification requires a >=120s window and a 1..60s sampling interval")
    receipt = _audit_receipt(audit_db, incident_id, request_id, scenario_id)
    required = set().union(*(set(contract[k]) for k in ("allowed", "forbidden", "related")))
    sample_count = (window_seconds + interval_seconds - 1) // interval_seconds + 1
    observations: list[StabilityObservation] = []
    samples: list[dict[str, bool]] = []
    started = monotonic()
    for index in range(sample_count):
        if index:
            target = started + min(index * interval_seconds, window_seconds)
            sleep(max(0.0, target - monotonic()))
        observed_at = now()
        sample = _run_probe(scenario_id, request_id)
        if set(sample) != required:
            missing, extra = sorted(required - set(sample)), sorted(set(sample) - required)
            raise ValueError(f"probe contract coverage mismatch; missing={missing}, extra={extra}")
        samples.append(sample)
        observations.append(StabilityObservation(
            observed_at=observed_at,
            healthy=all(sample[name] for name in contract["allowed"]),
            simulated=False,
        ))

    # Revalidate at the end of the window so a changed or corrupted audit
    # cannot be carried through a long-running verification session.
    receipt = _audit_receipt(audit_db, incident_id, request_id, scenario_id)
    typed_probes = []
    for group in ("allowed", "forbidden", "related"):
        for name in contract[group]:
            probe_result = ProbeResult(
                name=name,
                passed=all(sample[name] for sample in samples),
                simulated=False,
                details=f"{sum(sample[name] for sample in samples)}/{len(samples)} live samples passed",
            )
            typed_probes.append(_Probe(name, group, probe_result))
    incident = Incident(
        incident_id=incident_id,
        fingerprint=f"safe-action:{request_id}",
        title=f"Moodle safe action {scenario_id}",
        severity="warning",
        affected_resources=[scenario_id],
        status=IncidentStatus.VERIFYING,
    )
    result = VerificationAgent().verify(
        incident, contract, stability_window_seconds=window_seconds, dry_run=False,
        probes=typed_probes, stability_observations=observations,
        max_stability_gap_seconds=interval_seconds,
    )
    sample_payloads = [
        {"observed_at": observation.observed_at.isoformat(), "checks": sample}
        for observation, sample in zip(observations, samples, strict=True)
    ]
    payload = VerifierAttestationPayload.model_validate({
        "version": 1,
        "incident_id": incident_id,
        "request_id": request_id,
        "scenario_id": scenario_id,
        "receipt_event_hash": receipt["event_hash"],
        "signed_at": now().isoformat(),
        "verification": result.model_dump(mode="json"),
        "probe_samples": sample_payloads,
    })
    canonical = canonical_payload(payload)
    private_key = serialization.load_pem_private_key(signing_key.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("verifier signing key must be Ed25519")
    return {
        "verdict": "ready_for_runtime_acceptance" if result.resolution_eligible else "not_resolved",
        "resolution_eligible": result.resolution_eligible,
        "verification": result.model_dump(mode="json"),
        "probe_samples": sample_payloads,
        "attestation": {
            "payload": payload.model_dump(mode="json"),
            "signature": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
        },
    }


class _Probe:
    def __init__(self, name: str, probe_type: str, result: ProbeResult) -> None:
        self.name, self.probe_type, self._result = name, probe_type, result

    def check(self) -> ProbeResult:
        return self._result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-db", type=Path, required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--scenario-id", required=True, choices=sorted(_SCENARIO_RELATED_CHECKS))
    parser.add_argument("--window-seconds", type=int, default=120)
    parser.add_argument("--interval-seconds", type=int, default=30)
    args = parser.parse_args()
    try:
        result = verify_window(
            audit_db=args.audit_db, incident_id=args.incident_id, request_id=args.request_id,
            signing_key=args.signing_key,
            scenario_id=args.scenario_id, contract=_contract_for_scenario(args.scenario_id),
            window_seconds=args.window_seconds, interval_seconds=args.interval_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"verdict": "not_resolved", "resolution_eligible": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["resolution_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
