"""Shared helpers for the local Moodle lab: docker/compose wrappers, Prometheus queries, time and hashing."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT = os.environ.get("LAB_PROJECT", "aihs-lab")
COMPOSE_FILE = os.environ.get("LAB_COMPOSE_FILE", str(Path(__file__).resolve().parent / "docker-compose.yml"))
PROM_URL = os.environ.get("LAB_PROM_URL", "http://prometheus:9090")
LB_URL = os.environ.get("LAB_LB_URL", "http://moodle-lab:8080")
OUT_DIR = Path(os.environ.get("LAB_OUT_DIR", str(Path(__file__).resolve().parents[2] / "evaluation/aihs/.artifacts/lab-campaign")))
MOODLEDATA_VOLUME = f"{PROJECT}_moodledata"
APP_NET = f"{PROJECT}_app-net"
NETFAULT_IMAGE = "aihs-lab/netfault:lab"
PREFIX = os.environ.get("LAB_PREFIX", "lab")
WEB = {"a": f"{PREFIX}-moodle-web-a", "b": f"{PREFIX}-moodle-web-b"}
CRON = {"a": f"{PREFIX}-moodle-cron-a"}
DB = f"{PREFIX}-moodle-db"
LB = f"{PREFIX}-lb"
FAULT_CPU_CONTAINER = f"{PREFIX}-moodle-fault-res-01"   # staging fixture name: moodle-fault-res-01
FIXTURE_DIR = "/var/moodledata/synthetic-fixtures"
FIXTURE_DIR_IN_VOLUME = "/d/synthetic-fixtures"


class LabCommandError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def run(args: list[str], *, timeout: float = 60, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin, check=False)
    except subprocess.TimeoutExpired as exc:
        raise LabCommandError(f"timeout after {timeout}s: {' '.join(args[:4])}") from exc
    if check and result.returncode != 0:
        raise LabCommandError(f"exit {result.returncode}: {' '.join(args[:5])}: {(result.stderr or result.stdout)[-300:]}")
    return result


def docker(*args: str, timeout: float = 60, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    return run(["docker", *args], timeout=timeout, check=check, stdin=stdin)


def compose(*args: str, timeout: float = 180, check: bool = True) -> subprocess.CompletedProcess:
    return docker("compose", "-p", PROJECT, "-f", COMPOSE_FILE, *args, timeout=timeout, check=check)


def container_state(name: str) -> str | None:
    """Return 'running', 'stopped' or None when the container does not exist."""
    result = docker("inspect", "--format", "{{.State.Running}}", name, check=False, timeout=15)
    if result.returncode != 0:
        return None
    return "running" if result.stdout.strip() == "true" else "stopped"


def db_app_net_ip() -> str:
    out = docker("inspect", "--format", '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % APP_NET, DB, timeout=15).stdout.strip()
    if not out:
        raise LabCommandError("database container has no app-net address")
    return out


def prom_query(expr: str, *, timeout: float = 5.0) -> list[dict]:
    url = f"{PROM_URL}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read())
    if payload.get("status") != "success":
        raise LabCommandError("prometheus query failed")
    return payload["data"]["result"]


def prom_scalar_map(expr: str) -> dict[str, float]:
    """Return {instance-or-empty: value} for an instant vector."""
    out = {}
    for item in prom_query(expr):
        label = item["metric"].get("instance", "")
        try:
            out[label] = float(item["value"][1])
        except (ValueError, KeyError, IndexError):
            continue
    return out


def firing_alerts() -> list[dict]:
    """Currently firing alerts (alertname + instance), without any scenario labels."""
    result = []
    for item in prom_query('ALERTS{alertstate="firing"}'):
        metric = item["metric"]
        result.append({"alertname": metric.get("alertname", ""), "instance": metric.get("instance", "")})
    return sorted(result, key=lambda a: (a["alertname"], a["instance"]))


def wait_until(predicate, *, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
