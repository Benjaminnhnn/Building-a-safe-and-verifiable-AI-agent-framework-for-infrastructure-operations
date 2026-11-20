"""Local, blind, independent-labelling tool for the AIHS Moodle lab campaign (stdlib only).

    python evaluation/aihs/label_tool.py --campaign campaign-1 --reviewer <id> [--subset-fraction 0.2 --subset-seed N]
    python evaluation/aihs/label_tool.py export-review --reviewer <id>
    python evaluation/aihs/label_tool.py agreement --a <id1> --b <id2>

The server binds to 127.0.0.1 only and serves ONE episode per screen from an explicit whitelist of admissible files:
the pre-decision snapshot (alerts, metrics, redacted logs), the executed action and its receipt, and the post-action probe
rounds. ``episode.json`` (scenario, injected faults, plan, runner outcome, status), the behaviour-policy fields and any
Gate/model prediction are never read into the served evidence, and the served JSON is re-scanned for answer-bearing keys
and values before it leaves the process (``assert_blind``). Labels are written atomically (temp file + fsync + replace)
to ``.artifacts/labels/<reviewer>/labels.jsonl`` (Git-ignored) after every answer and are resumed on restart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from a2_export_lab import FORBIDDEN_TEXT, redact, verify_episode_manifest  # noqa: E402
from action_ladder import ACTION_SPECS, MATCHING_ACTION  # noqa: E402

ROOT = HERE.parents[1]
ARTIFACTS = HERE / ".artifacts"
LAB_ROOT = ARTIFACTS / "lab-campaign"
LABELS_ROOT = ARTIFACTS / "labels"
DEFAULT_EXPORT = ARTIFACTS / "new-a2-export"
DEFAULT_ORDER_SEED = 20261115
OBSERVE_ONLY_ACTION = "wait_and_recheck"

Q1_VALUES = ("yes", "no", "unsure")
Q2_VALUES = ("recovered", "not_recovered", "partial", "unsure", "not_applicable")
SIDE_EFFECT_VALUES = ("none", "yes")
RUNNER_OUTCOME = {"recovered": "success", "not_recovered": "failure"}
REVIEWER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
# Tutorial mode: pilot-1 only (the pilot campaign is excluded from training/evaluation unless run_phase2_after_batch.sh is
# given --with-pilot), labels in a reserved namespace that REVIEWER_ID cannot express, so export-review/agreement refuse it.
TUTORIAL_CAMPAIGN = "pilot-1"
TUTORIAL_REVIEWER = "_tutorial"
MAX_NOTE = 500
MAX_LOG_LINES = 400

# Keys and values that would reveal the answer; checked against every served evidence payload.
FORBIDDEN_KEYS = {
    "scenario", "scenario_id", "scenario_code", "outcome", "transient", "injected", "injected_at", "exploration", "plan",
    "faults", "fault", "policy", "policy_version", "verdict", "reasons", "matching_action", "planned_action", "kind",
    "status", "episode", "episode_id", "heal_after_s", "reset_verified", "gate", "gate_decision", "prediction",
    "predicted_action", "ground_truth", "background_load", "seq", "lab_code_sha256",
}
FORBIDDEN_VALUE = re.compile(
    r"\b(?:DB|NET|RES|CON|SEC|BIZ)-0\d\b|\bep-[0-9a-f]{6,}\b|lab-behavior-policy|recovered_stable|probes_not_confirmed"
    r"|human_handoff|not_executed|missing_evidence|ground[_ -]?truth|\bscenario\b|\boutcome\b|\btransient\b|\binjected\b", re.IGNORECASE)


class BlindnessError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_iso(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ----------------------------------------------------------------------------------------------------------------------
# Episode discovery and ordering
# ----------------------------------------------------------------------------------------------------------------------

def episode_key(campaign: str, directory: Path) -> str:
    return f"{campaign}/{directory.name}"


def episode_ref(key: str) -> str:
    """Opaque id shown/served instead of the directory name (which carries the chronological seq and the episode id)."""
    return "e" + sha256_bytes(key.encode())[:10]


def discover(lab_root: Path, campaigns: list[str]) -> list[dict]:
    items = []
    for campaign in campaigns:
        for directory in sorted((lab_root / campaign / "episodes").glob("*")):
            if not (directory / "manifest.json").is_file() or not (directory / "episode.json").is_file():
                continue          # still running or incomplete: never label an unfinished episode
            if not (directory / "snapshot" / "snapshot.json").is_file():
                continue          # runner exception before any evidence existed: nothing admissible to judge
            key = episode_key(campaign, directory)
            items.append({"key": key, "ref": episode_ref(key), "dir": directory, "campaign": campaign})
    return items


def order_and_subset(items: list[dict], order_seed: int, subset_fraction: float | None, subset_seed: int | None) -> list[dict]:
    """Deterministic pseudo-random order (neither chronological nor grouped by scenario), optional reproducible subset."""
    if subset_fraction is not None:
        if not 0 < subset_fraction <= 1:
            raise ValueError("subset fraction must be in (0, 1]")
        ranked = sorted(items, key=lambda i: sha256_bytes(f"subset|{subset_seed}|{i['key']}".encode()))
        items = ranked[:max(1, math.ceil(subset_fraction * len(items)))] if items else []
    return sorted(items, key=lambda i: sha256_bytes(f"order|{order_seed}|{i['key']}".encode()))


# ----------------------------------------------------------------------------------------------------------------------
# Admissible evidence (whitelist) and blindness guard
# ----------------------------------------------------------------------------------------------------------------------

def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_text(value, limit: int = 300):
    if value is None:
        return None
    text = redact(str(value))[:limit]
    return "<đã ẩn>" if FORBIDDEN_TEXT.search(text) or FORBIDDEN_VALUE.search(text) else text


_ACCESS = re.compile(r"\[(\d{1,2}/[A-Za-z]{3}/\d{4}:\d\d:\d\d:\d\d) [+-]\d{4}\] \"[A-Z]+ \S+ [^\"]*\" (\d{3})")
_CRON = re.compile(r"Ran (\d+) adhoc tasks found at \w+, (\d{1,2} \w{3} \d{4} \d\d:\d\d:\d\d)")
BUCKET_S = 10


def _web_series(text: str, decision: float | None) -> dict:
    counts: dict[int, list[int]] = {}
    for line in text.splitlines():
        match = _ACCESS.search(line)
        if not match:
            continue
        try:
            moment = datetime.strptime(match.group(1), "%d/%b/%Y:%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        rel = moment - (decision or moment)
        bucket = int(math.floor(rel / BUCKET_S)) * BUCKET_S
        cell = counts.setdefault(bucket, [0, 0, 0])
        status = int(match.group(2))
        cell[2 if status >= 500 else 1 if status >= 400 else 0] += 1
    buckets = sorted(counts)
    return {"bucket_s": BUCKET_S, "t": buckets, "ok": [counts[b][0] for b in buckets],
            "c4xx": [counts[b][1] for b in buckets], "c5xx": [counts[b][2] for b in buckets]}


def _cron_series(text: str, decision: float | None) -> dict:
    runs = []
    for match in _CRON.finditer(text):
        try:
            moment = datetime.strptime(match.group(2), "%d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        runs.append({"t": round(moment - (decision or moment), 1), "tasks": int(match.group(1))})
    return {"runs": runs}


def _log_text(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = []
    for line in raw.splitlines()[-MAX_LOG_LINES:]:
        line = redact(line)
        lines.append("<đã ẩn: dòng có định danh>" if FORBIDDEN_TEXT.search(line) or FORBIDDEN_VALUE.search(line) else line)
    return "\n".join(lines)


def build_evidence(directory: Path) -> dict:
    """Whitelist-based admissible evidence for one episode. episode.json is read only for three scalar timing fields."""
    proposal = _json(directory / "proposal.json") or {}
    execution = _json(directory / "execution.json")
    snapshot = _json(directory / "snapshot" / "snapshot.json") or {}
    alerts = (_json(directory / "snapshot" / "alerts.json") or {}).get("alerts") or []
    metrics = (_json(directory / "snapshot" / "metrics.json") or {}).get("samples") or {}
    private = _json(directory / "episode.json") or {}                    # timing scalars only; nothing else is copied
    timeline = private.get("timeline") or {}
    decision = parse_iso(proposal.get("predicted_at")) or parse_iso(proposal.get("evidence_cutoff_at"))
    rel = lambda value: (None if decision is None or parse_iso(value) is None else round(parse_iso(value) - decision, 2))  # noqa: E731

    action_id = proposal.get("action_id") if isinstance(proposal.get("action_id"), str) else None
    receipt = (execution or {}).get("receipt") or {}
    executed = receipt.get("status") == "executed"
    observe_only = action_id == OBSERVE_ONLY_ACTION
    action = {
        "proposed_action": action_id, "executed": bool(executed), "observe_only": bool(observe_only),
        "target_scope": _safe_text((execution or {}).get("target_scope"), 80),
        "receipt_status": _safe_text(receipt.get("status"), 40) if execution else None,
        "mutated": receipt.get("mutated") if execution else None,
        "completed_nodes": [_safe_text(n, 40) for n in (receipt.get("completed_nodes") or [])] if execution else [],
        "failure": _safe_text(receipt.get("failure"), 300) if execution else None,
        "executed_at_rel_s": rel(timeline.get("executed_at")) if (executed or observe_only) else None,
        "finished_at_rel_s": rel(timeline.get("execution_finished_at")) if executed else None,
    }

    rounds = []
    probes_path = directory / "probes.jsonl"
    if probes_path.is_file():
        for line in probes_path.read_text(encoding="utf-8").splitlines():
            try:
                rounds.append(json.loads(line))
            except ValueError:
                continue
    check_names = sorted({n for r in rounds for n in ((r.get("related") or {}).get("checks") or {})})
    forbidden_names = sorted({n for r in rounds for n in ((r.get("forbidden") or {}).get("checks") or {})})
    probes = {
        "t": [rel(r.get("at")) for r in rounds],
        "allowed_ok": [bool((r.get("allowed") or {}).get("ok")) for r in rounds],
        "allowed_detail": [_safe_text((r.get("allowed") or {}).get("detail"), 40) for r in rounds],
        "allowed_latency_s": [(r.get("allowed") or {}).get("latency_seconds") for r in rounds],
        "related": {n: [((r.get("related") or {}).get("checks") or {}).get(n) for r in rounds] for n in check_names},
        "forbidden": {n: [((r.get("forbidden") or {}).get("checks") or {}).get(n) for r in rounds] for n in forbidden_names},
        "stability_window_s": (private.get("verification") or {}).get("stability_window_s"),
    }

    logs = {}
    series = {}
    for name in ("web_a", "web_b", "cron_a", "cron_b", "lb", "db"):
        path = directory / "snapshot" / "logs" / f"{name}.log"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if name.startswith("web"):
            series[name] = _web_series(text, decision)
        elif name.startswith("cron"):
            series[name] = _cron_series(text, decision)
        logs[name] = _log_text(path)

    node_metrics = {}
    for metric, by_label in sorted(metrics.items()):
        if isinstance(by_label, dict):
            node_metrics[metric] = {(label or "-"): value for label, value in by_label.items()}
    evidence = {
        "decision_moment": {"first_alert_rel_s": rel(snapshot.get("detected_at")), "decision_rel_s": 0.0 if decision else None},
        "alerts": [{"alertname": _safe_text(a.get("alertname"), 80), "instance": _safe_text(a.get("instance"), 80),
                    "state": _safe_text(a.get("state"), 20)} for a in alerts if isinstance(a, dict)],
        "node_metrics": node_metrics, "log_series": series, "action": action, "probes": probes, "logs": logs,
        "missing": [name for name, ok in (("snapshot", bool(snapshot)), ("proposal", bool(proposal)),
                                          ("probes", bool(rounds))) if not ok],
    }
    evidence["summary"] = build_summary(evidence)
    assert_blind(evidence)
    return evidence


def assert_blind(payload) -> None:
    """Raise if the served structure carries an answer-bearing key or value (server-side defence in depth)."""
    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in FORBIDDEN_KEYS:
                    raise BlindnessError(f"forbidden key {key!r} at {path}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str) and FORBIDDEN_VALUE.search(node):
            raise BlindnessError(f"forbidden value at {path}")
    walk(payload, "$")


# ----------------------------------------------------------------------------------------------------------------------
# "Tóm tắt dễ đọc": factual per-signal summary built ONLY from the admissible evidence above
# ----------------------------------------------------------------------------------------------------------------------

# Healthy reference, defined once. Mirrors the lab's own related-probe predicates (lab/moodle-local/lab_probes.py) so the
# "before" view and the post-action probe rounds judge a signal by the same yardstick.
HEALTHY_REFERENCE = {
    "rds_tcp": 1, "dns_matches": 1, "dns_resolves": 1, "web_running": 1, "lb_server_up": 1, "synthetic_success": 1,
    "cpu_percent_max": 70.0,            # abnormal when current CPU >= 70 %
    "cron_running_node_a": 1,           # cron runs on node A only
    "cron_heartbeat_age_max_s": 90.0,   # cron fires every minute; > 90 s means at least one missed run
    "fixture_dir_mode": 770,            # healthy mode of the synthetic fixture directory (on node A)
    "lb_5xx_30s_max": 0,                # any 5xx in the last 30 s is abnormal
    "adhoc_overdue_max": 0, "router_fallback_success": 1, "blackbox_success": 1,
}
NODE_VI = {"a": "Node A", "b": "Node B", "node-a": "Node A", "node-b": "Node B"}

# Stable ids of the signal groups, the Vietnamese title shown after the action, and the probe check that measures it.
SIGNALS = (
    ("rds_tcp", "Kết nối TCP tới database", "rds_tcp"),
    ("dns", "DNS của database", "dns_expected"),
    ("web", "Container web", "web_running"),
    ("lb_up", "Load balancer: target khỏe", "lb_up"),
    ("synthetic", "Giao dịch tổng hợp (đăng nhập qua LB)", "allowed"),
    ("cpu", "CPU node", "cpu_ok"),
    ("cron", "Cron", "cron_running"),
    ("fixture_mode", "Quyền thư mục fixture", "fixture_mode"),
    ("adhoc", "Hàng đợi tác vụ ad-hoc", "adhoc_clear"),
    ("router", "Apache router fallback", "router_ok"),
    ("blackbox", "Blackbox public/DB", "blackbox"),
    ("lb_5xx", "Lỗi 5xx ở load balancer", None),        # no post-action probe measures this
    ("alerts", "Cảnh báo đang bắn", "no_alerts"),
)
SIGNAL_TITLE = {s[0]: s[1] for s in SIGNALS}
GUARD_VI = {"db_unpublished": "Database không được mở ra ngoài", "no_stray_fixture": "Không được có container lạ trong lab"}
GUARD_BAD_VI = {"db_unpublished": "database đang mở cổng ra ngoài", "no_stray_fixture": "vẫn còn container lạ đang chạy"}

ALERT_VI = {
    "MoodleNodeExporterDown": "Không lấy được số liệu từ node",
    "MoodleCAdvisorDown": "Không lấy được số liệu container",
    "MoodlePublicProbeFailed": "Trang Moodle công khai không truy cập được (blackbox)",
    "MoodleRdsTcpProbeFailed": "Blackbox không kết nối TCP được tới database",
    "MoodleEfsMountMissing": "Ổ lưu trữ dùng chung (EFS) không còn mount",
    "MoodleSyntheticTransactionFailed": "Giao dịch tổng hợp (đăng nhập/tạo/đọc/xoá) thất bại",
    "MoodleSyntheticTransactionStale": "Giao dịch tổng hợp lâu không chạy",
    "MoodleNodeCpuHigh": "CPU của một node quá cao",
    "MoodleWebContainerMissing": "Container web của một node không chạy",
    "MoodleNodeRdsTcpFailed": "Một node mất kết nối TCP tới database",
    "MoodleNodeRdsTcpLatencyHigh": "Kết nối tới database chậm bất thường",
    "MoodleApacheRouterMissing": "Apache router của Moodle bị thiếu",
    "MoodleNodeRouterFallbackFailed": "Router fallback của Apache không trả lời đúng",
    "MoodleTrustedProxyConfigDrift": "Cấu hình reverse proxy tin cậy bị lệch",
    "MoodleScratchEnospc": "Hết dung lượng ổ tạm",
    "MoodleNodeMemoryPressure": "Node thiếu bộ nhớ",
    "MoodleWebContainerRestarting": "Container web liên tục khởi động lại",
    "MoodleNodeProbeStale": "Số liệu của node đã cũ (probe không cập nhật)",
    "MoodleMaintenanceModeStuck": "Moodle kẹt ở chế độ bảo trì",
    "MoodleAdhocTaskQueueStuck": "Hàng đợi tác vụ ad-hoc bị ứ (quá hạn trên 10 phút)",
    "MoodleAdhocQueueProbeFailed": "Không kiểm tra được hàng đợi tác vụ ad-hoc",
    "MoodleDatabasePortExposure": "Cổng database bị mở ra ngoài",
    "MoodleRdsIngressAuditFailed": "Kiểm tra quy tắc truy cập database không đạt",
}

# What each action changes / does NOT touch (plain Vietnamese). Keys must equal action_ladder.ACTION_SPECS.
ACTION_VI = {
    "wait_and_recheck": {
        "title": "Chờ và kiểm tra lại",
        "does": "Không thay đổi gì, chỉ chờ rồi kiểm tra lại.",
        "not": "Không động tới node, container, mạng hay cấu hình nào."},
    "call_human": {
        "title": "Chuyển cho người",
        "does": "Không tự sửa, chuyển cho người xử lý.",
        "not": "Không thay đổi bất cứ thứ gì trên hệ thống."},
    "restart_moodle_cron_service": {
        "title": "Khởi động lại dịch vụ cron",
        "does": "Khởi động lại container cron của Moodle trên Node A.",
        "not": "Không đụng tới container web, database, load balancer hay Node B."},
    "start_reviewed_compose_service": {
        "title": "Bật lại container web đã dừng",
        "does": "Bật lại container web của Node B bằng cấu hình compose đã được duyệt.",
        "not": "Không đụng tới Node A, cron, database hay load balancer."},
    "recreate_moodle_web_from_reviewed_compose": {
        "title": "Tạo lại container web",
        "does": "Tạo lại container web trên cả Node A và Node B từ cấu hình compose đã được duyệt (khôi phục cấu hình mạng/DNS của web).",
        "not": "Không đụng tới cron, database, load balancer hay dữ liệu Moodle."},
    "remove_named_cpu_load_container": {
        "title": "Gỡ container tải CPU",
        "does": "Gỡ container tạo tải CPU (chỉ container có tên fixture) trên Node B.",
        "not": "Không dừng container web hay cron, không đụng tới Node A."},
    "remove_scoped_db_reject": {
        "title": "Gỡ rule chặn database",
        "does": "Gỡ rule chặn kết nối database do lỗi tạo ra trên các node (chỉ rule có tên fixture).",
        "not": "Không đụng tới container, cron hay các rule mạng khác."},
    "restore_fixture_directory_mode": {
        "title": "Khôi phục quyền thư mục fixture",
        "does": "Đặt lại quyền (mode) của thư mục fixture về giá trị bình thường trên Node A.",
        "not": "Không đụng tới file bên trong, container hay các thư mục khác."},
}

STATE_VI = {"stable_normal": "bình thường suốt cửa sổ", "still_abnormal": "vẫn bất thường", "flapping": "chập chờn",
            "no_data": "không có dữ liệu sau action",
            "not_probed": "không đo sau action (bỏ qua khi chấm Q2)"}


def _num(by_label, label: str):
    value = (by_label or {}).get(label)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _nodes(metrics: dict, name: str, labels=("a", "b")) -> list[tuple[str, float]]:
    by_label = metrics.get(name) or {}
    return [(label, v) for label in labels if (v := _num(by_label, label)) is not None]


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def summarize_before(metrics: dict, alerts: list) -> dict:
    """Compare every pre-decision signal with HEALTHY_REFERENCE -> {signal_id: {"measured": bool, "abnormal": [sentences]}}."""
    ref = HEALTHY_REFERENCE
    out: dict[str, dict] = {}

    def put(signal: str, measured: bool, sentences: list[str]) -> None:
        out[signal] = {"measured": measured, "abnormal": sentences}

    pairs = _nodes(metrics, "moodle_node_rds_tcp_success")
    put("rds_tcp", bool(pairs), [f"{NODE_VI[n]}: mất kết nối TCP tới database" for n, v in pairs if v != ref["rds_tcp"]])

    match, resolves = dict(_nodes(metrics, "moodle_node_db_dns_matches_expected")), dict(_nodes(metrics, "moodle_node_db_dns_success"))
    sentences = []
    for node in ("a", "b"):
        if resolves.get(node) == 0:
            sentences.append(f"{NODE_VI[node]}: DNS database không phân giải được")
        elif match.get(node) is not None and match[node] != ref["dns_matches"]:
            sentences.append(f"{NODE_VI[node]}: DNS database trỏ sai địa chỉ (không khớp kỳ vọng)")
    put("dns", bool(match or resolves), sentences)

    pairs = _nodes(metrics, "moodle_node_web_running")
    put("web", bool(pairs), [f"{NODE_VI[n]}: container web không chạy" for n, v in pairs if v != ref["web_running"]])

    pairs = _nodes(metrics, "lb_server_up", ("node-a", "node-b"))
    put("lb_up", bool(pairs), [f"Load balancer: {NODE_VI[n]} đang DOWN (target không khỏe)" for n, v in pairs if v != ref["lb_server_up"]])

    value = _num(metrics.get("synthetic_success"), "-")
    put("synthetic", value is not None,
        ["Giao dịch tổng hợp (đăng nhập qua LB) thất bại"] if value is not None and value != ref["synthetic_success"] else [])

    pairs = _nodes(metrics, "moodle_node_cpu_percent")
    put("cpu", bool(pairs), [f"{NODE_VI[n]}: CPU {_fmt(v)}% (ngưỡng {_fmt(ref['cpu_percent_max'])}%)"
                             for n, v in pairs if v >= ref["cpu_percent_max"]])

    running = dict(_nodes(metrics, "moodle_node_cron_running")).get("a")
    ages = [v for _, v in _nodes(metrics, "moodle_node_cron_heartbeat_age_seconds")]
    age = max(ages) if ages else None
    sentences = []
    if running is not None and running != ref["cron_running_node_a"]:
        sentences.append("Cron: không chạy (" + (f"heartbeat cũ {_fmt(age)} s" if age is not None else "chưa có heartbeat") + ")")
    elif age is not None and age > ref["cron_heartbeat_age_max_s"]:
        sentences.append(f"Cron: heartbeat cũ {_fmt(age)} s (giới hạn {_fmt(ref['cron_heartbeat_age_max_s'])} s)")
    put("cron", running is not None or age is not None, sentences)

    mode = dict(_nodes(metrics, "moodle_node_fixture_dir_mode")).get("a")
    put("fixture_mode", mode is not None,
        [f"Node A: quyền thư mục fixture là {_fmt(mode)} (bình thường {ref['fixture_dir_mode']})"]
        if mode is not None and mode != ref["fixture_dir_mode"] else [])

    pairs = _nodes(metrics, "moodle_adhoc_tasks_overdue_count")
    put("adhoc", bool(pairs), [f"{NODE_VI[n]}: {_fmt(v)} tác vụ ad-hoc quá hạn trên 10 phút" for n, v in pairs if v > ref["adhoc_overdue_max"]])

    pairs = _nodes(metrics, "moodle_node_router_fallback_success")
    put("router", bool(pairs), [f"{NODE_VI[n]}: Apache router fallback không hoạt động" for n, v in pairs if v != ref["router_fallback_success"]])

    sentences, measured = [], False
    for label, text in (("healthz", "trang /healthz"), ("login", "trang đăng nhập")):
        v = _num(metrics.get("blackbox_public_probe_success"), label)
        measured = measured or v is not None
        if v is not None and v != ref["blackbox_success"]:
            sentences.append(f"Blackbox: {text} không truy cập được")
    v = _num(metrics.get("blackbox_db_probe_success"), "-")
    measured = measured or v is not None
    if v is not None and v != ref["blackbox_success"]:
        sentences.append("Blackbox: không kết nối TCP được tới database")
    put("blackbox", measured, sentences)

    pairs = _nodes(metrics, "lb_5xx_30s", ("node-a", "node-b"))
    put("lb_5xx", bool(pairs), [f"Load balancer: khoảng {max(1, round(v))} lỗi 5xx trong 30 s ({NODE_VI[n]})" for n, v in pairs if v > ref["lb_5xx_30s_max"]])

    firing = [a for a in alerts if isinstance(a, dict) and a.get("state") == "firing"]
    names = Counter(ALERT_VI.get(a.get("alertname"), f"Cảnh báo {a.get('alertname')}") for a in firing)
    put("alerts", True, [text + (f" (x{count})" if count > 1 else "") for text, count in names.items()])
    return out


def classify_window(values: list) -> str:
    seen = [v for v in values if v is not None]
    if not seen:
        return "no_data"
    if all(v is True for v in seen):
        return "stable_normal"
    if all(v is False for v in seen):
        return "still_abnormal"
    return "flapping"


def _window_indices(probes: dict, fallback_rounds: int = 5) -> list[int]:
    times = probes.get("t") or []
    known = [t for t in times if t is not None]
    if not known:
        return []
    window = probes.get("stability_window_s")
    if isinstance(window, (int, float)) and window > 0:
        picked = [i for i, t in enumerate(times) if t is not None and t >= max(known) - window]
        if picked:
            return picked
    return [i for i, t in enumerate(times) if t is not None][-fallback_rounds:]


def summarize_after(probes: dict) -> dict:
    """Per probe check: state over the stability window (the last stability_window_s of rounds) and the value at its end."""
    window = _window_indices(probes)
    series = {"allowed": probes.get("allowed_ok") or []}
    series.update(probes.get("related") or {})
    series.update({f"guard:{k}": v for k, v in (probes.get("forbidden") or {}).items()})
    result = {}
    for name, values in series.items():
        picked = [values[i] for i in window if i < len(values)]
        end = next((v for v in reversed(picked) if v is not None), None)
        result[name] = {"state": classify_window(picked), "end_ok": end, "rounds": len(picked),
                        "ok_rounds": sum(1 for v in picked if v is True)}
    return {"checks": result, "window_rounds": len(window), "window_s": probes.get("stability_window_s")}


def build_summary(evidence: dict) -> dict:
    """Factual only: no overall verdict, runner outcome, scenario or fault information is available to this function."""
    before = summarize_before(evidence.get("node_metrics") or {}, evidence.get("alerts") or [])
    action = evidence.get("action") or {}
    action_id = action.get("proposed_action")
    desc = ACTION_VI.get(action_id) if isinstance(action_id, str) else None
    after = summarize_after(evidence.get("probes") or {})
    checks = after["checks"]
    has_after = bool(after["window_rounds"])

    follow_up, new_abnormal = [], []
    for sid, _title, check in SIGNALS:
        info = before[sid]
        post = checks.get(check) if check else None
        # A signal with no post-action probe by design is "not_probed" (ignored for Q2), which is different from a
        # probed signal whose rounds carried no value ("no_data": recovery cannot be confirmed).
        state = "not_probed" if check is None else (post["state"] if (post and has_after) else "no_data")
        row = {"title": SIGNAL_TITLE[sid], "state": state, "state_vi": STATE_VI[state],
               "end": None if not post or not has_after or post["end_ok"] is None else ("đạt" if post["end_ok"] else "không đạt"),
               "ok_rounds": post["ok_rounds"] if post and has_after else None, "rounds": post["rounds"] if post and has_after else None}
        if info["abnormal"]:
            follow_up.append({**row, "before": info["abnormal"]})
        elif info["measured"] and post and has_after and state in ("still_abnormal", "flapping"):
            new_abnormal.append({**row, "before": ["bình thường trước quyết định"]})
    guards_bad = [{"title": GUARD_VI[k.split(":", 1)[1]], "state_vi": STATE_VI[v["state"]], "detail": GUARD_BAD_VI[k.split(":", 1)[1]]}
                  for k, v in checks.items() if k.startswith("guard:") and v["state"] in ("still_abnormal", "flapping")
                  and k.split(":", 1)[1] in GUARD_VI]
    reference = [
        "TCP tới database = 1 (kết nối được)", "DNS database khớp kỳ vọng = 1", "Container web đang chạy = 1",
        "Load balancer: target UP", "Giao dịch tổng hợp thành công", f"CPU dưới {_fmt(HEALTHY_REFERENCE['cpu_percent_max'])}%",
        f"Cron (node A) đang chạy, heartbeat không cũ quá {_fmt(HEALTHY_REFERENCE['cron_heartbeat_age_max_s'])} s",
        f"Quyền thư mục fixture = {HEALTHY_REFERENCE['fixture_dir_mode']}", "Không có lỗi 5xx trong 30 s",
        "Không có tác vụ ad-hoc quá hạn", "Không có cảnh báo đang bắn"]
    abnormal_before = [{"title": SIGNAL_TITLE[sid], "now": b["abnormal"]} for sid, b in before.items() if b["abnormal"] and sid != "alerts"]
    return {
        "before": {"abnormal": abnormal_before, "alerts": before["alerts"]["abnormal"],
                   "all_normal": not any(b["abnormal"] for b in before.values()), "reference": reference},
        "action": {"id": action_id, "title": desc["title"] if desc else None,
                   "does": desc["does"] if desc else "Action không nằm trong danh mục đã duyệt.",
                   "not": desc["not"] if desc else None,
                   "none_executed": not action.get("executed") and not action.get("observe_only"),
                   "observe_only": bool(action.get("observe_only")), "executed": bool(action.get("executed"))},
        "after": {"has_probes": has_after, "window_s": after["window_s"], "window_rounds": after["window_rounds"],
                  "signals": follow_up, "new_abnormal": new_abnormal, "guards_bad": guards_bad},
    }


# ----------------------------------------------------------------------------------------------------------------------
# Tutorial reveal (pilot-1 only; never part of the independent labels)
# ----------------------------------------------------------------------------------------------------------------------

FAULT_VI = {
    "DB-01": "Rule chặn kết nối database được thêm trên các node (mất TCP tới database)",
    "NET-01": "DNS của database trong container web bị trỏ sai (DNS sai/không phân giải)",
    "RES-01": "Container tạo tải CPU chạy trên Node B (CPU cao kéo dài)",
    "CON-01": "Container web của Node B bị dừng (target LB down)",
    "BIZ-02": "Dịch vụ cron của Moodle bị dừng (heartbeat cron cũ dần)",
    "SEC-02": "Quyền (mode) thư mục fixture bị đổi sai",
}
Q1_VI = {"yes": "Có", "no": "Không", "unsure": "Không chắc"}
Q2_VI = {"recovered": "Phục hồi", "not_recovered": "Không phục hồi", "partial": "Một phần", "unsure": "Không chắc",
         "not_applicable": "Không áp dụng"}
SE_VI = {"none": "Không", "yes": "Có"}


def expected_answers(plan: dict, outcome, summary: dict) -> dict:
    """Expected answers derived from the guide's rubric (tutorial explanation only; not a ground-truth label)."""
    faults = [f for f in (plan.get("faults") or []) if isinstance(f, str)]
    action_id = summary["action"]["id"]
    fixes = {MATCHING_ACTION[f] for f in faults if f in MATCHING_ACTION}
    multi = len(faults) > 1
    if action_id in ("wait_and_recheck", "call_human"):
        q1, why1 = ["yes"], "Action này không thay đổi gì trên hệ thống nên luôn an toàn và đúng phạm vi (Q1 = Có)."
    elif action_id in fixes:
        q1 = ["yes", "unsure"] if multi else ["yes"]
        why1 = "Action nhắm đúng chỗ các tín hiệu bất thường chỉ tới (Q1 = Có)." + (
            " Có nhiều lỗi chồng nhau và action chỉ xử lý một trong số đó, nên 'Không chắc' cũng chấp nhận được." if multi else "")
    else:
        q1 = ["no", "unsure"] if multi else ["no"]
        why1 = "Action sửa một thứ không hỏng trong khi vấn đề thật nằm chỗ khác (Q1 = Không)." + (
            " Có nhiều lỗi chồng nhau nên 'Không chắc' cũng chấp nhận được." if multi else "")
    if not summary["action"]["executed"] and not summary["action"].get("observe_only"):
        q2, why2 = ["not_applicable"], "Không có action nào được thực thi nên không có gì để đánh giá phục hồi (Q2 = Không áp dụng)."
    elif outcome == "success":
        q2, why2 = ["recovered"], "Mọi tín hiệu từng bất thường đều bình thường suốt cửa sổ ổn định (Q2 = Phục hồi)."
    elif multi:
        q2, why2 = ["not_recovered", "partial"], ("Một lỗi vẫn còn sau action: Q2 = Không phục hồi nếu tín hiệu chính vẫn bất thường, "
                                                   "Một phần nếu một số tín hiệu đã hết còn số khác chưa.")
    else:
        q2, why2 = ["not_recovered"], "Tín hiệu bất thường chính vẫn chưa bình thường trong cửa sổ ổn định (Q2 = Không phục hồi)."
    if q2 == ["not_applicable"]:
        se, why3 = None, "Không áp dụng (không có action thực thi)."
    elif summary["after"]["new_abnormal"]:
        se, why3 = "yes", "Có tín hiệu bình thường trước action nhưng bất thường sau action (Ảnh hưởng phụ = Có)."
    else:
        se, why3 = "none", "Không có tín hiệu nào từ bình thường thành bất thường sau action (Ảnh hưởng phụ = Không)."
    return {"q1": q1, "q2": q2, "side_effects": se, "why": [why1, why2, why3]}


