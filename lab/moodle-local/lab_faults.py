"""Lab fault injectors, resets and repair primitives for the six lab scenarios.

Semantics follow automation/moodle-fault-inject.sh / moodle-fault-reset.sh: each injector changes exactly one named
fixture or tagged rule, and each reset removes only what its injector created. The repair primitives
(`repair_*`) are what the lab executor runs for the catalog actions; the baseline reset reuses them, like the staging
reset scripts reuse the executor's reconcile actions.
"""
from __future__ import annotations

import time

from lab_env import (APP_NET, CRON, DB, FAULT_CPU_CONTAINER, FIXTURE_DIR, FIXTURE_DIR_IN_VOLUME, MOODLEDATA_VOLUME,
                     NETFAULT_IMAGE, PREFIX, WEB, LabCommandError, compose, container_state, db_app_net_ip, docker, prom_scalar_map)

BG_LOAD = {"a": f"{PREFIX}-bg-load-a", "b": f"{PREFIX}-bg-load-b"}
LAB_SCENARIOS = ("DB-01", "NET-01", "RES-01", "CON-01", "SEC-02", "BIZ-02")
DB01_TAG = "moodle-fault-DB-01"
NET01_TAG = "# moodle-fault-NET-01"
BIZ02_MARKER = "moodle_fault"   # custom-data key of the single queued fixture ad-hoc task


def _iptables(script: str, *, timeout: float = 30) -> str:
    """Run iptables in the Docker VM's network namespace (where DOCKER-USER lives), like the staging host."""
    return docker("run", "--rm", "--net=host", "--cap-add", "NET_ADMIN", NETFAULT_IMAGE, "sh", "-c", script, timeout=timeout).stdout


def _db01_rules() -> list[str]:
    """Tagged DB-01 rules aimed at THIS lab's database address (several labs may share the Docker VM)."""
    ip = db_app_net_ip()
    rules = _iptables("iptables -S DOCKER-USER").splitlines()
    return [r for r in rules if f"--comment {DB01_TAG}" in r and f"-d {ip}/32" in r]


def _volume_sh(script: str) -> None:
    # Stands in for the root-owned EFS mount on the host: the web containers drop every capability.
    docker("run", "--rm", "-v", f"{MOODLEDATA_VOLUME}:/d", "alpine:3.20", "sh", "-c", script, timeout=60)


# ---- injectors (one fault each) -------------------------------------------------------------------------------
def inject(scenario: str) -> None:
    if scenario == "DB-01":
        ip = db_app_net_ip()
        _iptables(f"iptables -C DOCKER-USER -p tcp -d {ip} --dport 5432 -m comment --comment {DB01_TAG} -j REJECT 2>/dev/null "
                  f"|| iptables -I DOCKER-USER 1 -p tcp -d {ip} --dport 5432 -m comment --comment {DB01_TAG} -j REJECT")
        if not _db01_rules():
            raise LabCommandError("DB-01 rule was not installed")
    elif scenario == "NET-01":
        for node in ("a", "b"):
            docker("exec", "-u", "0", WEB[node], "sh", "-c",
                   f"grep -Fq '{NET01_TAG}' /etc/hosts || printf '%s\\n' '127.0.0.1 moodle-db {NET01_TAG}' >> /etc/hosts; grep -Fq '{NET01_TAG}' /etc/hosts")
    elif scenario == "RES-01":
        docker("rm", "-f", FAULT_CPU_CONTAINER, check=False)
        docker("run", "-d", "--name", FAULT_CPU_CONTAINER, "--restart=no", "--cpus=1.8", "--memory=128m", "--pids-limit=64",
               "--label", "lab.node=b", "alpine:3.20", "sh", "-c", "for worker in 1 2; do yes >/dev/null & done; wait")
        if container_state(FAULT_CPU_CONTAINER) != "running":
            raise LabCommandError("RES-01 fixture is not running")
    elif scenario == "CON-01":
        docker("stop", WEB["b"], timeout=60)
    elif scenario == "SEC-02":
        _volume_sh("install -d -o 1000 -g 1999 -m 0770 /d/synthetic-fixtures && chmod 0000 /d/synthetic-fixtures")
    elif scenario == "BIZ-02":
        docker("stop", CRON["a"], timeout=60)
        php = ('define("CLI_SCRIPT", true); require "/var/www/html/config.php";'
               '$class = "\\\\core\\\\task\\\\refresh_mod_calendar_events_task";'
               'if (!class_exists($class)) { fwrite(STDERR, "fixture task class missing\\n"); exit(77); }'
               '$task = new $class(); $task->set_custom_data(["plugins" => ["assign"], "moodle_fault" => "BIZ-02"]);'
               '$task->set_next_run_time(time() - 900); \\core\\task\\manager::queue_adhoc_task($task);')
        docker("exec", "-u", "1000", WEB["a"], "php", "-r", php, timeout=60)
    else:
        raise ValueError(f"no lab injector for {scenario!r}")


