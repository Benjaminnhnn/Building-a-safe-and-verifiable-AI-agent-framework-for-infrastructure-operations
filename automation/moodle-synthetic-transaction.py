#!/usr/bin/env python3
"""Authenticated Moodle synthetic transaction with secret-free evidence."""

from __future__ import annotations

import argparse
import fcntl
import http.cookiejar
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


LOGIN_TOKEN = re.compile(r'name=["\']logintoken["\'][^>]*value=["\']([^"\']+)', re.I)


def private_secret(path: Path) -> str:
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    # Local evidence secrets are 0600. On EC2, the reviewed release path is
    # root:1999 0640 so the root-run probe and UID 1000/GID 1999 container can
    # share the same file without making it world-readable.
    if mode & 0o007 or mode & 0o030 or (mode & 0o040 and metadata.st_uid != 0):
        raise RuntimeError(f"password file permissions are not private: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("password file is empty")
    return value


def request(opener, url: str, data: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    encoded = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={"User-Agent": "moodle-synthetic/1.0", "Accept": "application/json,text/html"},
    )
    try:
        with opener.open(req, timeout=20) as response:
            return response.status, response.read(), response.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.geturl()


def json_request(opener, url: str, data: dict[str, str] | None = None) -> dict:
    status, body, final_url = request(opener, url, data)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"expected JSON from {final_url}, received HTTP {status}") from exc
    if status != 200 or payload.get("status") != "ok":
        reason = payload.get("reason", "unknown")
        raise RuntimeError(f"transaction endpoint returned HTTP {status}: {reason}")
    return payload


