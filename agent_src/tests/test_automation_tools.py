from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harvester = load_script("benchmark_harvester", "automation/harvest-benchmark-results.py")
inspector = load_script("evidence_inspector", "automation/inspect_live_db.py")
trigger = load_script("local_agent_trigger", "automation/trigger_live_agent.py")
benchmark_runner = load_script("benchmark_runner", "automation/run-benchmark.py")


def test_harvest_labels_fixture_benchmark_as_simulated(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    record = {
        "run_id": "fixture-1",
        "scenario_id": "DB-01",
        "method": "ai_agent",
        "repetition": 1,
        "timestamps": {"t_inject": None, "t_detect": None},
    }
    (campaign / "benchmark_results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    rows = harvester.collect_campaign(campaign, "ai_agent")

    assert rows[0]["evidence_class"] == "simulated_fixture_benchmark"
    assert rows[0]["t_inject"] == ""


def test_harvest_preserves_runtime_method_and_authority(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    run = campaign / "run-1"
    run.mkdir(parents=True)
    payload = json.dumps({
        "run_id": "AUTH-01-1",
        "scenario_id": "AUTH-01",
        "status": "passed",
        "method": "ai_agent_shadow",
        "evidence_class": "staging_runtime_shadow",
        "ai_execution": False,
        "execution_authority": "allowlisted_test_harness",
        "t_inject": "2026-10-04T00:00:00Z",
        "mttd_seconds": 7,
    })
    (run / "result.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

    row = harvester.collect_campaign(campaign, "ai_agent_shadow")[0]

    assert row["evidence_class"] == "staging_runtime_shadow"
    assert row["method"] == "ai_agent_shadow"
    assert row["ai_execution"] is False
    assert row["execution_authority"] == "allowlisted_test_harness"
    assert row["t_inject"] == "2026-10-04T00:00:00Z"
    assert row["mttd_seconds"] == 7


def test_harvest_refuses_unmarked_runtime_result(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "result.json").write_text(json.dumps({
        "run_id": "run-1", "scenario_id": "AUTH-01", "status": "passed"
    }), encoding="utf-8")

    with pytest.raises(harvester.InputError, match="evidence_class"):
        harvester.collect_campaign(campaign, None)


def test_inspector_opens_read_only_and_emits_counts(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER)")
        connection.execute("INSERT INTO evidence VALUES (1)")
        connection.execute("CREATE TABLE checkpoints (id INTEGER)")

    assert inspector.inspect(database) == {"evidence": 1, "checkpoints": 0}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1


def test_trigger_only_allows_plain_loopback_webhook() -> None:
    assert trigger.loopback_webhook("http://127.0.0.1:8000/webhook")
    assert trigger.loopback_webhook("http://[::1]:8000/webhook")
    assert not trigger.loopback_webhook("https://127.0.0.1:8000/webhook")
    assert not trigger.loopback_webhook("http://example.com/webhook")
    assert not trigger.loopback_webhook("http://user:pass@localhost/webhook")
    assert not trigger.loopback_webhook("http://localhost/webhook/other")


def test_repeated_simulated_campaigns_get_distinct_run_ids() -> None:
    from datetime import datetime, timezone
    import random

    scenario = {"scenario_id": "DB-01"}
    base_time = datetime(2026, 10, 4, tzinfo=timezone.utc)
    first = benchmark_runner.simulate_manual_run(scenario, 1, base_time, random.Random(42), "campaign-a")
    second = benchmark_runner.simulate_manual_run(scenario, 1, base_time, random.Random(42), "campaign-b")

    assert first.run_id != second.run_id


def test_benchmark_cli_can_include_erpnext_explicitly() -> None:
    parser = benchmark_runner.build_argument_parser()

    assert parser.parse_args([]).moodle_only is True
    assert parser.parse_args(["--include-erpnext"]).moodle_only is False
    assert parser.parse_args(["--moodle-only"]).moodle_only is True
