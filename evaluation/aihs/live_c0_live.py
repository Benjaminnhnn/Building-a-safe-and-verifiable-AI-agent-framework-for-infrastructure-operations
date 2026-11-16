"""LIVE backends and preflight for the C0-C3 harness. Meant to run on the Operator's WSL host (~/moodle-ops).

Everything here shells out to the EXISTING reviewed staging scripts (fault inject/reset, scenario probe, environment
baseline, independent verifier, audit export) or talks to the Agent API through the SSH tunnels the wrapper opens. No
secret is ever read into a log or an artifact: only key PATHS are reported. Offline (no staging) ``preflight --offline``
reports BLOCKED on infrastructure/operator-host items only; integrity items are always checked.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import live_c0_harness as h
import lab_collector

AUTOMATION = h.ROOT / "automation"
ARTIFACTS = h.ROOT / "terraform/.artifacts"
SSH_CONFIG = ARTIFACTS / "moodle-ssh.config"
SECRETS_DIR = ARTIFACTS / "safe-executor"
PROTOCOL_LOCK = h.EVAL / ".artifacts/phase4/protocol-lock-primary.json"
CLOCK_SKEW_LIMIT_S = 5.0
REQUIRED_ENV = ("AWS_PROFILE", "AIHS_LIVE_CONFIRM", "AIHS_LIVE_PROM_URL", "AIHS_LIVE_AGENT_URL", "AIHS_LIVE_PUBLIC_URL",
                "MOODLE_OPERATOR_SIGNING_KEY_FILE", "MOODLE_VERIFIER_SIGNING_KEY_FILE")
SECRET_FILES = ("agent-hmac.key", "safe-action-api-token", "approval-ed25519.pub", "gate-approval-ed25519.pub",
                "verifier-ed25519.pub")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------------ helpers
def run_cmd(args: list[str], *, env: dict | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    """No shell, no stdin: an AWS MFA prompt can never hang a captured run."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, shell=False, stdin=subprocess.DEVNULL,
                          env={**os.environ, **(env or {})}, check=False)


def http_json(method: str, url: str, *, body=None, token: str | None = None, timeout: float = 120):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)


