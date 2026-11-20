"""Tests for the Moodle AIHS training pipeline (evaluation/aihs/moodle_aihs).

Needs scikit-learn and LightGBM, which are NOT in the hash-locked agent_src/requirements.txt; they are skipped where
those packages are absent (for example the Agent image CI job). Install evaluation/aihs/requirements-moodle-aihs.txt
to run them locally.
"""

import json
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluation/aihs"))

from action_ladder import CALL_HUMAN, COST_LADDER  # noqa: E402
from moodle_aihs import AS_IS_BASELINE_ID, PAPER_BASELINE_ID, data, pipeline  # noqa: E402
from moodle_aihs.features import AttributeDiscretizer, check_feature_names  # noqa: E402
from moodle_aihs.mapper import Mapper  # noqa: E402
from moodle_aihs.metrics import evaluate, rates  # noqa: E402
from moodle_aihs.model import MoodleAIHSModel  # noqa: E402
from moodle_aihs.purge import purge_temporal_overlap  # noqa: E402

START = datetime(2026, 11, 1, tzinfo=timezone.utc)
A_CRON, A_WEB, A_DB = "restart_moodle_cron_service", "recreate_moodle_web_from_reviewed_compose", "remove_scoped_db_reject"


def stamp(minutes):
    return (START + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_records(n=60, sessions_of=2):
    """Synthetic lab-style records: three actions, each with successes AND failures; feature `sig` ~ the action."""
    actions = [A_CRON, A_WEB, A_DB]
    records = []
    for i in range(n):
        action = actions[i % 3]
        success = (i // 3) % 2 == 0
        decision = 10 * i + 5
        records.append({
            "record_id": f"a2-{i + 1:04d}", "incident_group_id": f"g{i + 1:04d}", "session_group_id": f"s{i // sessions_of + 1:04d}",
            "scenario_code": "DB-01",
            "provenance": {"evidence_mode": "lab", "source_sha256": "a" * 64, "historical_code_version": "lab-test"},
            "timeline": {"detected_at": stamp(decision - 3), "decision_at": stamp(decision), "predicted_at": stamp(decision),
                         "executed_at": stamp(decision + 1), "verified_at": stamp(decision + 2)},
            "pre_decision": {"snapshot_at": stamp(decision - 1), "evidence_cutoff_at": stamp(decision - 1),
                             "captured_at": stamp(decision - 1), "snapshot_sha256": f"{i:064x}", "feature_status": "verified_predecision",
                             "evidence_ref_hashes": [f"{i + 1000:064x}"],
                             "features": {"sig_a": float(10 * (i % 3) + (i * 7) % 5), "sig_b": float(3 - i % 3), "noise": float((i * 13) % 7),
                                          "lb_5xx": float(i % 2), "missing_sometimes": None if i % 4 == 0 else float(i % 5)}},
            "proposal": {"source": "other", "action_id": action, "model_version": "behavior-policy"},
            "post_decision": {"gate_decision": None, "operator_approved": False, "executed_action_id": action,
                              "receipt_status": "executed", "verifier_verdict": "x", "runtime_status": "recovered_stable"},
            "outcome": "success" if success else "failure", "training_eligible": True, "exclusion_reasons": [],
            "episode": {"kind": "single", "fault_count": 1, "background_load": "none", "transient": i % 11 == 0,
                        "exploration": False, "stability_window_seconds": 25.0},
        })
    return records


@pytest.fixture(scope="module")
def dev(tmp_path_factory):
    work = tmp_path_factory.mktemp("phase2")
    records = make_records()
    path = work / "audit-records.jsonl"
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    split = data.dev_split(records)
    manifest = data.split_manifest(split, path, records)
    mpath = work / "split-manifest.json"
    mpath.write_text(json.dumps(manifest))
    return work, path, mpath, records, manifest


# ---------------------------------------------------------------- leakage
def test_answer_bearing_and_episode_fields_are_rejected_as_features():
    for bad in ("episode_transient", "scenario_code", "injected_fault", "outcome", "plan", "executed_action_id", "exploration_flag"):
        with pytest.raises(ValueError, match="leakage"):
            check_feature_names({bad: 1.0})
    with pytest.raises(ValueError, match="leakage"):
        AttributeDiscretizer().fit([{"scenario_marker": 1.0}])


def test_model_rows_expose_only_pre_decision_features(dev):
    _, _, _, records, _ = dev
    row = data.to_row(records[0])
    assert row["features"] == records[0]["pre_decision"]["features"]
    assert not {"episode", "scenario_code", "plan", "kind"} & set(row["features"])


def test_vocabulary_never_contains_label_side_tokens(dev):
    _, path, mpath, _, _ = dev
    splits, _ = pipeline.load_splits(path, mpath)
    mapper = Mapper(5, 1).fit([r["features"] for r in splits.rows("train")])
    assert all(not any(x in word for x in ("episode", "scenario", "transient", "outcome")) for word in mapper.vocabulary)


# ------------------------------------------------------- train-only fitting
def test_mapper_fit_ignores_validation_and_test_rows(dev):
    _, path, mpath, _, _ = dev
    splits, _ = pipeline.load_splits(path, mpath)
    train = [r["features"] for r in splits.rows("train")]
    base = Mapper(5, 1).fit(train)
    # Changing/adding validation-only content must not change a train-only fit...
    extra = train + []                                   # same input -> same output
    assert Mapper(5, 1).fit(extra).content_digest() == base.content_digest()
    # ...and a validation-only attribute is not in the vocabulary and gets no edges.
    foreign = {"only_in_validation": 99.0, **splits.rows("validation")[0]["features"]}
    assert "only_in_validation" not in base.vocabulary
    assert "only_in_validation" not in base.discretizer.levels(foreign)
    assert base.transform([foreign]).shape == (1, 5)
    # Fitting with the validation rows added DOES change the digest, proving the fit input matters.
    more = Mapper(5, 1).fit(train + [foreign])
    assert more.content_digest() != base.content_digest()


# ------------------------------------------------------------- determinism
def test_training_is_deterministic_in_hash_and_proposals(dev):
    work, path, mpath, _, _ = dev
    pipeline.train(path, mpath, PAPER_BASELINE_ID, work / "a", seed=7)
    pipeline.train(path, mpath, PAPER_BASELINE_ID, work / "b", seed=7)
    assert (work / "a/model.pkl").read_bytes() == (work / "b/model.pkl").read_bytes()
    pipeline.lock(work / "a", path, mpath)
    summary = pipeline.propose(work / "a", path, mpath)
    assert summary["scored"] is False and summary["labels_opened"] is False
    assert pipeline.reproduce(work / "a", path, mpath)["reproducible"] is True
    row = json.loads((work / "a/proposals-test.jsonl").read_text().splitlines()[0])
    for key in ("model_version", "snapshot_sha256", "evidence_cutoff_at", "predicted_at", "proposed_action", "controller_trace"):
        assert key in row
    assert not {"outcome", "action", "executed_action_id", "label"} & set(row)


def test_model_file_must_match_lock_before_unpickling(dev):
    work, path, mpath, _, _ = dev
    pipeline.train(path, mpath, PAPER_BASELINE_ID, work / "t", seed=3)
    pipeline.lock(work / "t", path, mpath)
    (work / "t/model.pkl").write_bytes((work / "t/model.pkl").read_bytes() + b"x")
    with pytest.raises(ValueError, match="refusing to unpickle"):
        pipeline.load_model(work / "t")


# -------------------------------------------------------- fallback ladder
class Stub:
    classes_ = [0, 1]

    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    def predict(self, x):
        self.calls += 1
        return [self.verdict]

    def predict_proba(self, x):
        return [[1 - self.verdict, self.verdict]]


class StubPredictor:
    def __init__(self, label):
        self.label = label

    def predict(self, x):
        return [self.label]


def _stub_model(baseline, proposed, verdicts):
    model = MoodleAIHSModel(baseline, 5, 1)
    model.mapper.transform = lambda rows: [[0.0]] * len(rows)
    model.predictor = StubPredictor(proposed)
    model.controllers = {a: Stub(v) for a, v in verdicts.items()}
    return model


def test_fallback_tries_remaining_actions_in_ascending_ladder_cost():
    verdicts = {a: 0 for a in COST_LADDER if a != CALL_HUMAN}
    verdicts.update({"restart_moodle_cron_service": 0, "start_reviewed_compose_service": 1, "remove_named_cpu_load_container": 1})
    model = _stub_model(PAPER_BASELINE_ID, "remove_scoped_db_reject", verdicts)
    result = model.propose({"x": 1.0})
    assert result["proposed_action"] == "start_reviewed_compose_service"     # cheapest accepting action, not the 2nd one
    order = [t["action"] for t in result["controller_trace"]]
    assert order == ["remove_scoped_db_reject", "wait_and_recheck", "restart_moodle_cron_service", "start_reviewed_compose_service"]
    assert model.controllers["remove_named_cpu_load_container"].calls == 0   # never reached
    assert [COST_LADDER.index(a) for a in order[1:]] == sorted(COST_LADDER.index(a) for a in order[1:])


def test_fallback_exhausted_goes_to_call_human_and_skips_missing_controllers():
    model = _stub_model(PAPER_BASELINE_ID, A_DB, {A_DB: 0, A_CRON: 0})        # all other controllers absent
    result = model.propose({"x": 1.0})
    assert result["proposed_action"] == CALL_HUMAN and result["path"] == "fallback_exhausted_call_human"
    assert any(t["controller"] == "absent" for t in result["controller_trace"])


def test_as_is_variant_reproduces_the_prototype_fallback_bug():
    verdicts = {A_DB: 0, "wait_and_recheck": 1}
    model = _stub_model(AS_IS_BASELINE_ID, A_DB, verdicts)
    result = model.propose({"x": 1.0})
    assert result["proposed_action"] == CALL_HUMAN and result["path"] == "prototype_fallback_bug_call_human"
    assert model.controllers["wait_and_recheck"].calls == 0
    paper = _stub_model(PAPER_BASELINE_ID, A_DB, verdicts).propose({"x": 1.0})
    assert paper["proposed_action"] == "wait_and_recheck"


# ----------------------------------------------- controller coverage / limits
def test_missing_controller_is_recorded_as_limitation(dev):
    work, path, mpath, records, _ = dev
    # Remove every failure of the DB action from the data: that action then has no both-class evidence.
    patched = []
    for r in records:
        if r["post_decision"]["executed_action_id"] == A_DB and r["outcome"] == "failure":
            r = json.loads(json.dumps(r))
            r["outcome"] = "success"
        patched.append(r)
    p2 = work / "patched.jsonl"
    p2.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in patched))
    manifest = data.split_manifest(data.dev_split(patched), p2, patched)
    m2 = work / "patched-manifest.json"
    m2.write_text(json.dumps(manifest))
    splits, _ = pipeline.load_splits(p2, m2)
    model = MoodleAIHSModel(PAPER_BASELINE_ID, 5, 1).fit(splits.rows("train"))
    assert A_DB not in model.controllers
    assert model.coverage[A_DB]["controller"] is False and model.coverage[A_DB]["executed_failure"] == 0
    assert any(x.startswith(f"controller_missing:{A_DB}") for x in model.limitations)
    assert A_CRON in model.controllers


