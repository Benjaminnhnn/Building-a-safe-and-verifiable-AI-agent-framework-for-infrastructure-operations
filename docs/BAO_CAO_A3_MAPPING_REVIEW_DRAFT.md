# A3 — Bản nháp mapping action C0 → Moodle (chờ domain owner duyệt)

**Cập nhật:** 16/11/2026 · **Trạng thái:** `draft_pending_domain_review` · **0/6** action `live_reviewed`.

Tài liệu này là **danh sách kiểm tra cho người chịu trách nhiệm miền Moodle** (bạn). Agent chỉ soạn bản nháp
`evaluation/aihs/a3-c0-action-mapping-draft.json`; agent **không** đặt `live_reviewed` và **không** điền
`review_reference`. Duyệt mapping **không** có nghĩa là cho chạy: mỗi lượt live vẫn cần Safety Gate, chữ ký
Operator gắn với action, Executor và Independent Verifier.

Lưu ý nền: `a3-action-mapping.json` cũ dùng 6 mã server Alibaba (NOP/MSR/ER/RB/RI/RMA) và giữ nguyên là
`unmapped`. C0 (`aihs-paper-repro-moodle-v1`) đề xuất trực tiếp **mã action trong catalog Moodle**, nên bản nháp
mới dùng bộ 8 lớp của C0: 6 action thay đổi hệ thống + `wait_and_recheck` + `call_human`.

## 1. Bảng mapping cần duyệt (6 action live)

| # | Action C0 (= catalog_action_id) | Kịch bản | target_scope | Tài nguyên (loại) | Vai trò | Blast | Rollback (catalog) | Probe Verifier (catalog) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `remove_scoped_db_reject` | DB-01 | `staging_moodle_nodes` | `postgres-db` (database) | executor | LOW | `reconcile_db01_safe_baseline` | `verify_tls_database_connection` |
| 2 | `recreate_moodle_web_from_reviewed_compose` | NET-01 | `staging_moodle_nodes` | `moodle-app` (application) | executor | **MEDIUM** | `reconcile_net01_safe_baseline` | `verify_database_hostname` |
| 3 | `remove_named_cpu_load_container` | RES-01 | `moodle-app-b` | `moodle-app` (application) | executor | LOW | `reconcile_res01_safe_baseline` | `verify_node_cpu_recovers` |
| 4 | `start_reviewed_compose_service` | CON-01 | `moodle-app-b` | `moodle-app` (application) | executor | LOW | `reconcile_con01_safe_baseline` | `wait_for_alb_target_health` |
| 5 | `restore_fixture_directory_mode` | SEC-02 | `moodledata_synthetic_fixture_directory` | `moodledata-volume` (volume) | executor | LOW | `reconcile_sec02_safe_baseline` | `verify_efs_write` |
| 6 | `restart_moodle_cron_service` | BIZ-02 | `staging_moodle_app` | `moodle-app` (application) | executor | LOW | `reconcile_biz02_cron_baseline` | `verify_moodle_cron_running` |
| 7 | `wait_and_recheck` | — | — | — | — | không đổi | — | — (chỉ quan sát, `observe_only`) |
| 8 | `call_human` | — | — | — | — | không đổi | — | — (luôn chuyển Operator, `human_only`) |

Không thuộc phạm vi live (giữ ngoài mapping): SEC-01 (human-only), DB-02 (đổi cấu hình AWS), BIZ-01 (bật/tắt
maintenance toàn site), mọi action `reconcile_*` (chỉ để reset). Cron (`restart_moodle_cron_service`) chỉ có trong
catalog nghiệp vụ/7 kịch bản, **không** có trong `load_moodle_catalog()`; adapter C0 dùng catalog 7 kịch bản.

## 2. Việc bạn phải kiểm tra cho TỪNG action 1–6

Chỉ tích khi đã tự đối chiếu với mã/hạ tầng, không dựa vào tóm tắt này:

- [ ] **Tương đương:** action catalog làm đúng việc đã mô tả trong trường `reason` của bản nháp, không hơn.
- [ ] **Target/phạm vi:** `target_scope`, `target_resource_id`, loại tài nguyên khớp
  `automation/moodle-safe-executor-api.py` (CATALOG) và `_S3_3_STAGING_EXECUTION_SCOPES` /
  `_BIZ_STAGING_EXECUTION_SCOPES` trong `agent_src/core/safety/action_catalog.py`; không có host/lệnh tùy ý.