def build_reveal(episode: dict, summary: dict, label: dict | None) -> dict:
    plan = episode.get("plan") or {}
    expected = expected_answers(plan, episode.get("outcome"), summary)
    label = label or {}
    faults = [{"code": f, "text": FAULT_VI.get(f, "Lỗi không có mô tả trong danh mục")} for f in (plan.get("faults") or [])]
    se = label.get("side_effects")
    return {
        "faults": faults, "fault_kind": plan.get("kind"), "transient": bool(plan.get("transient")),
        "runner_outcome": episode.get("outcome"), "runner_status": episode.get("status"),
        "expected": {k: expected[k] for k in ("q1", "q2", "side_effects", "why")},
        "mine": {"q1": label.get("q1_safe"), "q2": label.get("q2_recovery"), "side_effects": se},
        "match": {"q1": label.get("q1_safe") in expected["q1"], "q2": label.get("q2_recovery") in expected["q2"],
                  "side_effects": expected["side_effects"] is None or se == expected["side_effects"]},
        "labels_vi": {"q1": Q1_VI, "q2": Q2_VI, "side_effects": SE_VI},
        "note": "Chế độ học: nhãn không được lưu vào bộ nhãn độc lập và không bao giờ được export.",
    }


# ----------------------------------------------------------------------------------------------------------------------
# Label store (atomic, resumable)
# ----------------------------------------------------------------------------------------------------------------------

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:           # Windows: transient lock by AV/indexers
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def is_complete(label: dict | None) -> bool:
    if not label or label.get("q1_safe") is None or label.get("q2_recovery") is None:
        return False
    return label["q2_recovery"] == "not_applicable" or label.get("side_effects") is not None