def test_predictor_uses_only_successful_non_transient_rows(dev):
    _, path, mpath, _, _ = dev
    splits, _ = pipeline.load_splits(path, mpath)
    model = MoodleAIHSModel(PAPER_BASELINE_ID, 5, 1).fit(splits.rows("train"))
    expected = sum(1 for r in splits.rows("train") if r["outcome"] == "success" and not r["transient"])
    assert model.counts["predictor_rows"] == expected
    as_is = MoodleAIHSModel(AS_IS_BASELINE_ID, 10, 1).fit(splits.rows("train"))
    assert as_is.counts["predictor_rows"] == sum(1 for r in splits.rows("train") if r["outcome"] in ("success", "failure"))


# --------------------------------------------------------------- sealed test
def test_test_split_is_sealed_until_locked(dev):
    work, path, mpath, _, _ = dev
    splits, _ = pipeline.load_splits(path, mpath)
    with pytest.raises(data.SealedTestError):
        splits.rows("test")
    snaps = splits.test_snapshots()
    assert snaps and not {"outcome", "action", "transient", "exploration", "scenario_code"} & set(snaps[0])
    model = MoodleAIHSModel(PAPER_BASELINE_ID, 5, 1).fit(splits.rows("train"))
    with pytest.raises(data.SealedTestError):
        evaluate(model, splits, "test")
    with pytest.raises(data.SealedTestError):
        evaluate(model, splits, "test", {"model_content_sha256": "0" * 64})
    assert evaluate(model, splits, "validation")["rows"] == len(splits.rows("validation"))


