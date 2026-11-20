"""Baseline verification and the allowed / forbidden / related probe rounds used after an action.

Mirrors the verifier contract shape (allowed = the service works for users, forbidden = nothing was exposed or
widened, related = neighbouring health). The runner's outcome is `success` only when a stability window of consecutive
rounds passes every probe set; none of this reads ground truth or Gate verdicts.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import lab_faults
from lab_env import CRON, DB, LB_URL, PREFIX, WEB, container_state, docker, firing_alerts, iso_ms, prom_scalar_map, utc_now

SYNTHETIC_SCRIPT = Path(os.environ.get("LAB_SYNTHETIC_SCRIPT", Path(__file__).resolve().parents[2] / "automation/moodle-synthetic-transaction.py"))
ADMIN_PASSWORD = os.environ.get("LAB_ADMIN_PASSWORD", "LabAdmin#2026pw")
PROBE_MAX_AGE = 20.0


def synthetic_transaction() -> dict:
    """One authenticated login/create/read/update/delete transaction through the load balancer (allowed probe)."""
    with tempfile.TemporaryDirectory(prefix="lab-synthetic-") as directory:
        password = Path(directory) / "pw"
        fd = os.open(password, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(ADMIN_PASSWORD)
        try:
            result = subprocess.run(
                [sys.executable, str(SYNTHETIC_SCRIPT), "--url", LB_URL, "--password-file", str(password),
                 "--output-dir", str(Path(directory) / "out"), "--once"],
                capture_output=True, text=True, timeout=60, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": "timeout"}
    line = (result.stdout.strip().splitlines() or ["{}"])[-1]
    import json
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {"ok": False, "detail": "unparseable"}
    return {"ok": bool(data.get("success")), "detail": data.get("stage"), "latency_seconds": data.get("latency_seconds")}


def _metric(expr: str) -> dict[str, float]:
    try:
        return prom_scalar_map(expr)
    except Exception:
        return {}


def related_probe(*, require_no_alerts: bool = True) -> dict:
    """Neighbouring health from fresh metrics: both nodes, LB targets, DB path, DNS, fixture dir, cron, CPU."""
    checks = {}
    age = _metric('time() - moodle_node_probe_timestamp_seconds{job="moodle_node"}')
    checks["probe_fresh"] = len(age) == 2 and all(v <= PROBE_MAX_AGE for v in age.values())
    for name, expr, predicate in (
        ("web_running", 'moodle_node_web_running{job="moodle_node"}', lambda v: v == 1),
        ("rds_tcp", 'moodle_node_rds_tcp_success{job="moodle_node"}', lambda v: v == 1),
        ("dns_expected", 'moodle_node_db_dns_matches_expected{job="moodle_node"}', lambda v: v == 1),
        ("cpu_ok", 'moodle_node_cpu_percent{job="moodle_node"}', lambda v: v < 70),
        ("router_ok", 'moodle_node_router_fallback_success{job="moodle_node"}', lambda v: v == 1),
        ("adhoc_clear", 'moodle_adhoc_tasks_overdue_count{job="moodle_node"}', lambda v: v == 0),
        ("lb_up", 'haproxy_server_status{state="UP"}', lambda v: v == 1),
    ):
        values = _metric(expr) if name != "lb_up" else _lb_servers()
        checks[name] = len(values) >= 2 and all(predicate(v) for v in values.values())
    cron = _metric('moodle_node_cron_running{job="moodle_node"}')
    checks["cron_running"] = cron.get("moodle-app-a") == 1
    mode = _metric('moodle_node_fixture_dir_mode{job="moodle_node"}')
    checks["fixture_mode"] = mode.get("moodle-app-a") == 770
    blackbox = _metric('probe_success{job=~"blackbox_moodle_public|blackbox_moodle_rds"}')
    checks["blackbox"] = bool(blackbox) and all(v == 1 for v in blackbox.values())
    if require_no_alerts:
        try:
            checks["no_alerts"] = not firing_alerts()
        except Exception:
            checks["no_alerts"] = False
    return {"ok": all(checks.values()), "checks": checks}


def _lb_servers() -> dict[str, float]:
    from lab_env import prom_query
    try:
        return {i["metric"].get("server", ""): float(i["value"][1]) for i in prom_query('haproxy_server_status{state="UP"}')}
    except Exception:
        return {}


def forbidden_probe() -> dict:
    """Nothing widened: the database publishes no host port and no unexpected containers appeared."""
    published = docker("port", DB, check=False).stdout.strip()
    names = docker("ps", "-a", "--format", "{{.Names}}", "--filter", "label=com.docker.compose.project", check=False).stdout.split()
    extra = sorted(n for n in names if n.startswith(PREFIX) and "moodle-fault" in n)
    return {"ok": not published and not extra, "checks": {"db_unpublished": not published, "no_stray_fixture": not extra}}


def probe_round() -> dict:
    allowed = synthetic_transaction()
    forbidden = forbidden_probe()
    related = related_probe()
    return {"at": iso_ms(utc_now()), "allowed": allowed, "forbidden": forbidden, "related": related,
            "ok": bool(allowed["ok"]) and forbidden["ok"] and related["ok"]}


def verify_baseline(*, quiet: bool = True) -> dict:
    """Direct (non-Prometheus) fixture checks plus a probe round. The direct markers are runner-side only."""
    markers = lab_faults.direct_fault_markers()
    containers = {
        "db": container_state(DB) == "running", "web_a": container_state(WEB["a"]) == "running",
        "web_b": container_state(WEB["b"]) == "running", "cron_a": container_state(CRON["a"]) == "running",
    }
    clean = not any(markers.values()) and all(containers.values())
    round_ = probe_round() if clean else {"ok": False, "skipped": "fixtures_not_clean"}
    return {"at": iso_ms(utc_now()), "ok": bool(clean and round_["ok"]), "containers": containers,
            "fixture_markers_present": sorted(k for k, v in markers.items() if v), "probe_round": round_}


def ensure_baseline(*, max_wait: float = 240.0, heal: bool = True, log=print) -> dict:
    """Verify baseline, healing named fixtures once if needed; return the last verification."""
    started = time.monotonic()
    deadline = started + max_wait
    healed = False
    last = verify_baseline()
    while not last["ok"] and time.monotonic() < deadline:
        # Give alerts and probe series time to settle before touching fixtures again.
        if heal and not healed and time.monotonic() - started > 40:
            log("baseline not clean; resetting named fixtures")
            try:
                lab_faults.reset_all()
            except Exception as exc:  # keep trying; the next verification reports the real state
                log(f"reset_all error: {type(exc).__name__}: {str(exc)[:200]}")
            healed = True
        time.sleep(6)
        last = verify_baseline()
    return last
