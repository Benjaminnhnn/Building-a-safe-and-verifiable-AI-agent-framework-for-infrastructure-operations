"""Pre-decision collector, redaction and leakage-check tests (no Docker or Prometheus needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "lab/moodle-local", ROOT / "evaluation/aihs"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import lab_collector as lc  # noqa: E402
from b2_freeze_dataset import FORBIDDEN_FEATURE  # noqa: E402


def fake_query(promql):
    table = {
        'moodle_node_web_running{job="moodle_node"}': [
            {"metric": {"instance": "moodle-app-a"}, "value": [0, "1"]},
            {"metric": {"instance": "moodle-app-b"}, "value": [0, "0"]}],
        "moodle_synthetic_success": [{"metric": {}, "value": [0, "0"]}],
    }
    if promql.startswith("ALERTS"):
        return [{"metric": {"alertname": "MoodleSyntheticTransactionFailed", "instance": "monitor-ai-01", "alertstate": "firing"}, "value": [0, "1"]}]
    return table.get(promql, [])


def logs(container, **_):
    return f"10.0.0.7 - - GET /login?sesskey=abc123 token=secretvalue mail a@b.example\nERROR could not connect to server {container}\n"


class Clock:
    def __init__(self):
        from datetime import datetime, timedelta, timezone
        self.t = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
        self.step = timedelta(seconds=1)

    def __call__(self):
        self.t += self.step
        return self.t


def collect(tmp_path, detected="2026-11-15T12:00:00.500Z", **kwargs):
    collector = lc.Collector(tmp_path, query=fake_query, log_reader=kwargs.pop("log_reader", logs), clock=Clock())
    return collector.collect("snapshot", detected, **kwargs)


def test_redaction_removes_tokens_ips_emails_hex_and_blobs():
    text = "Authorization: Bearer abc.def  ip 10.1.2.3 user a@b.example id " + "a" * 40 + " sig " + "Zm9v" * 14
    out = lc.redact_text(text)
    for secret in ("10.1.2.3", "a@b.example", "a" * 40, "Zm9v" * 14, "abc.def"):
        assert secret not in out
    assert "<ip>" in out and "<redacted" in out


def test_snapshot_is_written_with_hashes_and_cutoff_before_prediction(tmp_path):
    index = collect(tmp_path)
    directory = tmp_path / "snapshot"
    assert (directory / "snapshot.json").is_file()
    assert index["detected_at"] <= index["evidence_cutoff_at"] == index["capture_finished_at"]
    for name, digest in index["parts"].items():
        assert lc.sha256_bytes((directory / name).read_bytes()) == digest
    assert index["features_sha256"] == lc.sha256_bytes((directory / "features.json").read_bytes())
    lc.assert_cutoff(index, "2026-11-15T12:05:00.000Z")
    with pytest.raises(lc.LeakageError):
        lc.assert_cutoff(index, "2026-11-15T12:00:00.000Z")           # prediction before the evidence cutoff
    for text in (directory / "logs" / "web_a.log").read_text().splitlines():
        assert "secretvalue" not in text and "10.0.0.7" not in text and "a@b.example" not in text


def test_features_are_numeric_and_pass_the_freeze_name_rule(tmp_path):
    collect(tmp_path)
    features = json.loads((tmp_path / "snapshot" / "features.json").read_text())
    assert features["moodle_node_web_running__a"] == 1.0 and features["moodle_node_web_running__b"] == 0.0
    assert features["alert_firing__MoodleSyntheticTxnFailed"] == 1.0
    assert features["log_db_connect_errors__web_a"] == 1.0
    for name, value in features.items():
        assert not FORBIDDEN_FEATURE.search(name), name                 # same rule b2_freeze_dataset enforces
        assert not lc.FORBIDDEN_FEATURE_NAME.search(name) and not lc.FORBIDDEN_EVIDENCE.search(name), name
        assert value is None or isinstance(value, (int, float))


@pytest.mark.parametrize("bad", [
    "scenario_code", "ground_truth_flag", "executor_receipt_status", "verifier_verdict", "gate_decision", "approval_state",
    "incident_id", "run_id", "fault_marker", "episode_variant", "signature", "label", "policy_choice", "outcome",
    "moodle_fault_db01_tagged_rule",
])
def test_forbidden_feature_names_are_rejected(bad):
    with pytest.raises(lc.LeakageError):
        lc.check_leakage({bad: 1.0}, {})


def test_non_numeric_or_non_finite_features_rejected():
    for value in ("DB-01", True, float("nan"), float("inf")):
        with pytest.raises(lc.LeakageError):
            lc.check_leakage({"ok_name": value}, {})


@pytest.mark.parametrize("text", [
    "fixture DB-01 injected", "rule moodle-fault-DB-01", "moodle_fault marker", "ground truth here",
    "idempotency key lab-ep-1", "approval signature", "run_id=7", "container ep-0123456789ab",
])
def test_scenario_markers_and_answer_bearing_ids_in_raw_evidence_are_blocked(tmp_path, text):
    with pytest.raises(lc.LeakageError):
        collect(tmp_path, log_reader=lambda container, **_: f"normal line\n{text}\n")


def test_run_specific_identifier_is_blocked(tmp_path):
    with pytest.raises(lc.LeakageError):
        collect(tmp_path, log_reader=lambda container, **_: "request lab-abc123xyz token\n", extra_forbidden=["lab-abc123xyz"])


def test_unexpected_post_decision_fields_in_snapshot_index_are_rejected():
    index = {"schema": lc.SNAPSHOT_SCHEMA, "executed_action": "x", "verifier_verdict": "y"}
    with pytest.raises(lc.LeakageError):
        lc.check_leakage({}, {}, snapshot_index=index)


def test_detection_after_capture_is_rejected(tmp_path):
    with pytest.raises(lc.LeakageError):
        collect(tmp_path, detected="2026-11-15T13:00:00.000Z")


def test_staging_marker_series_are_never_collected(tmp_path):
    def query(promql):
        if "moodle_fault" in promql:
            raise AssertionError("fault marker queried")
        return fake_query(promql)
    collector = lc.Collector(tmp_path, query=query, log_reader=logs, clock=Clock())
    collector.collect("snapshot", "2026-11-15T12:00:00.500Z")
    assert all("fault" not in name for name in json.loads((tmp_path / "snapshot" / "features.json").read_text()))