- [ ] **Điều kiện trước (`preconditions`):** đủ để không chạy nhầm lỗi khác (đặc biệt NET-01 và SEC-02 cùng alert
  `MoodleSyntheticTransactionFailed`; chỉ đúng khi có dấu hiệu runtime riêng của từng lỗi).
- [ ] **Rollback:** `rollback_action_id` là action đảo ngược có thật, idempotent, và rollback đã diễn tập trên lab.
- [ ] **Probe xác minh:** `verifier_probe_id` đủ để Verifier phát hiện phục hồi giả; kèm probe allowed/forbidden/
  related và cửa sổ ổn định (không rút ngắn để có RTO đẹp).
- [ ] **Rủi ro:** NET-01 (MEDIUM, tạo lại container ở cả hai node) chấp nhận chỉ chạy khi Operator duyệt; cron chỉ
  khởi động lại container, không sửa bảng DB.
- [ ] **Phê duyệt:** mọi action vẫn `requires_approval`; không có đường tự động ký hoặc tự thực thi.

Với `wait_and_recheck` và `call_human`: xác nhận giữ nguyên `observe_only`/`human_only` (không mapping, không vào
Gate, không thực thi).

## 3. Cách ghi nhận phê duyệt

1. Sao `evaluation/aihs/a3-c0-action-mapping-draft.json` thành
   `evaluation/aihs/a3-c0-action-mapping-reviewed.json` (không sửa bản nháp). Chỉ sửa tay các action bạn đã duyệt:
   - `status`: `"draft_unreviewed"` → `"live_reviewed"` (hoặc `"shadow_reviewed"` nếu chỉ duyệt cho shadow);
   - `review_reference`: chuỗi định danh người duyệt + ngày + nơi lưu biên bản, ví dụ
     `"REVIEW-2026-11-DD-<tên>-<ticket hoặc commit>"` (bắt buộc khi `live_reviewed`);
   - `review_status` cấp trên: `"reviewed_for_live"` khi có ít nhất một action `live_reviewed`; `mapping_version`
     đổi thành `aihs-c0-moodle-reviewed-v1`.
   Action chưa duyệt giữ `draft_unreviewed` (hệ thống fail-closed `unmapped_action`).
2. Kiểm tra định dạng: `PYTHONPATH=agent_src python -c "from core.moodle.aihs_c0 import load_c0_mapping as l; print(l('evaluation/aihs/a3-c0-action-mapping-reviewed.json').review_status)"`.
3. Chạy lại `PYTHONPATH=agent_src python -m pytest -q agent_src/tests/test_aihs_c0_contract.py`.
4. Ghi biên bản (ai, ngày, action nào duyệt, điều kiện kèm theo) vào nơi `review_reference` trỏ tới.
5. Báo lại cho agent giai đoạn sau; agent chỉ **đọc** file đã duyệt, không tự sửa trạng thái duyệt.

## 4. Kết quả replay offline trên proposal C0 thật (tập test, chưa mở nhãn)

Nguồn: `evaluation/aihs/c0-shadow-replay-2026-11-16.json`. Gate chạy với mapping **nháp** chỉ trong chế độ replay
(`draft_shadow_candidate`); staging handoff dùng bộ chuyển đổi nghiêm ngặt và **chặn cả 59** (`unmapped_action` 54,
`human_only_action` 5). Không có ALLOW (mọi action thay đổi hệ thống cần chữ ký Operator), không có DENY, 0 lần gọi
Executor, 0 incident `RESOLVED`. Số liệu này không dùng nhãn hay kết quả nên không phải điểm đánh giá.

Giới hạn cần biết khi đọc: (a) scenario trong ngữ cảnh lấy từ chính mapping của action, nên Gate xác nhận "request
hợp lệ trong phạm vi của nó", không xác nhận action đúng với sự cố — việc đó thuộc chấm điểm giai đoạn 4;
(b) proposal sinh hàng loạt sau sự kiện, nên với `predicted_at` ghi nhận cả 54 đề xuất bị chặn `stale_evidence`
(fail-closed đúng thiết kế); replay chính dùng `evidence_cutoff_at + 1s`.
