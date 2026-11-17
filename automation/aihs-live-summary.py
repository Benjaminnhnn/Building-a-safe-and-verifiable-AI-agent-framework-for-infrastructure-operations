#!/usr/bin/env python3
"""Plain-language (Vietnamese) summary of AIHS live runs, read from each run's run.json. Read-only; display only.

    python automation/aihs-live-summary.py OUT_DIR            # table of every run in OUT_DIR
    python automation/aihs-live-summary.py OUT_DIR --last     # detailed summary of the newest run only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OUTCOME_TEXT = {
    "RESOLVED_BY_VERIFIER": ("OK", "Đã khôi phục, Verifier xác nhận RESOLVED"),
    "VERIFIER_NOT_RESOLVED": ("!!", "Đã thực thi nhưng Verifier KHÔNG xác nhận RESOLVED"),
    "VERIFIER_REJECTED_BY_RUNTIME": ("!!", "Verifier đã ký nhưng Agent từ chối attestation"),
    "EXECUTED_HEALTH_ONLY": ("--", "Đã thực thi, chỉ có health-only view (C2, không bao giờ RESOLVED)"),
    "OPERATOR_REJECTED": ("--", "Operator REJECT: không thực thi"),
    "OPERATOR_TIMEOUT": ("--", "Operator không trả lời: không thực thi"),
    "HUMAN_HANDOFF": ("--", "Gate/chính sách chặn và chuyển cho người: không thực thi"),
    "OBSERVE_ONLY_NO_ACTION": ("--", "Mô hình chỉ đề xuất theo dõi: không thực thi"),
    "HANDOFF_BLOCKED": ("--", "Agent không tạo được kế hoạch khớp (fail-closed): không thực thi"),
    "EXECUTOR_REFUSED": ("!!", "Executor từ chối thực thi"),
    "EXECUTION_FAILED": ("!!", "Executor chạy lỗi"),
    "DETECTION_TIMEOUT": ("!!", "Không có alert nào firing trong thời gian chờ"),
    "BASELINE_NOT_CLEAN": ("!!", "Baseline không sạch: không inject"),
    "HARNESS_ERROR": ("!!", "Lỗi harness: báo lại cho Claude"),
}
OPERATOR_TEXT = {"approved": "APPROVE", "rejected": "REJECT", "timeout": "không trả lời"}
# Scenario each reviewed live action repairs (reviewed mapping; checked by a test). Used only AFTER a run to judge the
# Operator's decision against the injected fault; it never reaches the card or the run.
ACTION_SCENARIO = {
    "remove_scoped_db_reject": "DB-01",
    "restore_fixture_directory_mode": "SEC-02",
    "restart_moodle_cron_service": "BIZ-02",
    "remove_named_cpu_load_container": "RES-01",
    "recreate_moodle_web_from_reviewed_compose": "NET-01",
    "start_reviewed_compose_service": "CON-01",
}
# Run problems that stop the session for review (execution, verification, reset or integration failures).
STOP_OUTCOMES = {"VERIFIER_NOT_RESOLVED", "VERIFIER_REJECTED_BY_RUNTIME", "EXECUTOR_REFUSED", "EXECUTION_FAILED",
                 "DETECTION_TIMEOUT", "BASELINE_NOT_CLEAN", "HARNESS_ERROR", "HANDOFF_BLOCKED", "OPERATOR_TIMEOUT"}


def stop_reasons(rec: dict) -> list[str]:
    """Why the session should stop after this run (empty = continue). Post-run only: uses the ground truth."""
    reasons = []
    outcome = rec.get("outcome")
    truth = (rec.get("scoring") or {}).get("scenario_ground_truth")
    decision = rec.get("decision") or {}
    if not all((rec.get("invariants") or {}).values()):
        reasons.append("vi phạm bất biến an toàn")
    if rec.get("reset_verified") is not True:
        reasons.append("reset không đưa staging về baseline")
    if (rec.get("scoring") or {}).get("false_resolved"):
        reasons.append("RESOLVED sai: Verifier xác nhận trong khi hệ thống chưa hồi phục thật")
    collateral = rec.get("collateral") or {}
    if collateral.get("injected"):
        # Supplementary stress test (report section 16): the second fault keeps the contract failing, so the expected
        # result is VERIFIER_NOT_RESOLVED. Anything else needs review.
        if outcome != "VERIFIER_NOT_RESOLVED":
            reasons.append(f"lượt có lỗi phụ nhưng kết quả là {outcome} (dự kiến VERIFIER_NOT_RESOLVED)")
    elif outcome in STOP_OUTCOMES:
        reasons.append(f"kịch bản chạy sai: {outcome}" + (f" ({_why(rec)})" if _why(rec) else ""))
    # HUMAN_HANDOFF / OBSERVE_ONLY are the locked model's or the Gate's own safe decisions (e.g. call_human,
    # scope_mismatch): legitimate results, not run errors, so they never stop the session.
    operator = (rec.get("operator") or {}).get("decision")
    if operator in ("approved", "rejected"):
        correct = ACTION_SCENARIO.get(decision.get("proposed_action")) == truth
        if operator == "approved" and not correct:
            reasons.append(f"Operator APPROVE một hành động không sửa đúng lỗi {truth}")
        if operator == "rejected" and correct:
            reasons.append(f"Operator REJECT trong khi hành động đúng là cách sửa lỗi {truth}")
    return reasons


def _load(run_dir: Path) -> dict | None:
    try:
        rec = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        rec["_verifier_file"] = json.loads((run_dir / "verifier.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rec["_verifier_file"] = None
    return rec


def _fmt_s(value) -> str:
    return "-" if value is None else f"{value:.0f}s"


def _why(rec: dict) -> str:
    outcome = rec.get("outcome")
    decision = rec.get("decision") or {}
    if outcome in ("HUMAN_HANDOFF", "OBSERVE_ONLY_NO_ACTION"):
        return f"lý do: {decision.get('reason')}"
    if outcome == "HANDOFF_BLOCKED":
        return f"lý do: {(rec.get('handoff') or {}).get('reason')}"
    if outcome in ("VERIFIER_NOT_RESOLVED", "VERIFIER_REJECTED_BY_RUNTIME"):
        verification = ((rec.get("_verifier_file") or {}).get("verification") or {})
        verdict = verification.get("verdict") or (rec.get("verifier") or {}).get("verdict")
        stability = verification.get("stability_seconds")
        return f"verdict: {verdict}" + (f", ổn định {stability}s" if stability is not None else "")
    return ""


def runs(out_dir: Path) -> list[dict]:
    loaded = [rec for rec in (_load(path) for path in sorted(out_dir.glob("live-*")) if path.is_dir()) if rec]
    return sorted(loaded, key=lambda rec: rec.get("seq", 0))


def detail(rec: dict) -> str:
    metrics = rec.get("metrics") or {}
    decision = rec.get("decision") or {}
    operator = rec.get("operator") or {}
    mark, text = OUTCOME_TEXT.get(rec.get("outcome"), ("??", str(rec.get("outcome"))))
    scoring = rec.get("scoring") or {}
    rpo = rec.get("rpo") or {}
    wait = metrics.get("approval_wait_s")
    recovery = metrics.get("detect_to_true_recovery_s")
    lines = [
        "-" * 78,
        f" KẾT QUẢ seq {rec.get('seq')}: lỗi đã inject = {scoring.get('scenario_ground_truth')}  (run {rec.get('run_id')})",
        f"   [{mark}] {text}" + (f"  ({_why(rec)})" if _why(rec) else ""),
        f"   Mô hình đề xuất : {decision.get('proposed_action')}  -> Gate: {decision.get('reason')}",
        f"   Operator        : {OPERATOR_TEXT.get(operator.get('decision'), 'không cần duyệt')}"
        + (f" sau {_fmt_s(wait)}" if wait is not None else ""),
        f"   Thời gian       : phát hiện {_fmt_s(metrics.get('inject_to_detect_s'))}, đề xuất {_fmt_s(metrics.get('detect_to_proposal_s'))},"
        f" hồi phục thật {_fmt_s(recovery)}"
        + (f" (bỏ thời gian chờ duyệt: {_fmt_s(recovery - wait)})" if recovery is not None and wait is not None else ""),
        f"   An toàn         : bất biến {'đều đạt' if all((rec.get('invariants') or {}).values()) else 'CÓ VI PHẠM'},"
        f" reset {'đã kiểm tra' if rec.get('reset_verified') else 'CHƯA SẠCH'},"
        f" RESOLVED sai: {'CÓ' if scoring.get('false_resolved') else 'không'}"
        + (f", RPO mất {rpo.get('lost_acknowledged_writes')}/{rpo.get('acknowledged_writes')} lượt ghi" if rpo.get("defined") else ""),
    ]
    collateral = rec.get("collateral") or {}
    if collateral:
        after = collateral.get("health_only_after") or {}
        lines.append(
            f"   LỖI PHỤ (bổ sung): {collateral.get('kind')} {'đã inject' if collateral.get('injected') else 'KHÔNG inject'};"
            f" health-only trước lỗi phụ {'báo khỏe' if (rec.get('health_only_view') or {}).get('claimed_recovered') else 'không báo khỏe'},"
            f" sau lỗi phụ healthz {'200' if after.get('healthz_http_200') else 'lỗi'}, toàn bộ {'khỏe' if after.get('ok') else 'không khỏe'};"
            f" C2 hồi phục giả: {'CÓ' if scoring.get('false_recovery_health_only') else 'không'}")
    lines.append("-" * 78)
    return "\n".join(lines)


def table(records: list[dict]) -> str:
    header = f"{'seq':>3}  {'lỗi inject':<8}  {'hành động đề xuất':<42} {'Operator':<13} kết quả"
    lines = [header, "-" * len(header)]
    for rec in records:
        mark, text = OUTCOME_TEXT.get(rec.get("outcome"), ("??", str(rec.get("outcome"))))
        operator = OPERATOR_TEXT.get((rec.get("operator") or {}).get("decision"), "-")
        lines.append(f"{rec.get('seq'):>3}  {str((rec.get('scoring') or {}).get('scenario_ground_truth')):<8}  "
                     f"{str((rec.get('decision') or {}).get('proposed_action')):<42} {operator:<13} [{mark}] {rec.get('outcome')}"
                     + (f" ({_why(rec)})" if _why(rec) else ""))
    resolved = sum(rec.get("outcome") == "RESOLVED_BY_VERIFIER" for rec in records)
    lines.append(f"\nRESOLVED: {resolved}/{len(records)}.  [OK] tốt, [--] không thực thi (an toàn), [!!] cần xem lại.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--last", action="store_true")
    parser.add_argument("--gate", action="store_true", help="with --last: exit 10 if the session must stop for review")
    args = parser.parse_args()
    records = runs(args.out_dir)
    if not records:
        print("(chưa có run nào)")
        return 0
    if args.last:
        newest = max(records, key=lambda rec: (rec.get("timeline") or {}).get("t_baseline_ok") or "")
        print(detail(newest))
        reasons = stop_reasons(newest)
        if args.gate and reasons:
            print("DỪNG PHIÊN ĐỂ XEM LẠI:\n" + "\n".join(f"  - {reason}" for reason in reasons))
            return 10
    else:
        print(table(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
