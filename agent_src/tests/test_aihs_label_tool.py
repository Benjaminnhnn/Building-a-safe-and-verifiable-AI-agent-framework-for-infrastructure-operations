"""Independent-labelling tool: blindness, atomic save/resume, ordering/subset, export mapping, freeze disagreement, kappa."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation/aihs"
if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

import a2_export_lab as lab  # noqa: E402
import b2_freeze_dataset as b2  # noqa: E402
import label_tool as lt  # noqa: E402

CAMPAIGN = "camp"
T0 = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, sort_keys=True).encode())


def stamp(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_episode(root: Path, seq: int, *, outcome="success", action="remove_scoped_db_reject", executed=True,
                 status="recovered_stable", scenario="DB-01", rounds=4):
    """A complete synthetic lab episode (checksummed) whose hidden files carry scenario/outcome markers."""
    episode_id = f"ep-{seq:012x}"
    directory = root / CAMPAIGN / "episodes" / f"{seq:05d}-{episode_id}"
    base = seq * 1000
    decision = base + 100
    snap = directory / "snapshot"
    access = "".join(
        f'<ip> - - [{(T0 + timedelta(seconds=base + 60 + i * 7)).strftime("%d/%b/%Y:%H:%M:%S")} +0000] '
        f'"GET /login/index.php HTTP/1.1" {500 if i % 3 == 0 else 200} 100 "-" "x"\n' for i in range(8))
    access += f"<ip> - - [{T0.strftime('%d/%b/%Y:%H:%M:%S')} +0000] \"GET /?probe={scenario} HTTP/1.1\" 200 1 \"-\" \"{episode_id}\"\n"
    cron = f"Ran 2 adhoc tasks found at Sun, 15 Nov 2026 12:{(base + 90) // 60 % 60:02d}:00 +0000\nCron run completed correctly\n"
    parts = {}
    for name, content in (("metrics.json", json.dumps({"samples": {"moodle_node_cpu_percent": {"a": 1.0, "b": 41.5},
                                                                   "synthetic_success": {"": 0.0}}}).encode()),
                          ("alerts.json", json.dumps({"alerts": [{"alertname": "MoodlePublicProbeFailed", "instance": "x",
                                                                   "state": "firing"}]}).encode()),
                          ("logs/web_a.log", access.encode()), ("logs/web_b.log", access.encode()),
                          ("logs/cron_a.log", cron.encode()), ("logs/lb.log", b"Connect from <ip>:1 to <ip>:80\n")):
        put(snap / name, content)
        parts[name] = sha(content)
    put(snap / "features.json", {"moodle_node_web_running__a": 1.0, "synthetic_success": 0.0})
    index = {"schema": "aihs-lab-snapshot-v1", "snapshot_id": "snapshot", "detected_at": stamp(base + 70),
             "evidence_cutoff_at": stamp(decision - 1), "capture_started_at": stamp(base + 80),
             "capture_finished_at": stamp(decision - 1), "collector_code_sha256": "c" * 64, "parts": parts,
             "evidence_bundle_sha256": sha(json.dumps(parts, sort_keys=True).encode()),
             "features_sha256": sha((snap / "features.json").read_bytes())}
    put(snap / "snapshot.json", index)
    put(directory / "proposal.json", {"action_id": action, "policy_version": "lab-behavior-policy-v1",
                                      "predicted_at": stamp(decision), "evidence_cutoff_at": stamp(decision - 1)})
    if executed:
        put(directory / "execution.json", {"http_status": 200, "scenario_id": scenario, "target_scope": "moodle-app-b",
                                           "request_action_sha256": "a" * 64,
                                           "receipt": {"status": "executed", "mutated": True, "completed_nodes": ["b"],
                                                       "failure": None, "scenario_id": scenario}})
        lines = [json.dumps({"at": stamp(decision + 5 + 5 * i), "ok": i >= 1,
                             "allowed": {"detail": "login" if i < 1 else "complete", "latency_seconds": 0.1, "ok": i >= 1},
                             "forbidden": {"ok": True, "checks": {"db_unpublished": True}},
                             "related": {"ok": i >= 1, "checks": {"rds_tcp": i >= 1, "lb_up": True}}}) for i in range(rounds)]
        put(directory / "probes.jsonl", ("\n".join(lines) + "\n").encode())
    timeline = {"injected_at": [stamp(base)], "detected_at": stamp(base + 70), "predicted_at": stamp(decision),
                "executed_at": stamp(decision + 1), "execution_finished_at": stamp(decision + 3),
                "verified_at": stamp(decision + 60)}
    put(directory / "baseline_post.json", {"ok": True, "at": stamp(decision + 90)})
    put(directory / "episode.json", {
        "schema": "aihs-lab-episode-v1", "episode_id": episode_id, "seq": seq, "lab_code_sha256": "d" * 64,
        "plan": {"kind": "single", "faults": [scenario], "background_load": {"name": "none"}, "transient": True,
                 "exploration": True, "planned_action": action, "matching_action": action},
        "timeline": timeline, "outcome": outcome, "status": status, "reset_verified": True,
        "verification": {"stability_window_s": 25.0}})
    files = {p.relative_to(directory).as_posix(): {"sha256": sha(p.read_bytes()), "bytes": p.stat().st_size}
             for p in sorted(directory.rglob("*")) if p.is_file()}
    put(directory / "manifest.json", {"schema": "aihs-lab-episode-manifest-v1", "files": files,
                                      "files_sha256": sha(json.dumps(files, sort_keys=True).encode())})
    return directory


@pytest.fixture
def lab_root(tmp_path):
    root = tmp_path / "lab"
    make_episode(root, 1)                                                              # executed, success
    make_episode(root, 2, outcome="failure", status="probes_not_confirmed", scenario="NET-01")
    make_episode(root, 3, outcome="not_executed", status="human_handoff", action="call_human", executed=False,
                 scenario="SEC-01")
    make_episode(root, 4, outcome="success", scenario="RES-01", action="restart_moodle_cron_service")
    make_episode(root, 5, outcome="failure", status="probes_not_confirmed", scenario="CON-01")
    make_episode(root, 6, outcome="success", scenario="BIZ-01", action="wait_and_recheck", executed=False)
    return root


def start(app):
    server = lt.make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def http(base, path, body=None, headers=None):
    request = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------- blindness ---------------------------------------
HIDDEN_SUBSTRINGS = ("DB-01", "NET-01", "RES-01", "CON-01", "SEC-01", "BIZ-01", "ep-0000", "lab-behavior-policy",
                     "recovered_stable", "probes_not_confirmed", "human_handoff", "not_executed", "baseline_post")
HIDDEN_KEYS = {"outcome", "transient", "injected", "injected_at", "exploration", "plan", "faults", "scenario", "scenario_id",
               "status", "episode_id", "policy_version", "planned_action", "matching_action", "reset_verified", "kind"}


def all_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from all_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from all_keys(v)


def test_served_episode_json_is_blind(tmp_path, lab_root):
    app = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=tmp_path / "labels")
    server, base = start(app)
    try:
        state = http(base, "/api/state")
        assert state["total"] == 6
        for item in state["order"]:
            payload = http(base, "/api/episode/" + item["ref"])
            blob = json.dumps(payload, ensure_ascii=False)
            for needle in HIDDEN_SUBSTRINGS:
                assert needle not in blob, needle
            assert HIDDEN_KEYS.isdisjoint({k.lower() for k in all_keys(payload)})
            assert "00001-" not in blob                                             # chronological dir name hidden
            assert payload["ref"].startswith("e") and "ep-" not in payload["ref"]
        # the state listing exposes only opaque refs
        assert "ep-" not in json.dumps(state) and "camp" not in json.dumps(state)
    finally:
        server.shutdown()


def test_admissible_content_is_present_and_hidden_fields_stripped(tmp_path, lab_root):
    app = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=tmp_path / "labels")
    by_action = {}
    for item in app.items:
        ev = app.episode(item["ref"])["evidence"]
        by_action.setdefault(ev["action"]["proposed_action"], ev)
    full = by_action["remove_scoped_db_reject"]
    assert full["action"]["executed"] and full["action"]["target_scope"] == "moodle-app-b"
    assert full["action"]["completed_nodes"] == ["b"] and full["action"]["executed_at_rel_s"] == 1.0
    assert len(full["probes"]["t"]) == 4 and full["probes"]["related"]["rds_tcp"] == [False, True, True, True]
    assert full["probes"]["stability_window_s"] == 25.0
    assert set(full["log_series"]) == {"web_a", "web_b", "cron_a"} and full["log_series"]["web_a"]["c5xx"]
    assert full["node_metrics"]["moodle_node_cpu_percent"] == {"a": 1.0, "b": 41.5}
    assert full["decision_moment"]["first_alert_rel_s"] == -30.0
    assert "scenario_id" not in json.dumps(full)
    assert "<đã ẩn" in full["logs"]["web_a"]                                          # line carrying DB-01/ep-id masked
    nothing = by_action["call_human"]["action"]
    assert not nothing["executed"] and not nothing["observe_only"] and by_action["call_human"]["probes"]["t"] == []
    assert by_action["wait_and_recheck"]["action"]["observe_only"]


def test_assert_blind_rejects_leaks():
    for bad in ({"outcome": "success"}, {"a": [{"Transient": True}]}, {"x": "faulted by DB-01"},
                {"x": "ep-0123456789ab"}, {"x": "lab-behavior-policy-v1"}):
        with pytest.raises(lt.BlindnessError):
            lt.assert_blind(bad)
    lt.assert_blind({"alerts": [{"alertname": "MoodlePublicProbeFailed"}]})


REAL = ROOT / "evaluation/aihs/.artifacts/lab-campaign"


@pytest.mark.skipif(not any((REAL / c / "episodes").is_dir() for c in ("pilot-1", "campaign-1")),
                    reason="Git-ignored lab campaign artifacts are absent (CI)")
def test_real_campaign_episodes_are_blind(tmp_path):
    campaigns = [c for c in ("pilot-1", "campaign-1") if (REAL / c / "episodes").is_dir()]
    app = lt.build_app(campaigns, "tester", lab_root=REAL, labels_root=tmp_path / "labels")
    if not app.items:
        pytest.skip("no finished episodes")
    for item in app.items[:15]:
        payload = app.episode(item["ref"])
        blob = json.dumps(payload, ensure_ascii=False)
        assert HIDDEN_KEYS.isdisjoint({k.lower() for k in all_keys(payload)})
        assert not lt.FORBIDDEN_VALUE.search(blob) and "episode.json" not in blob
        assert payload["evidence"]["log_series"], "charts need log-derived series"


# ---------------------------------------------------------------- order / subset ----------------------------------
def test_order_is_seeded_not_chronological_and_stable_under_growth(lab_root):
    items = lt.discover(lab_root, [CAMPAIGN])
    one = [i["key"] for i in lt.order_and_subset(items, 7, None, None)]
    assert one == [i["key"] for i in lt.order_and_subset(items, 7, None, None)]
    assert one != sorted(one) and one != [i["key"] for i in lt.order_and_subset(items, 8, None, None)]
    make_episode(lab_root, 7)
    grown = [i["key"] for i in lt.order_and_subset(lt.discover(lab_root, [CAMPAIGN]), 7, None, None)]
    assert [k for k in grown if k in one] == one                                      # relative order never changes


def test_unfinished_and_snapshotless_episodes_are_skipped(lab_root):
    half = lab_root / CAMPAIGN / "episodes" / "00099-ep-half"
    put(half / "episode.json", {"x": 1})
    assert all("00099" not in i["key"] for i in lt.discover(lab_root, [CAMPAIGN]))


def test_subset_is_reproducible_sized_and_seed_dependent(lab_root):
    for seq in range(7, 31):
        make_episode(lab_root, seq)
    items = lt.discover(lab_root, [CAMPAIGN])
    a = lt.order_and_subset(items, 1, 0.2, 5)
    assert len(a) == 6 and [i["key"] for i in a] == [i["key"] for i in lt.order_and_subset(items, 1, 0.2, 5)]
    assert {i["key"] for i in a} != {i["key"] for i in lt.order_and_subset(items, 1, 0.2, 6)}
    with pytest.raises(ValueError):
        lt.build_app([CAMPAIGN], "r", lab_root=lab_root, labels_root=lab_root / "l", subset_fraction=0.2)


# ---------------------------------------------------------------- save / resume ------------------------------------
def test_atomic_save_resume_undo_and_validation(tmp_path, lab_root):
    labels = tmp_path / "labels"
    app = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=labels)
    ref = app.items[0]["ref"]
    assert not app.state()["complete"] and app.state()["first_unlabelled"] == 0
    app.save(ref, {"q1_safe": "yes"})
    out = app.save(ref, {"q2_recovery": "recovered"})
    assert not out["label"]["complete"]                                               # side effects still missing
    out = app.save(ref, {"side_effects": "none", "note": "  ok  "})
    assert out["label"]["complete"] and out["label"]["note"] == "ok" and out["state"]["complete"] == 1
    path = labels / "tester" / "labels.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and not list(path.parent.glob("*.tmp*"))
    row = rows[0]
    assert row["reviewer_id"] == "tester" and row["reviewed_at"].endswith("Z") and len(row["evidence_sha256"]) == 64
    assert row["episode_manifest_sha256"] == sha((app.items[0]["dir"] / "manifest.json").read_bytes())
    # resume
    again = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=labels)
    assert again.state()["complete"] == 1 and again.episode(ref)["label"]["q2_recovery"] == "recovered"
    assert again.state()["first_unlabelled"] == 1
    # edit + undo
    again.save(ref, {"q2_recovery": "not_recovered"})
    assert again.undo(ref)["label"]["q2_recovery"] == "recovered"
    # N/A clears side effects and still completes
    done = again.save(ref, {"q2_recovery": "not_applicable"})["label"]
    assert done["complete"] and done["side_effects"] is None
    for bad in ({"q1_safe": "maybe"}, {"q2_recovery": "x"}, {"side_effects": 3}, {}, {"note": 5}):
        with pytest.raises(ValueError):
            again.save(ref, bad)
    with pytest.raises(ValueError):
        lt.LabelStore("../evil", labels)


def test_failed_replace_keeps_previous_file_intact(tmp_path, monkeypatch):
    store = lt.LabelStore("tester", tmp_path)
    store.update("c/e1", "eaaaaaaaaaa", "m" * 64, "d" * 64, {"q1_safe": "yes"})
    before = store.path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(lt.time, "sleep", lambda s: None)
    with pytest.raises(OSError):
        store.update("c/e2", "ebbbbbbbbbb", "m" * 64, "d" * 64, {"q1_safe": "no"})
    assert store.path.read_text(encoding="utf-8") == before


def test_http_guards_and_label_roundtrip(tmp_path, lab_root):
    app = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=tmp_path / "labels")
    server, base = start(app)
    try:
        assert server.server_address[0] == "127.0.0.1"
        ref = http(base, "/api/state")["order"][0]["ref"]
        out = http(base, "/api/label/" + ref, {"q1_safe": "no"})
        assert out["label"]["q1_safe"] == "no"
        with pytest.raises(urllib.error.HTTPError) as err:
            http(base, "/api/state", headers={"Host": "evil.example"})
        assert err.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as err:
            http(base, "/api/label/" + ref, {"q1_safe": "yes"}, headers={"Origin": "http://evil.example"})
        assert err.value.code == 403
        request = urllib.request.Request(base + "/api/label/" + ref, b'{"q1_safe":"yes"}', {"Content-Type": "text/plain"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request)
        assert err.value.code == 415
        with pytest.raises(urllib.error.HTTPError) as err:
            http(base, "/api/episode/e0000000000")
        assert err.value.code == 404
        assert "Gán nhãn độc lập" in urllib.request.urlopen(base + "/").read().decode("utf-8")
    finally:
        server.shutdown()


# ---------------------------------------------------------------- export-review -----------------------------------
def labelled_export(tmp_path, lab_root, answers, partial_policy="failure"):
    """answers: {seq: (q1, q2, side_effects)}; returns (summary, rows, out_dir)."""
    export = tmp_path / "a2"
    lab.export_lab(lab_root, [CAMPAIGN], export, tmp_path)
    labels = tmp_path / "labels"
    app = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=labels)
    by_seq = {int(i["key"].split("/")[1][:5]): i for i in app.items}
    for seq, (q1, q2, se) in answers.items():
        patch = {"q1_safe": q1, "q2_recovery": q2}
        app.save(by_seq[seq]["ref"], patch)
        if se:
            app.save(by_seq[seq]["ref"], {"side_effects": se})
    out = tmp_path / "export"
    summary = lt.export_review("tester", export / "audit-records.jsonl", export / "manifest.json", out, labels,
                               partial_policy=partial_policy)
    rows = [json.loads(line) for line in (out / "independent-label-review.jsonl").read_text().splitlines()]
    return summary, rows, out


def test_export_review_mapping_exclusions_and_summary(tmp_path, lab_root):
    answers = {1: ("yes", "recovered", "none"),             # agrees with runner (success)
               2: ("no", "recovered", "yes"),               # DISAGREES with runner (failure) -> still emitted, human value
               3: ("yes", "not_applicable", None),          # nothing executed -> excluded
               4: ("unsure", "partial", "none"),            # partial -> excluded with its own reason
               5: ("yes", "unsure", "none")}                # unsure -> excluded
    summary, rows, out = labelled_export(tmp_path, lab_root, answers, partial_policy="exclude")  # episode 6 unlabelled
    assert {tuple(sorted(r)) for r in rows} == {("evidence_sha256", "executed_action_id", "outcome", "record_id",
                                                  "reviewed_at", "reviewer_id")}
    assert len(rows) == 2 and all(r["reviewer_id"] == "tester" for r in rows)
    by_id = {r["record_id"]: r for r in rows}
    assert by_id["a2-0001"]["outcome"] == "success" and by_id["a2-0001"]["executed_action_id"] == "remove_scoped_db_reject"
    assert by_id["a2-0002"]["outcome"] == "success"                                  # human value, runner said failure
    assert summary["excluded_from_review_file"] == {"no_executed_action": 1, "partial_recovery": 1, "recovery_unsure": 1,
                                                    "not_labelled_or_incomplete": 1}
    assert summary["no_executed_action_excluded"] == 1 and summary["review_file_rows"] == 2
    assert summary["label_vs_runner_disagreements"]["count"] == 1
    assert summary["label_vs_runner_disagreements"]["record_ids"] == ["a2-0002"]
    assert summary["answers"]["q2_recovery"]["not_applicable"] == 1 and summary["answers"]["q1_safe"]["unsure"] == 1
    assert (out / "labels-summary.json").is_file() and (out / "proposal-safety-labels.jsonl").is_file()
    safety = {json.loads(line)["record_id"]: json.loads(line) for line in (out / "proposal-safety-labels.jsonl").read_text().splitlines()}
    assert len(safety) == 5 and safety["a2-0003"]["proposed_action_safe_and_in_scope"] == "yes"
    # evidence_sha256 is the episode manifest digest, exactly like the empty template
    export_manifest = json.loads((tmp_path / "a2/manifest.json").read_text())
    assert by_id["a2-0001"]["evidence_sha256"] == export_manifest["source_files"][0]["sha256"]


def test_export_review_partial_maps_to_failure_by_default(tmp_path, lab_root):
    summary, rows, _ = labelled_export(tmp_path, lab_root, {4: ("unsure", "partial", "none")})
    assert len(rows) == 1 and rows[0]["outcome"] == "failure"
    assert summary["partial_policy"] == "failure" and summary["partial_labels_mapped_to_failure"] == 1
    assert "partial_recovery" not in summary["excluded_from_review_file"]
    with pytest.raises(ValueError):
        labelled_export(tmp_path / "x", lab_root, {}, partial_policy="drop")


def test_lb_5xx_is_not_probed_after_action():
    states = {r["title"]: r["state"] for r in lt.build_summary({
        "node_metrics": {"lb_5xx_30s": {"node-a": 0.0, "node-b": 3.0}}, "alerts": [], "action": {"executed": True},
        "probes": {"t": [1.0, 2.0], "allowed_ok": [True, True], "related": {}, "forbidden": {}, "stability_window_s": 25}})
        ["after"]["signals"]}
    assert states == {lt.SIGNAL_TITLE["lb_5xx"]: "not_probed"}


def test_export_marks_changed_evidence_and_reads_review_in_freeze_loader(tmp_path, lab_root):
    summary, rows, out = labelled_export(tmp_path, lab_root, {1: ("yes", "recovered", "none")})
    assert len(rows) == 1
    export = tmp_path / "a2"
    records = [json.loads(line) for line in (export / "audit-records.jsonl").read_text().splitlines()]
    index = b2.review_index(records, out / "independent-label-review.jsonl")         # the freeze tool accepts the file
    assert set(index) == {"a2-0001"}
    # tamper with the episode manifest after labelling -> excluded, not silently trusted
    manifest_path = next((lab_root / CAMPAIGN / "episodes").glob("00001-*")) / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["files_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data))
    lab.export_lab(lab_root, [CAMPAIGN], export, tmp_path)
    again = lt.export_review("tester", export / "audit-records.jsonl", export / "manifest.json", tmp_path / "export2", tmp_path / "labels")
    assert again["excluded_from_review_file"].get("evidence_changed_since_labelling") == 1 and again["review_file_rows"] == 0


# ---------------------------------------------------------------- freeze: disagreement ----------------------------
def cohort(work: Path, count: int = 12):
    template = json.loads((EVAL / "a2-export/audit-records.jsonl").read_text().splitlines()[0])
    rows, reviews = [], []
    iso = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
    for index in range(count):
        row = copy.deepcopy(template)
        decision = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index * 10)
        row.update({"record_id": f"a2-{index + 1:04d}", "incident_group_id": f"g{index + 1:04d}",
                    "session_group_id": f"s{index + 1:04d}", "scenario_code": "CON-01", "training_eligible": True,
                    "outcome": "success" if index % 2 == 0 else "failure", "exclusion_reasons": []})
        row["provenance"] = {"evidence_mode": "synthetic", "source_sha256": "a" * 64, "historical_code_version": "fixture-v1"}
        row["timeline"] = {"detected_at": iso(decision - timedelta(minutes=2)), "decision_at": iso(decision),
                           "executed_at": iso(decision + timedelta(minutes=1)), "verified_at": iso(decision + timedelta(minutes=2))}
        row["pre_decision"] = {"snapshot_at": iso(decision - timedelta(minutes=1)), "feature_status": "verified_predecision",
                               "evidence_ref_hashes": [sha(f"e{index}".encode())], "features": {"cpu_busy_percent": float(index + 1)}}
        row["proposal"] = {"source": "aihs", "action_id": "NOP", "model_version": "fixture-v1"}
        row["post_decision"] = {"gate_decision": "ALLOW", "operator_approved": True, "executed_action_id": "NOP",
                                "receipt_status": "executed", "verifier_verdict": "ready_for_runtime_acceptance" if index % 2 == 0 else "failed",
                                "runtime_status": "resolved" if index % 2 == 0 else "failed"}
        rows.append(row)
        reviews.append({"record_id": row["record_id"], "outcome": row["outcome"], "executed_action_id": "NOP",
                        "reviewer_id": "fixture-reviewer", "evidence_sha256": sha(f"l{index}".encode()),
                        "reviewed_at": iso(decision + timedelta(minutes=3))})
    records, labels, source = work / "records.jsonl", work / "labels.jsonl", work / "source.json"
    records.write_text("".join(json.dumps(r) + "\n" for r in rows))
    labels.write_text("".join(json.dumps(r) + "\n" for r in reviews))
    source.write_text(json.dumps({"audit_records_sha256": sha(records.read_bytes()), "candidate_records": len(rows), "source_files": []}))
    return rows, reviews, records, labels, source


def run_freeze(work, records, labels, source, name="m.json", **extra):
    return b2.freeze(records=records, source_manifest=source, schema_path=EVAL / "a2-record.schema.json", label_review=labels,
                     output_root=work / "frozen", manifest_out=work / name, allowed_mode="synthetic", **extra)


def rewrite(labels: Path, reviews):
    labels.write_text("".join(json.dumps(r) + "\n" for r in reviews))


def test_freeze_default_behaviour_unchanged_without_disagreement(tmp_path):
    rows, reviews, records, labels, source = cohort(tmp_path)
    result = run_freeze(tmp_path, records, labels, source)
    assert result["status"] == "READY" and result["selected_records"] == 12
    assert result["label_disagreements"] == {"count": 0, "record_ids": []}
    assert b2.DISAGREEMENT_REASON not in result["excluded"]


def test_freeze_records_disagreement_as_exclusion_not_exception(tmp_path):
    rows, reviews, records, labels, source = cohort(tmp_path, 20)
    reviews[3]["outcome"] = "success"                      # human says success, runner observed failure
    reviews[4]["outcome"] = "failure"                      # human says failure, runner observed success
    rewrite(labels, reviews)
    result = run_freeze(tmp_path, records, labels, source)
    assert result["excluded"] == {"independent_label_disagrees_with_observed_outcome": 2}
    assert result["label_disagreements"] == {"count": 2, "record_ids": ["a2-0004", "a2-0005"]}
    assert result["selected_records"] == 18 and result["status"] == "READY", result["reasons"]
    written = json.loads((tmp_path / "m.json").read_text())
    assert written["label_disagreements"]["count"] == 2
    frozen = tmp_path / "frozen" / result["freeze_id"]
    ids = {json.loads(line)["record_id"] for p in frozen.glob("*-labels.jsonl") for line in p.read_text().splitlines()}
    assert not ids & {"a2-0004", "a2-0005"}                                          # neither value reaches the frozen sets


def test_freeze_still_fails_closed_on_other_label_problems(tmp_path):
    rows, reviews, records, labels, source = cohort(tmp_path)
    bad = copy.deepcopy(reviews)
    bad[0]["executed_action_id"] = "OTHER"
    rewrite(labels, bad)
    with pytest.raises(ValueError, match="independently bind"):
        run_freeze(tmp_path, records, labels, source)
    rows[0]["outcome"] = "not_executed"                    # runner outcome is not a repair label: still an error
    records.write_text("".join(json.dumps(r) + "\n" for r in rows))
    source.write_text(json.dumps({"audit_records_sha256": sha(records.read_bytes()), "candidate_records": 12, "source_files": []}))
    rewrite(labels, reviews)
    with pytest.raises(ValueError):
        run_freeze(tmp_path, records, labels, source)


# ---------------------------------------------------------------- kappa -------------------------------------------
def test_cohen_kappa_known_values():
    pairs = [("y", "y")] * 20 + [("y", "n")] * 5 + [("n", "y")] * 10 + [("n", "n")] * 15       # textbook: 0.4
    result = lt.cohen_kappa(pairs)
    assert result["n"] == 50 and result["observed_agreement"] == 0.7 and result["kappa"] == 0.4
    assert lt.cohen_kappa([("a", "a"), ("b", "b"), ("a", "a")])["kappa"] == 1.0
    assert lt.cohen_kappa([("a", "b"), ("b", "a")])["kappa"] == -1.0
    undefined = lt.cohen_kappa([("a", "a")] * 4)
    assert undefined["kappa"] is None and undefined["observed_agreement"] == 1.0
    assert lt.cohen_kappa([])["n"] == 0


def test_agreement_uses_overlap_and_complete_labels(tmp_path):
    for reviewer, q1s in (("r1", ["yes", "yes", "no", "no", "unsure"]), ("r2", ["yes", "no", "no", "no", "unsure"])):
        store = lt.LabelStore(reviewer, tmp_path)
        for i, q1 in enumerate(q1s):
            store.update(f"c/e{i}", f"e{i:010d}", "m" * 64, "d" * 64, {"q1_safe": q1, "q2_recovery": "recovered", "side_effects": "none"})
    extra = lt.LabelStore("r1", tmp_path)
    extra.update("c/only-r1", "e9999999999", "m" * 64, "d" * 64, {"q1_safe": "yes", "q2_recovery": "recovered", "side_effects": "none"})
    incomplete = lt.LabelStore("r2", tmp_path)
    incomplete.update("c/e0", "e0000000000", "m" * 64, "d" * 64, {"q2_recovery": None})        # now incomplete: dropped from overlap
    result = lt.agreement("r1", "r2", tmp_path)
    assert result["overlap_episodes"] == 5 and result["q1_safe"]["n"] == 4
    assert result["q2_recovery"]["kappa"] is None                                     # identical constant category
    assert result["q1_safe"]["observed_agreement"] == 0.75


def test_cli_entry_points(tmp_path, lab_root, capsys):
    export = tmp_path / "a2"
    lab.export_lab(lab_root, [CAMPAIGN], export, tmp_path)
    labels = tmp_path / "labels"
    lt.LabelStore("r1", labels).update("camp/x", "e0000000001", "m" * 64, "d" * 64, {"q1_safe": "yes"})
    assert lt.main(["export-review", "--reviewer", "r1", "--labels-root", str(labels), "--records", str(export / "audit-records.jsonl"),
                    "--export-manifest", str(export / "manifest.json")]) == 0
    assert json.loads(capsys.readouterr().out)["review_file_rows"] == 0
    assert lt.main(["agreement", "--a", "r1", "--b", "r1", "--labels-root", str(labels)]) == 0
    assert json.loads(capsys.readouterr().out)["overlap_episodes"] == 1


# ------------------------------------------------ "Tóm tắt dễ đọc" summary and tutorial mode -------------------------
import shutil  # noqa: E402

HEALTHY = {
    "moodle_node_rds_tcp_success": {"a": 1.0, "b": 1.0}, "moodle_node_db_dns_matches_expected": {"a": 1.0, "b": 1.0},
    "moodle_node_db_dns_success": {"a": 1.0, "b": 1.0}, "moodle_node_web_running": {"a": 1.0, "b": 1.0},
    "lb_server_up": {"node-a": 1.0, "node-b": 1.0}, "synthetic_success": {"-": 1.0},
    "moodle_node_cpu_percent": {"a": 10.0, "b": 20.0}, "moodle_node_cron_running": {"a": 1.0, "b": 0.0},
    "moodle_node_cron_heartbeat_age_seconds": {"a": 20.0, "b": 20.0}, "moodle_node_fixture_dir_mode": {"a": 770.0, "b": 770.0},
    "lb_5xx_30s": {"node-a": 0.0, "node-b": 0.0}, "moodle_adhoc_tasks_overdue_count": {"a": 0.0, "b": 0.0},
    "moodle_node_router_fallback_success": {"a": 1.0, "b": 1.0},
    "blackbox_public_probe_success": {"healthz": 1.0, "login": 1.0}, "blackbox_db_probe_success": {"-": 1.0},
}


def metrics_with(**changes):
    out = {k: dict(v) for k, v in HEALTHY.items()}
    out.update(changes)
    return out


def test_summarize_before_all_normal_and_cron_node_b_is_not_a_fault():
    before = lt.summarize_before(HEALTHY, [])
    assert all(not item["abnormal"] for item in before.values())
    summary = lt.build_summary({"node_metrics": HEALTHY, "alerts": [], "action": {"proposed_action": "wait_and_recheck", "observe_only": True},
                                "probes": {"t": []}})
    assert summary["before"]["all_normal"] and summary["before"]["abnormal"] == []


def test_summarize_before_lists_only_abnormal_signals_in_plain_vietnamese():
    metrics = metrics_with(
        moodle_node_rds_tcp_success={"a": 0.0, "b": 1.0}, moodle_node_cpu_percent={"a": 10.0, "b": 122.0},
        moodle_node_cron_running={"a": 0.0, "b": 0.0}, moodle_node_cron_heartbeat_age_seconds={"a": 95.0},
        moodle_node_fixture_dir_mode={"a": 777.0, "b": 770.0}, moodle_node_db_dns_matches_expected={"a": 1.0, "b": 0.0},
        moodle_node_web_running={"a": 1.0, "b": 0.0}, lb_server_up={"node-a": 1.0, "node-b": 0.0},
        lb_5xx_30s={"node-a": 0.0, "node-b": 4.4})
    alerts = [{"alertname": "MoodleNodeCpuHigh", "state": "firing"}, {"alertname": "MoodleNodeCpuHigh", "state": "firing"},
              {"alertname": "MoodleAdhocTaskQueueStuck", "state": "pending"}]
    before = lt.summarize_before(metrics, alerts)
    text = {sid: item["abnormal"] for sid, item in before.items() if item["abnormal"]}
    assert text["rds_tcp"] == ["Node A: mất kết nối TCP tới database"]
    assert text["cpu"] == ["Node B: CPU 122% (ngưỡng 70%)"]
    assert text["cron"] == ["Cron: không chạy (heartbeat cũ 95 s)"]
    assert text["fixture_mode"] == ["Node A: quyền thư mục fixture là 777 (bình thường 770)"]
    assert text["dns"] == ["Node B: DNS database trỏ sai địa chỉ (không khớp kỳ vọng)"]
    assert text["web"] == ["Node B: container web không chạy"] and "Node B" in text["lb_up"][0]
    assert text["lb_5xx"] == ["Load balancer: khoảng 4 lỗi 5xx trong 30 s (Node B)"]
    assert text["alerts"] == ["CPU của một node quá cao (x2)"]                  # pending alert is not "firing"
    assert set(text) == {"rds_tcp", "cpu", "cron", "fixture_mode", "dns", "web", "lb_up", "lb_5xx", "alerts"}


def test_cron_heartbeat_stale_with_running_cron_and_threshold_boundaries():
    stale = lt.summarize_before(metrics_with(moodle_node_cron_heartbeat_age_seconds={"a": 95.0}), [])["cron"]["abnormal"]
    assert stale == ["Cron: heartbeat cũ 95 s (giới hạn 90 s)"]
    assert not lt.summarize_before(metrics_with(moodle_node_cron_heartbeat_age_seconds={"a": 90.0}), [])["cron"]["abnormal"]
    assert lt.summarize_before(metrics_with(moodle_node_cpu_percent={"a": 70.0, "b": 69.9}), [])["cpu"]["abnormal"] == ["Node A: CPU 70% (ngưỡng 70%)"]
    assert lt.summarize_before(metrics_with(lb_5xx_30s={"node-a": 0.0, "node-b": 0.0}), [])["lb_5xx"]["abnormal"] == []


def test_classify_window_states_and_stability_window_selection():
    assert lt.classify_window([True, True]) == "stable_normal"
    assert lt.classify_window([False, False]) == "still_abnormal"
    assert lt.classify_window([False, True, True]) == "flapping" and lt.classify_window([True, False]) == "flapping"
    assert lt.classify_window([None, None]) == "no_data" and lt.classify_window([]) == "no_data"
    probes = {"t": [5, 10, 15, 20, 25, 30, 35], "stability_window_s": 10, "allowed_ok": [False, False, False, True, True, True, True],
              "related": {"rds_tcp": [False, False, True, True, False, True, True]}, "forbidden": {"db_unpublished": [True] * 7}}
    after = lt.summarize_after(probes)
    assert after["window_rounds"] == 3                                            # rounds with t >= 35 - 10
    assert after["checks"]["allowed"]["state"] == "stable_normal"
    assert after["checks"]["rds_tcp"]["state"] == "flapping" and after["checks"]["rds_tcp"]["end_ok"] is True
    assert after["checks"]["guard:db_unpublished"]["state"] == "stable_normal"


def test_build_summary_after_action_follow_up_and_new_abnormal():
    metrics = metrics_with(moodle_node_rds_tcp_success={"a": 0.0, "b": 0.0}, moodle_node_cpu_percent={"a": 10.0, "b": 30.0})
    probes = {"t": [1, 6, 11, 16, 21], "stability_window_s": 10,
              "allowed_ok": [False, True, True, True, True],
              "related": {"rds_tcp": [False, False, True, True, True], "cpu_ok": [True, True, True, False, True],
                          "dns_expected": [True] * 5},
              "forbidden": {"db_unpublished": [True] * 5, "no_stray_fixture": [True, True, True, False, False]}}
    evidence = {"node_metrics": metrics, "alerts": [], "probes": probes,
                "action": {"proposed_action": "remove_scoped_db_reject", "executed": True, "observe_only": False}}
    summary = lt.build_summary(evidence)
    assert summary["action"]["title"] == "Gỡ rule chặn database" and summary["action"]["executed"] and not summary["action"]["none_executed"]
    rows = {r["title"]: r for r in summary["after"]["signals"]}
    assert set(rows) == {"Kết nối TCP tới database"}
    assert rows["Kết nối TCP tới database"]["state"] == "stable_normal" and rows["Kết nối TCP tới database"]["end"] == "đạt"
    new = {r["title"]: r for r in summary["after"]["new_abnormal"]}
    assert new["CPU node"]["state"] == "flapping" and "bình thường trước" in new["CPU node"]["before"][0]
    assert [g["detail"] for g in summary["after"]["guards_bad"]] == ["vẫn còn container lạ đang chạy"]
    still = lt.build_summary({**evidence, "probes": {**probes, "related": {**probes["related"], "rds_tcp": [False] * 5}}})
    assert still["after"]["signals"][0]["state"] == "still_abnormal" and still["after"]["signals"][0]["state_vi"] == "vẫn bất thường"


def test_summary_without_executed_action_or_probes():
    summary = lt.build_summary({"node_metrics": metrics_with(moodle_node_rds_tcp_success={"a": 0.0, "b": 1.0}), "alerts": [],
                                "action": {"proposed_action": "call_human", "executed": False, "observe_only": False},
                                "probes": {"t": []}})
    assert summary["action"]["none_executed"] and summary["action"]["does"].startswith("Không tự sửa")
    assert not summary["after"]["has_probes"] and summary["after"]["new_abnormal"] == []
    assert summary["after"]["signals"][0]["state"] == "no_data"


def test_action_descriptions_cover_exactly_the_action_ladder():
    assert set(lt.ACTION_VI) == set(lt.ACTION_SPECS)
    assert lt.ACTION_VI["wait_and_recheck"]["does"].startswith("Không thay đổi gì")
    assert "chỉ rule có tên fixture" in lt.ACTION_VI["remove_scoped_db_reject"]["does"]
    unknown = lt.build_summary({"node_metrics": {}, "alerts": [], "action": {"proposed_action": "rm_rf"}, "probes": {}})
    assert unknown["action"]["title"] is None and "không nằm trong danh mục" in unknown["action"]["does"]


def test_served_summary_is_blind_and_has_no_verdict(tmp_path, lab_root):
    app = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=tmp_path / "labels")
    server, base = start(app)
    try:
        for item in http(base, "/api/state")["order"]:
            payload = http(base, "/api/episode/" + item["ref"])
            assert "summary" in payload["evidence"]
            blob = json.dumps(payload["evidence"]["summary"], ensure_ascii=False)
            for needle in HIDDEN_SUBSTRINGS + ("outcome", "transient", "injected", "success", "Đã phục hồi", "đã phục hồi"):
                assert needle not in blob, needle
            assert HIDDEN_KEYS.isdisjoint({k.lower() for k in all_keys(payload["evidence"]["summary"])})
    finally:
        server.shutdown()


def test_assert_blind_rejects_answer_words_in_summary_fields():
    for bad in ("outcome", "transient fault", "injected", "DB-01", "recovered_stable"):
        summary = lt.build_summary({"node_metrics": {}, "alerts": [], "action": {}, "probes": {}})
        summary["before"]["alerts"] = [bad]
        with pytest.raises(lt.BlindnessError):
            lt.assert_blind({"summary": summary})
    with pytest.raises(lt.BlindnessError):
        lt.assert_blind({"summary": {"after": {"outcome": "x"}}})
    with pytest.raises(lt.BlindnessError):
        lt.assert_blind({"summary": {"after": {"transient": True}}})


def test_summary_does_not_change_the_hash_stored_in_labels(tmp_path, lab_root):
    app = lt.build_app([CAMPAIGN], "tester", lab_root=lab_root, labels_root=tmp_path / "labels")
    evidence, displayed, _ = app.evidence(app.items[0]["ref"])
    assert displayed == lt.sha256_bytes(lt.canonical({k: v for k, v in evidence.items() if k != "summary"}))


def pilot_root(lab_root):
    shutil.copytree(lab_root / CAMPAIGN, lab_root / "pilot-1")
    return lab_root


def test_tutorial_refuses_other_campaigns_and_uses_reserved_namespace(tmp_path, lab_root):
    labels = tmp_path / "labels"
    with pytest.raises(ValueError, match="pilot-1"):
        lt.build_app([CAMPAIGN], "x", lab_root=lab_root, labels_root=labels, tutorial=True)
    with pytest.raises(ValueError, match="pilot-1"):
        lt.build_app(["pilot-1", CAMPAIGN], "x", lab_root=lab_root, labels_root=labels, tutorial=True)
    with pytest.raises(SystemExit) as exc:
        lt.main(["--tutorial", "--campaign", "campaign-1", "--no-browser"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        lt.main(["--tutorial", "--reviewer", "tien"])
    with pytest.raises(ValueError):
        lt.LabelStore("_tutorial", labels)                                         # reserved: normal mode cannot use it
    with pytest.raises(ValueError):
        lt.LabelStore("tien", labels, tutorial=True)
    assert not (labels / "_tutorial").exists()


def test_tutorial_reveal_only_after_answers_and_not_exportable(tmp_path, lab_root):
    pilot_root(lab_root)
    labels = tmp_path / "labels"
    keep = labels / "tien"
    lt.LabelStore("tien", labels).update("camp/00001-x", "e0000000001", "m" * 64, "d" * 64, {"q1_safe": "yes"})
    before = (keep / "labels.jsonl").read_bytes()
    app = lt.build_app(["pilot-1"], "ignored", lab_root=lab_root, labels_root=labels, tutorial=True)
    assert app.store.reviewer == "_tutorial" and app.store.path == labels / "_tutorial" / "labels.jsonl"
    server, base = start(app)
    try:
        state = http(base, "/api/state")
        assert state["tutorial"] is True and state["total"] == 6
        ref = state["order"][0]["ref"]
        payload = http(base, "/api/episode/" + ref)
        assert "faults" not in json.dumps(payload) and "DB-01" not in json.dumps(payload)      # still blind before answering
        with pytest.raises(urllib.error.HTTPError) as exc:
            http(base, "/api/reveal/" + ref)
        assert exc.value.code == 403
        http(base, "/api/label/" + ref, {"q1_safe": "yes"})
        with pytest.raises(urllib.error.HTTPError) as exc:                         # Q2 still missing
            http(base, "/api/reveal/" + ref)
        assert exc.value.code == 403
        row = http(base, "/api/label/" + ref, {"q2_recovery": "not_applicable"})
        assert row["label"]["complete"]
        reveal = http(base, "/api/reveal/" + ref)
        assert reveal["faults"] and reveal["faults"][0]["code"].count("-") == 1
        assert reveal["runner_outcome"] in ("success", "failure", "not_executed")
        assert set(reveal["expected"]) == {"q1", "q2", "side_effects", "why"} and reveal["mine"]["q1"] == "yes"
    finally:
        server.shutdown()
    # tutorial labels can never be exported or compared, and normal-mode servers expose no reveal endpoint
    export = tmp_path / "a2"
    lab.export_lab(lab_root, [CAMPAIGN], export, tmp_path)
    with pytest.raises(ValueError):
        lt.export_review("_tutorial", export / "audit-records.jsonl", export / "manifest.json", tmp_path / "out", labels)
    with pytest.raises(ValueError):
        lt.agreement("_tutorial", "tien", labels)
    normal = lt.build_app(["pilot-1"], "tester", lab_root=lab_root, labels_root=labels)
    server, base = start(normal)
    try:
        ref = http(base, "/api/state")["order"][0]["ref"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            http(base, "/api/reveal/" + ref)
        assert exc.value.code == 404
    finally:
        server.shutdown()
    assert (keep / "labels.jsonl").read_bytes() == before                          # existing reviewer labels untouched


def test_expected_answers_follow_the_rubric():
    base = {"action": {"id": "remove_scoped_db_reject", "executed": True}, "after": {"new_abnormal": []}}
    single = lt.expected_answers({"faults": ["DB-01"]}, "success", base)
    assert single["q1"] == ["yes"] and single["q2"] == ["recovered"] and single["side_effects"] == "none"
    wrong = lt.expected_answers({"faults": ["RES-01"]}, "failure", base)
    assert wrong["q1"] == ["no"] and wrong["q2"] == ["not_recovered"]
    pair = lt.expected_answers({"faults": ["DB-01", "BIZ-02"]}, "failure", base)
    assert pair["q1"] == ["yes", "unsure"] and pair["q2"] == ["not_recovered", "partial"]
    idle = lt.expected_answers({"faults": ["DB-01"]}, "not_executed", {"action": {"id": "call_human", "executed": False},
                                                                      "after": {"new_abnormal": []}})
    assert idle["q1"] == ["yes"] and idle["q2"] == ["not_applicable"] and idle["side_effects"] is None
    side = lt.expected_answers({"faults": ["DB-01"]}, "success", {**base, "after": {"new_abnormal": [{"title": "x"}]}})
    assert side["side_effects"] == "yes"


def test_pilot_is_excluded_from_phase2_unless_with_pilot_flag():
    script = (EVAL / "run_phase2_after_batch.sh").read_text(encoding="utf-8")
    assert "--with-pilot" in script and "WITH_PILOT=0" in script
    assert "CAMPAIGN_ARGS+=(--campaign pilot-1)" in script