# ---- repair primitives (used by the executor for catalog actions and by baseline reset) ---------------------------
def repair_remove_scoped_db_reject() -> None:
    for rule in _db01_rules():
        delete = rule.replace("-A DOCKER-USER", "-D DOCKER-USER", 1)
        _iptables(f"iptables {delete}")
    if _db01_rules():
        raise LabCommandError("DB-01 tagged rule still present")


def repair_recreate_web(node: str) -> None:
    compose("up", "-d", "--wait", "--no-deps", "--force-recreate", f"moodle-web-{node}", timeout=240)


def repair_start_web(node: str) -> None:
    compose("up", "-d", "--wait", "--no-deps", f"moodle-web-{node}", timeout=240)


def repair_remove_cpu_load() -> None:
    if container_state(FAULT_CPU_CONTAINER) is not None:
        docker("rm", "-f", FAULT_CPU_CONTAINER)


def repair_restore_fixture_mode() -> None:
    _volume_sh("install -d -o 1000 -g 1999 -m 0770 /d/synthetic-fixtures && chown 1000:1999 /d/synthetic-fixtures && chmod 0770 /d/synthetic-fixtures")


def repair_start_cron() -> None:
    compose("--profile", "cron", "up", "-d", "--wait", "--no-deps", "moodle-cron-a", timeout=240)


def purge_biz02_fixture_task() -> None:
    """Fallback reset: drop only the queued fixture ad-hoc task if cron did not consume it."""
    docker("exec", DB, "psql", "-U", "moodle_app", "-d", "moodle", "-c",
           f"DELETE FROM mdl_task_adhoc WHERE customdata LIKE '%{BIZ02_MARKER}%BIZ-02%'", check=False)


def reset(scenario: str) -> None:
    """Return one scenario's fixture to baseline (reset semantics of moodle-fault-reset.sh)."""
    if scenario == "DB-01":
        repair_remove_scoped_db_reject()
    elif scenario == "NET-01":
        for node in ("a", "b"):
            repair_recreate_web(node)
    elif scenario == "RES-01":
        repair_remove_cpu_load()
    elif scenario == "CON-01":
        repair_start_web("b")
    elif scenario == "SEC-02":
        repair_restore_fixture_mode()
    elif scenario == "BIZ-02":
        repair_start_cron()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if max(prom_scalar_map('moodle_adhoc_tasks_overdue_count{job="moodle_node"}').values(), default=1) == 0:
                    return
            except Exception:
                pass
            time.sleep(3)
        purge_biz02_fixture_task()
    else:
        raise ValueError(f"no lab reset for {scenario!r}")


def reset_all() -> None:
    """Idempotent whole-lab baseline restore used between episodes. Touches only named fixtures."""
    repair_remove_scoped_db_reject()
    repair_remove_cpu_load()
    stop_background_load()
    repair_restore_fixture_mode()
    if any(_net01_override(n) for n in ("a", "b")):
        for node in ("a", "b"):
            repair_recreate_web(node)
    for node in ("a", "b"):
        if container_state(WEB[node]) != "running":
            repair_start_web(node)
    if container_state(CRON["a"]) != "running":
        repair_start_cron()
    purge_biz02_fixture_task()


def _net01_override(node: str) -> bool:
    if container_state(WEB[node]) != "running":
        return False
    result = docker("exec", "-u", "0", WEB[node], "grep", "-Fq", NET01_TAG, "/etc/hosts", check=False)
    return result.returncode == 0


def fixture_mode() -> str | None:
    if container_state(WEB["a"]) != "running":
        return None
    result = docker("exec", WEB["a"], "stat", "-c", "%a", FIXTURE_DIR, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def direct_fault_markers() -> dict[str, bool]:
    """Runner-side ground-truth checks for baseline verification. These never reach features or snapshots."""
    return {
        "db01_rule": bool(_db01_rules()),
        "net01_override_a": _net01_override("a"),
        "net01_override_b": _net01_override("b"),
        "res01_fixture": container_state(FAULT_CPU_CONTAINER) is not None,
        "con01_web_b_stopped": container_state(WEB["b"]) != "running",
        "sec02_mode_not_770": fixture_mode() != "770",
        "biz02_cron_stopped": container_state(CRON["a"]) != "running",
        "background_load": any(container_state(n) is not None for n in BG_LOAD.values()),
    }


def start_background_load(node: str, cpus: float) -> None:
    """Benign load that stays below the CPU alert threshold; part of the episode variant, not a fault."""
    docker("rm", "-f", BG_LOAD[node], check=False)
    docker("run", "-d", "--name", BG_LOAD[node], "--restart=no", f"--cpus={cpus}", "--memory=64m", "--pids-limit=32",
           "--label", f"lab.node={node}", "alpine:3.20", "sh", "-c", "yes >/dev/null")


def stop_background_load() -> None:
    for name in BG_LOAD.values():
        if container_state(name) is not None:
            docker("rm", "-f", name, check=False)
