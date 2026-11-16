"""Offline fakes for the live C0-C3 harness: simulated staging backend, Agent (real Gate/workflow/audit/attestation) and proposers.

Nothing here touches AWS, SSH, Docker or the network. What is REAL in the simulation: the locked model (optional), the
adapter, the Safety Gate, SafeActionLiveWorkflow, the append-only audit chain, the Independent Verifier's
``verify_window`` and the Agent's ``accept_verifier_attestation``. What is FAKE: Prometheus/Alertmanager data, the
fault, the Safe Executor and the probes (``SimBackend``). The simulated clock only advances the harness timeline.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import live_c0_harness as h
import lab_collector
from action_ladder import ACTION_SPECS, MATCHING_ACTION
from core.environment_profile import get_environment_profile
from core.evidence.adapters import SafeLiveActionRequest
from core.safety.safe_action_audit import SafeActionAuditStore  # noqa: F401  (patched module path below)
from core.safety.safe_execution_gate import ApprovalRecord, SafeExecutionGate
from core.safety.safe_action_live import SafeActionLiveWorkflow
from core.safety.safe_executor_client import action_digest
from core.safety.verifier_attestation import (
    VerifierAttestation, accept_verifier_attestation, get_runtime_verification_status)
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import Environment

SYNTHETIC_FAULTS = ("DB-01", "NET-01", "SEC-02")
FAULT_ALERTS = {
    "DB-01": ("MoodleNodeRdsTcpFailed", "MoodleSyntheticTransactionFailed"),
    "NET-01": ("MoodleSyntheticTransactionFailed",),
    "SEC-02": ("MoodleSyntheticTransactionFailed",),
    "RES-01": ("MoodleNodeCpuHigh",),
    "CON-01": ("MoodleWebContainerMissing",),
    "BIZ-02": ("MoodleAdhocTaskQueueStuck",),
}
TARGET_RESOURCE = {"DB-01": "postgres-db", "NET-01": "moodle-app", "RES-01": "moodle-app", "CON-01": "moodle-app",
                   "SEC-02": "moodledata-volume", "BIZ-02": "moodle-app"}
NODE_BASE = {
    "moodle_node_web_running": 1, "moodle_node_web_restart_count": 0, "moodle_node_apache_router_enabled": 1,
    "moodle_node_router_fallback_success": 1, "moodle_node_reverse_proxy_enabled": 0, "moodle_node_rds_tcp_success": 1,
    "moodle_node_rds_tcp_duration_seconds": 0.01, "moodle_node_db_dns_success": 1, "moodle_node_db_dns_matches_expected": 1,
    "moodle_node_cpu_percent": 8.0, "moodle_node_cron_running": 1, "moodle_cron_container_stopped": 0,
    "moodle_adhoc_queue_probe_success": 1, "moodle_adhoc_tasks_overdue_count": 0,
    "moodle_node_cron_heartbeat_age_seconds": 20, "moodle_node_fixture_dir_exists": 1, "moodle_node_fixture_dir_mode": 770,
    "moodle_efs_mount_available": 1, "node_probe_age_seconds": 5.0, "node_cpu_percent_max_30s": 8.0,
    "node_rds_tcp_success_min_30s": 1,
}
_VERIFIER_PATH = h.ROOT / "automation" / "moodle-independent-verifier.py"
_verifier_module = None


def verifier_module():
    global _verifier_module
    if _verifier_module is None:
        spec = importlib.util.spec_from_file_location("moodle_independent_verifier_sim", _VERIFIER_PATH)
        _verifier_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_verifier_module)
    return _verifier_module


class SimBackend:
    """Fake staging: Prometheus/Alertmanager/probes/executor/reset with a tiny fault model (no ground truth leaks out)."""

    name = "simulated-staging"

    def __init__(self, clock, *, symptom_override: str | None = None, collateral: bool = False, detect: bool = True,
                 baseline_ok: bool = True, reset_ok: bool = True, executor_enabled: bool = True):
        self.clock, self.symptom_override, self.collateral_after_fix = clock, symptom_override, collateral
        self.detect, self.baseline_ok, self.reset_ok, self.executor_enabled = detect, baseline_ok, reset_ok, executor_enabled
        self.faults: set[str] = set()
        self.collateral = False
        self.injected_label: list[str] = []              # test visibility only: the harness never reads it
        self.executions: list[dict] = []
        self.operator_public_key = None
        self._nonces: set[str] = set()
        self._idem: dict[str, dict] = {}
        self._queries = {promql: (name, label) for name, promql, label, _ in lab_collector.QUERIES}
        self.heartbeat_started = False

    # ---- observation (what Prometheus/Alertmanager would show)
    def _alerts(self) -> list[dict]:
        out = []
        for fault in sorted(self.faults):
            for alert in FAULT_ALERTS[fault]:
                out.append({"alertname": alert, "instance": "moodle-app-b" if fault in ("RES-01", "CON-01") else "monitor-ai-01"})
        return out

    def _node_value(self, name: str, node: str) -> float:
        value = NODE_BASE[name]
        if "DB-01" in self.faults and name in ("moodle_node_rds_tcp_success", "node_rds_tcp_success_min_30s"):
            value = 0
        if "NET-01" in self.faults and name == "moodle_node_db_dns_matches_expected":
            value = 0
        if "SEC-02" in self.faults and name == "moodle_node_fixture_dir_mode" and node == "moodle-app-a":
            value = 700
        if "RES-01" in self.faults and node == "moodle-app-b" and name in ("moodle_node_cpu_percent", "node_cpu_percent_max_30s"):
            value = 95.0
        if "CON-01" in self.faults and node == "moodle-app-b" and name == "moodle_node_web_running":
            value = 0
        if "BIZ-02" in self.faults and node == "moodle-app-a":
            value = {"moodle_cron_container_stopped": 1, "moodle_node_cron_running": 0,
                     "moodle_adhoc_tasks_overdue_count": 5}.get(name, value)
        return float(value)

    def prom_query(self, expr: str) -> list[dict]:
        if expr.startswith("ALERTS"):
            return [{"metric": {"alertname": a["alertname"], "instance": a["instance"], "alertstate": "firing"}, "value": [0, "1"]}
                    for a in self._alerts()]
        name, label = self._queries[expr]
        synthetic_down = any(f in self.faults for f in SYNTHETIC_FAULTS)
        if name in NODE_BASE or name in lab_collector.NODE_METRICS:
            return [{"metric": {"instance": node}, "value": [0, str(self._node_value(name, node))]}
                    for node in ("moodle-app-a", "moodle-app-b")]
        scalar = {"synthetic_success": 0.0 if synthetic_down else 1.0,
                  "synthetic_success_ratio_60s": 0.0 if synthetic_down else 1.0, "synthetic_latency_seconds": 0.4,
                  "synthetic_age_seconds": 10.0, "blackbox_db_probe_success": 0.0 if "DB-01" in self.faults else 1.0}
        if name in scalar:
            return [{"metric": {}, "value": [0, str(scalar[name])]}]
        if name.startswith("blackbox_public_probe"):
            value = "0.1" if name.endswith("duration_seconds") else "1"
            return [{"metric": {"probe": probe}, "value": [0, value]} for probe in ("healthz", "login")]
        return []                                   # staging has no HAProxy: lb_* series are absent, as on the real ALB stack

    def log_reader(self, container: str, **_kw) -> str:
        down = bool(self.faults)
        return ("INFO request served\n" * 3) + ("ERROR upstream dependency failed\n" if down else "")

    # ---- control plane
    def verify_baseline(self) -> dict:
        ok = self.baseline_ok and not self.faults and not self.collateral
        return {"ok": ok, "alerts_firing": [a["alertname"] for a in self._alerts()]}

    def inject(self, scenario: str) -> None:
        self.injected_label.append(scenario)
        self.faults.add(self.symptom_override or scenario)

    def wait_detect(self, timeout_s: float):
        self.clock.sleep(45)
        return self._alerts() if (self.detect and self.faults) else None

    def reset(self, scenario: str) -> dict:
        if not self.reset_ok:
            return {"ok": False, "error": "simulated reset failure"}
        self.faults.clear()
        self.collateral = False
        return {"ok": True}

    def start_heartbeat(self) -> None:
        self.heartbeat_started = True

    def stop_heartbeat(self) -> dict:
        self.heartbeat_started = False
        return {"acknowledged_writes": 40, "lost_acknowledged_writes": 0, "rpo_seconds": 0.0, "write_unavailability_s": 0.0,
                "simulated": True}

    # ---- probes
    def contract_probe(self, scenario: str) -> dict[str, bool]:
        names = h.contract_names(scenario)
        active = scenario in self.faults
        synthetic_down = any(f in self.faults for f in SYNTHETIC_FAULTS)
        checks = {}
        for name in names:
            if name == "internet_to_postgresql_blocked":
                checks[name] = True
            elif name == "common_synthetic_transaction":
                checks[name] = not synthetic_down
            elif name == "both_alb_targets_healthy":
                checks[name] = not (active or self.collateral)
            else:
                checks[name] = not active
        return checks

    def health_only(self) -> dict:
        synthetic = not any(f in self.faults for f in SYNTHETIC_FAULTS)
        alerts_clear = not self._alerts()
        return {"ok": synthetic and alerts_clear, "healthz_http_200": True, "synthetic_ok": synthetic, "alerts_clear": alerts_clear}

    # ---- fake Safe Executor (same refusal order as the staging API)
    def execute(self, body: dict, nonce: str) -> tuple[int, dict]:
        required = {"scenario_id", "catalog_action_id", "target_scope", "environment", "action_sha256", "idempotency_key", "approval"}
        if not isinstance(body, dict) or set(body) != required or body["environment"] != "staging":
            return 403, {"status": "denied", "reason": "request_schema_or_environment_rejected"}
        scenario, action, scope, key = body["scenario_id"], body["catalog_action_id"], body["target_scope"], body["idempotency_key"]
        spec = ACTION_SPECS.get(action)
        if spec is None or spec.scenario != scenario or spec.scope != scope:
            return 403, {"status": "denied", "reason": "outside_executor_allowlist_or_action_hash_mismatch"}
        digest = action_digest(scenario, action, scope, key)
        if body["action_sha256"] != digest:
            return 403, {"status": "denied", "reason": "outside_executor_allowlist_or_action_hash_mismatch"}
        if self.operator_public_key is None or not h.approval_binds(body["approval"], digest, self.operator_public_key):
            return 403, {"status": "awaiting_approval", "reason": "approval_missing_invalid_expired_or_wrong_action"}
        if not self.executor_enabled:
            return 423, {"status": "disabled", "reason": "live_execution_kill_switch_off", "mutated": False, "completed_nodes": []}
        if nonce in self._nonces:
            return 409, {"status": "denied", "reason": "replayed_request_nonce"}
        self._nonces.add(nonce)
        if key in self._idem:
            return 200, self._idem[key]
        nodes = sorted(get_environment_profile().executor_nodes(scope))
        if scenario in self.faults:
            self.faults.discard(scenario)
            if self.collateral_after_fix:
                self.collateral = True           # an unrelated degradation a health-only check would miss
        response = {"status": "executed", "scenario_id": scenario, "completed_nodes": nodes, "failure": None, "mutated": True}
        self.executions.append({"scenario_id": scenario, "catalog_action_id": action, "target_scope": scope})
        self._idem[key] = response
        return 200, response

    def executor_callable(self, *, scenario_id, catalog_action_id, target_scope, idempotency_key, approval, **_):
        body = {"scenario_id": scenario_id, "catalog_action_id": catalog_action_id, "target_scope": target_scope,
                "environment": "staging",
                "action_sha256": action_digest(scenario_id, catalog_action_id, target_scope, idempotency_key),
                "idempotency_key": idempotency_key, "approval": approval}
        return self.execute(body, uuid.uuid4().hex)[1]


class _BackdatedDatetime(datetime):
    """Audit receipts are stamped 200 s in the past so that a virtual 120 s verifier window can follow them."""

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) - timedelta(seconds=200)


class SimAgent:
    """Planner + real Gate/workflow/audit + real Verifier window + real attestation acceptance, over the fake executor."""

    def __init__(self, backend: SimBackend, operator_public_key, workdir: Path, *, verifier_key: Ed25519PrivateKey | None = None):
        self.backend, self.workdir = backend, Path(workdir)
        self.operator_pem = operator_public_key.public_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
        backend.operator_public_key = operator_public_key
        self.verifier_key = verifier_key or Ed25519PrivateKey.generate()
        self.verifier_private = self.workdir / "verifier-ed25519.pem"
        self.verifier_public = self.workdir / "verifier-ed25519.pub"
        self.verifier_private.write_bytes(self.verifier_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.verifier_public.write_bytes(self.verifier_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.audit_db: Path | None = None
        self._catalog = h.ActionCatalog.load_moodle_7_scenario_catalog()

    def _gate(self):
        return SafeExecutionGate(approval_verification_key=self.operator_pem, approved_actors={h.OPERATOR_ACTOR},
                                 approval_digest_mode="executor")

    def prepare(self, *, incident_id, features, decision, evidence_db, audit_db, clock):
        """Planner: derives the candidate from alert evidence ALONE (independent of the AIHS proposal)."""
        self.audit_db = Path(audit_db)
        derived = h.locked_scorer.derive_context(features)
        if derived["status"] != "single" or not derived["scenario"] or not decision.evidence_refs:
            return None
        scenario = derived["scenario"]
        action = MATCHING_ACTION[scenario]
        spec, entry = ACTION_SPECS[action], self._catalog.lookup(action)
        request_id, key = str(uuid.uuid4()), str(uuid.uuid4())
        request = SafeLiveActionRequest(
            request_id=request_id, idempotency_key=key, catalog_action_id=action, scenario_id=scenario,
            target_scope=spec.scope, timeout_seconds=120,
            action=TypedAction(
                action_id=f"act-{uuid.uuid4().hex[:12]}", incident_id=incident_id, action_type=entry.action_type,
                target_resource_id=TARGET_RESOURCE[scenario], environment=Environment.STAGING,
                reason="Planner candidate derived from alert evidence.", evidence_refs=list(decision.evidence_refs),
                expected_outcome="Reapply the reviewed baseline; an independent verifier must confirm recovery.",
                reversible=True, idempotency_key=key,
                rollback_plan=RollbackPlan(available=True, rollback_action_id=entry.rollback_action,
                                           method=f"Reapply the reviewed safe baseline using {entry.rollback_action}."),
                requires_approval=True))
        gate = self._gate()
        return {
            "status": "approval_required", "gate_preview": gate.explain(request, actor_role="executor", confidence=0.9),
            "incident_id": incident_id, "candidate_id": f"cand-{uuid.uuid4().hex[:10]}",
            "request": request.model_dump(mode="json"), "diagnosis_confidence": 0.9,
            "gate_action_sha256": gate.action_hash(request),
            "executor_action_sha256": action_digest(scenario, action, spec.scope, key),
            "execution_permitted": False, "resolution_eligible": False}

    def refresh(self, prepared, fresh_decision, *, evidence_db):
        return {**prepared, "current_evidence_refs": list(fresh_decision.evidence_refs)}

    def execute(self, prepared, approval, *, decision, clock):
        request = SafeLiveActionRequest.model_validate(prepared["request"])
        gate_approval = ApprovalRecord.model_validate({
            "action_sha256": approval["action_sha256"], "actor_id": approval["actor_id"],
            "expires_at": approval["expires_at"], "signature": "ed25519:" + approval["signature"]})
        with patch("core.safety.safe_action_audit.datetime", _BackdatedDatetime), \
                h.SafeActionAuditStore(self.audit_db) as audit:
            workflow = SafeActionLiveWorkflow(gate=self._gate(), audit=audit, executor=self.backend.executor_callable)
            return workflow.run(
                request, actor_role="executor", confidence=float(prepared["diagnosis_confidence"]),
                pre_snapshot={"candidate_id": prepared["candidate_id"], "evidence_refs": list(request.action.evidence_refs),
                              "current_evidence_refs": prepared.get("current_evidence_refs", [])},
                gate_approval=gate_approval, executor_approval=approval)

    def verify(self, prepared, *, clock):
        module = verifier_module()
        request = prepared["request"]
        scenario = request["scenario_id"]
        virtual = {"t": datetime.now(timezone.utc) - timedelta(seconds=130), "mono": 0.0}

        def sleep(seconds):
            virtual["t"] += timedelta(seconds=seconds)
            virtual["mono"] += seconds
            clock.sleep(seconds)

        original = module._run_probe
        module._run_probe = lambda scen, request_id: self.backend.contract_probe(scen)
        try:
            return module.verify_window(
                audit_db=self.audit_db, incident_id=prepared["incident_id"], request_id=request["request_id"],
                scenario_id=scenario, signing_key=self.verifier_private, contract=module._contract_for_scenario(scenario),
                window_seconds=120, interval_seconds=60, sleep=sleep, monotonic=lambda: virtual["mono"],
                now=lambda: virtual["t"])
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"verdict": "not_resolved", "resolution_eligible": False, "error": str(exc)[:200]}
        finally:
            module._run_probe = original

    def accept(self, request_id, attestation):
        return accept_verifier_attestation(
            audit_db=self.audit_db, public_key_path=self.verifier_public, request_id=request_id,
            attestation=VerifierAttestation.model_validate(attestation))

    def status(self, incident_id, request_id):
        return get_runtime_verification_status(
            audit_db=self.audit_db, incident_id=incident_id, request_id=request_id, public_key_path=self.verifier_public)


class ScriptedProposer:
    """Test/oracle proposer (NOT the locked model): ``chooser(features) -> action``. Default: the alert-derived action."""

    kind = "scripted_oracle_not_locked_model"
    version = "aihs-paper-repro-moodle-v1+scripted-test-double"
    baseline_id = "aihs-paper-repro-moodle-v1"

    def __init__(self, chooser=None):
        self.chooser = chooser or self.alert_oracle
        self.seen: list[dict] = []

    @staticmethod
    def alert_oracle(features: dict) -> str:
        derived = h.locked_scorer.derive_context(features)
        return MATCHING_ACTION[derived["scenario"]] if derived["status"] == "single" and derived["scenario"] in MATCHING_ACTION else h.CALL_HUMAN

    def propose(self, features: dict) -> dict:
        self.seen.append(dict(features))
        action = self.chooser(features)
        return {"predictor_action": action, "proposed_action": action, "path": "controller_accepted",
                "controller_trace": [{"action": action, "controller": "present", "predicted_success": True, "p_success": 0.99}]}


def build_sim(clock, tmp: Path, *, operator=None, **backend_kwargs):
    operator = operator or h.ScriptedOperator()
    backend = SimBackend(clock, **backend_kwargs)
    agent = SimAgent(backend, operator.public_key, tmp)
    return backend, agent, operator


def dry_run_cli(args) -> int:
    mapping = h.load_c0_mapping(h.REVIEWED_MAPPING)
    catalog = h.ActionCatalog.load_moodle_7_scenario_catalog()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    proposer = ScriptedProposer() if args.proposer == "oracle" else h.LockedModelProposer()
    rows = []
    arms = [a for a in args.arms.split(",") if a]
    for scenario in [s for s in args.scenarios.split(",") if s]:
        for arm in arms:
            clock = h.SimClock()
            with tempfile.TemporaryDirectory(prefix="aihs-sim-") as tmp:
                backend, agent, operator = build_sim(clock, Path(tmp))
                cfg = h.RunConfig(scenario=scenario, arm=arm, seq=len(rows), run_id=f"dry-{arm}-n{len(rows):02d}", settle_s=10)
                rec = h.run_one(cfg, backend=backend, agent=agent, operator=operator, proposer=proposer, mapping=mapping,
                                catalog=catalog, out_dir=out, clock=clock)
            rows.append(rec)
    print(f"{'run':<14}{'fault(label)':<13}{'proposal':<44}{'derived':<10}{'gate':<22}{'outcome':<24}resolved  recovered  reset  invariants")
    for r in rows:
        d = r["decision"] or {}
        inv = "ok" if all(r["invariants"].values()) else "VIOLATION"
        print(f"{r['run_id']:<14}{r['scoring']['scenario_ground_truth']:<13}{str(d.get('proposed_action')):<44}{str((d.get('derived_context') or {}).get('scenario')):<10}"
              f"{str((d.get('gate') or {}).get('decision') or d.get('reason')):<22}{r['outcome']:<24}"
              f"{str(r['resolved']):<9} {str(r['scoring'].get('recovered_true')):<10} {str(r['reset_verified']):<6} {inv}")
    summary = {"runs": len(rows), "by_outcome": {}, "invariant_violations": sum(1 for r in rows if not all(r["invariants"].values())),
               "resolved_runs": [r["run_id"] for r in rows if r["resolved"]], "simulated": True, "proposer": getattr(proposer, "kind", "locked_model")}
    for r in rows:
        summary["by_outcome"][r["outcome"]] = summary["by_outcome"].get(r["outcome"], 0) + 1
    h.write_json(out / "dry-run-summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["invariant_violations"] == 0 else 2


sys.modules.setdefault("live_c0_sim", sys.modules[__name__])
