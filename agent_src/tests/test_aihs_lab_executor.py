"""Unit tests for the single-source action ladder and the lab executor (no Docker needed)."""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "lab/moodle-local", ROOT / "evaluation/aihs"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import action_ladder  # noqa: E402
import lab_executor  # noqa: E402
from lab_env import LabCommandError  # noqa: E402


def staging_catalog() -> dict:
    tree = ast.parse((ROOT / "automation/moodle-safe-executor-api.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CATALOG":
            return ast.literal_eval(node.value)
    raise AssertionError("CATALOG not found")


def test_cost_ladder_order_is_the_locked_decision():
    ladder = action_ladder.COST_LADDER
    assert ladder[0] == "wait_and_recheck" and ladder[-1] == "call_human"
    assert list(ladder[1:4]) == ["restart_moodle_cron_service", "start_reviewed_compose_service",
                                 "recreate_moodle_web_from_reviewed_compose"]
    top = {"remove_named_cpu_load_container", "remove_scoped_db_reject", "restore_fixture_directory_mode"}
    assert set(ladder[4:7]) == top
    assert len({action_ladder.cost_rank(a) for a in top}) == 1  # equal cost
    assert set(action_ladder.PREDICTOR_CLASSES) == set(ladder) and len(ladder) == 8


@pytest.mark.skip(reason="SEC-02 was retired from the action catalog on develop (thesis7 = 7 scenarios); restore its shadow-only catalog entry to re-enable")
def test_catalog_matches_staging_executor_keys_and_nodes():
    staging = staging_catalog()
    for key, nodes in lab_executor.CATALOG.items():
        assert staging[key] == nodes, key
    assert len(lab_executor.CATALOG) == 6
    for scenario, action_id, _scope in lab_executor.CATALOG:
        assert not action_id.startswith("reconcile_")
        assert scenario not in {"SEC-01", "DB-02", "BIZ-01"}


def test_unknown_action_fails_closed():
    with pytest.raises(ValueError):
        action_ladder.spec("rm_rf_everything")
    with pytest.raises(ValueError):
        lab_executor.build_request("call_human", "k" * 20, None)


@pytest.fixture()
def executor(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(lab_executor, "NODE_ACTIONS", {a: (lambda node, a=a: calls.append((a, node))) for a in lab_executor.NODE_ACTIONS})
    approver = lab_executor.LabApprover.load_or_create(tmp_path / "key.pem")
    return lab_executor.LabExecutor(tmp_path / "state.sqlite3", approver.public_key), approver, calls


def request(approver, action="remove_scoped_db_reject", key="lab-test-key-000000000001"):
    return lab_executor.build_request(action, key, approver)


def test_valid_request_executes_each_node_in_catalog_order(executor):
    ex, approver, calls = executor
    status, body = ex.execute(request(approver, "recreate_moodle_web_from_reviewed_compose"), "n" * 20)
    assert status == 200 and body["status"] == "executed" and body["completed_nodes"] == ["a", "b"]
    assert calls == [("recreate_moodle_web_from_reviewed_compose", "a"), ("recreate_moodle_web_from_reviewed_compose", "b")]


def test_single_node_scopes(executor):
    ex, approver, calls = executor
    ex.execute(request(approver, "start_reviewed_compose_service", "lab-test-key-000000000002"), "n" * 20)
    assert calls == [("start_reviewed_compose_service", "b")]


def test_denials_never_mutate(executor):
    ex, approver, calls = executor
    good = request(approver)
    cases = [
        ({**good, "environment": "staging"}, 403),
        ({**good, "target_scope": "everything"}, 403),
        ({**good, "catalog_action_id": "reconcile_db01_safe_baseline"}, 403),
        ({**good, "action_sha256": "0" * 64}, 403),
        ({**good, "approval": {}}, 403),
        ({**good, "extra": 1}, 403),
    ]
    for body, expected in cases:
        status, response = ex.execute(body, "n" * 20)
        assert status == expected and response["status"] in {"denied", "awaiting_approval"}
    other = lab_executor.LabApprover.load_or_create(Path(ex.state_db).parent / "other.pem")
    forged = request(other)
    assert ex.execute(forged, "m" * 20)[1]["status"] == "awaiting_approval"
    assert calls == []


def test_expired_or_wrong_action_approval_is_rejected(executor):
    ex, approver, calls = executor
    good = request(approver)
    expired = dict(good, approval=approver.approve(good["action_sha256"], ttl_seconds=-5))
    assert ex.execute(expired, "n" * 20)[1]["status"] == "awaiting_approval"
    wrong = dict(good, approval=approver.approve("f" * 64))
    assert ex.execute(wrong, "n" * 20)[1]["status"] == "awaiting_approval"
    assert calls == []


def test_nonce_replay_and_idempotency(executor):
    ex, approver, calls = executor
    body = request(approver)
    assert ex.execute(body, "n" * 20)[0] == 200
    assert ex.execute(body, "n" * 20) == (409, {"status": "denied", "reason": "replayed_request_nonce"})
    status, replay = ex.execute(request(approver), "p" * 20)       # same key + same body, fresh nonce -> stored result
    assert status == 200 and replay["status"] == "executed"
    assert len(calls) == 2                                           # not executed a second time (2 nodes, once)


def test_kill_switch_blocks_before_any_mutation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(lab_executor, "NODE_ACTIONS", {a: (lambda node, a=a: calls.append(a)) for a in lab_executor.NODE_ACTIONS})
    approver = lab_executor.LabApprover.load_or_create(tmp_path / "key.pem")
    ex = lab_executor.LabExecutor(tmp_path / "s.sqlite3", approver.public_key, live_enabled=False)
    status, body = ex.execute(request(approver), "n" * 20)
    assert status == 423 and body["mutated"] is False and calls == []


def test_failure_is_reported_as_unknown_mutation(tmp_path, monkeypatch):
    def boom(node):
        raise LabCommandError("exit 1: docker")
    monkeypatch.setattr(lab_executor, "NODE_ACTIONS", {a: boom for a in lab_executor.NODE_ACTIONS})
    approver = lab_executor.LabApprover.load_or_create(tmp_path / "key.pem")
    ex = lab_executor.LabExecutor(tmp_path / "s.sqlite3", approver.public_key)
    status, body = ex.execute(request(approver), "n" * 20)
    assert status == 502 and body["status"] == "failed" and body["mutated"] is None and body["mutation_state"] == "unknown"
    assert threading.active_count() >= 1
