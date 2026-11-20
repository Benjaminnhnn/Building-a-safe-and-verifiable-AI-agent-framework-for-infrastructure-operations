"""Tests for the offline C0-C3 scorer (evaluation/aihs/score_c0_c3_offline.py).

Synthetic fixtures only: nothing here reads the real labels, the real test split, the locked model or the real
phase4 directory. The mapping/catalog are the git-tracked draft mapping and the Moodle catalog.
"""

import inspect
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluation/aihs"))

import score_c0_c3_offline as sc  # noqa: E402
from moodle_aihs import data  # noqa: E402

CRON, WEB, DB = "restart_moodle_cron_service", "recreate_moodle_web_from_reviewed_compose", "remove_scoped_db_reject"
START = datetime(2026, 11, 15, 16, 0, 0, tzinfo=timezone.utc)
BATCH = "2026-11-15T18:01:42.708Z"


def stamp(minutes):
    return (START + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def features(*alerts):
    out = {f"alert_firing__{a}": 1.0 for a in alerts}
    out["moodle_node_cpu_percent__a"] = 0.1
    return out


def proposal(rid, action, minutes=0, p_success=0.99):
    return {"baseline_id": "aihs-paper-repro-moodle-v1",
            "controller_trace": [{"action": action, "controller": "present", "p_success": p_success, "predicted_success": True}],
            "evidence_cutoff_at": stamp(minutes), "model_version": "aihs-paper-repro-moodle-v1+test", "path": "controller_accepted",
            "predicted_at": BATCH, "predictor_action": action, "proposed_action": action, "record_hash": f"{int(rid[-4:]):064x}",
            "record_id": rid, "snapshot_sha256": f"{int(rid[-4:]) + 5000:064x}", "split": "test", "status": "LOCKED_C0"}


def label_row(rid, executed, outcome="success", transient=False, feats=None):
    return {"record_id": rid, "features": feats or {}, "action": executed, "outcome": outcome, "transient": transient,
            "record_hash": f"{int(rid[-4:]):064x}"}


@pytest.fixture(scope="module")
def mapping_catalog():
    return sc.load_mapping_and_catalog()


# ------------------------------------------------------------------ trusted context (D1)
def test_context_single_primary_cause_with_symptoms():
    ctx = sc.derive_context(features("MoodleNodeRdsTcpFailed", "MoodlePublicProbeFailed", "MoodleSyntheticTxnFailed"))
    assert (ctx["status"], ctx["scenario"]) == ("single", "DB-01")


def test_context_synthetic_only_is_ambiguous_and_never_guessed():
    ctx = sc.derive_context(features("MoodleSyntheticTxnFailed") | {
        "moodle_node_db_dns_matches_expected__a": 1.0, "moodle_node_fixture_dir_mode__a": 770.0})
    assert ctx["status"] == "ambiguous" and ctx["scenario"] is None
    assert ctx["candidates"] == ["NET-01", "SEC-02"]


def test_synthetic_only_disambiguated_by_dns_metrics():
    base = features("MoodleSyntheticTxnFailed") | {"moodle_node_fixture_dir_mode__a": 770.0}
    for extra in ({"moodle_node_db_dns_matches_expected__b": 0.0}, {"moodle_node_db_dns_success__a": 0.0}):
        ctx = sc.derive_context(base | extra)
        assert (ctx["status"], ctx["scenario"]) == ("single", "NET-01")


def test_synthetic_only_disambiguated_by_fixture_mode():
    base = features("MoodleSyntheticTxnFailed") | {"moodle_node_db_dns_matches_expected__a": 1.0, "moodle_node_db_dns_success__a": 1.0}
    ctx = sc.derive_context(base | {"moodle_node_fixture_dir_mode__a": 700.0})
    assert (ctx["status"], ctx["scenario"]) == ("single", "SEC-02")


def test_synthetic_only_both_or_neither_stays_ambiguous():
    both = features("MoodleSyntheticTxnFailed") | {"moodle_node_db_dns_success__a": 0.0, "moodle_node_fixture_dir_mode__a": 700.0}
    assert sc.derive_context(both)["candidates"] == ["NET-01", "SEC-02"]
    assert sc.derive_context(features("MoodleSyntheticTxnFailed"))["status"] == "ambiguous"


def test_metrics_do_not_add_candidates_without_the_synthetic_alert():
    ctx = sc.derive_context({"moodle_node_db_dns_success__a": 0.0, "moodle_node_fixture_dir_mode__a": 700.0})
    assert ctx["status"] == "none"
    ctx = sc.derive_context(features("MoodleNodeCpuHigh") | {"moodle_node_db_dns_success__a": 0.0})
    assert ctx["scenario"] == "RES-01"


def test_fixture_mode_constant_matches_label_tool():
    label_tool = pytest.importorskip("label_tool")
    assert label_tool.HEALTHY_REFERENCE["fixture_dir_mode"] == sc.HEALTHY_FIXTURE_MODE


def test_context_compound_unknown_and_empty_fail_closed():
    assert sc.derive_context(features("MoodleNodeCpuHigh", "MoodleWebContainerMissing"))["status"] == "ambiguous"
    assert sc.derive_context(features("MoodleEfsMountMissing"))["candidates"] == [sc.OUT_OF_SCOPE]
    assert sc.derive_context(features("MoodleEfsMountMissing"))["scenario"] is None
    assert sc.derive_context({})["status"] == "none"
    assert sc.derive_context({"alert_firing__MoodleNodeCpuHigh": None})["status"] == "none"


# ------------------------------------------------------------------ Gate stage
def run_gate(rows_props, feats, mapping_catalog, clock="decision"):
    mapping, catalog = mapping_catalog
    return sc.gate_stage(rows_props, feats, mapping=mapping, catalog=catalog, clock=clock)


def test_gate_forwards_matching_blocks_mismatch_and_ambiguity(mapping_catalog):
    props = [proposal("a2-0001", CRON), proposal("a2-0002", DB), proposal("a2-0003", WEB),
             proposal("a2-0004", "call_human"), proposal("a2-0005", sc.WAIT)]
    feats = {"a2-0001": features("MoodleAdhocTaskQueueStuck"), "a2-0002": features("MoodleAdhocTaskQueueStuck"),
             "a2-0003": features("MoodleSyntheticTxnFailed"), "a2-0004": features(), "a2-0005": features()}
    out = run_gate(props, feats, mapping_catalog)
    assert (out["a2-0001"]["kind"], out["a2-0001"]["gate_decision"]) == ("FORWARD", "REQUIRE_APPROVAL")
    assert (out["a2-0002"]["kind"], out["a2-0002"]["reason"]) == ("BLOCK", "scope_mismatch")
    assert (out["a2-0003"]["kind"], out["a2-0003"]["reason"]) == ("BLOCK", "ambiguous_context")
    assert out["a2-0004"]["kind"] == "NONMUT" and out["a2-0005"]["kind"] == "NONMUT"


def test_gate_forwards_net01_when_dns_metric_supports_it(mapping_catalog):
    feats = features("MoodleSyntheticTxnFailed") | {"moodle_node_db_dns_matches_expected__a": 0.0}
    out = run_gate([proposal("a2-0001", WEB), proposal("a2-0002", CRON)], {"a2-0001": feats, "a2-0002": feats}, mapping_catalog)
    assert out["a2-0001"]["kind"] == "FORWARD"
    assert out["a2-0002"]["reason"] == "scope_mismatch"


def test_control_batch_clock_is_stale_evidence(mapping_catalog):
    out = run_gate([proposal("a2-0001", CRON)], {"a2-0001": features("MoodleAdhocTaskQueueStuck")}, mapping_catalog, "recorded")
    assert (out["a2-0001"]["kind"], out["a2-0001"]["reason"]) == ("BLOCK", "stale_evidence")


def test_unknown_action_and_no_context_fail_closed(mapping_catalog):
    props = [proposal("a2-0001", "DROP_DATABASE"), proposal("a2-0002", CRON)]
    out = run_gate(props, {"a2-0001": features("MoodleAdhocTaskQueueStuck"), "a2-0002": features()}, mapping_catalog)
    assert out["a2-0001"]["reason"] == "unknown_aihs_action" and out["a2-0002"]["reason"] == "no_context"


# ------------------------------------------------------------------ ground truth never feeds the Gate
def scoring_inputs():
    rows = [label_row("a2-0001", CRON, "success", feats=features("MoodleAdhocTaskQueueStuck")),
            label_row("a2-0002", DB, "failure", feats=features("MoodleAdhocTaskQueueStuck")),
            label_row("a2-0003", WEB, "success", feats=features("MoodleSyntheticTxnFailed"))]
    props = [proposal("a2-0001", CRON), proposal("a2-0002", DB), proposal("a2-0003", WEB)]
    q1 = {"a2-0001": "yes", "a2-0002": "no", "a2-0003": "yes"}
    groups = {"a2-0001": "g1", "a2-0002": "g2", "a2-0003": "g3"}
    return rows, props, q1, groups


def test_gate_signature_has_no_label_inputs():
    names = set(inspect.signature(sc.gate_one).parameters) | set(inspect.signature(sc.gate_stage).parameters)
    assert not names & {"label", "labels", "outcome", "q1", "executed", "ground_truth", "scenario", "row_labels", "rows"}


def test_labels_outcomes_and_p_success_never_change_gate_output(mapping_catalog, monkeypatch):
    mapping, catalog = mapping_catalog
    rows, props, q1, groups = scoring_inputs()
    seen = []
    original = sc.gate_one

    def spy(row, feats, **kwargs):
        seen.append((dict(row), dict(feats)))
        return original(row, feats, **kwargs)

    monkeypatch.setattr(sc, "gate_one", spy)
    base = sc.run_scoring(rows, props, q1, groups, b=20, mapping=mapping, catalog=catalog)["scoring"]["per_row"]
    flipped_rows = [dict(r, outcome="failure" if r["outcome"] == "success" else "success", action=CRON, transient=True)
                    for r in rows]
    flipped_q1 = {k: "unsure" for k in q1}
    flipped_props = [proposal(p["record_id"], p["proposed_action"], p_success=0.0) for p in props]
    flipped = sc.run_scoring(flipped_rows, flipped_props, flipped_q1, groups, b=20, mapping=mapping, catalog=catalog)["scoring"]["per_row"]
    key = lambda items: [(i["record_id"], i["disposition"], i["reason"]) for i in items]  # noqa: E731
    assert key(base) == key(flipped)
    forbidden = {"outcome", "action", "transient", "q1", "post_decision", "scenario_code", "episode", "proposal"}
    for row, feats in seen:
        assert not forbidden & set(row) and not forbidden & set(feats)


# ------------------------------------------------------------------ metrics
def test_metric_formulas_and_unknowns():
    counts = {"n": 10, "succ": 4, "hit_c0": 3, "hit_c1": 2, "fail": 2, "rev_c0": 1, "rev_c1": 2, "L": 4, "L_unsafe": 2,
              "unsafe_forwarded": 1, "blocked_unsafe": 1, "L_safe": 2, "blocked_safe": 1, "handoff_c0": 1, "handoff_c1": 4,
              "mutating": 8, "forwarded": 5}
    m = sc.metrics_from_counts(counts)
    assert m["C0.HitRate"] == 0.75 and m["C0.ReverseRate"] == 0.5
    assert m["C0.OverallRate"] == pytest.approx(0.6)
    assert m["C1.wrong_action_blocked_rate"] == 0.5 and m["C1.false_block_rate"] == 0.5
    assert m["C0.unsafe_action_rate"] == 0.5 and m["C1.unsafe_forwarded_rate"] == 0.25
    assert m["C1.human_handoff_share"] == 0.4
    empty = sc.metrics_from_counts({"n": 3})
    assert empty["C0.HitRate"] is None and empty["C0.OverallRate"] is None and empty["C1.false_block_rate"] is None
    assert sc._harmonic(0.0, 0.0) == 0.0


def test_row_counts_labelled_set_and_ties():
    allow = {"kind": "FORWARD", "reason": "require_approval"}
    block = {"kind": "BLOCK", "reason": "scope_mismatch"}
    # proposal == executed with Q1=no, blocked -> blocked_unsafe
    c = sc.row_counts(label_row("a2-0001", CRON, "failure"), CRON, block, "no")
    assert c["L_unsafe"] == 1 and c["blocked_unsafe"] == 1 and c["rev_c0"] == 0 and c["rev_c1"] == 1
    # proposal differs from executed -> no Q1 applies, only descriptive counters
    c = sc.row_counts(label_row("a2-0002", WEB, "success"), CRON, allow, "yes")
    assert "L" not in c and c["unlabelled_proposal_differs"] == 1 and c["unlabelled_forwarded"] == 1
    # unsure is excluded from numerators and denominators
    c = sc.row_counts(label_row("a2-0003", CRON, "success"), CRON, allow, "unsure")
    assert "L" not in c and c["unlabelled_q1_unsure"] == 1
    # transient rows are excluded from Hit/Reverse
    c = sc.row_counts(label_row("a2-0004", CRON, "success", transient=True), CRON, allow, "yes")
    assert "succ" not in c and c["L_safe"] == 1
    # call_human never mutates: no L, counts as hand-off
    c = sc.row_counts(label_row("a2-0005", CRON, "failure"), "call_human", {"kind": "NONMUT", "reason": "human_only_action"}, "yes")
    assert "L" not in c and c["handoff_c0"] == 1 and c["rev_c0"] == 1


def test_bootstrap_is_deterministic_and_seed_dependent():
    vectors = {f"g{i}": {"n": 1, "succ": 1, "hit_c0": i % 2, "hit_c1": 1, "fail": 1, "rev_c0": 1, "rev_c1": i % 3 == 0, "L": 1,
                         "L_safe": 1, "blocked_safe": i % 2, "mutating": 1, "forwarded": 1 - i % 2} for i in range(12)}
    first = sc.bootstrap(vectors, b=200, seed=7)
    assert first == sc.bootstrap(vectors, b=200, seed=7)
    assert first != sc.bootstrap(vectors, b=200, seed=8)
    ci = first["C0.HitRate"]
    assert ci["ci_low"] <= ci["ci_high"] and ci["n_valid_resamples"] == 200
    assert sc.BOOTSTRAP_SEED == 20261116 and sc.BOOTSTRAP_B == 2000


def test_insufficient_denominator_is_flagged(mapping_catalog):
    mapping, catalog = mapping_catalog
    rows, props, q1, groups = scoring_inputs()
    result = sc.run_scoring(rows, props, q1, groups, b=20, mapping=mapping, catalog=catalog)
    assert "C1.wrong_action_blocked_rate" in result["scoring"]["INSUFFICIENT_DENOMINATOR"]
    assert result["configs"]["C2"]["status"] == result["configs"]["C3"]["status"] == "OFFLINE_EQUALS_C1_BY_CONSTRUCTION"
    assert "false_recovery_rate" in result["not_measurable_until_live_staging"]


def test_connected_groups_merge_incident_and_session():
    recs = {"a2-0001": {"incident_group_id": "i1", "session_group_id": "s1"},
            "a2-0002": {"incident_group_id": "i2", "session_group_id": "s1"},
            "a2-0003": {"incident_group_id": "i3", "session_group_id": "s3"}}
    g = sc.connected_groups(recs, list(recs))
    assert g["a2-0001"] == g["a2-0002"] != g["a2-0003"]


# ------------------------------------------------------------------ protocol lock and single opening
class Env:
    """A throwaway repo root with a protocol doc, a scorer stand-in, a split, a locked model dir and test proposals."""

    def __init__(self, tmp: Path):
        self.root = tmp / "root"
        self.files = ("doc.md", "scorer.py")
        self.root.mkdir()
        (self.root / "doc.md").write_text("protocol")
        (self.root / "scorer.py").write_text("code")
        self.phase4 = tmp / "phase4"
        model_dir = tmp / "model"
        model_dir.mkdir()
        (model_dir / "model.pkl").write_bytes(b"model-bytes")
        (model_dir / "model-lock.json").write_text(json.dumps({"status": "LOCKED_C0", "model_content_sha256": "c" * 64}))
        self.model_dir = model_dir
        spec = [("a2-0001", CRON, "success", ("MoodleAdhocTaskQueueStuck",)), ("a2-0002", DB, "failure", ("MoodleAdhocTaskQueueStuck",)),
                ("a2-0003", WEB, "success", ("MoodleSyntheticTxnFailed",))]
        records = []
        for i, (rid, action, outcome, alerts) in enumerate(spec):
            records.append({"record_id": rid, "incident_group_id": f"g{i}", "session_group_id": f"s{i}", "outcome": outcome,
                            "timeline": {"decision_at": stamp(10 * i + 5)}, "episode": {"transient": False},
                            "post_decision": {"executed_action_id": action},
                            "pre_decision": {"features": features(*alerts), "snapshot_sha256": f"{i:064x}",
                                             "evidence_cutoff_at": stamp(10 * i), "captured_at": stamp(10 * i)}})
        self.records = records
        records_path = tmp / "records.jsonl"
        records_path.write_text("".join(json.dumps(r) + "\n" for r in records))
        hashes = {r["record_id"]: data.record_hash(r) for r in records}
        empty = {"record_ids": [], "record_hashes": {}, "groups": 0}
        manifest = {"mode": "FROZEN", "records_file": str(records_path), "records_sha256": data.sha256_file(records_path),
                    "split_ids_sha256": "d" * 64,
                    "splits": {"train": empty, "validation": empty,
                               "test": {"record_ids": list(hashes), "record_hashes": hashes, "groups": 3}}}
        self.manifest_path = tmp / "split-manifest.json"
        self.manifest_path.write_text(json.dumps(manifest))
        props = [dict(proposal(r["record_id"], r["post_decision"]["executed_action_id"]), record_hash=hashes[r["record_id"]])
                 for r in records]
        self.proposals_path = model_dir / "proposals-test.jsonl"
        self.proposals_path.write_text("".join(json.dumps(p) + "\n" for p in props))
        self.q1_path = tmp / "q1.jsonl"
        self.q1_path.write_text("".join(json.dumps({"record_id": r, "proposed_action_safe_and_in_scope": v}) + "\n"
                                        for r, v in {"a2-0001": "yes", "a2-0002": "no", "a2-0003": "yes"}.items()))
        self.arm = {"split_manifest": self.manifest_path, "model_dir": model_dir, "q1_labels": self.q1_path,
                    "label_policy": "partial_to_failure"}

    def lock(self):
        return sc.write_lock("primary", self.arm, phase4=self.phase4, root=self.root, files=self.files)

    def test(self):
        return sc.run_test("primary", self.arm, phase4=self.phase4, root=self.root, files=self.files, b=30)


@pytest.fixture()
def env(tmp_path):
    return Env(tmp_path)


def test_refuses_without_lock_and_never_reads_test_labels(env, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("test labels were opened without a lock")

    monkeypatch.setattr(sc, "load_split", boom)
    monkeypatch.setattr(sc, "load_q1", boom)
    with pytest.raises(sc.ProtocolError, match="missing"):
        env.test()
    assert not (env.phase4 / "test-opened-primary.json").exists()
    with pytest.raises(data.SealedTestError):
        data.Splits({"mode": "FROZEN", "records": {"train": [], "validation": [], "test": []}, "groups": {}}).rows("test")


@pytest.mark.parametrize("mutate,expected", [
    (lambda e: (e.root / "doc.md").write_text("edited protocol"), "code differs"),
    (lambda e: (e.root / "scorer.py").write_text("edited scorer"), "code differs"),
    (lambda e: e.proposals_path.write_text(e.proposals_path.read_text() + "\n"), "proposals_sha256"),
    (lambda e: (e.model_dir / "model.pkl").write_bytes(b"other model"), "model_file_sha256"),
    (lambda e: (e.model_dir / "model-lock.json").write_text(json.dumps({"status": "LOCKED_C0", "model_content_sha256": "e" * 64})),
     "model_content_sha256"),
    (lambda e: e.q1_path.write_text(""), "q1_labels_sha256"),
])
def test_refuses_on_hash_mismatch(env, mutate, expected):
    env.lock()
    mutate(env)
    with pytest.raises(sc.ProtocolError, match=expected):
        env.test()
    assert not (env.phase4 / "test-opened-primary.json").exists()


def test_lock_is_not_overwritable_and_requires_locked_model(env):
    env.lock()
    with pytest.raises(sc.ProtocolError, match="already exists"):
        env.lock()
    (env.model_dir / "model-lock.json").write_text(json.dumps({"status": "DEV_MODEL_NOT_FROZEN_C0", "model_content_sha256": "c" * 64}))
    shutil.rmtree(env.phase4)
    with pytest.raises(sc.ProtocolError, match="not LOCKED_C0"):
        env.lock()


def test_test_split_opens_exactly_once_and_scores(env):
    env.lock()
    summary = env.test()
    assert summary["scope"] == "TEST_CONFIRMATORY_SINGLE_OPENING" and summary["scoring"]["rows"] == 3
    assert (env.phase4 / "test-primary" / "results.json").is_file()
    assert (env.phase4 / "test-opened-primary.json").is_file()
    with pytest.raises(sc.ProtocolError, match="already opened"):
        env.test()


def test_proposal_record_hash_mismatch_is_refused(env):
    rows = [json.loads(line) for line in env.proposals_path.read_text().splitlines()]
    rows[0]["record_hash"] = "f" * 64
    env.proposals_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    env.lock()
    with pytest.raises(sc.ProtocolError, match="record_hash"):
        env.test()


def test_default_lock_and_marker_names():
    # Tests only ever use tmp phase4 directories; they never create the real lock or marker.
    assert sc.lock_path(sc.PHASE4, "primary").name == "protocol-lock-primary.json"
    assert "test-opened" in sc.marker_path(sc.PHASE4, "primary").name
