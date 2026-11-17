"""Offline end-to-end tests for the live C0-C3 harness (evaluation/aihs/live_c0_harness.py) against fakes.

No AWS, SSH, Docker or network. The Gate, workflow, audit chain, Verifier window and attestation acceptance are the
real implementations; Prometheus/Alertmanager data, the fault, the Safe Executor and the probes are simulated.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "evaluation/aihs", ROOT / "lab/moodle-local"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import live_c0_harness as h  # noqa: E402
import live_c0_live as live  # noqa: E402
import live_c0_sim as sim  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

DRAFT = ROOT / "evaluation/aihs/a3-c0-action-mapping-draft.json"


def run(tmp_path, scenario, arm, *, chooser=None, operator=None, mapping=None, backend_hook=None,
        harness_collateral=None, **backend_kw):
    clock = h.SimClock()
    work = tmp_path / "agent"
    work.mkdir(parents=True, exist_ok=True)
    backend, agent, operator = sim.build_sim(clock, work, operator=operator, **backend_kw)
    if backend_hook:
        backend_hook(backend)
    proposer = sim.ScriptedProposer(chooser)
    cfg = h.RunConfig(scenario=scenario, arm=arm, seq=1, run_id=f"t-{arm}-n{abs(hash((arm, scenario, str(tmp_path)))) % 100000}",
                      settle_s=10, collateral=harness_collateral)
    rec = h.run_one(cfg, backend=backend, agent=agent, operator=operator, proposer=proposer,
                    mapping=mapping or h.load_c0_mapping(h.REVIEWED_MAPPING),
                    catalog=h.ActionCatalog.load_moodle_7_scenario_catalog(), out_dir=tmp_path / "runs", clock=clock)
    rec["_backend"], rec["_agent"], rec["_proposer"], rec["_operator"] = backend, agent, proposer, operator
    return rec


def audit_types(rec):
    with h.SafeActionAuditStore(rec["_agent"].audit_db, read_only=True) as audit:
        events = audit.list_for_incident(f"inc-live-{rec['run_id']}")
        return [e["event_type"] for e in events], audit.verify_chain(f"inc-live-{rec['run_id']}")


# ---------------------------------------------------------------------------------------------- happy paths
@pytest.mark.parametrize("scenario", h.LIVE_SCENARIOS)
def test_c3_chain_resolves_only_through_verifier_attestation(tmp_path, scenario):
    rec = run(tmp_path, scenario, "C3")
    assert rec["outcome"] == "RESOLVED_BY_VERIFIER" and rec["resolved"] is True
    assert rec["resolved_by"] == "independent_verifier_attestation"
    assert rec["runtime_status"]["status"] == "resolved" and rec["runtime_status"]["verification_event_hash"]
    assert rec["operator"]["decision"] == "approved" and rec["execution"]["status"] == "awaiting_verification"
    assert rec["scoring"]["recovered_true"] is True and rec["scoring"]["false_resolved"] is False
    assert rec["reset_verified"] is True and all(rec["invariants"].values())
    types, chain_ok = audit_types(rec)
    assert chain_ok
    for needed in ("aihs_shadow_evaluation", "live_gate", "live_executor_result", "live_verification_pending", "live_verification_result"):
        assert needed in types
    assert types.index("live_gate") < types.index("live_executor_result") < types.index("live_verification_result")
    assert rec["_backend"].executions[0]["scenario_id"] == scenario


def test_c2_executes_with_approval_but_is_never_resolved(tmp_path):
    rec = run(tmp_path, "DB-01", "C2")
    assert rec["outcome"] == "EXECUTED_HEALTH_ONLY" and rec["resolved"] is False
    assert rec["health_only_view"]["claimed_recovered"] is True and rec["scoring"]["recovered_true"] is True
    assert rec["runtime_status"]["status"] == "verifying"          # the Agent never reached RESOLVED
    assert rec["verifier"] is None
    types, _ = audit_types(rec)
    assert "live_verification_result" not in types
    assert rec["rpo"]["defined"] is True and rec["rpo"]["lost_acknowledged_writes"] == 0
    assert rec["metrics"]["detect_to_true_recovery_s"] is not None and rec["metrics"]["detect_to_resolved_s"] is None


@pytest.mark.parametrize("arm", ["C0", "C1"])
def test_shadow_arms_record_proposal_and_gate_but_never_execute(tmp_path, arm):
    rec = run(tmp_path, "NET-01", arm)
    assert rec["_backend"].executions == [] and rec["execution"] is None and rec["operator"] is None
    assert rec["decision"]["would_dispatch_as_is"] == "recreate_moodle_web_from_reviewed_compose"
    assert rec["decision"]["gate"]["decision"] == "REQUIRE_APPROVAL"
    assert rec["outcome"] == ("SHADOW_C0_RECORDED" if arm == "C0" else "SHADOW_C1_FORWARD")
    assert rec["reset_verified"] is True and rec["resolved"] is False


def test_rpo_defined_only_for_database_path_scenarios(tmp_path):
    assert run(tmp_path, "RES-01", "C1")["rpo"] == {"defined": False}
    assert run(tmp_path / "x", "NET-01", "C1")["rpo"]["defined"] is True


# ---------------------------------------------------------------------------------------------- supplementary
def test_main_sample_runs_carry_no_collateral(tmp_path):
    rec = run(tmp_path, "CON-01", "C3")
    assert rec["collateral"] is None and rec["outcome"] == "RESOLVED_BY_VERIFIER"


def test_supplementary_collateral_is_caught_by_verifier_and_marks_health_only_false_recovery(tmp_path):
    rec = run(tmp_path, "CON-01", "C3", harness_collateral="web_a_stop")
    assert rec["collateral"]["injected"] is True and rec["collateral"]["supplementary"] is True
    tl = rec["timeline"]
    assert tl["t_execute_end"] <= tl["t_health_claim"] <= tl["t_collateral_inject"] <= tl["t_verify_start"]
    assert rec["health_only_view"]["claimed_recovered"] is True
    assert rec["outcome"] == "VERIFIER_NOT_RESOLVED" and rec["resolved"] is False
    assert rec["scoring"]["false_recovery_health_only"] is True and rec["scoring"]["false_resolved"] is False
    assert rec["runtime_status"] is None or rec["runtime_status"].get("status") != "resolved"
    assert rec["reset_verified"] is True and rec["_backend"].collateral is False and all(rec["invariants"].values())
    assert _summary_module().stop_reasons(rec) == []      # the expected supplementary result does not stop the session


def test_collateral_reset_failure_is_not_a_verified_reset(tmp_path):
    rec = run(tmp_path, "CON-01", "C3", harness_collateral="web_a_stop", reset_ok=False)
    assert rec["reset_verified"] is False
    assert "reset không đưa staging về baseline" in _summary_module().stop_reasons(rec)


@pytest.mark.parametrize("scenario,arm,kind", [("CON-01", "C2", "web_a_stop"), ("DB-01", "C3", "web_a_stop"),
                                               ("CON-01", "C3", "drop_database")])
def test_collateral_is_refused_outside_the_reviewed_supplementary_design(scenario, arm, kind):
    with pytest.raises(ValueError):
        h.RunConfig(scenario=scenario, arm=arm, collateral=kind)


# ---------------------------------------------------------------------------------------------- fail closed
@pytest.mark.parametrize("arm", ["C2", "C3"])
def test_unknown_action_fails_closed_no_execution(tmp_path, arm):
    rec = run(tmp_path, "DB-01", arm, chooser=lambda f: "DROP_DATABASE")
    assert rec["outcome"] == "HUMAN_HANDOFF" and rec["decision"]["reason"] == "unknown_aihs_action"
    assert rec["_backend"].executions == [] and rec["resolved"] is False and rec["reset_verified"] is True
    assert rec["scoring"]["recovered_true"] is False                   # noncompletion stays in the data


def test_reset_only_and_alibaba_codes_are_unknown_actions(tmp_path):
    for bad in ("reconcile_db01_safe_baseline", "RI", "disable_moodle_cli_maintenance_mode"):
        rec = run(tmp_path / bad, "DB-01", "C3", chooser=lambda f, b=bad: b)
        assert rec["decision"]["reason"] == "unknown_aihs_action" and rec["_backend"].executions == []


def test_unreviewed_draft_mapping_never_reaches_execution(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", mapping=h.load_c0_mapping(DRAFT))
    assert rec["outcome"] == "HUMAN_HANDOFF" and rec["decision"]["reason"] == "unmapped_action"
    assert rec["_backend"].executions == []


def test_scope_mismatch_between_proposal_and_alert_context_blocks(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", chooser=lambda f: "restart_moodle_cron_service")
    assert rec["decision"]["reason"] == "scope_mismatch" and rec["outcome"] == "HUMAN_HANDOFF"
    assert rec["_backend"].executions == []


def test_ambiguous_synthetic_context_blocks(tmp_path):
    def both(backend):
        backend.inject = lambda s: backend.faults.update({"NET-01", "SEC-02"})
    rec = run(tmp_path, "NET-01", "C3", chooser=lambda f: "recreate_moodle_web_from_reviewed_compose", backend_hook=both)
    assert rec["decision"]["reason"] == "ambiguous_context" and rec["_backend"].executions == []


def test_observe_only_and_call_human_never_execute(tmp_path):
    for action, outcome in (("wait_and_recheck", "OBSERVE_ONLY_NO_ACTION"), ("call_human", "HUMAN_HANDOFF")):
        rec = run(tmp_path / action, "DB-01", "C3", chooser=lambda f, a=action: a)
        assert rec["outcome"] == outcome and rec["_backend"].executions == []


@pytest.mark.parametrize("policy,outcome", [("reject", "OPERATOR_REJECTED"), ("timeout", "OPERATOR_TIMEOUT")])
def test_no_signature_no_execution(tmp_path, policy, outcome):
    rec = run(tmp_path, "DB-01", "C3", operator=h.ScriptedOperator(policy))
    assert rec["outcome"] == outcome and rec["_backend"].executions == [] and rec["resolved"] is False
    assert rec["reset_verified"] is True


class WrongDigestOperator(h.ScriptedOperator):
    """Signs a DIFFERENT action than the one on the card."""

    def decide(self, card, clock):
        self.cards.append(card)
        other = h.sha256_bytes(b"some other action")
        return h.OperatorDecision("approved", h.sign_executor_approval(self.key, other), 1.0)


def test_signature_for_another_action_is_refused_before_dispatch(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", operator=WrongDigestOperator())
    assert rec["outcome"] == "EXECUTOR_REFUSED" and "approval_not_bound_to_prepared_action" in rec["notes"]
    assert rec["_backend"].executions == [] and rec["resolved"] is False


def test_signature_is_bound_to_action_digest_expiry_actor_and_key():
    key = Ed25519PrivateKey.generate()
    digest = h.sha256_bytes(b"A")
    approval = h.sign_executor_approval(key, digest)
    assert h.approval_binds(approval, digest, key.public_key())
    assert not h.approval_binds(approval, h.sha256_bytes(b"B"), key.public_key())
    assert not h.approval_binds({**approval, "action_sha256": h.sha256_bytes(b"B")}, h.sha256_bytes(b"B"), key.public_key())
    assert not h.approval_binds({**approval, "actor_id": "human-approver"}, digest, key.public_key())
    assert not h.approval_binds(approval, digest, Ed25519PrivateKey.generate().public_key())
    assert not h.approval_binds({k: v for k, v in approval.items() if k != "signature"}, digest, key.public_key())
    expired = h.sign_executor_approval(key, digest, now=datetime.now(timezone.utc) - timedelta(minutes=20))
    assert not h.approval_binds(expired, digest, key.public_key())


def test_executor_itself_rejects_approval_for_other_digest_and_unreviewed_triples(tmp_path):
    clock = h.SimClock()
    backend, _agent, operator = sim.build_sim(clock, tmp_path)
    key = h.action_digest("DB-01", "remove_scoped_db_reject", "staging_moodle_nodes", "k" * 20)
    body = {"scenario_id": "DB-01", "catalog_action_id": "remove_scoped_db_reject", "target_scope": "staging_moodle_nodes",
            "environment": "staging", "action_sha256": key, "idempotency_key": "k" * 20,
            "approval": h.sign_executor_approval(operator.key, h.sha256_bytes(b"other"))}
    assert backend.execute(body, "n" * 20)[1]["status"] == "awaiting_approval"
    body["approval"] = h.sign_executor_approval(operator.key, key)
    assert backend.execute({**body, "catalog_action_id": "reconcile_db01_safe_baseline"}, "m" * 20)[0] == 403
    backend.executor_enabled = False
    assert backend.execute(body, "o" * 20)[1]["status"] == "disabled"
    assert backend.executions == []


def test_kill_switch_off_means_executor_refusal_not_execution(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", executor_enabled=False)
    assert rec["outcome"] == "EXECUTOR_REFUSED" and rec["resolved"] is False and rec["_backend"].executions == []


def test_verifier_failure_on_collateral_damage_is_never_resolved(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", collateral=True)
    assert rec["outcome"] == "VERIFIER_NOT_RESOLVED" and rec["resolved"] is False
    assert rec["runtime_status"] is None or rec["runtime_status"]["status"] != "resolved"
    types, _ = audit_types(rec)
    assert "live_verification_result" not in types
    assert rec["health_only_view"]["claimed_recovered"] is True       # health-only would have been fooled
    assert rec["scoring"]["recovered_true"] is False


def test_health_only_false_recovery_is_detected_for_c2(tmp_path):
    rec = run(tmp_path, "DB-01", "C2", collateral=True)
    assert rec["scoring"]["false_recovery_health_only"] is True and rec["resolved"] is False


def test_runtime_rejects_attestation_signed_by_wrong_key(tmp_path):
    clock = h.SimClock()
    work = tmp_path / "agent"
    work.mkdir()
    backend, agent, operator = sim.build_sim(clock, work)
    agent.verifier_public.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        h.serialization.Encoding.PEM, h.serialization.PublicFormat.SubjectPublicKeyInfo))
    cfg = h.RunConfig(scenario="DB-01", arm="C3", run_id="t-wrongkey-n1", settle_s=5)
    rec = h.run_one(cfg, backend=backend, agent=agent, operator=operator, proposer=sim.ScriptedProposer(),
                    mapping=h.load_c0_mapping(h.REVIEWED_MAPPING), catalog=h.ActionCatalog.load_moodle_7_scenario_catalog(),
                    out_dir=tmp_path / "runs", clock=clock)
    assert rec["outcome"] == "VERIFIER_REJECTED_BY_RUNTIME" and rec["resolved"] is False and all(rec["invariants"].values())


def test_slow_approval_triggers_evidence_refresh_and_same_action_still_executes(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", operator=h.ScriptedOperator("approve", delay_s=400))
    assert "evidence_refreshed_same_action" in rec["notes"] and rec["outcome"] == "RESOLVED_BY_VERIFIER"
    assert rec["metrics"]["approval_wait_s"] >= 400


def test_slow_approval_with_changed_proposal_blocks_execution(tmp_path):
    calls = []

    def chooser(features):
        calls.append(1)
        return "remove_scoped_db_reject" if len(calls) == 1 else "call_human"
    rec = run(tmp_path, "DB-01", "C3", chooser=chooser, operator=h.ScriptedOperator("approve", delay_s=400))
    assert rec["outcome"] == "HANDOFF_BLOCKED" and rec["_backend"].executions == []
    assert any(n.startswith("evidence_refresh_changed") for n in rec["notes"])


def test_environment_failures_are_recorded_and_reset(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", baseline_ok=False)
    assert rec["outcome"] == "BASELINE_NOT_CLEAN" and rec["_backend"].injected_label == []
    rec = run(tmp_path / "d", "DB-01", "C3", detect=False)
    assert rec["outcome"] == "DETECTION_TIMEOUT" and rec["reset_verified"] is True and rec["_backend"].executions == []
    rec = run(tmp_path / "r", "DB-01", "C1", reset_ok=False)
    assert rec["reset_verified"] is False                              # campaign must stop on this


def test_harness_exception_still_resets(tmp_path):
    def boom(features):
        raise RuntimeError("model crashed")
    rec = run(tmp_path, "DB-01", "C3", chooser=boom)
    assert rec["outcome"] == "HARNESS_ERROR" and rec["reset_verified"] is True and rec["_backend"].executions == []


# ---------------------------------------------------------------------------------------------- ground truth isolation
def test_decision_stage_has_no_ground_truth_parameter():
    names = {n.lower() for n in inspect.signature(h.decide).parameters}
    assert not names & h.GROUND_TRUTH_PARAMS
    assert not {n.lower() for n in inspect.signature(h._decide).parameters} & h.GROUND_TRUTH_PARAMS


def test_decisions_follow_observed_symptoms_not_the_plan_label(tmp_path):
    rec = run(tmp_path, "DB-01", "C3", symptom_override="RES-01")      # label says DB-01, the system shows RES-01
    assert rec["scoring"]["scenario_ground_truth"] == "DB-01"
    assert rec["decision"]["derived_context"]["scenario"] == "RES-01"
    assert rec["decision"]["proposed_action"] == "remove_named_cpu_load_container"
    blob = json.dumps({"decision": rec["decision"], "handoff": rec["handoff"], "operator": rec["operator"],
                       "card": rec["_operator"].cards, "execution": rec["execution"]}, default=str)
    assert "DB-01" not in blob                                         # the label reached nothing a decision could use
    assert rec["_backend"].executions[0]["scenario_id"] == "RES-01"


def test_model_only_ever_sees_clean_features(tmp_path):
    rec = run(tmp_path, "DB-01", "C1")
    from moodle_aihs.features import check_feature_names
    assert rec["_proposer"].seen
    for features in rec["_proposer"].seen:
        check_feature_names(features)                                  # raises on any answer-bearing name
        assert not any(isinstance(v, str) for v in features.values())
        assert not any(k.startswith("lb_") and v is not None for k, v in features.items())   # ALB staging: no HAProxy


def test_run_artifacts_are_preserved_with_manifest(tmp_path):
    rec = run(tmp_path, "BIZ-02", "C3")
    run_dir = tmp_path / "runs" / rec["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    for name in ("run.json", "proposal.json", "decision.json", "operator_card.json", "execution.json", "verifier.json",
                 "scoring_samples.jsonl", "baseline_pre.json", "baseline_post.json", "audit.sqlite3", "snapshot/features.json"):
        assert name in manifest["files"], name
    assert h.sha256_bytes((run_dir / "run.json").read_bytes()) == manifest["files"]["run.json"]["sha256"]
    saved = json.loads((run_dir / "run.json").read_text())
    assert "private" not in json.dumps(saved).lower()


# ---------------------------------------------------------------------------------------------- config, plan, invariants
def test_never_live_scenarios_and_unknown_arms_are_refused():
    for scenario in ("SEC-01", "DB-02", "BIZ-01", "XX-99"):
        with pytest.raises(ValueError):
            h.RunConfig(scenario=scenario, arm="C3")
    with pytest.raises(ValueError):
        h.RunConfig(scenario="DB-01", arm="C4")


def test_resolved_without_verifier_violates_invariants():
    base = {"arm": "C3", "resolved": True, "resolved_by": None, "runtime_status": None, "execution": None,
            "operator": None, "handoff": None}
    assert h.check_invariants(base)["resolved_only_via_verifier"] is False
    assert h.check_invariants({**base, "arm": "C2", "resolved_by": "independent_verifier_attestation"})["c2_never_resolved"] is False
    assert h.check_invariants({**base, "arm": "C1", "execution": {"status": "x"}})["shadow_arms_never_execute"] is False


def test_plan_is_balanced_randomised_and_reproducible():
    plan = h.build_plan(seed=7, replicates=3)
    assert plan == h.build_plan(seed=7, replicates=3) and plan != h.build_plan(seed=8, replicates=3)
    assert len(plan) == 3 * len(h.LIVE_SCENARIOS) * 2
    for block in range(3):
        cells = [(p["scenario"], p["arm"]) for p in plan if p["block"] == block]
        assert sorted(cells) == sorted((s, a) for s in h.LIVE_SCENARIOS for a in ("C2", "C3"))
    assert [p["seq"] for p in plan] == list(range(len(plan)))


def test_stable_recovery_requires_unbroken_window():
    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    sample = lambda sec, ok: {"at": h.iso_ms(t0 + timedelta(seconds=sec)), "ok": ok}   # noqa: E731
    flaky = [sample(0, True), sample(60, False), sample(90, True), sample(200, True)]
    assert h.stable_recovery(flaky, 120)["recovered"] is False          # the failure at t=60 resets the streak
    good = [sample(0, False), sample(15, True), sample(75, True), sample(135, True)]
    assert h.stable_recovery(good, 120) == {"recovered": True, "t_recovered": h.iso_ms(t0 + timedelta(seconds=15)), "stable_for_s": 120.0}


# ---------------------------------------------------------------------------------------------- preflight and live adapters
def test_offline_preflight_blocks_only_on_infrastructure_and_operator_host(monkeypatch):
    if not (h.MODEL_DIR / "model.pkl").is_file() or not live.PROTOCOL_LOCK.is_file():
        pytest.skip("locked C0 artifacts are git-ignored and absent here")
    for name in live.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    report = live.preflight(offline=True)
    assert report["status"] == "BLOCKED" and report["blocked_integrity"] == []
    categories = {c["category"] for c in report["checks"] if c["status"] == "BLOCKED"}
    assert categories <= {"infrastructure", "operator_host"}
    ids = {c["id"]: c for c in report["checks"]}
    assert ids["locked_evidence_unmodified"]["status"] == "OK" and ids["locked_model_hashes"]["status"] == "OK"
    assert ids["reviewed_mapping"]["status"] == "OK" and ids["executor_allowlist_matches_mapping"]["status"] == "OK"
    assert ids["feature_schema_gap_lab_vs_staging"]["status"] == "WARN"
    assert not any("sk-" in c["detail"] for c in report["checks"])


def test_preflight_key_check_reports_paths_only_and_rejects_open_permissions(tmp_path):
    from cryptography.hazmat.primitives import serialization
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "operator.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pub = tmp_path / "approval-ed25519.pub"
    pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    ok = live._key_check("operator_signing_key", str(path), pub)
    if sys.platform != "win32":
        path.chmod(0o600)
        ok = live._key_check("operator_signing_key", str(path), pub)
    assert ok["status"] == "OK" and "BEGIN" not in ok["detail"]
    pub.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert live._key_check("operator_signing_key", str(path), pub)["status"] == "BLOCKED"
    assert live._key_check("operator_signing_key", None, pub)["status"] == "BLOCKED"


def test_api_agent_fails_closed_on_ambiguous_candidates_and_http_errors(tmp_path):
    calls = []

    def http(method, url, body=None, token=None, timeout=0):
        calls.append((method, url))
        return 500, {}, {}

    class Done:
        def __init__(self, out):
            self.stdout, self.returncode = out, 0

    seen = []

    def runner(args, **_):
        seen.append(args[-1])
        return Done(out)

    agent = live.ApiAgent({"AIHS_LIVE_AGENT_URL": "http://agent.invalid", "MOODLE_VERIFIER_SIGNING_KEY_FILE": ""},
                          http=http, runner=runner)
    clock = h.SimClock()
    no_context = type("D", (), {"evidence_refs": [], "context": None})()
    out = "cand-1"
    assert agent.prepare(incident_id="i", features={}, decision=no_context, evidence_db=None, audit_db=tmp_path / "a.sqlite3", clock=clock) is None
    assert calls == [] and seen == []                                   # no derived scenario: no lookup, no Agent call
    decision = type("D", (), {"evidence_refs": [], "context": type("C", (), {"scenario_id": "CON-01"})()})()
    for out in ("", "cand-1,cand-2"):                                   # none, or malformed/ambiguous: never guess
        assert agent.prepare(incident_id="i", features={}, decision=decision, evidence_db=None, audit_db=tmp_path / "a.sqlite3", clock=clock) is None
    assert calls == []
    assert "'CON-01'" in seen[-1] or " CON-01 " in seen[-1]             # the lookup is bound to this run's scenario
    out = "cand-1"
    assert agent.prepare(incident_id="i", features={}, decision=decision, evidence_db=None, audit_db=tmp_path / "a.sqlite3", clock=clock) is None
    assert calls[0][1].endswith("/api/moodle/remediation/cand-1/prepare?notify=false")
    result = agent.execute({"candidate_id": "cand-1", "request": {}}, {}, decision=decision, clock=clock)
    assert result["status"] == "escalated"                              # a failed execute call never looks like success
    assert agent.accept("req-1", {})["status"] == "rejected" and agent.status("i", "req-1")["status"] == "unknown"


def test_locked_model_proposal_is_a_valid_c0_row_when_artifacts_exist():
    if not (h.MODEL_DIR / "model.pkl").is_file():
        pytest.skip("locked C0 artifacts are git-ignored and absent here")
    from core.moodle.aihs_c0 import C0Proposal
    clock = h.SimClock()
    backend = sim.SimBackend(clock)
    backend.faults.add("BIZ-02")
    import lab_collector
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        snap = lab_collector.Collector(Path(tmp), query=backend.prom_query, log_reader=backend.log_reader, clock=clock.now).collect(
            "s", h.iso_ms(clock.now()))
        features = json.loads((Path(tmp) / "s" / "features.json").read_text())
    proposer = h.LockedModelProposer()
    row = h.build_proposal_row(proposer.propose(features), features=features, snapshot=snap, predicted_at=h.iso_ms(clock.now()),
                               model_version=proposer.version, baseline_id=proposer.baseline_id, seq=3)
    parsed = C0Proposal.model_validate(row)
    assert parsed.model_version.startswith("aihs-paper-repro-moodle-v1+0da82b58") and parsed.record_id == "a2-9003"


# ---------------------------------------------------------------------------------------------- staging metric schema check
LB_FEATURES = {"lb_server_up", "lb_active_servers", "lb_5xx_30s", "lb_check_failures_60s"}


def _fake_prometheus(drop=frozenset()):
    """Instant-query fake that answers every collector query for every expected label, except dropped feature names."""
    import lab_collector as lc
    by_promql = {promql: (name, label, expected) for name, promql, label, expected in lc.QUERIES}

    def query(expr: str):
        if expr not in by_promql:
            return []
        name, label, expected = by_promql[expr]
        out = []
        for value in expected:
            if f"{name}__{value}" in drop or name in drop:
                continue
            instance = {"a": "moodle-app-a", "b": "moodle-app-b"}.get(value, value)
            out.append({"metric": {label: instance} if label else {}, "value": [0, "1"]})
        return out
    return query


@pytest.fixture(scope="module")
def vocabulary():
    if not (h.MODEL_DIR / "model.pkl").is_file():
        pytest.skip("locked C0 artifacts are git-ignored and absent here")
    return list(h.LockedModelProposer().model.mapper.vocabulary)


def test_metric_schema_gap_ok_when_only_haproxy_words_missing(vocabulary):
    gap = live.metric_schema_gap(_fake_prometheus(drop=LB_FEATURES), vocabulary)
    assert gap["blocked"] is False and gap["d1_missing"] == [] and gap["unexpected_missing"] == []
    assert gap["missing"] and all(word.startswith("lb_") for word in gap["missing"]) and len(gap["missing"]) <= live.KNOWN_LB_GAP


@pytest.mark.parametrize("dropped", ["moodle_node_db_dns_matches_expected", "moodle_node_db_dns_success",
                                     "moodle_node_fixture_dir_mode"])
def test_metric_schema_gap_blocks_when_a_d1_metric_is_absent(vocabulary, dropped):
    gap = live.metric_schema_gap(_fake_prometheus(drop=LB_FEATURES | {dropped}), vocabulary)
    assert gap["blocked"] is True and gap["d1_missing"]
    assert all(word.startswith(dropped) for word in gap["d1_missing"])


def test_metric_schema_gap_blocks_on_any_other_missing_word(vocabulary):
    gap = live.metric_schema_gap(_fake_prometheus(drop=LB_FEATURES | {"moodle_node_cpu_percent"}), vocabulary)
    assert gap["blocked"] is True and gap["d1_missing"] == []
    assert any(word.startswith("moodle_node_cpu_percent") for word in gap["unexpected_missing"])


def test_metric_schema_gap_blocks_when_prometheus_returns_nothing(vocabulary):
    gap = live.metric_schema_gap(lambda _expr: [], vocabulary)
    assert gap["blocked"] is True and set(gap["d1_missing"]) == set(live.D1_REQUIRED)


def test_metric_schema_check_reports_unreachable_prometheus_as_blocked(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no route")
    monkeypatch.setattr(live, "http_json", boom)
    check = live._metric_schema_check({"AIHS_LIVE_PROM_URL": "http://127.0.0.1:1"})
    assert check["id"] == "staging_metric_schema" and check["status"] == "BLOCKED"


def test_offline_preflight_lists_the_metric_schema_check_as_blocked_infrastructure(monkeypatch, tmp_path):
    if not (h.MODEL_DIR / "model.pkl").is_file() or not live.PROTOCOL_LOCK.is_file():
        pytest.skip("locked C0 artifacts are git-ignored and absent here")
    monkeypatch.setattr(live, "SECRETS_DIR", tmp_path)
    for name in live.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    ids = {c["id"]: c for c in live.preflight(offline=True)["checks"]}
    assert ids["staging_metric_schema"]["status"] == "BLOCKED" and ids["staging_metric_schema"]["category"] == "infrastructure"


def test_injection_failure_keeps_a_short_output_tail():
    backend = live.CommandBackend(env={})
    backend._script = lambda *_a, **_k: type("R", (), {"returncode": 255, "stdout": "PHP Fatal error: boom\n", "stderr": ""})()
    with pytest.raises(RuntimeError, match=r"rc=255\): PHP Fatal error: boom"):
        backend.inject("BIZ-02")


def test_console_operator_hold_reprompts_until_exact_answer(tmp_path, monkeypatch):
    """timeout_s=0 holds the card open: wrong lines (e.g. 'APPROVED') re-prompt, only APPROVE <token> approves."""
    import io
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = tmp_path / "op.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    digest = "ab" * 32
    card = {"proposed_action": "x", "catalog_action_id": "x", "scenario_id": "RES-01", "target_scope": "moodle-app-b",
            "executor_action_sha256": digest}
    op = live.KeyFileOperator(str(key), timeout_s=0)
    monkeypatch.setattr(live.sys, "stdin", io.StringIO(f"APPROVED\napprove {digest[:12]}\n"))
    decision = op.decide(card, h.SimClock())
    assert decision.decision == "approved" and decision.approval is not None
    monkeypatch.setattr(live.sys, "stdin", io.StringIO("Approve\n"))   # stdin closes without a valid answer
    assert op.decide(card, h.SimClock()).decision == "timeout"


def test_operator_card_explains_action_and_marks_evidence_without_ground_truth():
    card = {"proposed_action": "remove_scoped_db_reject", "scenario_id": "DB-01", "target_scope": "staging_moodle_nodes",
            "executor_action_sha256": "ab" * 32, "gate_preview": {"decision": "REQUIRE_APPROVAL", "checks": ["a", "b"],
                                                                  "approval_reasons": ["confidence 0.65 < 0.8"]},
            "firing_alerts": ["MoodleNodeRdsTcpFailed", "MoodlePublicProbeFailed", "MoodleNodeCpuHigh"]}
    text = live.render_operator_card(card, "abababababab")
    assert "MoodleNodeRdsTcpFailed" in text and "[KHỚP]" in text
    assert "[TRIỆU CHỨNG]" in text and "[KHÁC]" in text
    assert "REQUIRE_APPROVAL" in text and "APPROVE" in text and "REJECT" in text
    assert "ground_truth" not in text and "scenario_ground_truth" not in text
    assert set(live.ACTION_GUIDE) == {e.aihs_action for e in h.load_c0_mapping(h.REVIEWED_MAPPING).actions
                                      if e.status == "live_reviewed"}


def test_timeline_progress_lines_are_display_only(capsys):
    tl = h._Timeline()
    tl["t_inject"] = "2026-11-16T05:00:00.000Z"
    tl["t_detect"] = "2026-11-16T05:00:42.000Z"
    err = capsys.readouterr().err
    assert "+42 s" in err and "phát hiện" in err
    assert json.loads(json.dumps({"timeline": tl}))["timeline"] == {"t_inject": "2026-11-16T05:00:00.000Z",
                                                                    "t_detect": "2026-11-16T05:00:42.000Z"}


def _summary_module():
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "automation" / "aihs-live-summary.py"
    spec = importlib.util.spec_from_file_location("aihs_live_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_stop_gate_flags_wrong_operator_decisions_and_bad_runs():
    summary = _summary_module()
    mapping = h.load_c0_mapping(h.REVIEWED_MAPPING)
    assert summary.ACTION_SCENARIO == {e.aihs_action: e.scenario_id for e in mapping.actions if e.status == "live_reviewed"}
    ok_inv = {"a": True}

    def rec(outcome, truth, action, operator=None, reason="gate_require_approval", reset=True):
        return {"outcome": outcome, "scoring": {"scenario_ground_truth": truth}, "invariants": ok_inv,
                "reset_verified": reset, "decision": {"proposed_action": action, "reason": reason},
                "operator": {"decision": operator} if operator else None}

    assert summary.stop_reasons(rec("RESOLVED_BY_VERIFIER", "SEC-02", "restore_fixture_directory_mode", "approved")) == []
    assert summary.stop_reasons(rec("OPERATOR_REJECTED", "DB-01", "call_human_x", "rejected")) == []
    assert summary.stop_reasons(rec("HUMAN_HANDOFF", "NET-01", "recreate_moodle_web_from_reviewed_compose",
                                    reason="scope_mismatch")) == []
    assert any("REJECT" in r for r in summary.stop_reasons(rec("OPERATOR_REJECTED", "DB-01", "remove_scoped_db_reject", "rejected")))
    assert any("APPROVE" in r for r in summary.stop_reasons(rec("RESOLVED_BY_VERIFIER", "NET-01", "remove_scoped_db_reject", "approved")))
    assert summary.stop_reasons(rec("VERIFIER_NOT_RESOLVED", "BIZ-02", "restart_moodle_cron_service", "approved", reset=False))
    assert summary.stop_reasons(rec("HUMAN_HANDOFF", "DB-01", "remove_scoped_db_reject", reason="ambiguous_context")) == []
    assert summary.stop_reasons(rec("HUMAN_HANDOFF", "NET-01", "call_human", reason="human_only_action")) == []
    assert summary.stop_reasons(rec("HANDOFF_BLOCKED", "CON-01", "start_reviewed_compose_service"))
