#!/usr/bin/env python3
"""Per-node read-only probe for the local Moodle lab (port of ansible/files/moodle-monitoring/moodle-node-probes.sh).

Publishes the same metric names as staging for the signals the lab can reproduce. Differences, all documented in
lab.sh: no node_exporter (CPU comes from `docker stats`, summed over the containers of this node and scaled to
LAB_NODE_VCPUS), and the staging `moodle_fault_*` ground-truth marker series are NOT exported, because they name the
injected scenario and must never reach a model feature.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = os.environ.get("LAB_NODE", "a")
PROJECT = os.environ.get("LAB_PROJECT", "aihs-lab")
PREFIX = os.environ.get("LAB_PREFIX", "lab")
VCPUS = float(os.environ.get("LAB_NODE_VCPUS", "2"))
INTERVAL = float(os.environ.get("LAB_PROBE_INTERVAL", "5"))
PORT = int(os.environ.get("LAB_PROBE_PORT", "9101"))
WEB = f"{PREFIX}-moodle-web-{NODE}"
CRON = f"{PREFIX}-moodle-cron-a" if NODE == "a" else None
FIXTURE_DIR = "/var/moodledata/synthetic-fixtures"
# Containers whose CPU counts toward this node (the RES-01 fixture lives on node B only, as in staging).
NODE_CONTAINERS = {"a": {f"{PREFIX}-moodle-web-a", f"{PREFIX}-moodle-cron-a", f"{PREFIX}-bg-load-a"},
                   "b": {f"{PREFIX}-moodle-web-b", f"{PREFIX}-moodle-fault-res-01", f"{PREFIX}-bg-load-b"}}[NODE]
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

_state = {"text": "# not ready\n", "cpu": None}
_lock = threading.Lock()


def docker(*args: str, timeout: float = 8.0) -> str | None:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def exec_in(container: str, *cmd: str, user: str | None = None, timeout: float = 8.0) -> str | None:
    base = ["exec"] + (["-u", user] if user else []) + [container, *cmd]
    return docker(*base, timeout=timeout)


def inspect(container: str, fmt: str) -> str | None:
    out = docker("inspect", "--format", fmt, container, timeout=5)
    return out.strip() if out else None


def cpu_loop() -> None:
    while True:
        out = docker("stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}", timeout=20)
        if out is not None:
            total = 0.0
            for line in out.splitlines():
                name, _, value = line.partition(" ")
                if name in NODE_CONTAINERS and value.strip().endswith("%"):
                    try:
                        total += float(value.strip().rstrip("%"))
                    except ValueError:
                        pass
            with _lock:
                _state["cpu"] = min(100.0, total / VCPUS)
        time.sleep(1)


def collect() -> dict:
    m: dict = {}
    running = inspect(WEB, "{{.State.Running}}")
    m["moodle_node_web_running"] = 1 if running == "true" else 0
    restarts = inspect(WEB, "{{.RestartCount}}")
    m["moodle_node_web_restart_count"] = int(restarts) if restarts and restarts.isdigit() else 0
    if CRON:
        cron_state = inspect(CRON, "{{.State.Running}}")
        m["moodle_node_cron_running"] = 1 if cron_state == "true" else 0
        m["moodle_cron_container_stopped"] = 1 if cron_state == "false" else 0
    else:
        m["moodle_node_cron_running"] = 0
        m["moodle_cron_container_stopped"] = 0
    for name in ("moodle_node_apache_router_enabled", "moodle_node_router_fallback_success",
                 "moodle_node_reverse_proxy_enabled", "moodle_node_rds_tcp_success", "moodle_node_db_dns_success",
                 "moodle_node_db_dns_matches_expected", "moodle_adhoc_queue_probe_success",
                 "moodle_efs_mount_available"):
        m[name] = 0
    m["moodle_node_rds_tcp_duration_seconds"] = 0
    if m["moodle_node_web_running"] == 1:
        expected = inspect(f"{PREFIX}-moodle-db", '{{(index .NetworkSettings.Networks "%s_app-net").IPAddress}}' % PROJECT)

        def router() -> None:
            m["moodle_node_apache_router_enabled"] = int(
                exec_in(WEB, "test", "-e", "/etc/apache2/conf-enabled/moodle-router.conf") is not None)

        def fallback() -> None:
            body = exec_in(WEB, "curl", "--max-time", "5", "--silent",
                           "http://127.0.0.1:8080/__moodle_router_probe__/missing")
            m["moodle_node_router_fallback_success"] = int(
                bool(body) and "The requested resource could not be found. Please verify the URI" in body)

        def proxy() -> None:
            m["moodle_node_reverse_proxy_enabled"] = int(
                exec_in(WEB, "sh", "-c", 'test "$MOODLE_REVERSE_PROXY" = true') is not None)

        def tcp() -> None:
            out = exec_in(WEB, "php", "-r",
                          '$h=getenv("MOODLE_DB_HOST");$s=microtime(true);$k=@fsockopen($h,5432,$e,$r,3);'
                          'if($k===false)exit(1);fclose($k);printf("%.6f\\n",microtime(true)-$s);', timeout=9)
            if out and re.fullmatch(r"\d+\.\d{6}\s*", out):
                m["moodle_node_rds_tcp_success"] = 1
                m["moodle_node_rds_tcp_duration_seconds"] = float(out)

        def dns() -> None:
            out = exec_in(WEB, "php", "-r", 'echo gethostbyname(getenv("MOODLE_DB_HOST"));')
            answer = out.strip() if out else ""
            if IPV4.fullmatch(answer):
                m["moodle_node_db_dns_success"] = 1
                m["moodle_node_db_dns_matches_expected"] = int(bool(expected) and answer == expected)

        def adhoc() -> None:
            out = exec_in(WEB, "php", "-r",
                          'define("CLI_SCRIPT", true);require "/var/www/html/config.php";'
                          'echo (int)$DB->count_records_select("task_adhoc","nextruntime < :c",["c"=>time()-600])," ",'
                          '(int)$DB->get_field("config","value",["name"=>"lastcronstart"]);', timeout=10)
            parts = out.split() if out else []
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                m["moodle_adhoc_queue_probe_success"] = 1
                m["moodle_adhoc_tasks_overdue_count"] = int(parts[0])
                last = int(parts[1])
                m["moodle_node_cron_heartbeat_age_seconds"] = max(0, int(time.time()) - last) if last else None

        def fixture() -> None:
            mode = exec_in(WEB, "stat", "-c", "%a", FIXTURE_DIR)
            m["moodle_node_fixture_dir_exists"] = int(mode is not None)
            if mode and mode.strip().isdigit():
                m["moodle_node_fixture_dir_mode"] = int(mode.strip())
            writable = exec_in(WEB, "test", "-w", "/var/moodledata")
            m["moodle_efs_mount_available"] = int(writable is not None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in [pool.submit(f) for f in (router, fallback, proxy, tcp, dns, adhoc, fixture)]:
                future.result()
    with _lock:
        cpu = _state["cpu"]
    m["moodle_node_cpu_percent"] = round(cpu, 2) if cpu is not None else None
    m["moodle_node_probe_timestamp_seconds"] = int(time.time())
    return m


def render(m: dict) -> str:
    return "".join(f"# TYPE {k} gauge\n{k} {v}\n" for k, v in sorted(m.items()) if v is not None)


def probe_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            text = render(collect())
            with _lock:
                _state["text"] = text
        except Exception as exc:  # keep serving the previous sample; staleness alerts cover a stuck probe
            print(f"probe iteration failed: {type(exc).__name__}", flush=True)
        time.sleep(max(0.5, INTERVAL - (time.monotonic() - started)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        with _lock:
            body = _state["text"].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


if __name__ == "__main__":
    threading.Thread(target=cpu_loop, daemon=True).start()
    threading.Thread(target=probe_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
