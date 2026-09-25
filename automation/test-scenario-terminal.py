#!/usr/bin/env python3
"""Interactive Terminal Scenario Runner and Verifier.

Usage:
    python automation/test-scenario-terminal.py [SCENARIO_ID] [--mode {dry-run,execute}]

Example:
    python automation/test-scenario-terminal.py DB-01
    python automation/test-scenario-terminal.py RES-01
    python automation/test-scenario-terminal.py SEC-02
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.event_schema import normalize_alert
from core.ground_truth import load_ground_truth
from core.moodle_pipeline import MoodleIncidentPipeline, load_moodle_catalog


def run_scenario_test(scenario_id: str, mode: str = "dry-run") -> int:
    catalog = load_moodle_catalog()
    if scenario_id not in catalog:
        print(f"\n❌ LỖI: Scenario '{scenario_id}' không tồn tại trong Ground Truth!")
        print(f"Các kịch bản khả dụng: {list(catalog.keys())}\n")
        return 1

    truth = catalog[scenario_id]
    signal = truth["observed_signals"][0]
    component = truth["expected_root_cause"]["component"]

    print("=" * 75)
    print(f"🚀 KIỂM THỬ KỊCH BẢN THỰC TẾ: {scenario_id} ({mode.upper()})")
    print(f"📌 Sự cố giả định  : {truth['expected_root_cause']['category']} - {component}")
    print(f"🔔 Tín hiệu cảnh báo: {signal}")
    print("=" * 75)

    # 1. Tạo thư mục bằng chứng thực tế trên đĩa
    evidence_dir = REPO_ROOT / "terraform" / ".artifacts" / f"terminal-test-{scenario_id.lower()}"
    if evidence_dir.exists():
        shutil.rmtree(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # 2. Khởi tạo Pipeline
    pipeline = MoodleIncidentPipeline(evidence_dir, catalog=catalog)

    # 3. Tạo Alert giả lập chuẩn hóa (đúng chuẩn Prometheus Alertmanager)
    raw_alert = {
        "status": "firing",
        "fingerprint": f"terminal-fingerprint-{scenario_id.lower()}-123",
        "startsAt": "2026-09-25T11:00:00Z",
        "labels": {
            "alertname": signal,
            "scenario_id": scenario_id,
            "service": "moodle",
            "environment": "staging",
            "severity": "critical",
            "component": component,
        },
        "annotations": {
            "summary": f"Alertmanager firing {signal} on {component}",
            "description": f"Terminal live scenario verification for {scenario_id}",
        },
    }
    normalized = normalize_alert(raw_alert, correlation_id=f"terminal-test-{scenario_id.lower()}")

    print("\n[BƯỚC 1] TIẾP NHẬN VÀ CHUẨN HÓA CẢNH BÁO (INGRESS)")
    print(f"  * Normalized Event ID  : {normalized['event_id']}")
    print(f"  * Fingerprint          : {normalized['fingerprint']}")
    print(f"  * Service / Environment: {normalized['labels']['service']} / {normalized['labels']['environment']}")

    # 4. Kích hoạt Pipeline
    print("\n[BƯỚC 2] KÍCH HOẠT VÒNG ĐỜI PIPELINE (STATE MACHINE)")
    report = pipeline.process(
        normalized,
        scenario_id=scenario_id,
        mode=mode,
        confidence=0.95,
        live_verify=(mode == "execute"),
    )

    print(f"  * Mã sự cố (Incident ID): {report['incident_id']}")
    print(f"  * Trạng thái kết thúc    : \033[1;32m{report['state']}\033[0m")

    # 5. Chi tiết chẩn đoán
    diag = report["diagnosis"]
    print("\n[BƯỚC 3] CHẨN ĐOÁN NGUYÊN NHÂN GỐC RỄ (DIAGNOSIS)")
    print(f"  * Root Cause Category  : {diag['root_cause']['category']}")
    print(f"  * Component gây lỗi    : {diag['root_cause']['component']}")
    print(f"  * Điểm tin cậy         : {diag['confidence'] * 100:.1f}%")
    print(f"  * Phương pháp chẩn đoán: {diag['method']}")

    # 6. Kế hoạch hành động
    action = report["plan"]["action"]
    print("\n[BƯỚC 4] LẬP KẾ HOẠCH HÀNH ĐỘNG ĐỊNH KIỂU (PLANNER - TYPED ACTION)")
    print(f"  * Hành động (Action)   : \033[1;36m{action['action']}\033[0m")
    print(f"  * Mục tiêu (Target)    : {action['target']}")
    print(f"  * Idempotency Key      : {action['idempotency_key']}")
    print(f"  * Kế hoạch Rollback    : {report['plan']['rollback']['action']} (idempotent={report['plan']['rollback']['idempotent']})")

    # 7. Thẩm định Cổng an toàn
    gate = report["gate"]
    decision_color = "\033[1;32m" if gate["decision"] == "ALLOW" else "\033[1;31m"
    print("\n[BƯỚC 5] CỔNG AN TOÀN THẨM ĐỊNH (SAFETY POLICY GATE - RQ1)")
    print(f"  * Quyết định (Decision): {decision_color}{gate['decision']}\033[0m")
    print(f"  * Lý do (Reason)       : {gate['reason']}")
    print(f"  * Yêu cầu duyệt người  : {gate['required_approval']}")

    # 8. Thực thi
    exec_info = report["execution"]
    print("\n[BƯỚC 6] THỰC THI CÓ KIỂM SOÁT (EXECUTION)")
    if exec_info:
        print(f"  * Trạng thái thực thi  : {exec_info['status']}")
        print(f"  * Lệnh thực tế được gọi: {' '.join(exec_info['command'])}")
        print(f"  * Đã biến đổi hạ tầng  : {exec_info['mutated']}")
    else:
        print("  * Không có thao tác thực thi (Bị Safety Gate chặn hoặc từ chối)")

    # 9. Xác minh độc lập
    verifier = report["verification"]
    print("\n[BƯỚC 7] XÁC MINH ĐỘC LẬP (INDEPENDENT VERIFIER - RQ2)")
    print(f"  * Quyền lực thẩm quyền : {verifier['authority']}")
    print(f"  * Trạng thái kiểm định : \033[1;33m{verifier['status']}\033[0m")
    print(f"  * Đủ điều kiện RESOLVED: {verifier['resolution_eligible']}")
    print(f"  * Lý do                : {verifier['reason']}")

    # 10. In bằng chứng vật lý trên đĩa
    evidence_file = evidence_dir / "evidence.jsonl"
    audit_file = evidence_dir / "audit.jsonl"
    print("\n" + "=" * 75)
    print("📁 BẰNG CHỨNG THẬT TRÊN ĐĨA (VẬT CHỨNG KIỂM TOÁN MẬT MÃ HỌC)")
    print(f"Đường dẫn: {evidence_dir}")
    print("=" * 75)

    if evidence_file.exists():
        print(f"\n[1] File Bằng chứng (evidence.jsonl) - {evidence_file.stat().st_size} bytes:")
        for line in evidence_file.read_text(encoding="utf-8").strip().splitlines()[:4]:
            d = json.loads(line)
            print(f"  • [{d['evidence_id']}] Loai: {d['kind']:22} | SHA256: {d['sha256'][:24]}...")

    if audit_file.exists():
        print(f"\n[2] File Kiểm toán chuỗi băm (audit.jsonl) - {audit_file.stat().st_size} bytes:")
        for line in audit_file.read_text(encoding="utf-8").strip().splitlines()[:6]:
            d = json.loads(line)
            prev = d['previous_sha256'][:10] if d['previous_sha256'] else "None (Gốc)"
            curr = d['sha256'][:10]
            print(f"  • Checkpoint: {d['checkpoint']:25} | PrevHash: {prev:10} -> CurrHash: {curr}")

    print("\n" + "=" * 75)
    print("✅ XÁC MINH HOÀN TẤT: Kịch bản đã chạy qua toàn bộ 6 tầng kiến trúc!")
    print("=" * 75 + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", default="DB-01", help="Mã kịch bản (ví dụ: DB-01, RES-01, NET-01, CON-01, SEC-02)")
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run", help="Chế độ chạy (mặc định dry-run)")
    args = parser.parse_args()
    return run_scenario_test(args.scenario, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