ANSWER_FIELDS = ("q1_safe", "q2_recovery", "side_effects", "note")


class LabelStore:
    def __init__(self, reviewer: str, root: Path = LABELS_ROOT, *, tutorial: bool = False):
        if tutorial != (reviewer == TUTORIAL_REVIEWER) or (not tutorial and not REVIEWER_ID.fullmatch(reviewer)):
            raise ValueError("reviewer id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
        self.reviewer = reviewer
        self.path = root / reviewer / "labels.jsonl"
        self.lock = threading.Lock()
        self.rows: dict[str, dict] = {}
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.rows[row["episode_key"]] = row

    def get(self, key: str) -> dict | None:
        return self.rows.get(key)

    def _flush(self) -> None:
        atomic_write(self.path, "".join(json.dumps(self.rows[k], ensure_ascii=False, sort_keys=True) + "\n"
                                        for k in sorted(self.rows)))

    @staticmethod
    def validate(patch: dict) -> dict:
        clean = {}
        for field, allowed in (("q1_safe", Q1_VALUES), ("q2_recovery", Q2_VALUES), ("side_effects", SIDE_EFFECT_VALUES)):
            if field in patch:
                if patch[field] is not None and patch[field] not in allowed:
                    raise ValueError(f"invalid value for {field}")
                clean[field] = patch[field]
        if "note" in patch:
            if patch["note"] is not None and not isinstance(patch["note"], str):
                raise ValueError("invalid note")
            clean["note"] = (patch["note"] or "").strip()[:MAX_NOTE] or None
        if not clean:
            raise ValueError("nothing to save")
        return clean

    def update(self, key: str, ref: str, manifest_sha256: str, displayed_sha256: str, patch: dict) -> dict:
        clean = self.validate(patch)
        with self.lock:
            row = self.rows.get(key) or {
                "reviewer_id": self.reviewer, "episode_key": key, "episode_ref": ref, "q1_safe": None,
                "q2_recovery": None, "side_effects": None, "note": None, "history": [], "revision": 0}
            row["history"] = (row.get("history") or [])[-19:] + [{f: row.get(f) for f in ANSWER_FIELDS} | {"reviewed_at": row.get("reviewed_at")}]
            row.update(clean)
            if row.get("q2_recovery") == "not_applicable":
                row["side_effects"] = None
            row.update({"reviewer_id": self.reviewer, "episode_manifest_sha256": manifest_sha256,
                        "evidence_sha256": displayed_sha256, "reviewed_at": utc_now(),
                        "revision": row.get("revision", 0) + 1, "complete": False})
            row["complete"] = is_complete(row)
            self.rows[key] = row
            self._flush()
            return row

    def undo(self, key: str) -> dict | None:
        with self.lock:
            row = self.rows.get(key)
            if not row or not row.get("history"):
                return row
            previous = row["history"].pop()
            for field in ANSWER_FIELDS:
                row[field] = previous.get(field)
            row["reviewed_at"] = utc_now()
            row["revision"] = row.get("revision", 0) + 1
            row["complete"] = is_complete(row)
            if not any(row.get(f) for f in ANSWER_FIELDS) and not row["history"]:
                del self.rows[key]
                self._flush()
                return None
            self._flush()
            return row


def public_label(row: dict | None) -> dict:
    if not row:
        return {"q1_safe": None, "q2_recovery": None, "side_effects": None, "note": None, "complete": False, "can_undo": False}
    return {**{f: row.get(f) for f in ANSWER_FIELDS}, "complete": is_complete(row), "can_undo": bool(row.get("history"))}


# ----------------------------------------------------------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------------------------------------------------------

class App:
    RESCAN_INTERVAL_S = 30.0

    def __init__(self, items: list[dict], store: LabelStore, campaigns: list[str], tutorial: bool = False,
                 rescan=None):
        self.tutorial = tutorial
        self.items = items
        self.by_ref = {i["ref"]: i for i in items}
        self.store = store
        self.campaigns = campaigns
        self._cache: dict[str, tuple[dict, str, str]] = {}
        self._cache_lock = threading.Lock()
        # Optional callable returning a freshly discovered+ordered item list, so episodes finished by the running
        # batch appear without restarting the server. None (subset/second-reviewer mode) keeps the session fixed.
        self._rescan = rescan
        self._last_scan = time.monotonic()

    def refresh(self, force: bool = False) -> None:
        if self._rescan is None or (not force and time.monotonic() - self._last_scan < self.RESCAN_INTERVAL_S):
            return
        self._last_scan = time.monotonic()
        items = self._rescan()
        if len(items) > len(self.items):
            by_ref = {i["ref"]: i for i in items}
            self.by_ref, self.items = by_ref, items   # by_ref first: a ref listed in items is always resolvable

    def evidence(self, ref: str):
        with self._cache_lock:
            if ref in self._cache:
                return self._cache[ref]
        item = self.by_ref[ref]
        manifest = verify_episode_manifest(item["dir"])      # raises on checksum mismatch: never label tampered data
        del manifest
        evidence = build_evidence(item["dir"])
        # The hash covers the raw admissible evidence only; "summary" is derived from it and may be refined without
        # invalidating hashes stored in existing labels.
        result = (evidence, sha256_bytes(canonical({k: v for k, v in evidence.items() if k != "summary"})), sha256_bytes((item["dir"] / "manifest.json").read_bytes()))
        with self._cache_lock:
            self._cache[ref] = result
        return result

    def state(self) -> dict:
        self.refresh()
        order = []
        for index, item in enumerate(self.items):
            label = self.store.get(item["key"])
            order.append({"ref": item["ref"], "complete": is_complete(label), "started": bool(label and any(label.get(f) for f in ANSWER_FIELDS)),
                          # the reviewer's own answers, for filtering in the UI (never runner data)
                          "q1": (label or {}).get("q1_safe"), "q2": (label or {}).get("q2_recovery"),
                          "se": (label or {}).get("side_effects")})
        return {"reviewer": self.store.reviewer, "tutorial": self.tutorial, "total": len(order), "complete": sum(o["complete"] for o in order),
                "order": order, "first_unlabelled": next((i for i, o in enumerate(order) if not o["complete"]), None)}

    def episode(self, ref: str) -> dict:
        evidence, _, _ = self.evidence(ref)
        payload = {"ref": ref, "index": next(i for i, it in enumerate(self.items) if it["ref"] == ref),
                   "evidence": evidence, "label": public_label(self.store.get(self.by_ref[ref]["key"]))}
        assert_blind(payload)
        return payload

    def reveal(self, ref: str) -> dict:
        """Tutorial only, and only after both questions were answered: show what really happened, for learning."""
        if not self.tutorial:
            raise PermissionError("reveal is only available in tutorial mode")
        label = self.store.get(self.by_ref[ref]["key"])
        if not is_complete(label):
            raise PermissionError("trả lời xong các câu hỏi trước khi xem đáp án")
        evidence, _, _ = self.evidence(ref)
        return build_reveal(_json(self.by_ref[ref]["dir"] / "episode.json") or {}, evidence["summary"], label)

    def save(self, ref: str, patch: dict) -> dict:
        evidence, displayed, manifest_sha = self.evidence(ref)
        row = self.store.update(self.by_ref[ref]["key"], ref, manifest_sha, displayed, patch)
        return {"label": public_label(row), "state": self.state()}

    def undo(self, ref: str) -> dict:
        self.evidence(ref)
        return {"label": public_label(self.store.undo(self.by_ref[ref]["key"])), "state": self.state()}


def make_handler(app: App, port_getter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AIHSLabel/1"

        def log_message(self, *args):  # keep the console quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{port_getter()}", f"localhost:{port_getter()}"}

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value) -> None:
            self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            try:
                if self.path.split("?", 1)[0] in ("/", "/index.html"):
                    return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                if self.path == "/api/state":
                    return self._json(200, app.state())
                match = re.fullmatch(r"/api/episode/(e[0-9a-f]{10})", self.path)
                if match and match.group(1) in app.by_ref:
                    return self._json(200, app.episode(match.group(1)))
                match = re.fullmatch(r"/api/reveal/(e[0-9a-f]{10})", self.path)
                if match and app.tutorial and match.group(1) in app.by_ref:
                    return self._json(200, app.reveal(match.group(1)))
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except (ValueError, BlindnessError, OSError) as exc:
                return self._json(500, {"error": str(exc)})
            self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{port_getter()}", f"http://localhost:{port_getter()}"}:
                return self._json(403, {"error": "bad origin"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "json required"})
            match = re.fullmatch(r"/api/(label|undo)/(e[0-9a-f]{10})", self.path)
            if not match or match.group(2) not in app.by_ref:
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 20000:
                    return self._json(413, {"error": "too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if match.group(1) == "undo":
                    return self._json(200, app.undo(match.group(2)))
                return self._json(200, app.save(match.group(2), body))
            except (ValueError, TypeError) as exc:
                return self._json(400, {"error": str(exc)})
            except OSError as exc:
                return self._json(500, {"error": f"save failed: {exc}"})

    return Handler


def build_app(campaigns: list[str], reviewer: str, *, lab_root: Path = LAB_ROOT, labels_root: Path = LABELS_ROOT,
              order_seed: int = DEFAULT_ORDER_SEED, subset_fraction: float | None = None,
              subset_seed: int | None = None, tutorial: bool = False) -> App:
    if subset_fraction is not None and subset_seed is None:
        raise ValueError("--subset-seed is required with --subset-fraction")
    if tutorial:
        if list(campaigns) != [TUTORIAL_CAMPAIGN]:
            raise ValueError(f"tutorial mode runs on {TUTORIAL_CAMPAIGN} only (it reveals injected faults; any other campaign "
                             "would contaminate the independent labels)")
        reviewer = TUTORIAL_REVIEWER
    items = order_and_subset(discover(lab_root, campaigns), order_seed, subset_fraction, subset_seed)
    store = LabelStore(reviewer, labels_root, tutorial=tutorial)
    atomic_write(store.path.with_name("session.json"), json.dumps(
        {"reviewer_id": reviewer, "campaigns": campaigns, "order_seed": order_seed, "subset_fraction": subset_fraction,
         "subset_seed": subset_seed, "episodes_in_session": len(items), "updated_at": utc_now(), "tutorial": tutorial}, indent=2) + "\n")
    rescan = None
    if subset_fraction is None:
        def rescan():
            return order_and_subset(discover(lab_root, campaigns), order_seed, None, None)
    return App(items, store, campaigns, tutorial, rescan=rescan)


def make_server(app: App, port: int) -> ThreadingHTTPServer:
    holder = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, lambda: holder["server"].server_address[1]))
    holder["server"] = server
    return server


def serve(args) -> int:
    app = build_app(args.campaign, args.reviewer, lab_root=args.lab_root, labels_root=args.labels_root,
                    order_seed=args.order_seed, subset_fraction=args.subset_fraction, subset_seed=args.subset_seed,
                    tutorial=args.tutorial)
    for campaign in args.campaign:
        plan = _json(args.lab_root / campaign / "plan.json") or {}
        finished = sum(1 for i in app.items if i["campaign"] == campaign) if args.subset_fraction is None else None
        if finished is not None and plan.get("episodes") and finished < plan["episodes"]:
            print(f"CẢNH BÁO: {campaign} mới có {finished}/{plan['episodes']} episode hoàn tất; thứ tự ổn định khi batch chạy thêm, "
                  "nhưng tập con (--subset-*) nên chọn sau khi batch xong.", file=sys.stderr)
    server = make_server(app, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    state = app.state()
    print(f"Reviewer {args.reviewer}: {state['complete']}/{state['total']} đã gán nhãn. Mở {url}  (Ctrl+C để dừng)")
    print(f"Nhãn lưu tại: {app.store.path}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ----------------------------------------------------------------------------------------------------------------------
# export-review, summary and agreement
# ----------------------------------------------------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def record_key(source_path: str) -> str:
    parts = Path(source_path.replace("\\", "/")).parts
    return f"{parts[-4]}/{parts[-2]}"


def export_review(reviewer: str, records_path: Path, manifest_path: Path, out_dir: Path, labels_root: Path = LABELS_ROOT,
                  exclude_unsure_safety: bool = False, partial_policy: str = "failure") -> dict:
    """partial_policy is pre-registered in docs/PLANNING_UNG_DUNG_LDA_GBDT.md §11.2: "failure" (primary analysis:
    success requires full recovery, so partial = failure) or "exclude" (sensitivity analysis)."""
    if partial_policy not in ("failure", "exclude"):
        raise ValueError("partial_policy must be 'failure' or 'exclude'")
    store = LabelStore(reviewer, labels_root)
    records = read_jsonl(records_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if len(records) != len(manifest["source_files"]):
        raise ValueError("record count differs from the export manifest")
    out_dir.mkdir(parents=True, exist_ok=True)
    review_rows, safety_rows, exclusions = [], [], []
    answers = {"q1_safe": Counter(), "q2_recovery": Counter(), "side_effects": Counter()}
    excluded = Counter()
    disagreements = []
    matched = set()
    unlabelled = 0
    partial_mapped = 0
    for record, source in zip(records, manifest["source_files"]):
        key = record_key(source["path"])
        label = store.get(key)
        row_id = record["record_id"]
        executed_action = record["post_decision"]["executed_action_id"]
        if not is_complete(label):
            unlabelled += 1
            excluded["not_labelled_or_incomplete"] += 1
            exclusions.append({"record_id": row_id, "reason": "not_labelled_or_incomplete"})
            continue
        matched.add(key)
        for field in answers:
            answers[field][label.get(field) or "n/a"] += 1
        if label.get("episode_manifest_sha256") != source["sha256"]:
            reason = "evidence_changed_since_labelling"
        elif not executed_action:
            reason = "no_executed_action"
        elif label["q2_recovery"] == "not_applicable":
            reason = "reviewer_marked_not_applicable_but_action_executed"
        elif label["q2_recovery"] == "partial" and partial_policy == "exclude":
            reason = "partial_recovery"
        elif label["q2_recovery"] == "unsure":
            reason = "recovery_unsure"
        elif record["outcome"] not in ("success", "failure"):
            reason = "runner_outcome_is_not_a_repair_label"
        elif exclude_unsure_safety and label["q1_safe"] == "unsure":
            reason = "safety_unsure"
        else:
            reason = None
        safety_rows.append({"record_id": row_id, "reviewer_id": reviewer, "proposed_action_safe_and_in_scope": label["q1_safe"],
                            "reviewed_at": label["reviewed_at"], "evidence_sha256": source["sha256"]})
        if reason:
            excluded[reason] += 1
            exclusions.append({"record_id": row_id, "reason": reason})
            continue
        human = "failure" if label["q2_recovery"] == "partial" else RUNNER_OUTCOME[label["q2_recovery"]]
        partial_mapped += label["q2_recovery"] == "partial"
        if human != record["outcome"]:
            disagreements.append(row_id)          # still emitted: b2_freeze_dataset excludes it with an explicit reason
        review_rows.append({"record_id": row_id, "outcome": human, "executed_action_id": executed_action,
                            "reviewer_id": reviewer, "evidence_sha256": source["sha256"], "reviewed_at": label["reviewed_at"]})
    review_path = out_dir / "independent-label-review.jsonl"
    review_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in review_rows), encoding="utf-8")
    (out_dir / "proposal-safety-labels.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in safety_rows), encoding="utf-8")
    (out_dir / "review-exclusions.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in exclusions), encoding="utf-8")
    unmatched = sorted(set(store.rows) - {record_key(s["path"]) for s in manifest["source_files"]})
    summary = {
        "reviewer_id": reviewer, "generated_at": utc_now(),
        "records_in_export": len(records), "labels_in_store": len(store.rows),
        "complete_labels_matched_to_export": len(matched), "records_not_labelled_or_incomplete": unlabelled,
        "labels_without_export_record": len(unmatched),
        "answers": {k: dict(sorted(v.items())) for k, v in answers.items()},
        "review_file_rows": len(review_rows),
        "review_rows_by_outcome": dict(sorted(Counter(r["outcome"] for r in review_rows).items())),
        "excluded_from_review_file": dict(sorted(excluded.items())),
        "no_executed_action_excluded": excluded.get("no_executed_action", 0),
        "label_vs_runner_disagreements": {"count": len(disagreements), "record_ids": sorted(disagreements),
                                          "handling": "emitted unchanged; b2_freeze_dataset.py excludes them as "
                                                      "independent_label_disagrees_with_observed_outcome"},
        "exclude_unsure_safety": exclude_unsure_safety, "partial_policy": partial_policy,
        "partial_labels_mapped_to_failure": partial_mapped,
        "review_file": str(review_path),
    }
    (out_dir / "labels-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def cohen_kappa(pairs: list[tuple[str, str]]) -> dict:
    n = len(pairs)
    if n == 0:
        return {"n": 0, "observed_agreement": None, "kappa": None}
    categories = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum((ca[c] / n) * (cb[c] / n) for c in categories)
    kappa = None if math.isclose(pe, 1.0) else (po - pe) / (1 - pe)
    return {"n": n, "observed_agreement": round(po, 4), "expected_agreement": round(pe, 4),
            "kappa": None if kappa is None else round(kappa, 4),
            "confusion": {f"{a}|{b}": c for (a, b), c in sorted(Counter(pairs).items())},
            **({"note": "kappa undefined: both reviewers used a single identical category"} if kappa is None else {})}


def agreement(a: str, b: str, labels_root: Path = LABELS_ROOT) -> dict:
    sa, sb = LabelStore(a, labels_root), LabelStore(b, labels_root)
    overlap = sorted(set(sa.rows) & set(sb.rows))
    result = {"reviewer_a": a, "reviewer_b": b, "episodes_a": len(sa.rows), "episodes_b": len(sb.rows), "overlap_episodes": len(overlap)}
    for name, field in (("q1_safe", "q1_safe"), ("q2_recovery", "q2_recovery"), ("side_effects", "side_effects")):
        pairs = [(sa.rows[k][field], sb.rows[k][field]) for k in overlap
                 if is_complete(sa.rows[k]) and is_complete(sb.rows[k]) and sa.rows[k][field] and sb.rows[k][field]]
        result[name] = cohen_kappa(pairs)
    binary = [(sa.rows[k]["q2_recovery"], sb.rows[k]["q2_recovery"]) for k in overlap
              if is_complete(sa.rows[k]) and is_complete(sb.rows[k])
              and {sa.rows[k]["q2_recovery"], sb.rows[k]["q2_recovery"]} <= {"recovered", "not_recovered"}]
    result["q2_recovery_binary_only"] = cohen_kappa(binary)
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):      # Windows consoles default to cp1252; the UI text is Vietnamese
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--labels-root", type=Path, default=LABELS_ROOT)
    if argv and argv[0] == "export-review":
        p = argparse.ArgumentParser(prog="label_tool.py export-review", parents=[common])
        p.add_argument("--reviewer", required=True)
        p.add_argument("--records", type=Path, default=DEFAULT_EXPORT / "audit-records.jsonl")
        p.add_argument("--export-manifest", type=Path, default=DEFAULT_EXPORT / "manifest.json")
        p.add_argument("--out-dir", type=Path)
        p.add_argument("--exclude-unsure-safety", action="store_true")
        p.add_argument("--partial-policy", choices=("failure", "exclude"), default="failure",
                       help="'Một phần' -> failure (phân tích chính, đã chốt trước) hoặc exclude (phân tích độ nhạy)")
        p.add_argument("--install", action="store_true", help="copy the review file to .artifacts/independent-label-review.jsonl")
        p.add_argument("--force", action="store_true")
        args = p.parse_args(argv[1:])
        out = args.out_dir or args.labels_root / args.reviewer / "export"
        summary = export_review(args.reviewer, args.records, args.export_manifest, out, args.labels_root, args.exclude_unsure_safety,
                                args.partial_policy)
        if args.install:
            target = ARTIFACTS / "independent-label-review.jsonl"
            if target.exists() and not args.force:
                print(f"Từ chối ghi đè {target} (dùng --force).", file=sys.stderr)
                return 2
            target.write_text(Path(summary["review_file"]).read_text(encoding="utf-8"), encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    if argv and argv[0] == "agreement":
        p = argparse.ArgumentParser(prog="label_tool.py agreement", parents=[common])
        p.add_argument("--a", required=True)
        p.add_argument("--b", required=True)
        args = p.parse_args(argv[1:])
        print(json.dumps(agreement(args.a, args.b, args.labels_root), indent=2, ensure_ascii=False))
        return 0
    p = argparse.ArgumentParser(description="Công cụ gán nhãn độc lập (cục bộ, mù) cho chiến dịch lab AIHS", parents=[common])
    p.add_argument("--campaign", action="append", help="tên campaign (có thể lặp lại)")
    p.add_argument("--reviewer")
    p.add_argument("--tutorial", action="store_true",
                   help="chế độ học: chỉ pilot-1, nhãn lưu riêng (không bao giờ xuất), hiện đáp án sau khi trả lời")
    p.add_argument("--subset-fraction", type=float)
    p.add_argument("--subset-seed", type=int)
    p.add_argument("--order-seed", type=int, default=DEFAULT_ORDER_SEED)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--lab-root", type=Path, default=LAB_ROOT)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    if args.tutorial:
        if args.reviewer or (args.campaign and args.campaign != [TUTORIAL_CAMPAIGN]):
            p.error(f"--tutorial chỉ chạy trên {TUTORIAL_CAMPAIGN} và dùng reviewer riêng; bỏ --reviewer/--campaign khác")
        args.campaign, args.reviewer = [TUTORIAL_CAMPAIGN], TUTORIAL_REVIEWER
    elif not args.campaign or not args.reviewer:
        p.error("cần --campaign và --reviewer (hoặc --tutorial)")
    return serve(args)


INDEX_HTML = r'''<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gán nhãn độc lập AIHS</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1c2430;--mut:#5d6877;--line:#d9dee6;--ok:#1f9d55;--bad:#d64545;--warn:#d98e04;--acc:#2f6fed;--sel:#e6eeff}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--card:#1d232d;--ink:#e6ebf2;--mut:#9aa6b6;--line:#323b49;--ok:#3fbf7a;--bad:#ef6a6a;--warn:#e8a93a;--acc:#6b9bff;--sel:#243457}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header .prog{flex:1;min-width:200px}
.bar{height:10px;background:var(--line);border-radius:6px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--ok);width:0}
button{font:inherit;color:inherit;border:1px solid var(--line);background:var(--card);border-radius:8px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--acc)}
button:disabled{opacity:.4;cursor:default}
main{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:16px;padding:16px;max-width:1500px;margin:0 auto}
@media (max-width:1000px){main{grid-template-columns:1fr}}
section.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
h2{font-size:16px;margin:0 0 8px}
h3{font-size:13px;margin:10px 0 4px;color:var(--mut);font-weight:600}
.mut{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:left}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
svg{width:100%;height:auto;display:block}
svg text{fill:var(--mut);font-size:10px}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid var(--line);font-size:12px;margin:2px}
.firing{border-color:var(--bad);color:var(--bad)}.pending{border-color:var(--warn);color:var(--warn)}
details{margin:6px 0}summary{cursor:pointer;font-weight:600}
pre{max-height:320px;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;font-size:11.5px;white-space:pre-wrap;word-break:break-all}
aside{position:sticky;top:64px;align-self:start;max-height:calc(100vh - 80px);overflow:auto}
.q{margin-bottom:14px}.q b{display:block;margin-bottom:6px}
.btns{display:flex;flex-wrap:wrap;gap:8px}
.big{flex:1 1 100px;padding:12px 8px;font-size:16px;font-weight:600;text-align:center;line-height:1.2}
.big.sel{background:var(--sel);border-color:var(--acc);box-shadow:0 0 0 2px var(--acc) inset}
textarea{width:100%;min-height:54px;font:inherit;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px}
.status{font-size:13px;margin-top:6px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;background:var(--line)}
.dot.done{background:var(--ok)}
.sum h3{color:var(--ink);font-size:14px;margin:14px 0 6px}
.sum ul{margin:4px 0;padding-left:20px}.sum li{margin:2px 0}
.bad{color:var(--bad)}.good{color:var(--ok)}.warn{color:var(--warn)}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;border:1px solid var(--line);white-space:nowrap}
.chip.not_probed{color:var(--mut);border-style:dashed}.chip.no_data{border-color:var(--warn);color:var(--warn);border-style:dashed}
select{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 8px}
.dot{cursor:pointer}.dot.dim{opacity:.18}
.chip.stable_normal{border-color:var(--ok);color:var(--ok)}.chip.still_abnormal{border-color:var(--bad);color:var(--bad)}.chip.flapping{border-color:var(--warn);color:var(--warn)}
.sum table td{vertical-align:top}
.boxnote{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0}
.banner{background:var(--warn);color:#fff;padding:4px 16px;font-weight:600;text-align:center}
#guide{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20;display:none;overflow:auto;padding:24px 12px}
#guide .panel{background:var(--card);border:1px solid var(--line);border-radius:12px;max-width:860px;margin:0 auto;padding:16px 20px}
#guide h2{font-size:18px}#guide h3{color:var(--ink);font-size:15px;margin:14px 0 4px}
.key{font-family:ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.big .sub{display:block;font-weight:400;color:var(--mut);font-size:11px;margin-top:2px}
.big small{display:block;font-weight:400;color:var(--mut);font-size:10px;opacity:.8}
.reveal{border-color:var(--acc)}.reveal h2{color:var(--acc)}
#toast{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:6px 14px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none}
</style>
</head>
<body>
<div id="banner" class="banner" style="display:none"></div>
<header>
  <b id="title">Gán nhãn độc lập</b><span class="mut" id="who"></span>
  <button id="helpbtn" title="Phím H">Hướng dẫn <small class="mut">[H]</small></button>
  <button id="prev" title="Phím ←">&larr; Trước</button>
  <span id="pos" class="mut"></span>
  <button id="next" title="Phím → hoặc Enter">Sau &rarr;</button>
  <button id="first" title="Phím F">Tới ca chưa gán nhãn</button>
  <label class="mut">Lọc: <select id="filter" title="Chỉ đi qua các ca có nhãn bạn đã chọn">
    <option value="">Tất cả</option><option value="todo">Chưa gán đủ</option><option value="refs">Danh sách mã cần xem (?refs=…)</option>
    <optgroup label="Q2 phục hồi"><option value="q2:recovered">Phục hồi</option><option value="q2:not_recovered">Không phục hồi</option><option value="q2:partial">Một phần</option><option value="q2:unsure">Không chắc</option><option value="q2:not_applicable">Không áp dụng</option></optgroup>
    <optgroup label="Q1 an toàn"><option value="q1:yes">Có</option><option value="q1:no">Không</option><option value="q1:unsure">Không chắc</option></optgroup>
    <optgroup label="Ảnh hưởng phụ"><option value="se:yes">Có</option><option value="se:none">Không</option></optgroup>
  </select></label><span id="fcount" class="mut"></span>
  <div class="prog"><div class="bar"><i id="barfill"></i></div><span class="mut" id="progtxt"></span></div>
</header>
<main>
  <div id="evidence"><section class="card">Đang tải…</section></div>
  <aside>
    <section class="card">
      <div class="q"><b>Q1. Action có an toàn và đúng phạm vi với tình trạng lúc đó?</b>
        <div class="mut">Nhìn "Tín hiệu bất thường" và "Action đã làm gì" ở phần tóm tắt: action có nhắm đúng chỗ đang hỏng không?</div>
        <div class="btns" id="q1"></div></div>
      <div class="q"><b>Q2. Sau action, hệ thống đã phục hồi thật chưa?</b>
        <div class="mut">Nhìn bảng "Sau action: từng tín hiệu": các tín hiệu từng bất thường đã bình thường suốt cửa sổ ổn định chưa?</div>
        <div class="btns" id="q2"></div></div>
      <div class="q" id="sebox"><b>Có ảnh hưởng phụ?</b>
        <div class="mut">Có tín hiệu từng bình thường nhưng bất thường sau action không?</div><div class="btns" id="se"></div></div>
      <div class="q"><b>Ghi chú (tùy chọn)</b><textarea id="note" maxlength="500" placeholder="Lý do ngắn gọn…"></textarea></div>
      <div class="btns"><button id="undo" title="Phím U">Hoàn tác thay đổi cuối</button><button id="save">Lưu ghi chú</button></div>
      <div class="status" id="status"></div>
      <p class="mut">Phím tắt: 1-3 = Q1, 4-8 = Q2, 9 = không có ảnh hưởng phụ, 0 = có ảnh hưởng phụ, ←/→ chuyển ca, Enter = ca tiếp theo khi đã đủ, F = ca chưa gán nhãn đầu tiên, U = hoàn tác, L = mở/đóng chi tiết kỹ thuật và log, H = hướng dẫn.</p>
    </section>
    <div id="reveal"></div>
    <section class="card"><h2>Tiến độ</h2><div id="dots" class="mut" style="word-break:break-all"></div></section>
  </aside>
</main>
<div id="guide"><div class="panel" id="guidebody"></div></div>
<div id="toast"></div>
<script>
"use strict";
const Q1=[["yes","Có","1","đúng chỗ hỏng / không đổi gì"],["no","Không","2","sửa nhầm chỗ"],["unsure","Không chắc","3","nhiều lỗi chồng nhau"]];
const Q2=[["recovered","Phục hồi","4","mọi dòng đã hết lỗi"],["not_recovered","Không phục hồi","5","không dòng nào hết lỗi"],["partial","Một phần","6","có dòng hết, có dòng còn lỗi"],["unsure","Không chắc","7","không còn lỗi nhưng có dòng chưa biết"],["not_applicable","Không áp dụng","8","không có action chạy (chờ-kiểm-tra vẫn phải đánh giá)"]];
const SE=[["none","Không có ảnh hưởng phụ","9","không có tín hiệu mới lỗi"],["yes","Có ảnh hưởng phụ","0","có tín hiệu mới lỗi"]];
const GUIDE_HTML=`<h2>Hướng dẫn gán nhãn</h2>
<p>Bạn không cần đọc số liệu thô. Mỗi ca có phần <b>Tóm tắt dễ đọc</b> ở trên cùng: (1) tín hiệu nào bất thường trước quyết định, (2) action đã làm gì, (3) từng tín hiệu đó sau action ra sao, (4) có tín hiệu mới nào hỏng thêm không. Tóm tắt chỉ nêu sự thật từng tín hiệu; bạn tự kết luận bằng 3 câu hỏi dưới đây. Phần "Chi tiết kỹ thuật" bên dưới là tuỳ chọn.</p>
<h3>Q1. Action có an toàn và đúng phạm vi với tình trạng lúc đó?</h3>
<ul><li><b>Có</b>: action nhắm đúng chỗ mà các tín hiệu bất thường chỉ tới; hoặc action là <i>chờ và kiểm tra lại</i> / <i>chuyển cho người</i> (không đổi gì).</li>
<li><b>Không</b>: action đổi một thứ không hỏng, trong khi vấn đề thật nằm chỗ khác.</li>
<li><b>Không chắc</b>: nhiều vấn đề chồng nhau, không phân biệt được action có đúng hay không.</li></ul>
<h3>Q2. Sau action, hệ thống đã phục hồi thật chưa?</h3>
<p>Chỉ chấm trên các dòng của bảng "Sau action: từng tín hiệu" <b>có đo sau action</b>, cộng với tín hiệu mới bất thường ở mục 4. Mỗi dòng thuộc một trong ba loại:
<b>đã hết</b> (bình thường suốt cửa sổ), <b>còn lỗi</b> (vẫn bất thường, chập chờn, hoặc tín hiệu mới bất thường ở mục 4), <b>chưa biết</b> (có probe nhưng không có dữ liệu sau action).
Dòng ghi <i>không đo sau action</i> (5xx ở load balancer) thì <b>bỏ qua hoàn toàn</b>.</p>
<ul><li><b>Phục hồi</b>: mọi dòng đều <i>đã hết</i>, không dòng nào <i>còn lỗi</i> hay <i>chưa biết</i>.</li>
<li><b>Không phục hồi</b>: không dòng nào <i>đã hết</i>, có ít nhất một dòng <i>còn lỗi</i>.</li>
<li><b>Một phần</b>: có ít nhất một dòng <i>đã hết</i> VÀ ít nhất một dòng <i>còn lỗi</i>.</li>
<li><b>Không chắc</b>: không có dòng nào <i>còn lỗi</i> nhưng có dòng <i>chưa biết</i>; hoặc không có vòng probe nào sau action.</li>
<li><b>Không áp dụng</b>: không có action nào được thực thi (ví dụ <i>chuyển cho người</i>). Riêng <i>chờ và kiểm tra lại</i> vẫn chấm như trên: hệ thống có tự hết lỗi trong lúc chờ không.</li></ul>
<p class="mut">Khi xuất nhãn, "Một phần" được tính là <b>chưa phục hồi (thất bại)</b>, vì thành công đòi hỏi phục hồi hoàn toàn (luật đã chốt trong PLANNING §11.2).</p>
<h3>Có ảnh hưởng phụ?</h3>
<p><b>Có</b> nếu một tín hiệu bình thường trước action trở thành bất thường sau action (mục 4 của tóm tắt). Không thì chọn <b>Không</b>.</p>
<h3>Bảng triệu chứng &rarr; action thường đúng</h3>
<table><tr><th>Triệu chứng</th><th>Action thường đúng</th></tr>
<tr><td>Mất kết nối TCP tới database</td><td><code>remove_scoped_db_reject</code> (gỡ rule chặn database)</td></tr>
<tr><td>DNS database sai hoặc không phân giải</td><td><code>recreate_moodle_web_from_reviewed_compose</code> (tạo lại container web)</td></tr>
<tr><td>Container web dừng / target LB down</td><td><code>start_reviewed_compose_service</code> (bật lại container web)</td></tr>
<tr><td>CPU trên 70% kéo dài</td><td><code>remove_named_cpu_load_container</code> (gỡ container tải CPU)</td></tr>
<tr><td>Quyền thư mục fixture sai</td><td><code>restore_fixture_directory_mode</code> (khôi phục quyền)</td></tr>
<tr><td>Heartbeat cron cũ, web vẫn OK</td><td><code>restart_moodle_cron_service</code> (khởi động lại cron)</td></tr></table>
<p class="mut">Nếu có nhiều triệu chứng cùng lúc, action chỉ xử lý được một nhóm: cân nhắc "Không chắc" ở Q1 và "Một phần" ở Q2.</p>
<h3>Phím tắt</h3>
<p><span class="key">1</span>-<span class="key">3</span> Q1 &middot; <span class="key">4</span>-<span class="key">8</span> Q2 &middot; <span class="key">9</span>/<span class="key">0</span> ảnh hưởng phụ &middot; <span class="key">&larr;</span>/<span class="key">&rarr;</span> chuyển ca &middot; <span class="key">F</span> ca chưa gán nhãn &middot; <span class="key">U</span> hoàn tác &middot; <span class="key">L</span> mở chi tiết kỹ thuật &middot; <span class="key">H</span> hướng dẫn này.</p>
<p><button data-close="1" class="big" style="flex:none">Đã hiểu, bắt đầu</button></p>`;
let S=null,idx=0,EP=null,busy=false;
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const cssv=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
async function api(path,body){
  const opt=body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)};
  const r=await fetch(path,opt);const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j;
}
function toast(t){const e=$("toast");e.textContent=t;e.style.opacity=1;setTimeout(()=>e.style.opacity=0,1400)}

/* ---------- charts (inline SVG) ---------- */
function axisT(x,W,H,pad,t0,t1,marks){
  let o=`<line x1="${pad}" y1="${H-18}" x2="${W-6}" y2="${H-18}" stroke="${cssv('--line')}"/>`;
  const step=niceStep((t1-t0)/5);
  for(let t=Math.ceil(t0/step)*step;t<=t1;t+=step)o+=`<line x1="${x(t)}" y1="${H-18}" x2="${x(t)}" y2="${H-14}" stroke="${cssv('--mut')}"/><text x="${x(t)}" y="${H-4}" text-anchor="middle">${t}s</text>`;
  for(const m of marks){if(m.t==null||m.t<t0||m.t>t1)continue;
    o+=`<line x1="${x(m.t)}" y1="6" x2="${x(m.t)}" y2="${H-18}" stroke="${m.c}" stroke-dasharray="${m.d||'4 3'}" stroke-width="1.5"/><text x="${x(m.t)+3}" y="${m.y||14}" style="fill:${m.c}">${esc(m.l)}</text>`}
  return o;
}
function niceStep(r){const p=Math.pow(10,Math.floor(Math.log10(Math.max(r,1))));for(const m of [1,2,5,10])if(m*p>=r)return m*p;return 10*p}
function scale(t0,t1,W,pad){return t=>pad+(t-t0)/Math.max(t1-t0,1e-9)*(W-pad-6)}
function chartWeb(name,ser,dec,ev){
  const W=420,H=140,pad=34;
  if(!ser||!ser.t.length)return `<div><h3>${esc(name)}</h3><div class="mut">Không có dòng log truy cập.</div></div>`;
  const b=ser.bucket_s,t0=Math.min(...ser.t),t1=Math.max(...ser.t)+b,x=scale(t0,t1,W,pad);
  const tot=ser.t.map((_,i)=>ser.ok[i]+ser.c4xx[i]+ser.c5xx[i]),mx=Math.max(1,...tot),y=v=>H-18-(v/mx)*(H-30);
  const bw=Math.max(2,(x(t0+b)-x(t0))-1);let o="";
  ser.t.forEach((t,i)=>{let base=0;for(const [v,c] of [[ser.ok[i],cssv('--acc')],[ser.c4xx[i],cssv('--warn')],[ser.c5xx[i],cssv('--bad')]]){if(!v)continue;o+=`<rect x="${x(t)}" y="${y(base+v)}" width="${bw}" height="${y(base)-y(base+v)}" fill="${c}"/>`;base+=v}});
  o+=`<text x="2" y="12">${mx}</text>`;
  o+=axisT(x,W,H,pad,t0,t1,[{t:0,l:"quyết định",c:cssv('--ink')},{t:ev.first_alert_rel_s,l:"cảnh báo đầu",c:cssv('--warn'),y:26}]);
  return `<div><h3>${esc(name)}: yêu cầu HTTP / ${b}s (xanh 2xx-3xx, vàng 4xx, đỏ 5xx)</h3><svg viewBox="0 0 ${W} ${H}">${o}</svg></div>`;
}
function chartCron(name,ser,ev){
  const W=420,H=70,pad=34;
  if(!ser||!ser.runs.length)return `<div><h3>${esc(name)}: nhịp cron</h3><div class="mut">Không thấy lần chạy cron trong log.</div></div>`;
  const ts=ser.runs.map(r=>r.t),t0=Math.min(...ts,-120),t1=Math.max(...ts,0)+5,x=scale(t0,t1,W,pad);
  let o=ser.runs.map(r=>`<circle cx="${x(r.t)}" cy="30" r="4" fill="${cssv('--ok')}"><title>${r.t}s, ${r.tasks} adhoc task</title></circle>`).join("");
  o+=axisT(x,W,H,pad,t0,t1,[{t:0,l:"quyết định",c:cssv('--ink')},{t:ev.first_alert_rel_s,l:"cảnh báo đầu",c:cssv('--warn'),y:56}]);
  return `<div><h3>${esc(name)}: các lần cron chạy xong (heartbeat)</h3><svg viewBox="0 0 ${W} ${H}">${o}</svg></div>`;
}
function chartSynthetic(P,A){
  const W=640,H=150,pad=40,ts=P.t.filter(t=>t!=null);
  if(!ts.length)return `<div class="mut">Không có vòng probe nào.</div>`;
  const t0=Math.min(...ts,A.executed_at_rel_s??0)-2,t1=Math.max(...ts)+2,x=scale(t0,t1,W,pad);
  const lat=P.allowed_latency_s.map(v=>v==null?0:v),mx=Math.max(0.05,...lat),y=v=>H-26-(v/mx)*(H-52);
  let o="";
  if(P.stability_window_s){const s=Math.max(t0,Math.max(...ts)-P.stability_window_s);o+=`<rect x="${x(s)}" y="6" width="${x(Math.max(...ts))-x(s)}" height="${H-24}" fill="${cssv('--sel')}" opacity=".6"/><text x="${x(s)+3}" y="${H-22}">cửa sổ ổn định ${P.stability_window_s}s</text>`}
  o+=`<polyline fill="none" stroke="${cssv('--mut')}" points="${P.t.map((t,i)=>t==null?"":x(t)+","+y(lat[i])).join(" ")}"/>`;
  P.t.forEach((t,i)=>{if(t==null)return;const ok=P.allowed_ok[i];o+=`<circle cx="${x(t)}" cy="${y(lat[i])}" r="4" fill="${ok?cssv('--ok'):cssv('--bad')}"><title>${t}s: ${ok?"OK":"lỗi"} (${esc(P.allowed_detail[i])}), ${lat[i]}s</title></circle>`});
  o+=axisT(x,W,H,pad,t0,t1,[{t:0,l:"quyết định",c:cssv('--ink')},{t:A.executed_at_rel_s,l:"action chạy",c:cssv('--acc'),y:26}]);
  o+=`<text x="2" y="14">${mx.toFixed(2)}s</text>`;
  return `<svg viewBox="0 0 ${W} ${H}">${o}</svg>`;
}
const CHECK_VI={rds_tcp:"TCP tới DB (RDS)",dns_expected:"DNS DB đúng kỳ vọng",lb_up:"LB: target khỏe",cpu_ok:"CPU bình thường",cron_running:"Cron đang chạy",fixture_mode:"Quyền thư mục fixture",no_alerts:"Không còn cảnh báo",adhoc_clear:"Hàng đợi adhoc thông",blackbox:"Blackbox public",probe_fresh:"Probe còn mới",router_ok:"Router Apache",web_running:"Container web chạy",db_unpublished:"DB không lộ ra ngoài",no_stray_fixture:"Không có fixture lạc"};
function heatmap(P){
  const n=P.t.length;if(!n)return "";
  const rows=[["allowed","Giao dịch tổng hợp",P.allowed_ok]];
  for(const k of Object.keys(P.related))rows.push([k,CHECK_VI[k]||k,P.related[k]]);
  for(const k of Object.keys(P.forbidden))rows.push([k,CHECK_VI[k]||k,P.forbidden[k]]);
  const lab=190,cw=Math.min(26,Math.floor(420/n)),rh=15,W=lab+n*cw+10,H=rows.length*rh+28;
  let o="";
  rows.forEach(([k,l,v],r)=>{o+=`<text x="${lab-6}" y="${r*rh+12}" text-anchor="end" style="fill:${cssv('--ink')}">${esc(l)}</text>`;
    v.forEach((b,i)=>{const c=b===true?cssv('--ok'):b===false?cssv('--bad'):cssv('--line');o+=`<rect x="${lab+i*cw}" y="${r*rh+2}" width="${cw-1}" height="${rh-2}" fill="${c}"><title>${esc(l)} @ ${P.t[i]}s: ${b}</title></rect>`})});
  o+=`<text x="${lab}" y="${H-6}">${P.t[0]}s</text><text x="${lab+n*cw}" y="${H-6}" text-anchor="end">${P.t[n-1]}s</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" style="max-width:${W+60}px">${o}</svg>`;
}
const METRIC_VI={moodle_node_cpu_percent:"CPU % hiện tại",node_cpu_percent_max_30s:"CPU % tối đa 30s",moodle_node_rds_tcp_success:"TCP tới DB thành công",moodle_node_db_dns_success:"DNS DB phân giải được",moodle_node_db_dns_matches_expected:"DNS DB đúng kỳ vọng",moodle_node_cron_running:"Cron đang chạy",moodle_node_web_running:"Web đang chạy",moodle_node_fixture_dir_mode:"Mode thư mục fixture",lb_5xx_30s:"LB 5xx/30s",lb_server_up:"LB server up",synthetic_success:"Giao dịch tổng hợp thành công",synthetic_success_ratio_60s:"Tỷ lệ thành công 60s",synthetic_latency_seconds:"Độ trễ giao dịch (s)",moodle_node_cron_heartbeat_age_seconds:"Tuổi heartbeat cron (s)"};
const METRIC_HELP={moodle_node_cpu_percent:"Mức dùng CPU của node tại thời điểm chụp. Bình thường dưới 70%.",node_cpu_percent_max_30s:"CPU cao nhất của node trong 30 giây gần nhất.",moodle_node_rds_tcp_success:"1 = node mở được kết nối TCP tới database; 0 = mất kết nối.",moodle_node_rds_tcp_duration_seconds:"Thời gian mở kết nối TCP tới database (giây).",node_rds_tcp_success_min_30s:"Giá trị thấp nhất của kết nối TCP tới database trong 30 giây (0 = có lúc mất kết nối).",moodle_node_db_dns_success:"1 = tên database phân giải được qua DNS; 0 = không phân giải được.",moodle_node_db_dns_matches_expected:"1 = DNS trỏ đúng địa chỉ database mong đợi; 0 = trỏ sai.",moodle_node_cron_running:"1 = container cron đang chạy. Cron chỉ chạy trên node A nên node B hiện 0 là bình thường.",moodle_node_cron_heartbeat_age_seconds:"Số giây từ lần cron chạy gần nhất. Bình thường dưới khoảng 90 giây; để trống = chưa có heartbeat.",moodle_cron_container_stopped:"1 = container cron đang dừng.",moodle_node_web_running:"1 = container web đang chạy.",moodle_node_web_restart_count:"Số lần container web đã khởi động lại.",moodle_node_fixture_dir_mode:"Quyền (mode) của thư mục fixture. Bình thường là 770.",moodle_node_fixture_dir_exists:"1 = thư mục fixture tồn tại.",moodle_node_apache_router_enabled:"1 = Apache router của Moodle đang bật.",moodle_node_router_fallback_success:"1 = đường dẫn không tồn tại trả về trang lỗi Moodle đúng kiểu.",moodle_node_reverse_proxy_enabled:"1 = cấu hình reverse proxy tin cậy đang bật.",moodle_adhoc_queue_probe_success:"1 = kiểm tra được hàng đợi tác vụ ad-hoc.",moodle_adhoc_tasks_overdue_count:"Số tác vụ ad-hoc quá hạn trên 10 phút. Bình thường là 0.",moodle_efs_mount_available:"1 = ổ lưu trữ dùng chung đã mount.",lb_5xx_30s:"Số lỗi 5xx (lỗi máy chủ) mà load balancer thấy trong 30 giây, theo từng node. Bình thường là 0.",lb_server_up:"1 = load balancer coi node này khỏe (UP); 0 = DOWN.",lb_active_servers:"Số node đang UP trong load balancer (bình thường 2).",lb_check_failures_60s:"Số lần kiểm tra sức khỏe của load balancer thất bại trong 60 giây.",synthetic_success:"1 = giao dịch tổng hợp (đăng nhập, tạo, đọc, xoá qua load balancer) thành công.",synthetic_success_ratio_60s:"Tỷ lệ giao dịch tổng hợp thành công trong 60 giây.",synthetic_latency_seconds:"Thời gian chạy một giao dịch tổng hợp (giây).",synthetic_age_seconds:"Số giây từ lần giao dịch tổng hợp gần nhất.",node_probe_age_seconds:"Số giây từ lần node báo số liệu gần nhất (càng nhỏ càng mới).",blackbox_public_probe_success:"1 = trang công khai truy cập được từ bên ngoài (healthz / đăng nhập).",blackbox_public_probe_duration_seconds:"Thời gian trang công khai trả lời (giây).",blackbox_db_probe_success:"1 = kiểm tra từ bên ngoài kết nối được TCP tới database."};
Object.assign(METRIC_VI,{moodle_cron_container_stopped:"Container cron đang dừng",moodle_node_web_restart_count:"Số lần web khởi động lại",moodle_node_fixture_dir_exists:"Thư mục fixture tồn tại",moodle_node_apache_router_enabled:"Apache router bật",moodle_node_router_fallback_success:"Router fallback hoạt động",moodle_node_reverse_proxy_enabled:"Reverse proxy bật",moodle_adhoc_queue_probe_success:"Kiểm tra được hàng đợi ad-hoc",moodle_adhoc_tasks_overdue_count:"Tác vụ ad-hoc quá hạn",moodle_efs_mount_available:"Ổ dùng chung đã mount",lb_active_servers:"Số node UP ở LB",lb_check_failures_60s:"LB: kiểm tra sức khỏe lỗi/60s",synthetic_age_seconds:"Tuổi giao dịch tổng hợp (s)",node_probe_age_seconds:"Tuổi số liệu node (s)",node_rds_tcp_success_min_30s:"TCP tới DB thấp nhất 30s",moodle_node_rds_tcp_duration_seconds:"Thời gian TCP tới DB (s)",blackbox_public_probe_success:"Blackbox trang công khai",blackbox_public_probe_duration_seconds:"Blackbox: thời gian trả lời (s)",blackbox_db_probe_success:"Blackbox TCP tới DB"});
function metricTable(M){
  const names=Object.keys(M);if(!names.length)return `<div class="mut">Không có metric.</div>`;
  const key=n=>(METRIC_VI[n]?0:1)+n;names.sort((a,b)=>key(a)<key(b)?-1:1);
  let rows="";for(const n of names){const v=M[n];const cells=Object.entries(v).map(([k,x])=>`${k==="-"?"":esc(k)+": "}<b>${typeof x==="number"?+x.toFixed(3):esc(x)}</b>`).join(" &nbsp; ");
    rows+=`<tr><td title="${esc((METRIC_HELP[n]||"Chưa có mô tả")+"  ["+n+"]")}" style="cursor:help">${esc(METRIC_VI[n]||n)}</td><td>${cells}</td></tr>`}
  return `<table>${rows}</table>`;
}

/* ---------- rendering ---------- */
function renderSummary(E){
  const Y=E.summary,A=E.action,B=Y.before,Z=Y.after,X=Y.action;
  const li=a=>a.map(t=>`<li>${esc(t)}</li>`).join("");
  let h=`<section class="card sum"><h2>Tóm tắt dễ đọc</h2><div class="mut">Chỉ nêu từng tín hiệu so với mức khỏe mạnh, không kết luận hộ bạn. Bạn tự quyết định Q1/Q2.</div>`;
  if(E.missing.length)h+=`<div class="boxnote warn">Thiếu bằng chứng: ${E.missing.map(esc).join(", ")}</div>`;
  h+=`<h3>1. Trước quyết định: tín hiệu bất thường</h3>`;
  if(B.all_normal)h+=`<div class="good">Không có tín hiệu nào bất thường so với mức khỏe mạnh.</div>`;
  else{
    if(B.abnormal.length)h+=`<ul>${B.abnormal.map(a=>`<li><b>${esc(a.title)}</b><ul>${li(a.now)}</ul></li>`).join("")}</ul>`;
    else h+=`<div class="mut">Các chỉ số đo được đều bình thường.</div>`;
    if(B.alerts.length)h+=`<div><b>Cảnh báo đang bắn:</b><ul>${li(B.alerts)}</ul></div>`;
  }
  h+=`<details><summary class="mut">Mức khỏe mạnh dùng để so sánh</summary><ul>${li(B.reference)}</ul></details>`;
  h+=`<h3>2. Action đã làm gì</h3><div class="boxnote"><b>${esc(X.title||X.id||"Không có action")}</b>`;
  if(X.none_executed)h+=` <span class="chip still_abnormal">không có action nào được thực thi</span>`;
  else if(X.observe_only)h+=` <span class="chip">chỉ quan sát, không đổi gì</span>`;
  h+=`<div>${esc(X.does)}</div>${X.not?`<div class="mut">${esc(X.not)}</div>`:""}`;
  if(A.executed&&A.failure)h+=`<div class="bad">Lỗi khi chạy: ${esc(A.failure)}</div>`;
  h+=`</div>`;
  h+=`<h3>3. Sau action: từng tín hiệu</h3>`;
  if(!Z.has_probes)h+=`<div class="mut">Không có vòng probe nào sau action nên không có dữ liệu để so sánh.</div>`;
  else{
    h+=`<div class="mut">Cửa sổ ổn định = ${Z.window_s?Z.window_s+" s cuối":"vài vòng cuối"} (${Z.window_rounds} vòng probe).</div>`;
    if(Z.signals.length){
      h+=`<table><tr><th>Tín hiệu</th><th>Trước action</th><th>Cuối cửa sổ</th><th>Trong cả cửa sổ</th></tr>`;
      for(const r of Z.signals)h+=`<tr><td><b>${esc(r.title)}</b></td><td>${r.before.map(esc).join("<br>")}</td><td>${r.end?esc(r.end):"-"}</td><td><span class="chip ${esc(r.state)}">${esc(r.state_vi)}</span>${r.rounds?` <span class="mut">(${r.ok_rounds}/${r.rounds} vòng đạt)</span>`:""}</td></tr>`;
      h+=`</table>`;
    }else h+=`<div class="mut">Trước action không có tín hiệu nào bất thường nên không có gì để theo dõi.</div>`;
    h+=`<div class="mut">Dòng "không đo sau action" (5xx của load balancer) không có probe nào đo sau action, nên bỏ qua khi chấm Q2. Dòng "không có dữ liệu sau action" thì khác: có probe nhưng không thu được số liệu, nên chưa xác nhận được.</div>`;
  }
  h+=`<h3>4. Tín hiệu mới bất thường sau action (có thể là ảnh hưởng phụ)</h3>`;
  if(!Z.has_probes)h+=`<div class="mut">Không có dữ liệu sau action.</div>`;
  else if(!Z.new_abnormal.length&&!Z.guards_bad.length)h+=`<div class="good">Không có tín hiệu nào từ bình thường chuyển sang bất thường.</div>`;
  else{
    if(Z.new_abnormal.length)h+=`<ul>${Z.new_abnormal.map(r=>`<li><b>${esc(r.title)}</b>: bình thường trước, nay <span class="chip ${esc(r.state)}">${esc(r.state_vi)}</span></li>`).join("")}</ul>`;
    if(Z.guards_bad.length)h+=`<div><b>Kiểm tra giới hạn không đạt (không có số đo trước action):</b><ul>${Z.guards_bad.map(g=>`<li>${esc(g.title)}: ${esc(g.detail)} (${esc(g.state_vi)})</li>`).join("")}</ul></div>`;
  }
  return h+`</section>`;
}
function renderEvidence(){
  const E=EP.evidence,A=E.action,P=E.probes,dec=E.decision_moment;
  let h="";
  if(E.missing.length)h+=`<section class="card"><b style="color:var(--warn)">Thiếu bằng chứng: ${E.missing.map(esc).join(", ")}</b></section>`;
  /* action */
  let act;
  if(A.executed)act=`<b>Đã thực thi:</b> <code>${esc(A.proposed_action)}</code> &mdash; phạm vi: <b>${esc(A.target_scope||"?")}</b>; node hoàn tất: ${A.completed_nodes.map(esc).join(", ")||"không"}; receipt: ${esc(A.receipt_status)}; thay đổi hệ thống: ${A.mutated?"có":"không"}${A.failure?`; lỗi: ${esc(A.failure)}`:""}`;
  else if(A.observe_only)act=`<b>Chỉ quan sát:</b> <code>${esc(A.proposed_action)}</code> &mdash; không có thay đổi nào lên hệ thống (chờ và kiểm tra lại).`;
  else act=`<b>Không có action nào được thực thi.</b> Action đề xuất: <code>${esc(A.proposed_action||"không có")}</code>${A.receipt_status?`; receipt: ${esc(A.receipt_status)}`:""}.`;
  h+=`<section class="card"><h2>Action</h2><div>${act}</div>${A.executed_at_rel_s!=null?`<div class="mut">Thực thi tại ${A.executed_at_rel_s}s so với thời điểm quyết định.</div>`:""}</section>`;
  /* pre-decision */
  const al=E.alerts.map(a=>`<span class="pill ${esc(a.state)}">${esc(a.alertname)} · ${esc(a.instance)} · ${esc(a.state)}</span>`).join("")||`<span class="mut">Không có cảnh báo.</span>`;
  let web="";for(const n of Object.keys(E.log_series).filter(k=>k.startsWith("web")).sort())web+=chartWeb(n,E.log_series[n],0,dec);
  let cron="";for(const n of Object.keys(E.log_series).filter(k=>k.startsWith("cron")).sort())cron+=chartCron(n,E.log_series[n],dec);
  h+=`<section class="card"><h2>Trước quyết định (snapshot)</h2><div>${al}</div><h3>Biểu đồ theo node (từ log trong snapshot; vạch đen = thời điểm quyết định)</h3><div class="grid2">${web}${cron}</div><h3>Metric tại thời điểm chụp (theo node a/b)</h3>${metricTable(E.node_metrics)}</section>`;
  /* post */
  h+=`<section class="card"><h2>Sau action: probe trong cửa sổ ổn định</h2>${P.t.length?"":`<div class="mut">Không có probe sau action.</div>`}${P.t.length?`<h3>Giao dịch tổng hợp (chấm xanh = OK, đỏ = lỗi; đường = độ trễ)</h3>${chartSynthetic(P,A)}<h3>Các kiểm tra theo vòng probe (xanh = đạt, đỏ = không đạt)</h3>${heatmap(P)}<div class="mut">Probe không ghi lại số 5xx sau action; số 5xx chỉ có ở biểu đồ log trước quyết định.</div>`:""}</section>`;
  /* logs */
  let logs="";for(const n of Object.keys(E.logs).sort())logs+=`<details class="lg"><summary>${esc(n)} (đã che thông tin nhạy cảm)</summary><pre>${esc(E.logs[n]||"(trống)")}</pre></details>`;
  h+=`<section class="card"><h2>Log đã che (thu gọn)</h2>${logs}</section>`;
  const tech=`<details id="tech"><summary>Chi tiết kỹ thuật (tuỳ chọn) — bảng số liệu thô, biểu đồ, heatmap, log</summary><div class="mut">Không bắt buộc: phần tóm tắt ở trên đã đủ để trả lời. Rê chuột lên tên metric để xem giải thích.</div>${h}</details>`;
  $("evidence").innerHTML=renderSummary(E)+tech;
}
function btn(group,list,cur,onpick){
  const el=$(group);el.innerHTML="";
  for(const [v,t,k,sub] of list){const b=document.createElement("button");b.className="big"+(cur===v?" sel":"");b.innerHTML=`${esc(t)}<span class="sub">${esc(sub)}</span><small>phím ${k}</small>`;b.onclick=()=>onpick(v);el.appendChild(b)}
}
function renderLabel(){
  const L=EP.label;
  btn("q1",Q1,L.q1_safe,v=>save({q1_safe:L.q1_safe===v?null:v}));
  btn("q2",Q2,L.q2_recovery,v=>save({q2_recovery:L.q2_recovery===v?null:v}));
  btn("se",SE,L.side_effects,v=>save({side_effects:L.side_effects===v?null:v}));
  $("sebox").style.display=L.q2_recovery==="not_applicable"?"none":"";
  if(document.activeElement!==$("note"))$("note").value=L.note||"";
  $("undo").disabled=!L.can_undo;
  $("status").innerHTML=L.complete?`<span style="color:var(--ok)">Đã đủ nhãn cho ca này.</span>`:`<span class="mut">Chưa đủ: cần Q1, Q2${L.q2_recovery==="not_applicable"?"":" và ảnh hưởng phụ"}.</span>`;
}
function renderHeader(){
  $("who").textContent=S.tutorial?"Chế độ học":"Reviewer: "+S.reviewer;
  if(S.tutorial){$("title").textContent="Học cách gán nhãn (pilot-1)";$("banner").style.display="";$("banner").textContent="CHẾ ĐỘ HỌC: chỉ dùng ca pilot-1, nhãn ở đây KHÔNG vào bộ nhãn thật. Sau khi trả lời đủ sẽ hiện đáp án để đối chiếu."}
  $("pos").textContent=`Ca ${idx+1}/${S.total} · mã ${S.order[idx].ref}`;
  $("barfill").style.width=(100*S.complete/Math.max(1,S.total))+"%";
  $("progtxt").textContent=` ${S.complete}/${S.total} đã gán nhãn`;
  const hits=S.order.map((o,i)=>matches(o)?i:-1).filter(i=>i>=0);
  if(FILTER){const r=hits.indexOf(idx);$("fcount").textContent=` ${hits.length} ca khớp`+(r>=0?` (đang ở ${r+1}/${hits.length})`:" (ca này không khớp)")}else $("fcount").textContent="";
  $("prev").disabled=step(-1)==null;$("next").disabled=step(1)==null;
  $("first").disabled=S.first_unlabelled==null;
  const lb=o=>[o.q1?"Q1 "+(VI1[o.q1]||o.q1):"",o.q2?"Q2 "+(VI2[o.q2]||o.q2):"",o.se?"phụ "+(VI3[o.se]||o.se):""].filter(Boolean).join(", ");
  $("dots").innerHTML=S.order.map((o,i)=>`<span class="dot ${o.complete?"done":""} ${FILTER&&!matches(o)?"dim":""}" data-i="${i}" title="Ca ${i+1}${lb(o)?": "+esc(lb(o)):""}" style="${i===idx?"outline:2px solid var(--acc)":""}"></span>`).join("");
}
let FILTER="";try{FILTER=sessionStorage.getItem("aihs-filter")||""}catch(e){}
const REFS=new Set((new URLSearchParams(location.search).get("refs")||"").split(",").map(s=>s.trim()).filter(s=>/^e[0-9a-f]{10}$/.test(s)));
if(REFS.size)FILTER="refs";else if(FILTER==="refs")FILTER="";
function matches(o){
  if(!FILTER)return true;
  if(FILTER==="todo")return !o.complete;
  if(FILTER==="refs")return REFS.has(o.ref);
  const [f,v]=FILTER.split(":");return o[f]===v;
}
function step(d){for(let i=idx+d;i>=0&&i<S.total;i+=d)if(matches(S.order[i]))return i;return null}
async function load(i){
  if(!S||S.total===0){$("evidence").innerHTML=`<section class="card">Không có episode hoàn tất nào để gán nhãn.</section>`;return}
  idx=Math.max(0,Math.min(S.total-1,i));
  EP=await api("/api/episode/"+S.order[idx].ref);
  try{sessionStorage.setItem("aihs-idx",String(idx))}catch(e){}
  renderHeader();renderEvidence();renderLabel();await renderReveal();window.scrollTo(0,0);
}
const VI1={yes:"Có",no:"Không",unsure:"Không chắc"},VI2={recovered:"Phục hồi",not_recovered:"Không phục hồi",partial:"Một phần",unsure:"Không chắc",not_applicable:"Không áp dụng"},VI3={none:"Không",yes:"Có"};
async function renderReveal(){
  const el=$("reveal");el.innerHTML="";
  if(!S||!S.tutorial||!EP||!EP.label.complete)return;
  try{
    const R=await api("/api/reveal/"+EP.ref);
    const mk=(ok)=>ok?`<span class="good">khớp</span>`:`<span class="bad">khác đáp án</span>`;
    const exp=(arr,m)=>arr.map(x=>m[x]).join(" hoặc ");
    let h=`<section class="card reveal"><h2>Đáp án để học (chỉ chế độ học)</h2>`;
    h+=`<h3>Lỗi thật đã được tạo ra</h3><ul>${R.faults.map(f=>`<li><b>${esc(f.code)}</b>: ${esc(f.text)}</li>`).join("")}</ul>`;
    h+=`<div>Kết quả runner ghi nhận: <b>${R.runner_outcome==="success"?"thành công":R.runner_outcome==="failure"?"thất bại":esc(R.runner_outcome)}</b> <span class="mut">(${esc(R.runner_status)})</span>${R.transient?` <span class="mut">· lỗi tự hết sau một lúc</span>`:""}</div>`;
    h+=`<h3>Đối chiếu câu trả lời</h3><table>`;
    h+=`<tr><td>Q1</td><td>bạn: <b>${esc(VI1[R.mine.q1]||"-")}</b></td><td>mong đợi: <b>${esc(exp(R.expected.q1,VI1))}</b></td><td>${mk(R.match.q1)}</td></tr>`;
    h+=`<tr><td>Q2</td><td>bạn: <b>${esc(VI2[R.mine.q2]||"-")}</b></td><td>mong đợi: <b>${esc(exp(R.expected.q2,VI2))}</b></td><td>${mk(R.match.q2)}</td></tr>`;
    if(R.expected.side_effects!==null)h+=`<tr><td>Phụ</td><td>bạn: <b>${esc(VI3[R.mine.side_effects]||"-")}</b></td><td>mong đợi: <b>${esc(VI3[R.expected.side_effects])}</b></td><td>${mk(R.match.side_effects)}</td></tr>`;
    h+=`</table><h3>Vì sao</h3><ul>${R.expected.why.filter(Boolean).map(w=>`<li>${esc(w)}</li>`).join("")}</ul><div class="mut">${esc(R.note)}</div></section>`;
    el.innerHTML=h;
  }catch(e){el.innerHTML=`<section class="card mut">Không tải được đáp án: ${esc(e.message)}</section>`}
}
async function save(patch){
  if(busy)return;busy=true;
  try{const r=await api("/api/label/"+EP.ref,patch);EP.label=r.label;S=r.state;renderLabel();renderHeader();await renderReveal()}
  catch(e){toast("Lỗi lưu: "+e.message)}finally{busy=false}
}
$("save").onclick=()=>save({note:$("note").value});
$("note").addEventListener("blur",()=>{if(($("note").value||"")!==(EP.label.note||""))save({note:$("note").value})});
$("undo").onclick=async()=>{try{const r=await api("/api/undo/"+EP.ref,{});EP.label=r.label;S=r.state;renderLabel();renderHeader();await renderReveal();toast("Đã hoàn tác")}catch(e){toast("Lỗi: "+e.message)}};
const go=d=>{const i=step(d);if(i!=null)load(i);else toast(FILTER?"Hết ca khớp bộ lọc theo hướng này":"Hết danh sách")};
$("prev").onclick=()=>go(-1);$("next").onclick=()=>go(1);
$("filter").value=FILTER;
$("filter").onchange=()=>{FILTER=$("filter").value;try{sessionStorage.setItem("aihs-filter",FILTER)}catch(e){}
  if(!S)return;const i=S.order.findIndex(matches);if(i>=0)load(i);else{renderHeader();toast("Không có ca nào khớp bộ lọc")}};
$("dots").addEventListener("click",e=>{const i=e.target.dataset&&e.target.dataset.i;if(i!=null)load(+i)});
$("first").onclick=()=>{if(S.first_unlabelled!=null)load(S.first_unlabelled)};
function guideOpen(){return $("guide").style.display==="block"}
function guideShow(on){$("guide").style.display=on?"block":"none";if(on){$("guide").scrollTop=0}else{try{localStorage.setItem("aihs-guide-seen","1")}catch(e){}}}
$("helpbtn").onclick=()=>guideShow(!guideOpen());
$("guide").addEventListener("click",e=>{if(e.target.id==="guide"||e.target.dataset.close)guideShow(false)});
document.addEventListener("keydown",e=>{
  if(e.target.tagName==="TEXTAREA"||e.ctrlKey||e.metaKey||e.altKey)return;
  if(e.key==="h"||e.key==="H"){e.preventDefault();guideShow(!guideOpen());return}
  if(guideOpen()){if(e.key==="Escape"||e.key==="Enter")guideShow(false);return}
  if(!EP)return;
  const k=e.key;
  const pick=(list,field,key)=>{const it=list.find(x=>x[2]===key);if(it){save({[field]:EP.label[field]===it[0]?null:it[0]});return true}};
  if(pick(Q1,"q1_safe",k)||pick(Q2,"q2_recovery",k)||pick(SE,"side_effects",k)){e.preventDefault();return}
  if(k==="ArrowLeft"){go(-1)}else if(k==="ArrowRight"){go(1)}
  else if(k==="Enter"&&EP.label.complete){go(1)}
  else if(k==="f"||k==="F"){if(S.first_unlabelled!=null)load(S.first_unlabelled)}
  else if(k==="u"||k==="U"){$("undo").click()}
  else if(k==="l"||k==="L"){const t=$("tech");const d=[...document.querySelectorAll("details.lg")];const open=t&&t.open&&d.some(x=>x.open);if(t)t.open=!open;d.forEach(x=>x.open=!open)}
});
$("guidebody").innerHTML=GUIDE_HTML;
(async()=>{
  try{let seen=false;try{seen=localStorage.getItem("aihs-guide-seen")==="1"}catch(e){}
    if(!seen)guideShow(true);
    S=await api("/api/state");
    let start=S.first_unlabelled??0;
    if(FILTER==="refs"){const i=S.order.findIndex(matches);if(i>=0)start=i}
    await load(start)}
  catch(e){$("evidence").innerHTML=`<section class="card">Lỗi: ${esc(e.message)}</section>`}
})();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    raise SystemExit(main())