# ------------------------------------------------------------------- split
def test_split_groups_never_cross_and_are_time_ordered(dev):
    *_, manifest = dev
    sp = manifest["splits"]
    assert sum(sp[n]["records"] for n in sp) + len(manifest["purged_overlapping_records"]) == manifest["eligible_records"]
    ranges = [sp[n]["decision_at_range"] for n in ("train", "validation", "test")]
    assert ranges[0][1] < ranges[1][0] and ranges[1][1] < ranges[2][0]
    assert 0.6 < sp["train"]["records"] / manifest["eligible_records"] < 0.8
    ids = [i for n in sp for i in sp[n]["record_ids"]]
    assert len(ids) == len(set(ids)) and not set(ids) & set(manifest["purged_overlapping_records"])


def test_edited_record_is_detected_by_the_split_manifest(dev):
    work, path, mpath, records, manifest = dev
    tampered = json.loads(json.dumps(records))
    tampered[0]["pre_decision"]["features"]["noise"] = 12345.0
    with pytest.raises(data.SplitError):
        pipeline.splits_from_manifest(tampered, manifest)


def test_overlapping_groups_are_purged_from_the_earlier_split():
    def rec(i, minute):
        return {"record_id": i, "timeline": {"decision_at": stamp(minute)}}
    groups = {"train": [[rec("a", 0), rec("b", 12)], [rec("c", 20)]], "validation": [[rec("d", 15), rec("e", 30)]],
              "test": [[rec("f", 40)]]}
    clean, purged = purge_temporal_overlap(groups)
    assert [r["record_id"] for g in purged for r in g] == ["c"]
    assert [r["record_id"] for g in clean["train"] for r in g] == ["a", "b"]


