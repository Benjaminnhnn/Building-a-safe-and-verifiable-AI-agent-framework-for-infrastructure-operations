# A3 — Biên bản duyệt mapping action C0 → Moodle

- **Mã tham chiếu:** `REVIEW-2026-11-16-tien-chat-approval-docs/BAO_CAO_A3_MAPPING_REVIEW_RECORD.md`
- **Người duyệt (domain owner):** `tien`
- **Ngày:** 16/11/2026
- **Hình thức:** xác nhận trực tiếp trong phiên làm việc, sau khi đọc
  [BAO_CAO_A3_MAPPING_REVIEW_DRAFT.md](BAO_CAO_A3_MAPPING_REVIEW_DRAFT.md).
- **File đã duyệt:** `evaluation/aihs/a3-c0-action-mapping-reviewed.json` (`mapping_version` =
  `aihs-c0-moodle-reviewed-v1`, `review_status` = `reviewed_for_live`). Bản nháp
  `a3-c0-action-mapping-draft.json` giữ nguyên.

## Nội dung duyệt

| Action | Kịch bản | Trạng thái sau duyệt |
| --- | --- | --- |
| `remove_scoped_db_reject` | DB-01 | `live_reviewed` |
| `recreate_moodle_web_from_reviewed_compose` | NET-01 | `live_reviewed` |
| `remove_named_cpu_load_container` | RES-01 | `live_reviewed` |
| `start_reviewed_compose_service` | CON-01 | `live_reviewed` |
| `restore_fixture_directory_mode` | SEC-02 | `live_reviewed` |
| `restart_moodle_cron_service` | BIZ-02 | `live_reviewed` |
| `wait_and_recheck` | — | xác nhận `observe_only`, không đổi hệ thống |
| `call_human` | — | xác nhận `human_only`, không đổi hệ thống |

Catalog, target, phạm vi, điều kiện trước, rollback và probe Verifier của từng action giữ nguyên như bản nháp.

## Điều kiện kèm theo

- Duyệt mapping **không** phải cho phép chạy. Mỗi lượt live vẫn cần Safety Gate, chữ ký Operator gắn với đúng action,
  Safe Executor và Independent Verifier. Chỉ Verifier được chuyển sự cố sang `RESOLVED`.
- Chỉ áp dụng cho môi trường staging Moodle của đồ án. Các action ngoài danh sách vẫn fail-closed.
- Protocol offline C0–C3 đã khoá trên bản nháp (`a3-c0-action-mapping-draft.json`). Biên bản này không làm thay đổi
  kết quả offline đã chấm.