def transaction(base_url: str, username: str, password: str) -> dict:
    started = time.monotonic()
    fixture_id = f"probe-{uuid.uuid4().hex}"
    initial_value = f"created-{uuid.uuid4().hex}"
    updated_value = f"updated-{uuid.uuid4().hex}"
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    endpoint = f"{base_url}/synthetic-transaction.php"
    sesskey = None
    stage = "login"
    try:
        status, body, _ = request(opener, f"{base_url}/login/index.php")
        if status != 200:
            raise RuntimeError(f"login page returned HTTP {status}")
        token = LOGIN_TOKEN.search(body.decode("utf-8", errors="replace"))
        if not token:
            raise RuntimeError("Moodle login token was not found")
        status, _, final_url = request(
            opener,
            f"{base_url}/login/index.php",
            {"username": username, "password": password, "logintoken": token.group(1)},
        )
        if status != 200 or "/login/index.php" in final_url:
            raise RuntimeError("Moodle login failed")

        stage = "read_initial"
        state = json_request(opener, f"{endpoint}?{urllib.parse.urlencode({'action': 'read', 'fixture_id': fixture_id})}")
        sesskey = state.get("sesskey")
        if not sesskey or state.get("exists"):
            raise RuntimeError("unexpected initial fixture state")

        stage = "create"
        json_request(opener, endpoint, {"action": "create", "fixture_id": fixture_id, "value": initial_value, "sesskey": sesskey})
        stage = "read_created"
        state = json_request(opener, f"{endpoint}?{urllib.parse.urlencode({'action': 'read', 'fixture_id': fixture_id})}")
        if state.get("value") != initial_value or not state.get("consistent"):
            raise RuntimeError("created fixture did not match in RDS and EFS")

        stage = "update"
        json_request(opener, endpoint, {"action": "update", "fixture_id": fixture_id, "value": updated_value, "sesskey": sesskey})
        stage = "read_updated"
        state = json_request(opener, f"{endpoint}?{urllib.parse.urlencode({'action': 'read', 'fixture_id': fixture_id})}")
        if state.get("value") != updated_value or not state.get("consistent"):
            raise RuntimeError("updated fixture did not match in RDS and EFS")

        stage = "delete"
        json_request(opener, endpoint, {"action": "delete", "fixture_id": fixture_id, "sesskey": sesskey})
        stage = "logout"
        status, _, _ = request(opener, f"{base_url}/login/logout.php?sesskey={urllib.parse.quote(sesskey)}")
        if status != 200:
            raise RuntimeError(f"logout returned HTTP {status}")
        return {"success": True, "stage": "complete", "error": None, "latency_seconds": round(time.monotonic() - started, 6)}
    except Exception as exc:  # evidence must capture the failed stage without a traceback or secret
        if sesskey:
            try:
                json_request(opener, endpoint, {"action": "delete", "fixture_id": fixture_id, "sesskey": sesskey})
                request(opener, f"{base_url}/login/logout.php?sesskey={urllib.parse.quote(sesskey)}")
            except Exception:
                pass
        return {"success": False, "stage": stage, "error": str(exc)[:300], "latency_seconds": round(time.monotonic() - started, 6)}


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def write_metrics(path: Path, state: dict, result: dict) -> None:
    success = 1 if result["success"] else 0
    content = (
        "# HELP moodle_synthetic_success Whether the latest authenticated transaction passed.\n"
        "# TYPE moodle_synthetic_success gauge\n"
        f"moodle_synthetic_success {success}\n"
        "# HELP moodle_synthetic_latency_seconds Latest transaction latency.\n"
        "# TYPE moodle_synthetic_latency_seconds gauge\n"
        f"moodle_synthetic_latency_seconds {result['latency_seconds']}\n"
        "# HELP moodle_synthetic_runs_total Number of authenticated transactions attempted.\n"
        "# TYPE moodle_synthetic_runs_total counter\n"
        f"moodle_synthetic_runs_total {state['total']}\n"
        "# HELP moodle_synthetic_success_total Number of authenticated transactions that passed.\n"
        "# TYPE moodle_synthetic_success_total counter\n"
        f"moodle_synthetic_success_total {state['successful']}\n"
        "# HELP moodle_synthetic_last_run_timestamp_seconds Unix time of the latest attempt.\n"
        "# TYPE moodle_synthetic_last_run_timestamp_seconds gauge\n"
        f"moodle_synthetic_last_run_timestamp_seconds {int(time.time())}\n"
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def run(args: argparse.Namespace) -> int:
    base_url = args.url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise RuntimeError("url must start with http:// or https://")
    password = private_secret(args.password_file)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.output_dir, 0o700)
    lock_path = args.output_dir / ".lock"
    state_path = args.output_dir / "state.json"
    jsonl_path = args.output_dir / "transactions.jsonl"
    started = time.monotonic()
    final_result = {"success": False}

    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            iteration_started = time.monotonic()
            result = transaction(base_url, args.username, password)
            result.update({"observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "url": base_url})
            state = {"total": 0, "successful": 0, "failed": 0, "started_at": result["observed_at"]}
            if state_path.exists():
                state.update(json.loads(state_path.read_text(encoding="utf-8")))
            state["total"] += 1
            state["successful" if result["success"] else "failed"] += 1
            state["last_result"] = result
            state["success_rate"] = round(state["successful"] / state["total"], 6)
            atomic_json(state_path, state)
            with jsonl_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(result, sort_keys=True) + "\n")
            os.chmod(jsonl_path, 0o600)
            if args.metrics_file:
                args.metrics_file.parent.mkdir(parents=True, exist_ok=True)
                write_metrics(args.metrics_file, state, result)
            print(json.dumps(result, sort_keys=True), flush=True)
            final_result = result
            if args.once or time.monotonic() - started >= args.duration_seconds:
                break
            sleep_for = max(0.0, args.interval_seconds - (time.monotonic() - iteration_started))
            time.sleep(sleep_for)
    return 0 if final_result["success"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--metrics-file", type=Path)
    parser.add_argument("--duration-seconds", type=int, default=7200)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.duration_seconds < 1 or args.interval_seconds < 1:
        parser.error("duration and interval must be positive")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args()))
    except BlockingIOError:
        print("another synthetic transaction process is already running", file=sys.stderr)
        raise SystemExit(75)
    except Exception as exc:
        print(f"synthetic transaction setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