def verifier_module():
    spec = importlib.util.spec_from_file_location("moodle_independent_verifier_live", AUTOMATION / "moodle-independent-verifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------------------------ live backend
class CommandBackend:
    """Staging through the reviewed scripts. Ground truth (scenario) is used ONLY by inject/reset/contract_probe."""

    name = "live-staging-commands"
    WEB_CONTAINER = os.environ.get("MOODLE_WEB_CONTAINER", "release-moodle-web-1")
    CRON_CONTAINER = os.environ.get("MOODLE_CRON_CONTAINER", "release-moodle-cron-1")

    def __init__(self, env=os.environ):
        self.env = env
        self.prom = env.get("AIHS_LIVE_PROM_URL", "http://127.0.0.1:19090").rstrip("/")
        self.public_url = env.get("AIHS_LIVE_PUBLIC_URL", "").rstrip("/")
        self.confirm = {"MOODLE_FAULT_CONFIRM": "staging", "MOODLE_SAFE_EXECUTOR_CONFIRM": "staging"}
        self.heartbeat: Heartbeat | None = None
        names = {"web_a": ("moodle-app-a", self.WEB_CONTAINER), "web_b": ("moodle-app-b", self.WEB_CONTAINER),
                 "cron_a": ("moodle-app-a", self.CRON_CONTAINER)}
        # lab collector asks by lab container name; map back to the log source and then to a staging host/container
        self._by_container = {container: names.get(source) for source, container in lab_collector.LOG_SOURCES.items()}

    def _script(self, name: str, *args: str, timeout: float = 900):
        return run_cmd(["bash", str(AUTOMATION / name), *args], env=self.confirm, timeout=timeout)

    def verify_baseline(self) -> dict:
        done = self._script("moodle-environment-baseline.sh", "verify", timeout=600)
        return {"ok": done.returncode == 0, "returncode": done.returncode, "tail": (done.stdout + done.stderr)[-300:]}

    def inject(self, scenario: str) -> None:
        done = self._script("moodle-fault-inject.sh", scenario)
        if done.returncode != 0:
            tail = " ".join((((done.stdout or "") + (done.stderr or ""))[-300:]).split())
            raise RuntimeError(f"fault injection failed (rc={done.returncode})" + (f": {tail}" if tail else ""))

    def prom_query(self, expr: str) -> list[dict]:
        status, payload, _ = http_json("GET", f"{self.prom}/api/v1/query?{urllib.parse.urlencode({'query': expr})}", timeout=8)
        if status != 200 or payload.get("status") != "success":
            raise RuntimeError("prometheus query failed")
        return payload["data"]["result"]

    def wait_detect(self, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                firing = self.prom_query('ALERTS{alertstate="firing"}')
                if firing:
                    return [{"alertname": m["metric"].get("alertname", ""), "instance": m["metric"].get("instance", "")} for m in firing]
            except Exception:  # noqa: BLE001 - keep polling until the deadline
                pass
            time.sleep(5)
        return None

    def log_reader(self, container: str, **_kw) -> str:
        target = self._by_container.get(container)
        if target is None:                       # no HAProxy / RDS container logs on the ALB+RDS staging stack
            return ""
        node, name = target
        done = run_cmd(["ssh", "-n", "-F", str(SSH_CONFIG), "-o", "BatchMode=yes", node,
                        f"sudo -n docker logs --since 120s --tail 400 {shlex.quote(name)}"], timeout=40)
        return (done.stdout or "") + (done.stderr or "")

    def contract_probe(self, scenario: str) -> dict[str, bool]:
        return verifier_module()._run_probe(scenario, f"score-{int(time.time())}")

    def health_only(self) -> dict:
        try:
            status = urllib.request.urlopen(f"{self.public_url}/healthz.php", timeout=10).status
        except Exception:  # noqa: BLE001
            status = 0
        try:
            synthetic = bool(self.prom_query("moodle_synthetic_success == 1"))
            clear = not self.prom_query('ALERTS{alertstate="firing"}')
        except Exception:  # noqa: BLE001
            synthetic = clear = False
        return {"ok": status == 200 and synthetic and clear, "healthz_http_200": status == 200, "synthetic_ok": synthetic,
                "alerts_clear": clear}

    def reset(self, scenario: str) -> dict:
        done = run_cmd(["bash", str(AUTOMATION / "aihs-live-c0.sh"), "reset-scenario", scenario], env=self.confirm, timeout=900)
        return {"ok": done.returncode == 0, "returncode": done.returncode, "tail": (done.stdout + done.stderr)[-300:]}

    def start_heartbeat(self) -> None:
        self.heartbeat = Heartbeat(web_container=self.WEB_CONTAINER)
        self.heartbeat.start()

    def stop_heartbeat(self) -> dict:
        return self.heartbeat.stop() if self.heartbeat else {"error": "heartbeat was not started"}


PHP_INSERT = '<?php define("CLI_SCRIPT", true); require "/var/www/html/config.php"; $DB->insert_record("config_plugins", (object)["plugin" => "local_rpo_hb", "name" => $argv[1], "value" => $argv[2]]); echo "ACK\\n";'
PHP_LIST = '<?php define("CLI_SCRIPT", true); require "/var/www/html/config.php"; foreach ($DB->get_records_menu("config_plugins", ["plugin" => "local_rpo_hb"], "", "name,value") as $n => $v) { echo "$n\\t$v\\n"; }'
PHP_PURGE = '<?php define("CLI_SCRIPT", true); require "/var/www/html/config.php"; $DB->delete_records("config_plugins", ["plugin" => "local_rpo_hb"]); echo "PURGED\\n";'


class Heartbeat:
    """RPO probe, same semantics as automation/moodle-rto-rpo-ablation.sh: acknowledged rows must survive recovery."""

    def __init__(self, *, web_container: str, interval_s: float = 2.0, node: str = "moodle-app-a"):
        self.container, self.interval, self.node = web_container, interval_s, node
        self.rows: list[tuple[float, int, str]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _php(self, code: str, *args: str, timeout: float = 20) -> str:
        encoded = base64.b64encode(code.encode()).decode()
        remote = (f"printf %s {encoded} | base64 -d | sudo -n docker exec -i {shlex.quote(self.container)} php -- "
                  + " ".join(shlex.quote(a) for a in args))
        return run_cmd(["ssh", "-n", "-F", str(SSH_CONFIG), "-o", "BatchMode=yes", self.node, remote], timeout=timeout).stdout

    def start(self) -> None:
        self._php(PHP_PURGE)

        def loop():
            seq = 0
            while not self._stop.is_set():
                seq += 1
                stamp = time.time()
                try:
                    ack = "ACK" if "ACK" in self._php(PHP_INSERT, f"hb-{seq}", f"{stamp:.3f}", timeout=15) else "FAIL"
                except Exception:  # noqa: BLE001
                    ack = "FAIL"
                self.rows.append((stamp, seq, ack))
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        present = {line.split("\t")[0] for line in self._php(PHP_LIST).splitlines() if line}
        self._php(PHP_PURGE)
        acked = [(t, s) for t, s, a in self.rows if a == "ACK"]
        lost = [(t, s) for t, s in acked if f"hb-{s}" not in present]
        rpo = (max(t for t, _ in lost) - min(t for t, _ in lost) + self.interval) if lost else 0.0
        return {"acknowledged_writes": len(acked), "lost_acknowledged_writes": len(lost), "rpo_seconds": round(rpo, 3)}


# ------------------------------------------------------------------------------------------------ live Agent
class ApiAgent:
    """The deployed Agent on monitor-ai-01 (prepare/execute/verification endpoints), reached through the SSH tunnel."""

    def __init__(self, env=os.environ, *, http=http_json, runner=run_cmd):
        self.base = env.get("AIHS_LIVE_AGENT_URL", "http://127.0.0.1:18000").rstrip("/")
        token_file = Path(env.get("AIHS_LIVE_AGENT_TOKEN_FILE", str(SECRETS_DIR / "safe-action-api-token")))
        self.token = token_file.read_text().strip() if token_file.is_file() else ""
        self.verifier_key = env.get("MOODLE_VERIFIER_SIGNING_KEY_FILE", "")
        self.http, self.runner = http, runner
        self.audit_export: Path | None = None
        self.run_dir: Path | None = None

    def prepare(self, *, incident_id, features, decision, evidence_db, audit_db, clock):
        self.run_dir = Path(audit_db).parent
        # Session 1 (16/11/2026): a 30-minute, scenario-blind window let the previous run's candidate make the lookup
        # ambiguous (CON-01 blocked by the RES-01 candidate). Bind the lookup to THIS run's derived scenario and a
        # 15-minute window (runs are >= ~9 min apart and the plan never repeats a scenario back-to-back). Repeated
        # Alertmanager notifications can create several candidates for the same scenario; take the newest and let
        # check_handoff() bind action, scope, target, environment and both digests (any mismatch still fails closed).
        scenario = getattr(getattr(decision, "context", None), "scenario_id", None)
        if not scenario:
            return None
        since = (clock.now() - h.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = ("import sqlite3,sys; db=sqlite3.connect('/app/data/evidence.db'); r=db.execute("
                 "\"select evidence_id from evidence where source='ai_decision' and json_extract(metadata_json,'$.decision_kind')='remediation_candidate' "
                 "and json_extract(json_extract(metadata_json,'$.decision_json'),'$.scenario_id') = ? "
                 "and julianday(collected_at) >= julianday(?) order by collected_at desc limit 1\", (sys.argv[1], sys.argv[2])).fetchall(); "
                 "print(','.join(x[0] for x in r))")
        done = self.runner(["ssh", "-n", "-F", str(SSH_CONFIG), "-o", "BatchMode=yes", "monitor-ai-01",
                            f"sudo docker exec moodle-ai-agent python -c {shlex.quote(query)} {shlex.quote(scenario)} {shlex.quote(since)}"],
                           timeout=60)
        ids = [x for x in (done.stdout or "").strip().split(",") if x]
        if len(ids) != 1:                          # no candidate from the Agent's own planner: never invent one (fail closed)
            return None
        status, payload, _ = self.http("POST", f"{self.base}/api/moodle/remediation/{ids[0]}/prepare?notify=false", token=self.token)
        return payload if status == 200 else None

    def refresh(self, prepared, fresh_decision, *, evidence_db):
        return prepared                            # the Agent refreshes stale evidence itself (refresh_if_stale=True)

    def execute(self, prepared, approval, *, decision, clock):
        status, payload, _ = self.http("POST", f"{self.base}/api/moodle/remediation/{prepared['candidate_id']}/execute",
                                       body={"request": prepared["request"], "executor_approval": approval}, token=self.token, timeout=150)
        return payload if status == 200 else {"status": "escalated", "result": {"status": "denied", "http_status": status}}

    def verify(self, prepared, *, clock):
        self.audit_export = (self.run_dir or Path(".")) / "agent-audit-export.sqlite3"
        exported = self.runner([sys.executable, str(AUTOMATION / "export-moodle-safe-action-audit.py"), "--ssh-config", str(SSH_CONFIG),
                                "--output", str(self.audit_export)], timeout=120)
        if exported.returncode != 0:
            return {"verdict": "not_resolved", "resolution_eligible": False, "error": "audit export failed"}
        request = prepared["request"]
        done = self.runner([sys.executable, str(AUTOMATION / "moodle-independent-verifier.py"), "--audit-db", str(self.audit_export),
                            "--signing-key", self.verifier_key, "--incident-id", prepared["incident_id"],
                            "--request-id", request["request_id"], "--scenario-id", request["scenario_id"],
                            "--window-seconds", "120", "--interval-seconds", "60"], timeout=900)
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError:
            return {"verdict": "not_resolved", "resolution_eligible": False, "error": "verifier produced no JSON"}

    def accept(self, request_id, attestation):
        status, payload, _ = self.http("POST", f"{self.base}/api/moodle/verifications/{request_id}", body=attestation)
        return payload if status == 200 else {"status": "rejected", "error": f"http {status}"}

    def status(self, incident_id, request_id):
        status, payload, _ = self.http("GET", f"{self.base}/api/moodle/incidents/{incident_id}/requests/{request_id}/status", token=self.token)
        return payload if status == 200 else {"status": "unknown"}


# ------------------------------------------------------------------------------------------------ live operator
# What each reviewed live action does and which primary-cause alert points to it. Display only: the Operator decides
# from the pre-decision evidence on the card. Nothing here is ground truth or an authorization input.
ACTION_GUIDE = {
    "remove_scoped_db_reject": (
        "Gỡ rule tường lửa (có tag của kịch bản) đang chặn node Moodle kết nối tới RDS PostgreSQL",
        ("MoodleNodeRdsTcpFailed",),
        "các node Moodle không kết nối được tới RDS (MoodleNodeRdsTcpFailed)"),
    "restore_fixture_directory_mode": (
        "Trả quyền thư mục fixture trên EFS về 770 (chỉ đổi quyền, không xoá dữ liệu)",
        (),
        "giao dịch tổng hợp lỗi trong khi không có alert gốc nào khác (DB, CPU, container, cron đều bình thường)"),
    "restart_moodle_cron_service": (
        "Khởi động lại container cron của Moodle để hàng đợi adhoc task chạy tiếp",
        ("MoodleAdhocTaskQueueStuck",),
        "hàng đợi adhoc task bị kẹt hoặc có task quá hạn (MoodleAdhocTaskQueueStuck)"),
    "remove_named_cpu_load_container": (
        "Xoá đúng container gây tải CPU có tên đã được review",
        ("MoodleNodeCpuHigh",),
        "CPU node quá tải (MoodleNodeCpuHigh)"),
    "recreate_moodle_web_from_reviewed_compose": (
        "Tạo lại container web Moodle từ file compose đã review (bỏ cấu hình DNS/hosts bị ghi đè)",
        (),
        "giao dịch tổng hợp lỗi trong khi không có alert gốc nào khác (tên miền RDS phân giải sai)"),
    "start_reviewed_compose_service": (
        "Khởi động lại container web Moodle đang bị dừng, theo file compose đã review",
        ("MoodleWebContainerMissing",),
        "container web Moodle trên một node không chạy (MoodleWebContainerMissing)"),
}
SYMPTOM_ALERTS = frozenset({"MoodlePublicProbeFailed", "MoodleSyntheticTxnFailed", "MoodleSyntheticTxnStale",
                            "MoodleNodeProbeStale", "MoodleRdsTcpProbeFailed", "MoodleNodeRdsTcpLatencyHigh",
                            "MoodleAdhocQueueProbeFailed"})


def render_operator_card(card: dict, token: str) -> str:
    """Plain-language approval card (Vietnamese). Shows pre-decision evidence only; never the injected fault."""
    action = card.get("proposed_action") or "?"
    what, supporting, approve_if = ACTION_GUIDE.get(
        action, ("(chưa có mô tả cho hành động này)", (), "bằng chứng khớp với hành động"))
    gate = card.get("gate_preview") or {}
    firing = list(card.get("firing_alerts") or [])
    bar = "=" * 78
    out = [bar, " CẦN BẠN DUYỆT (Operator): hệ thống muốn chạy MỘT hành động trên staging", bar,
           f" Hành động đề xuất : {action}",
           f" Việc sẽ làm       : {what}",
           f" Phạm vi           : {card.get('target_scope')} (môi trường staging)",
           f" Ngữ cảnh từ alert : {card.get('scenario_id')} (Gate suy ra từ alert, không phải đáp án)",
           "", " Bằng chứng: các alert đang firing lúc ra quyết định"]
    if not firing:
        out.append("   (thẻ này không có danh sách alert)")
    for name in firing:
        if name in supporting:
            mark = "[KHỚP]        alert gốc khớp với hành động này"
        elif name in SYMPTOM_ALERTS:
            mark = "[TRIỆU CHỨNG] dấu hiệu chung, không tự chỉ ra nguyên nhân"
        else:
            mark = "[KHÁC]        alert gốc trỏ tới vấn đề khác, cần cân nhắc"
        out.append(f"   - {name:<30} {mark}")
    checks = gate.get("checks") or []
    out += ["", f" Safety Gate: {gate.get('decision', '?')} ({len(checks)} kiểm tra đã đạt)"]
    out += [f"   - cần người duyệt vì: {reason}" for reason in gate.get("approval_reasons") or []]
    out += ["", " Cách quyết định:",
            f"   APPROVE nếu bằng chứng cho thấy {approve_if},",
            "           và không có alert [KHÁC] nào mâu thuẫn.",
            "   REJECT  nếu alert chỉ ra vấn đề khác, hành động có vẻ sai chỗ, hoặc bạn không chắc.",
            "   Chỉ dựa vào bằng chứng trên thẻ, không dựa vào việc bạn biết lỗi nào đã được inject.",
            "", " Điều gì xảy ra tiếp theo:",
            "   APPROVE: Executor chạy đúng hành động này (chữ ký gắn với mã bên dưới), rồi Verifier",
            "            kiểm tra khoảng 2-3 phút. Chỉ Verifier mới được đánh dấu RESOLVED.",
            "   REJECT : không có gì được thực thi; lỗi được reset, kết quả ghi OPERATOR_REJECTED.",
            bar]
    return "\n".join(out) + "\n"


class KeyFileOperator(h.OperatorPort):
    """The human Operator. Console: type the printed token. The key is read from the Operator host and never printed."""

    def __init__(self, key_path: str, *, mode: str = "console", timeout_s: float = 600, actor: str = h.OPERATOR_ACTOR):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(Path(key_path).read_bytes(), password=None)
        if not isinstance(key, h.Ed25519PrivateKey):
            raise ValueError("operator key must be Ed25519")
        self.key, self.public_key, self.mode, self.timeout_s, self.actor = key, key.public_key(), mode, timeout_s, actor

    def decide(self, card, clock):
        started = time.monotonic()
        token = card["executor_action_sha256"][:12]
        sys.stdout.write("\n" + render_operator_card(card, token))
        if self.mode == "telegram":
            prepared = card.get("prepared") or {}
            tmp = (h.ROOT / ".pytest_tmp" / f"prepared-{token}.json")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(prepared))
            done = run_cmd([sys.executable, str(AUTOMATION / "moodle-telegram-approval.py"), "approve", "--prepared", str(tmp),
                            "--timeout-seconds", str(int(self.timeout_s))], timeout=self.timeout_s + 30)
            tmp.unlink(missing_ok=True)
            try:
                decision = json.loads(done.stdout).get("decision")
            except json.JSONDecodeError:
                decision = "timeout"
            answer = "approve" if decision == "approved" else ("reject" if decision == "rejected" else "")
        else:
            # Discard anything typed before the card appeared, so stray input can never answer this card.
            try:
                import termios
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            except Exception:
                pass
            # Session 1: one mistyped line ended the card as "timeout" after 170 s. Re-prompt on anything that is not
            # exactly APPROVE <token> / REJECT (case-insensitive) until the deadline; only those two answers count.
            # timeout_s <= 0 = HOLD: the card stays open until the Operator answers (no automatic timeout). Nothing is
            # executed while holding; the fault simply stays injected and approval_wait_s records the real wait.
            answer = ""
            deadline = started + self.timeout_s if self.timeout_s > 0 else None
            if deadline is None:
                sys.stdout.write("(HOLD: thẻ này chờ đến khi bạn trả lời, không tự hết giờ)\a\n")
            while not answer and (deadline is None or time.monotonic() < deadline):
                sys.stdout.write(f"Gõ  APPROVE {token}  để duyệt đúng hành động này, hoặc  REJECT  để từ chối: ")
                sys.stdout.flush()
                box: list[str] = []
                reader = threading.Thread(target=lambda: box.append(sys.stdin.readline()), daemon=True)
                reader.start()
                reader.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
                if not box or box[0] == "":        # deadline reached, or stdin closed
                    break
                line = " ".join(box[0].split()).upper()
                if line == f"APPROVE {token.upper()}":
                    answer = "approve"
                elif line == "REJECT":
                    answer = "reject"
                else:
                    sys.stdout.write(f"  -> không hợp lệ ({line[:40]!r}); thẻ vẫn mở, hãy gõ lại.\n")
        waited = time.monotonic() - started
        sys.stdout.write({"approve": "  -> ĐÃ DUYỆT. Executor sẽ chạy đúng hành động này.\n",
                          "reject": "  -> ĐÃ TỪ CHỐI. Không có gì được thực thi.\n"}.get(
                              answer, "  -> Không có câu trả lời (hết giờ hoặc stdin đóng): không thực thi.\n"))
        sys.stdout.flush()
        if answer == "approve":
            return h.OperatorDecision("approved", h.sign_executor_approval(self.key, card["executor_action_sha256"], actor=self.actor), waited)
        return h.OperatorDecision("rejected" if answer == "reject" else "timeout", None, waited)


# ------------------------------------------------------------------------------------------------ preflight
def _check(cid: str, category: str, ok: bool | None, detail: str, *, warn: bool = False) -> dict:
    status = "WARN" if (warn and ok is not False) else ("OK" if ok else "BLOCKED")
    return {"id": cid, "category": category, "status": status, "detail": detail}


def preflight(*, offline: bool, env=os.environ) -> dict:
    checks: list[dict] = []
    add = checks.append
    # ---------------- integrity (always checked)
    lock = json.loads(PROTOCOL_LOCK.read_text()) if PROTOCOL_LOCK.is_file() else None
    add(_check("protocol_lock_present", "integrity", lock is not None, str(PROTOCOL_LOCK.relative_to(h.ROOT))))
    if lock:
        drift = [p for p, digest in lock["code_sha256"].items() if not (h.ROOT / p).is_file() or sha256_file(h.ROOT / p) != digest]
        add(_check("locked_evidence_unmodified", "integrity", not drift,
                   f"{len(lock['code_sha256'])} locked files hashed" if not drift else f"CHANGED: {drift}"))
        model_lock_file = h.MODEL_DIR / "model-lock.json"
        art = lock["artifacts_sha256"]
        mlock = json.loads(model_lock_file.read_text()) if model_lock_file.is_file() else {}
        model_ok = (model_lock_file.is_file() and sha256_file(model_lock_file) == art["model_lock_sha256"]
                    and (h.MODEL_DIR / "model.pkl").is_file() and sha256_file(h.MODEL_DIR / "model.pkl") == art["model_file_sha256"]
                    and mlock.get("model_content_sha256") == art["model_content_sha256"] and mlock.get("status") == "LOCKED_C0"
                    and mlock.get("model_content_sha256", "").startswith("0da82b58"))
        add(_check("locked_model_hashes", "integrity", model_ok, f"LOCKED_C0 content {str(mlock.get('model_content_sha256'))[:12]}"))
    mapping_detail, mapping_ok = "", False
    try:
        mapping = h.load_c0_mapping(h.REVIEWED_MAPPING)
        live = [e for e in mapping.actions if e.status == "live_reviewed"]
        by = {e.aihs_action: e for e in mapping.actions}
        mapping_ok = (mapping.review_status == "reviewed_for_live" and mapping.environment == "staging" and len(live) == 6
                      and all(e.review_reference for e in live) and by["wait_and_recheck"].status == "observe_only"
                      and by["call_human"].status == "human_only"
                      and {e.scenario_id for e in live} == set(h.LIVE_SCENARIOS))
        mapping_detail = f"{mapping.mapping_version}; live_reviewed={len(live)} sha256={sha256_file(h.REVIEWED_MAPPING)[:12]}"
        approval_spec = importlib.util.spec_from_file_location("safe_approval", AUTOMATION / "moodle-safe-approval.py")
        approval = importlib.util.module_from_spec(approval_spec)
        approval_spec.loader.exec_module(approval)
        allow_ok = all(approval.CATALOG.get(e.scenario_id, {}).get(e.catalog_action_id) == e.target_scope for e in live)
        catalog = h.ActionCatalog.load_moodle_7_scenario_catalog()
        catalog_ok = all(
            catalog.is_safe_execution_allowed(e.catalog_action_id, scenario_id=e.scenario_id, target_scope=e.target_scope,
                                              environment="staging", role="executor")
            and e.scenario_id in h.MOODLE_LIVE_CONTRACTS for e in live)
        never = any(e.scenario_id in h.NEVER_LIVE_SCENARIOS or (e.catalog_action_id or "").startswith("reconcile_") for e in live)
        add(_check("executor_allowlist_matches_mapping", "integrity", allow_ok and not never, "scenario/action/scope triples"))
        add(_check("catalog_and_verifier_contract_cover_mapping", "integrity", catalog_ok, "catalog RBAC + MOODLE_LIVE_CONTRACTS"))
    except Exception as exc:  # noqa: BLE001
        mapping_detail = f"{type(exc).__name__}: {str(exc)[:160]}"
    add(_check("reviewed_mapping", "integrity", mapping_ok, mapping_detail))
    try:
        from core.environment_profile import get_environment_profile
        profile = get_environment_profile()
        add(_check("environment_profile_staging", "integrity", profile.environment == "staging",
                   f"approval={profile.approval.mode} stability={profile.verification.stability_window_seconds}s"))
    except Exception as exc:  # noqa: BLE001
        add(_check("environment_profile_staging", "integrity", False, f"{type(exc).__name__}"))
    deps = []
    for module in ("numpy", "sklearn", "lightgbm", "cryptography", "pydantic"):
        try:
            __import__(module)
        except ImportError:
            deps.append(module)
    add(_check("python_dependencies", "integrity", not deps, "all present" if not deps else f"missing {deps}"))
    if lock and (h.MODEL_DIR / "model-lock.json").is_file():
        from importlib import metadata
        pinned = json.loads((h.MODEL_DIR / "model-lock.json").read_text()).get("dependencies", {})
        drift = {}
        for name, version in pinned.items():
            if name == "python":
                installed = ".".join(map(str, sys.version_info[:2]))
                if not version.startswith(installed):
                    drift[name] = f"locked {version}, running {installed}"
                continue
            try:
                installed = metadata.version(name)
            except metadata.PackageNotFoundError:
                installed = "absent"
            if installed != version:
                drift[name] = f"locked {version}, installed {installed}"
        add(_check("model_dependency_versions_match_lock", "integrity", not drift,
                   "numpy/scikit-learn/lightgbm/scipy/joblib match model-lock.json" if not drift else f"drift: {drift}"))
    gap = _lb_vocabulary_gap()
    add(_check("feature_schema_gap_lab_vs_staging", "integrity", True, gap, warn="lb_" in gap))
    # ---------------- infrastructure / operator host (BLOCKED offline by definition)
    if offline:
        for cid, why in (("aws_identity", "no AWS call in offline mode; staging destroyed"),
                         ("ssh_config_and_host_keys", f"{SSH_CONFIG} is written by Terraform and absent offline"),
                         ("prometheus_reachable", "no monitor host"), ("agent_health_live_switch_off", "no Agent endpoint"),
                         ("executor_health_live_switch_off", "no Safe Executor endpoint"),
                         ("public_probe_healthz", "no ALB"), ("environment_baseline", "no staging"),
                         ("staging_metric_schema", "no staging Prometheus to compare against"),
                         ("clock_skew", "no endpoint to compare against")):
            add(_check(cid, "infrastructure", False, why))
    else:
        add(_live_infra_checks(env))
    # ---------------- operator host: env vars and keys (paths only)
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    add(_check("required_env_vars", "operator_host", not missing, "all set" if not missing else f"unset: {missing}"))
    for var, label in (("MOODLE_OPERATOR_SIGNING_KEY_FILE", "operator_signing_key"), ("MOODLE_VERIFIER_SIGNING_KEY_FILE", "verifier_signing_key")):
        add(_key_check(label, env.get(var), SECRETS_DIR / ("approval-ed25519.pub" if "OPERATOR" in var else "verifier-ed25519.pub")))
    absent = [name for name in SECRET_FILES if not (SECRETS_DIR / name).is_file()]
    add(_check("safe_executor_secret_files", "operator_host", not absent, f"{SECRETS_DIR}: " + ("all present" if not absent else f"missing {absent}")))
    flat = [c for entry in checks for c in (entry if isinstance(entry, list) else [entry])]
    blocked = [c for c in flat if c["status"] == "BLOCKED"]
    return {"status": "BLOCKED" if blocked else "READY", "offline": offline, "blocked": [c["id"] for c in blocked],
            "blocked_integrity": [c["id"] for c in blocked if c["category"] == "integrity"], "checks": flat,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# Pre-decision features the protocol D1 context rule reads to separate NET-01 (DNS) from SEC-02 (fixture mode).
D1_REQUIRED = tuple(f"{metric}__{node}" for metric in ("moodle_node_db_dns_matches_expected", "moodle_node_db_dns_success")
                    for node in ("a", "b")) + ("moodle_node_fixture_dir_mode__a",)
KNOWN_LB_GAP = 5                      # HAProxy-only lb_* words; the ALB staging has no such series
LOCAL_FEATURE_PREFIXES = ("alert_firing", "log_")   # derived from alerts/logs by the collector, not from Prometheus samples


def metric_schema_gap(query, vocabulary) -> dict:
    """Which locked-vocabulary words and D1-required features the staging Prometheus does NOT currently provide.

    ``query`` is the instant-query callable the harness uses (promql -> result list). The lab collector's own query
    table is reused, so the check sees exactly the features the live harness would build. Read-only.
    """
    import tempfile
    collector = lab_collector.Collector(Path(tempfile.gettempdir()), query=query)
    samples, _all_alerts, _firing = collector._samples()
    present = {f"{name}__{label}" if label else name
               for name, by_label in samples.items() for label, value in by_label.items() if value is not None}
    wanted = [w for w in vocabulary if not w.startswith(LOCAL_FEATURE_PREFIXES)]
    missing = sorted(w for w in wanted if w not in present)
    d1_missing = sorted(w for w in D1_REQUIRED if w not in present)
    unexpected = [w for w in missing if not w.startswith("lb_")]
    blocked = bool(d1_missing or unexpected or len(missing) > KNOWN_LB_GAP)
    return {"blocked": blocked, "vocabulary_checked": len(wanted), "missing": missing, "d1_missing": d1_missing,
            "unexpected_missing": unexpected}


def _metric_schema_check(env) -> dict:
    prom = env.get("AIHS_LIVE_PROM_URL", "").rstrip("/")

    def query(expr: str) -> list[dict]:
        status, payload, _ = http_json("GET", f"{prom}/api/v1/query?{urllib.parse.urlencode({'query': expr})}", timeout=8)
        if status != 200 or payload.get("status") != "success":
            raise RuntimeError("prometheus query failed")
        return payload["data"]["result"]

    try:
        query("up")
        vocabulary = list(h.LockedModelProposer().model.mapper.vocabulary)
        gap = metric_schema_gap(query, vocabulary)
    except Exception as exc:  # noqa: BLE001
        return _check("staging_metric_schema", "infrastructure", False, f"cannot compare schema ({type(exc).__name__})")
    detail = (f"{gap['vocabulary_checked']} Prometheus-backed vocabulary words checked; missing={gap['missing']}; "
              f"d1_missing={gap['d1_missing']}")
    return _check("staging_metric_schema", "infrastructure", not gap["blocked"], detail,
                  warn=bool(gap["missing"]) and not gap["blocked"])


def _lb_vocabulary_gap() -> str:
    """Words of the locked vocabulary that only exist on the lab's HAProxy and are therefore ABSENT on the ALB staging."""
    try:
        words = list(h.LockedModelProposer().model.mapper.vocabulary)
    except Exception as exc:  # noqa: BLE001
        return f"vocabulary unreadable ({type(exc).__name__})"
    lb = [w for w in words if w.startswith("lb_")]
    return (f"{len(lb)} of {len(words)} vocabulary words are HAProxy-only lb_* features ({', '.join(lb)}); staging has an ALB, "
            "so they will be missing (no word) live. Quantify with the shadow pilot before the main runs.") if lb else "no lab-only vocabulary words"


def _key_check(label: str, path: str | None, installed_public: Path) -> dict:
    if not path:
        return _check(label, "operator_host", False, "path variable unset")
    key_path = Path(path).expanduser()
    if not key_path.is_file():
        return _check(label, "operator_host", False, f"{key_path} not found")
    if os.name == "posix" and (key_path.stat().st_mode & 0o077):
        return _check(label, "operator_host", False, f"{key_path} is group/world accessible (chmod 600)")
    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        derived = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        if not isinstance(key, h.Ed25519PrivateKey):
            return _check(label, "operator_host", False, "not an Ed25519 key")
        if installed_public.is_file() and installed_public.read_bytes().strip() != derived.strip():
            return _check(label, "operator_host", False, f"does not match installed {installed_public.name}")
        return _check(label, "operator_host", True, f"{key_path} Ed25519" + ("" if installed_public.is_file() else " (installed public key not checked)"))
    except Exception as exc:  # noqa: BLE001
        return _check(label, "operator_host", False, f"unreadable: {type(exc).__name__}")


def _live_infra_checks(env) -> list[dict]:
    out: list[dict] = []
    profile = env.get("AWS_PROFILE", "")
    done = run_cmd(["aws", "sts", "get-caller-identity", "--profile", profile, "--query", "Account", "--output", "text"], timeout=30) if profile else None
    out.append(_check("aws_identity", "infrastructure", bool(done and done.returncode == 0 and done.stdout.strip().isdigit()),
                      "caller identity resolved" if done and done.returncode == 0 else "aws sts get-caller-identity failed (MFA/profile?)"))
    out.append(_check("ssh_config_and_host_keys", "infrastructure", SSH_CONFIG.is_file(), str(SSH_CONFIG)))
    prom = env.get("AIHS_LIVE_PROM_URL", "").rstrip("/")
    skew_detail = "no Date header"
    try:
        status, payload, headers = http_json("GET", f"{prom}/api/v1/query?query=up", timeout=8)
        down = [m["metric"].get("instance") for m in payload["data"]["result"] if m["value"][1] != "1"]
        out.append(_check("prometheus_reachable", "infrastructure", status == 200 and not down, f"targets down: {down}" if down else "all up"))
        if "Date" in headers:
            skew = abs((parsedate_to_datetime(headers["Date"]) - datetime.now(timezone.utc)).total_seconds())
            out.append(_check("clock_skew", "infrastructure", skew <= CLOCK_SKEW_LIMIT_S, f"{skew:.1f}s (limit {CLOCK_SKEW_LIMIT_S}s)"))
        else:
            out.append(_check("clock_skew", "infrastructure", False, skew_detail))
    except Exception as exc:  # noqa: BLE001
        out.append(_check("prometheus_reachable", "infrastructure", False, type(exc).__name__))
        out.append(_check("clock_skew", "infrastructure", False, "prometheus unreachable"))
    try:
        status, payload, _ = http_json("GET", env.get("AIHS_LIVE_AGENT_URL", "").rstrip("/") + "/health", timeout=8)
        out.append(_check("agent_health_live_switch_off", "infrastructure",
                          status == 200 and payload.get("safe_action_live_enabled") is False and payload.get("approval_mode") == "operator",
                          f"live_enabled={payload.get('safe_action_live_enabled')} approval_mode={payload.get('approval_mode')}"))
    except Exception as exc:  # noqa: BLE001
        out.append(_check("agent_health_live_switch_off", "infrastructure", False, type(exc).__name__))
    ex = run_cmd(["ssh", "-n", "-F", str(SSH_CONFIG), "-o", "BatchMode=yes", "monitor-ai-01",
                  "curl --fail --silent http://172.17.0.1:8765/healthz"], timeout=30)
    try:
        out.append(_check("executor_health_live_switch_off", "infrastructure", json.loads(ex.stdout).get("execution_enabled") is False, "execution_enabled=false"))
    except Exception:  # noqa: BLE001
        out.append(_check("executor_health_live_switch_off", "infrastructure", False, "executor health unreadable"))
    try:
        status = urllib.request.urlopen(env.get("AIHS_LIVE_PUBLIC_URL", "").rstrip("/") + "/healthz.php", timeout=10).status
    except Exception:  # noqa: BLE001
        status = 0
    out.append(_check("public_probe_healthz", "infrastructure", status == 200, f"HTTP {status}"))
    base = run_cmd(["bash", str(AUTOMATION / "moodle-environment-baseline.sh"), "verify"], timeout=600)
    out.append(_check("environment_baseline", "infrastructure", base.returncode == 0, f"rc={base.returncode}"))
    out.append(_metric_schema_check(env))
    return out


def preflight_cli(args) -> int:
    report = preflight(offline=args.offline)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"PREFLIGHT {report['status']} ({'offline' if args.offline else 'online'})")
        for c in report["checks"]:
            print(f"  [{c['status']:<7}] {c['category']:<14} {c['id']:<40} {c['detail']}")
        if report["blocked"]:
            print("BLOCKED: " + ", ".join(report["blocked"]))
    return 0 if report["status"] == "READY" else 3


def run_cli(args) -> int:
    if os.environ.get("AIHS_LIVE_CONFIRM") != "staging":
        print("Set AIHS_LIVE_CONFIRM=staging to authorise one LIVE staging run.", file=sys.stderr)
        return 77
    if not getattr(args, "skip_preflight", False):
        report = preflight(offline=False)
        if report["status"] != "READY":
            print(json.dumps({"preflight": "BLOCKED", "blocked": report["blocked"]}))
            return 3
    mapping = h.load_c0_mapping(h.REVIEWED_MAPPING)
    catalog = h.ActionCatalog.load_moodle_7_scenario_catalog()
    # AIHS_OPERATOR_TIMEOUT_S: seconds the approval card stays open (default 600); 0 = hold until the Operator answers.
    operator_timeout = float(os.environ.get("AIHS_OPERATOR_TIMEOUT_S", "600"))
    if args.approval == "telegram" and operator_timeout <= 0:
        operator_timeout = 3600.0                 # the Telegram helper needs a finite timeout
    operator = KeyFileOperator(os.environ["MOODLE_OPERATOR_SIGNING_KEY_FILE"], mode=args.approval, timeout_s=operator_timeout)
    cfg = h.RunConfig(scenario=args.scenario, arm=args.arm, seq=args.seq)
    rec = h.run_one(cfg, backend=CommandBackend(), agent=ApiAgent(), operator=operator, proposer=h.LockedModelProposer(),
                    mapping=mapping, catalog=catalog, out_dir=args.out, clock=h.RealClock())
    print(json.dumps({k: rec[k] for k in ("run_id", "arm", "outcome", "resolved", "reset_verified", "metrics", "invariants")},
                     indent=2, sort_keys=True))
    if not rec["reset_verified"]:
        return 4                                  # stop the campaign: the environment is not back to baseline
    return 0 if all(rec["invariants"].values()) else 5


sys.modules.setdefault("live_c0_live", sys.modules[__name__])
