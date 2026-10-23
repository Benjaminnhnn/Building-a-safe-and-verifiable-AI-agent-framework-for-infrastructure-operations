#!/usr/bin/env python3
"""Read-only live board for the CON-01 and SSO-13 demo scenarios.

Every few seconds it asks monitor-ai-01 (one reused SSH connection) for the
Prometheus alerts and probe values, the Agent's recent decisions, the live
switches and the executor's execution count, and reads the drill's local
evidence files. It then redraws one board from fault to verifier.

It never writes to staging: Prometheus, the Agent health endpoint and the
executor health endpoint are read with GET, and both SQLite databases are
opened with mode=ro. Secrets, signatures and keys are never printed.

Run it in its own terminal from the repository root, before starting the
scenario:

    python3 automation/moodle-demo-watch.py CON-01
    python3 automation/moodle-demo-watch.py SSO-13
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SSH_CONFIG = REPO_ROOT / "terraform" / ".artifacts" / "moodle-ssh.config"
EVIDENCE_DIR = REPO_ROOT / "terraform" / ".artifacts" / "moodle-safe-executor"
# macOS $TMPDIR is too long for a Unix socket path; %C is a short hash of the connection.
CONTROL_PATH = "/tmp/mdw-%C"

SCENARIOS = {
    "CON-01": {
        "alert": "MoodleWebContainerMissing",
        "cause": "web_container_not_running",
        "title": "CON-01 · web container trên node B bị dừng",
    },
    "SSO-13": {
        "alert": "MoodleSsoLoginOptionMissing",
        "cause": "sso_login_option_missing",
        "title": "SSO-13 · issuer Okta trong Moodle bị tắt",
    },
}

# Runs on monitor-ai-01 as root. Read-only: GET requests and mode=ro SQLite.
REMOTE_COLLECTOR = r'''
import json, sqlite3, subprocess, sys, urllib.parse, urllib.request
since = sys.argv[1]
out = {}
def get(url):
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"_error": type(exc).__name__}
alerts = get("http://127.0.0.1:9090/api/v1/alerts")
out["alerts"] = [
    {"name": a["labels"].get("alertname"), "state": a.get("state"),
     "instance": a["labels"].get("instance", ""), "active_at": a.get("activeAt")}
    for a in alerts.get("data", {}).get("alerts", [])
] if "_error" not in alerts else alerts
def query(expr):
    r = get("http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
    rows = r.get("data", {}).get("result", [])
    return {row["metric"].get("instance", row["metric"].get("job", "-")): float(row["value"][1]) for row in rows}
out["web_running"] = query('moodle_node_web_running{job="moodle_node"}')
out["sso_probe"] = query('probe_success{job="blackbox_moodle_sso"}')
out["login_probe"] = query('probe_success{job="blackbox_moodle_public",probe="login"}')
agent = get("http://127.0.0.1:8000/health")
executor = get("http://172.17.0.1:8765/healthz")
out["agent_live"] = agent.get("safe_action_live_enabled")
out["executor_live"] = executor.get("execution_enabled")
try:
    db = sqlite3.connect("file:/var/lib/aiops-executor/api/executions.sqlite3?mode=ro", uri=True)
    out["executions"] = db.execute("select count(*) from executions").fetchone()[0]
except Exception as exc:
    out["executions"] = None
decisions_py = """
import json, sqlite3, sys
db = sqlite3.connect('file:/app/data/evidence.db?mode=ro', uri=True)
rows = db.execute(
    "select evidence_id, incident_id, collected_at, json_extract(metadata_json,'$.decision_kind'),"
    " json_extract(metadata_json,'$.decision_json') from evidence"
    " where source='ai_decision' and collected_at >= ? order by collected_at", (sys.argv[1],)).fetchall()
keep = ('scenario_id','catalog_action_id','target_scope','cause_code','disposition','status','confidence','hypothesis')
res = []
for eid, inc, at, kind, payload in rows:
    d = json.loads(payload or '{}')
    item = {k: d.get(k) for k in keep if d.get(k) is not None}
    item.update(id=eid, incident=inc, at=at, kind=kind, refs=len(d.get('evidence_refs') or []))
    res.append(item)
