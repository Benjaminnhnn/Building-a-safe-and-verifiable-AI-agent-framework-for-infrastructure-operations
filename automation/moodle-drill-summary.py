#!/usr/bin/env python3
"""Send measured recovery summary after signed verifier acceptance."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from telegram_client import TelegramClient, TelegramError  # noqa: E402


def format_summary(evidence: dict) -> str:
    if evidence.get("status") != "passed" or (evidence.get("runtime_status") or {}).get("status") != "resolved":
        raise ValueError("signed verification has not resolved the incident")
    times = evidence.get("stage_times") or {}
    detected = datetime.fromisoformat(times["t_detect"].replace("Z", "+00:00"))
    resolved = datetime.fromisoformat(times["t_resolved"].replace("Z", "+00:00"))
    rto = round((resolved - detected).total_seconds())
    if rto < 0:
        raise ValueError("resolution time precedes detection")
    samples = (evidence.get("verifier") or {}).get("probe_samples") or []
    passed = sum(bool(sample.get("checks")) and all(sample["checks"].values()) for sample in samples)
    rate = round(100 * passed / len(samples)) if samples else 0
    alert_line = (
        ["Alert MoodleWebContainerMissing trên node B: đã hết firing."]
        if evidence.get("scenario_id") == "CON-01" and evidence.get("alert_resolved") == "passed"
        else []
    )
    return "\n".join([
        f"🏁 KHÔI PHỤC {evidence.get('scenario_id')} · RESOLVED (1/1 sự cố)",
        *alert_line,
        f"Verifier: {passed}/{len(samples)} mẫu đạt ({rate}%), attestation đã được Agent chấp nhận.",
        f"RTO đo từ lúc phát hiện đến RESOLVED: {rto} giây.",
        "RPO: không áp dụng; kịch bản này không khôi phục dữ liệu từ bản sao lưu.",
        f"Evidence: {evidence.get('run_id')}",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    message = format_summary(evidence)
    try:
        TelegramClient().send(message)
    except TelegramError as exc:
        print(f"Telegram recovery summary failed: {exc}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