def test_rates_follow_the_paper_definitions():
    rows = [{"action": "x", "outcome": "success", "transient": False}, {"action": "x", "outcome": "success", "transient": False},
            {"action": "y", "outcome": "failure", "transient": False}, {"action": "y", "outcome": "failure", "transient": False},
            {"action": "y", "outcome": "success", "transient": True}]
    finals = ["x", "z", "z", "y", "z"]
    result = rates(rows, finals)
    assert (result["HitRate"], result["ReverseRate"]) == (0.5, 0.5)
    assert result["OverallRate"] == 0.5 and result["transient_excluded"] == 1
    assert rates([{"action": "x", "outcome": "success", "transient": False}], ["x"])["OverallRate"] is None


def test_lock_records_provenance_fields(dev):
    work, path, mpath, _, _ = dev
    pipeline.train(path, mpath, PAPER_BASELINE_ID, work / "lk", seed=11)
    lock = pipeline.lock(work / "lk", path, mpath)
    for key in ("code", "dependencies", "seeds", "K", "lightgbm_params", "feature_spec_sha256", "vocabulary_sha256",
                "model_file_sha256", "split_manifest_sha256", "locked_at", "lda"):
        assert key in lock
    assert {"git_sha", "dirty_diff_sha256"} <= set(lock["code"])
    assert lock["status"] == "DEV_MODEL_NOT_FROZEN_C0" and "Gibbs" in lock["lda"]["deviation_from_paper"]
    assert pickle.loads((work / "lk/model.pkl").read_bytes())["model"].n_topics == lock["K"]