print(json.dumps(res))
"""
try:
    proc = subprocess.run(["docker", "exec", "-i", "moodle-ai-agent", "python", "-", since],
                          input=decisions_py, capture_output=True, text=True, timeout=10)
    out["decisions"] = json.loads(proc.stdout or "[]")
except Exception as exc:
    out["decisions"] = {"_error": type(exc).__name__}
print(json.dumps(out))
'''

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"
WAIT, RUN, DONE, FAIL = "wait", "run", "done", "fail"
ICON = {WAIT: f"{DIM}○{RESET}", RUN: f"{YELLOW}◐{RESET}", DONE: f"{GREEN}●{RESET}", FAIL: f"{RED}✕{RESET}"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hhmmss(value: str | None) -> str:
    if not value:
        return "--:--:--"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%H:%M:%S")
    except ValueError:
        return value[11:19]


def collect_remote(since_iso: str) -> dict:
    command = [
        "ssh", "-F", str(SSH_CONFIG), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "ControlMaster=auto", "-o", f"ControlPath={CONTROL_PATH}", "-o", "ControlPersist=120",
        "-o", "LogLevel=ERROR", "monitor-ai-01", f"sudo python3 - {since_iso}",
    ]
    try:
        proc = subprocess.run(command, input=REMOTE_COLLECTOR, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return {"_error": "ssh timeout"}
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.splitlines() if "post-quantum" not in line and line.strip()]
        return {"_error": (lines[-1] if lines else f"ssh exit {proc.returncode}")[:160]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"_error": "unreadable collector output"}


def latest_evidence(scenario: str, started: float) -> dict[str, dict]:
    """Newest local drill files of this run, without signatures or keys."""
    found: dict[str, dict] = {}
    if not EVIDENCE_DIR.is_dir():
        return found
    for suffix in ("prepared", "gate-approval", "executor-approval", "execute-result", "verifier",
                   "runtime-verification.json.status"):
        name_suffix = f"-{suffix}.json" if not suffix.endswith(".status") else f"-{suffix}.json"
        candidates = [
            path for path in EVIDENCE_DIR.glob(f"{scenario}-*{name_suffix}")
            if path.stat().st_mtime >= started
        ]
        if not candidates:
            continue
        path = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data["_mtime"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%H:%M:%S")
        found[suffix] = data
    return found


class Board:
    def __init__(self, scenario: str, since: datetime | None = None, baseline_exec: int | None = None) -> None:
        self.scenario = scenario
        self.spec = SCENARIOS[scenario]
        start = since or utc_now()
        self.started = start.timestamp()
        self.since_iso = start.strftime("%Y-%m-%dT%H:%M:%S")
        self.first_seen: dict[str, str] = {}
        self.baseline_exec = baseline_exec
        self.sso_ok_streak: list[str] = []

    def mark(self, key: str) -> str:
        return self.first_seen.setdefault(key, utc_now().strftime("%H:%M:%S"))

    def rows(self, remote: dict, files: dict) -> list[tuple[str, str, str, str]]:
        alert_name, cause = self.spec["alert"], self.spec["cause"]
        alerts = remote.get("alerts") if isinstance(remote.get("alerts"), list) else []
        mine = [a for a in alerts if a["name"] == alert_name]
        others = sorted({a["name"] for a in alerts if a["name"] != alert_name and a["state"] == "firing"})
        decisions = remote.get("decisions") if isinstance(remote.get("decisions"), list) else []
        executions = remote.get("executions")
        if self.baseline_exec is None and isinstance(executions, int):
            self.baseline_exec = executions
        rows: list[tuple[str, str, str, str]] = []

        agent_live, exec_live = remote.get("agent_live"), remote.get("executor_live")

        def word(value):
            return "?" if value is None else ("BẬT" if value else "tắt")

        if agent_live is None or exec_live is None:
            switch_state = WAIT
        else:
            switch_state = DONE if not agent_live and not exec_live else RUN
        rows.append(("0", "Công tắc live", switch_state,
                     f"Agent={word(agent_live)} · Executor={word(exec_live)}"
                     + (" · đang mở cửa sổ live" if switch_state == RUN else "")))

        if self.scenario == "CON-01":
            b_running = remote.get("web_running", {}).get("moodle-app-b")
            faulted = b_running == 0.0
            if faulted:
                self.mark("fault")
            fault_state = RUN if faulted else (DONE if "fault" in self.first_seen else WAIT)
            rows.append(("①", "Lỗi: web node B", fault_state,
                         f"web_running B={b_running} A={remote.get('web_running', {}).get('moodle-app-a')}"
                         + (f" · thấy lúc {self.first_seen['fault']}" if "fault" in self.first_seen else "")))
        else:
            sso, page = remote.get("sso_probe", {}), remote.get("login_probe", {})
            sso_v = next(iter(sso.values()), None)
            page_v = next(iter(page.values()), None)
            faulted = sso_v == 0.0 and page_v == 1.0
            if faulted:
                self.mark("fault")
            fault_state = RUN if faulted else (DONE if "fault" in self.first_seen else WAIT)
            rows.append(("①", "Lỗi: nút Okta biến mất", fault_state,
                         f"probe SSO={sso_v} · trang login={page_v}"
                         + (f" · thấy lúc {self.first_seen['fault']}" if "fault" in self.first_seen else "")))

        if mine:
            a = mine[0]
            self.mark("alert_" + a["state"])
            state = DONE if a["state"] == "firing" else RUN
            detail = f"{a['state']} từ {hhmmss(a['active_at'])} {a['instance']}"
        elif "alert_firing" in self.first_seen:
            state, detail = DONE, f"đã hết firing (firing lúc {self.first_seen['alert_firing']})"
        else:
            state, detail = WAIT, "chưa có"
        if others:
            detail += f" · alert khác: {', '.join(others)}"
        rows.append(("②", f"Alert {alert_name}", state, detail))

        relevant = [d for d in decisions if d.get("cause_code") == cause or d.get("scenario_id") == self.scenario]
        diag = next((d for d in reversed(relevant) if d["kind"] == "diagnosis"), None)
        cand = next((d for d in reversed(relevant) if d["kind"] == "remediation_candidate"), None)
        if diag or cand:
            parts = []
            if diag:
                parts.append(f"chẩn đoán {diag.get('status')} conf={diag.get('confidence')} → {diag.get('disposition')}")
            if cand:
                parts.append(f"candidate {cand['id'][:14]}… {cand.get('catalog_action_id')} scope={cand.get('target_scope')} evidence={cand['refs']}")
            elif self.scenario == "SSO-13" and diag:
                parts.append("không có candidate (chỉ người được sửa)")
            rows.append(("③", "AI phân tích", DONE, f"{hhmmss((cand or diag)['at'])} " + " · ".join(parts)))
        else:
            rows.append(("③", "AI phân tích", WAIT, "chưa có quyết định mới"))

        if self.scenario == "CON-01":
            rows.extend(self._con01_gate_to_verifier(files, executions))
        else:
            rows.extend(self._sso13_gate_to_verifier(remote, executions))
        return rows

    def _con01_gate_to_verifier(self, files: dict, executions) -> list[tuple[str, str, str, str]]:
        rows = []
        prep = files.get("prepared")
        if prep:
            rows.append(("④a", "Gate: yêu cầu phê duyệt", DONE,
                         f"{prep['_mtime']} {prep.get('status')} · execution_permitted={prep.get('execution_permitted')}"
                         f" · gate hash {str(prep.get('gate_action_sha256'))[:12]}…"))
        else:
            rows.append(("④a", "Gate: yêu cầu phê duyệt", WAIT, "chưa prepare"))
        gate, op = files.get("gate-approval"), files.get("executor-approval")
        if gate and op:
            op_actor = (op.get("approval") or {}).get("actor_id")
            bound = prep and gate.get("action_sha256") == prep.get("gate_action_sha256")
            rows.append(("④b", "Gate: hai chữ ký", DONE if bound else FAIL,
                         f"{gate['_mtime']} {gate.get('actor_id')} + {op_actor}"
                         f" · hết hạn {hhmmss(gate.get('expires_at'))} · khớp hash={'có' if bound else 'KHÔNG'}"))
        else:
            rows.append(("④b", "Gate: hai chữ ký", WAIT, "chưa ký"))
        result = files.get("execute-result")
        delta = (executions - self.baseline_exec) if isinstance(executions, int) and self.baseline_exec is not None else None
        if result:
            r = result.get("result") or {}
            ok = r.get("status") == "executed" and r.get("mutated") is True
            rows.append(("⑤", "Executor thực thi", DONE if ok else FAIL,
                         f"{result['_mtime']} {r.get('status')} · mutated={r.get('mutated')} · node={r.get('completed_nodes')}"
                         f" · executions +{delta}"))
        else:
            rows.append(("⑤", "Executor thực thi", WAIT, f"chưa · executions +{delta}"))
        verifier = files.get("verifier")
        if verifier:
            samples = verifier.get("probe_samples") or []
            passed = sum(all(s["checks"].values()) for s in samples)
            ok = verifier.get("resolution_eligible") is True
            rows.append(("⑥a", "Verifier lấy mẫu", DONE if ok else FAIL,
                         f"{verifier['_mtime']} {passed}/{len(samples)} mẫu đạt · {verifier.get('verdict')}"
                         + ("" if ok else self._failing_probes(samples))))
        elif result:
            rows.append(("⑥a", "Verifier lấy mẫu", RUN, "đang chạy cửa sổ ổn định 120 s (5 mẫu × 30 s)"))
        else:
            rows.append(("⑥a", "Verifier lấy mẫu", WAIT, "chưa"))
        status = files.get("runtime-verification.json.status")
        if status:
            ok = status.get("status") == "resolved"
            rows.append(("⑥b", "Incident RESOLVED", DONE if ok else FAIL,
                         f"{status['_mtime']} {status.get('status')} · event {str(status.get('verification_event_hash'))[:12]}…"))
        else:
            rows.append(("⑥b", "Incident RESOLVED", WAIT, "chỉ Verifier mới chuyển được"))
        return rows

    @staticmethod
    def _failing_probes(samples: list[dict]) -> str:
        bad = sorted({name for s in samples for name, ok in s["checks"].items() if not ok})
        return f" · probe hỏng: {', '.join(bad)}" if bad else ""

    def _sso13_gate_to_verifier(self, remote: dict, executions) -> list[tuple[str, str, str, str]]:
        rows = []
        delta = (executions - self.baseline_exec) if isinstance(executions, int) and self.baseline_exec is not None else None
        gate_state = WAIT if delta is None else (DONE if delta == 0 else FAIL)
        rows.append(("④", "Gate: không thực thi", gate_state,
                     f"executions {self.baseline_exec} → {executions} (phải không đổi) · prepare/forced-command thử tay ở T3"))
        sso_v = next(iter(remote.get("sso_probe", {}).values()), None)
        if "fault" in self.first_seen and sso_v == 1.0:
            self.mark("fixed")
            now = utc_now().strftime("%H:%M:%S")
            if not self.sso_ok_streak or self.sso_ok_streak[-1] != now:
                self.sso_ok_streak.append(now)
            rows.append(("⑤", "Operator bật lại issuer", DONE, f"probe SSO=1 từ {self.first_seen['fixed']}"))
            elapsed = (utc_now() - datetime.strptime(self.first_seen["fixed"], "%H:%M:%S").replace(
                year=utc_now().year, month=utc_now().month, day=utc_now().day, tzinfo=timezone.utc)).total_seconds()
            state = DONE if elapsed >= 120 else RUN
            rows.append(("⑥", "Ổn định ≥ 120 s", state,
                         f"probe SSO liên tục =1 trong {int(elapsed)} s · chạy probe OIDC 5 mẫu ở T3 (mục 5.4)"))
        elif "fault" in self.first_seen:
            rows.append(("⑤", "Operator bật lại issuer", WAIT, "đang chờ người sửa"))
            rows.append(("⑥", "Ổn định ≥ 120 s", WAIT, "chưa"))
        else:
            rows.append(("⑤", "Operator bật lại issuer", WAIT, "chưa"))
            rows.append(("⑥", "Ổn định ≥ 120 s", WAIT, "chưa"))
        return rows


def render(board: Board, rows, remote: dict, clear: bool) -> None:
    out = []
    if clear:
        out.append("\033[2J\033[H")
    out.append(f"{BOLD}{board.spec['title']}{RESET}   {DIM}theo dõi từ {board.since_iso[11:]} UTC · "
               f"cập nhật {utc_now().strftime('%H:%M:%S')} · Ctrl+C để thoát{RESET}")
    if "_error" in remote:
        out.append(f"{RED}Không đọc được monitor-ai-01: {remote['_error']}{RESET} "
                   f"{DIM}(IP đổi? credential? SSH config?){RESET}")
    out.append("")
    for step, label, state, detail in rows:
        out.append(f" {ICON[state]} {CYAN}{step:<3}{RESET} {label:<26} {detail}")
    out.append("")
    out.append(f"{DIM}○ chưa · ◐ đang diễn ra · ● xong · ✕ sai kỳ vọng. Chỉ đọc: không thay đổi gì trên staging.{RESET}")
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--interval", type=float, default=3.0, help="giây giữa hai lần cập nhật (mặc định 3)")
    parser.add_argument("--once", action="store_true", help="in một lần rồi thoát")
    parser.add_argument("--since", help="mốc bắt đầu UTC, ví dụ 2026-10-23T14:19:30 (mặc định: lúc mở bảng)")
    parser.add_argument("--baseline-executions", type=int, help="số executions trước kịch bản (mặc định: lần đọc đầu)")
    args = parser.parse_args()
    if not SSH_CONFIG.is_file():
        print(f"Không thấy {SSH_CONFIG}; chạy từ thư mục gốc repo sau khi dựng hạ tầng.", file=sys.stderr)
        return 66
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    board = Board(args.scenario, since, args.baseline_executions)
    try:
        while True:
            remote = collect_remote(board.since_iso)
            files = latest_evidence(args.scenario, board.started) if args.scenario == "CON-01" else {}
            render(board, board.rows(remote, files), remote, clear=not args.once)
            if args.once:
                return 0 if "_error" not in remote else 1
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
