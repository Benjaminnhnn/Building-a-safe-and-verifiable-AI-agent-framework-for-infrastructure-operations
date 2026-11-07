# Báo cáo kiến trúc AI Agent và cơ chế an toàn: Safety Gate, Executor, Independent Verifier

**Cập nhật:** 26/10/2026 · **Phạm vi:** Moodle staging trên AWS, theo mã nguồn hiện tại của `agent_src/` và `automation/`.
**Đối tượng đọc:** người cần hiểu Agent quyết định thế nào, cái gì ngăn nó làm sai, và đã chứng minh được tới đâu.

Tài liệu này tách rõ **thiết kế**, **mã nguồn đã có** và **bằng chứng đã đạt**. Một thành phần có trong code chưa có
nghĩa là đã chạy thật; kết quả replay offline chưa có nghĩa là đã khôi phục thật. Các báo cáo liên quan:
[kiến trúc hạ tầng](BAO_CAO_KIEN_TRUC_HA_TANG.md), [hướng dẫn triển khai](BAO_CAO_HUONG_DAN_TRIEN_KHAI.md),
[kịch bản lỗi](BAO_CAO_KICH_BAN_LOI.md).

---

## Mục lục

1. [Nguyên tắc thiết kế](#1-nguyên-tắc-thiết-kế)
2. [Kiến trúc sáu tầng](#2-kiến-trúc-sáu-tầng)
3. [Luồng xử lý một alert](#3-luồng-xử-lý-một-alert)
4. [Bằng chứng, chẩn đoán và đề xuất](#4-bằng-chứng-chẩn-đoán-và-đề-xuất)
5. [Safety Gate](#5-safety-gate)
6. [Safe Executor](#6-safe-executor)
7. [Independent Verifier](#7-independent-verifier)
8. [Phê duyệt và báo cáo qua Telegram](#8-phê-duyệt-và-báo-cáo-qua-telegram)
9. [Hermes Agent và mô hình đa AI](#9-hermes-agent-và-mô-hình-đa-ai)
10. [Mô hình mối đe doạ](#10-mô-hình-mối-đe-doạ)
11. [Trạng thái bằng chứng](#11-trạng-thái-bằng-chứng)

---

## 1. Nguyên tắc thiết kế

**"Quản trị hạ tầng, không quản trị ứng dụng."** Agent không được sửa trực tiếp cấu hình ứng dụng, bảng
`mdl_*`, phân quyền học vụ, khoá học hay sổ điểm của Moodle. Ví dụ: với BIZ-02 (hàng đợi tác vụ kẹt), Agent đề xuất
khởi động lại container `moodle-cron`, **không** chạy SQL xoá khoá trong `mdl_task_adhoc`.

**Bốn lớp ranh giới:**

| Lớp | Agent được làm gì | Cơ chế giữ ranh giới |
|---|---|---|
| 1. Thu thập bằng chứng | Chỉ đọc: Prometheus, Blackbox, trạng thái Docker | Truy vấn cố định, hữu hạn theo từng alert |
| 2. Chẩn đoán | Kết luận phải trỏ tới `evidence_refs` có hash | Luật đã duyệt; mâu thuẫn hoặc thiếu bằng chứng thì escalate |
| 3. Đề xuất | Chỉ hành động có trong catalog, có rollback đối ứng | Catalog hữu hạn; ngoài catalog thì không có đề xuất |
| 4. Thực thi và xác minh | Không tự làm | Safety Gate + một chữ ký Operator; Executor riêng; chỉ Verifier được kết luận `RESOLVED` |

**Bất biến an toàn** (AGENTS.md):
- Hành động lạ hoặc không được phép **fail closed**; phê duyệt khác với thực thi.
- Chỉ Independent Verifier được chuyển incident sang `RESOLVED`.
- Dữ liệu ground truth chỉ dùng để chấm điểm **sau** quyết định, không bao giờ là đầu vào cho quyết định.
- $\text{Actor}(\text{khắc phục}) \neq \text{Actor}(\text{xác minh})$.

---

## 2. Kiến trúc sáu tầng

| Tầng | Trách nhiệm | Mã nguồn |
|---|---|---|
| 1. Tiếp nhận alert | Nhận, chuẩn hoá, xác thực, khử trùng lặp, xếp hàng | `core/main.py` (`/webhook`, bearer token từ file), `core/schema/event_schema.py`, Redis/Celery trong `core/tasks.py` |
| 2. Quan sát và bằng chứng | Gắn tín hiệu vào tài nguyên; thu metric có giới hạn thời gian; lưu nguồn gốc | `core/shadow_pipeline.py`, `core/evidence/evidence_collectors.py`, `core/evidence/evidence_store.py` (SQLite append-only, hash), `core/platform_adapter.py` |
| 3. Ngữ cảnh và tri thức | Runbook theo alert, kèm nguồn, thời điểm, SHA-256; đồ thị phụ thuộc | `core/rag/rag_engine.py`, `config/knowledge_base/moodle/`, `core/evidence/dependency_graph.py` |
| 4. Chẩn đoán và lập kế hoạch | Giả thuyết có bằng chứng; đề xuất hành động có kiểu | `core/moodle/diagnosis.py`, `core/moodle/planner.py` (`MoodlePlatformAdapter`), `core/erpnext/` |
| 5. An toàn và thực thi | Catalog, vai trò, môi trường, phạm vi, bằng chứng, phê duyệt, idempotency, timeout, kill switch | `core/safety/action_catalog.py`, `safe_execution_gate.py`, `safe_action_live.py`, `safe_action_audit.py`, `core/moodle/live_handoff.py`, `automation/moodle-safe-executor-api.py` |
| 6. Xác minh độc lập | Probe allowed/forbidden/related + cửa sổ ổn định; chỉ Verifier được `RESOLVED` | `core/verification/` (`verification_agent.py`, `verifier_contract.py`, `state_machine.py`, `live_contracts.py`), `core/safety/verifier_attestation.py`, `automation/moodle-independent-verifier.py` |

**LLM ở đâu?** Với Moodle, chẩn đoán và đề xuất **không** dùng LLM: chúng là luật đã duyệt trên bằng chứng đo được.
Gemini (`google-genai`) chỉ dùng để xem xét phản hồi của admin, giới hạn số lần gọi, không nằm trên đường alert. Nếu
sau này thêm một bước giả thuyết bằng LLM, Safety Gate và Verifier vẫn là nơi quyết định, và bước đó phải được đo so
với baseline luật.

---

## 3. Luồng xử lý một alert

```text
Alertmanager
  → POST /webhook (bearer token, chuẩn hoá, khử trùng lặp ingress)
  → Redis/Celery (kiểm tra độ sâu hàng đợi) → process_alerts_task
  → process_single_alert (giới hạn đồng thời, cooldown theo fingerprint)
       ├─ alert resolved: xoá cooldown, báo Telegram "hết firing, chưa RESOLVED", kết thúc
       └─ alert firing: process_moodle_alert (core/moodle/alert_integration.py)
             ├─ không phải Moodle: ghi log unsupported, không gọi gì thêm
             ├─ không phải staging: escalate
             ├─ chế độ off/không hợp lệ: escalate (không có đường cũ dự phòng)
             ├─ chế độ live: escalate (thực thi là đường riêng của operator)
             └─ chế độ shadow / investigate:
                   → nhận diện tài nguyên → thu bằng chứng Prometheus mới
                   → chẩn đoán theo luật → đề xuất probe chỉ đọc
                   → (chỉ investigate) đề xuất sửa nếu khớp chính xác dấu hiệu
                   → lưu quyết định thành bản ghi evidence có hash
                   → trả báo cáo: execution_permitted=False, resolution_eligible=False
```

| Chế độ `AIOPS_UNIFIED_CORE_MODE` | Hành vi | Staging đang dùng |
|---|---|---|
| `off` (mặc định) | Mọi alert Moodle escalate | — |
| `shadow` | Chỉ quan sát | — |
| `investigate` | Chẩn đoán, đề xuất probe chỉ đọc hoặc candidate sửa lỗi; **không** bao giờ thực thi | ✅ |
| `live` | Escalate; đường thực thi là handoff riêng của operator | — |

**Cooldown và khử trùng lặp:** danh tính alert lấy từ `fingerprint` của Alertmanager, hoặc hash của
`alertname/instance/job/service/target`. Khoá Redis `alert-ai-cooldown:<identity>`, TTL `ALERT_AI_COOLDOWN_SECONDS`
(mặc định 900). Redis lỗi thì dùng cooldown trong bộ nhớ; alert `resolved` xoá cooldown.

**Đường alert không bao giờ thực thi.** Từ candidate tới thực thi phải đi qua handoff của operator
(`/prepare` → Operator reply `approve` và ký → `/execute`), hoàn toàn tách khỏi đường nhận alert.

---

## 4. Bằng chứng, chẩn đoán và đề xuất

### 4.1. Bằng chứng

- Mỗi alert có tập truy vấn Prometheus cố định (`METRIC_QUERIES_BY_ALERT`, `BUSINESS_METRIC_QUERIES_BY_ALERT`).
- Collector từ chối phản hồi sai dạng, giá trị hoặc timestamp không hữu hạn, vector quá 20 mẫu.
- Bằng chứng lưu trong SQLite append-only, kiểm tra digest khi ghi **và** khi đọc; lỗi toàn vẹn thì escalate.
- Mỗi quyết định (chẩn đoán, đề xuất) là một bản ghi `ai_decision` riêng, trỏ tới bằng chứng đầu vào.
- Cửa sổ mẫu 60 s, tuổi mẫu tối đa 120 s; lệch giờ tương lai quá 30 s bị từ chối.

### 4.2. Chẩn đoán

Luật bảo thủ, theo từng alert. Ví dụ RES-01: độ tin cậy lên **0.80** chỉ khi fixture tải CPU chạy đúng trên B và
không có trên A, web hai node đều chạy, CPU A ≤ 70 % < CPU B. SSO-13: `probe SSO = 0` **và** `trang login = 1` → giả
thuyết `sso_login_option_missing` (0.65); thiếu một trong hai → `conflicting_evidence`. Mẫu "xanh" trong lúc alert
đang firing, mẫu cũ, hoặc thiếu danh tính node đều dẫn tới escalate.

Runbook chỉ là ngữ cảnh tham khảo: chunk được lọc theo tên alert, mang theo đường dẫn nguồn, thời điểm và SHA-256;
nội dung runbook **không** đi vào chẩn đoán hay chọn hành động. Bộ nhớ sự cố động bị loại khỏi ngữ cảnh Moodle.

### 4.3. Đề xuất và catalog

| Kịch bản | Hành động (catalog) | Phạm vi | Rollback đối ứng | Disposition |
|---|---|---|---|---|
| BIZ-01 | `disable_moodle_cli_maintenance_mode` (chỉ khi không có upgrade đang chạy) | `staging_moodle_app` | `enable_moodle_cli_maintenance_mode` | Cho đề xuất, cần duyệt |
| BIZ-02 | `restart_moodle_cron_service` | `staging_moodle_app` | `reconcile_biz02_cron_baseline` | Cho đề xuất, cần duyệt |
| CON-01 | `start_reviewed_compose_service` | `moodle-app-b` | `reconcile_con01_safe_baseline` | Cho đề xuất, cần duyệt |
| DB-01 | `remove_scoped_db_reject` | `staging_moodle_nodes` | `reconcile_db01_safe_baseline` | Cho đề xuất, cần duyệt |
| RES-01 | `remove_named_cpu_load_container` | `moodle-app-b` | `reconcile_res01_safe_baseline` | Cho đề xuất, cần duyệt |
| NET-01 | `recreate_moodle_web_from_reviewed_compose` | `staging_moodle_nodes` | `reconcile_net01_safe_baseline` | Cho đề xuất, cần duyệt |
| SEC-02 | `restore_fixture_directory_mode` | thư mục fixture | `reconcile_sec02_safe_baseline` | Cho đề xuất (bộ 15 cũ) |
| SEC-01 | `remove_tagged_database_ingress_rule` (HIGH) | SG của RDS | — | **Chỉ người** |
| SSO-13 | `restore_moodle_oauth2_issuer` (operator) | issuer OAuth 2 | — | **Chỉ người** |

Bộ 15 kịch bản kỹ thuật cũ (cho CI replay) dùng `SCENARIO_REMEDIATION_DISPOSITION`; bộ 7 kịch bản luận văn dùng thêm
`BUSINESS_SCENARIO_REMEDIATION_DISPOSITION` và `load_moodle_7_scenario_catalog()`. Test
`agent_src/tests/test_executor_catalog_contract.py` buộc catalog của Agent, allowlist của Executor API, công cụ ký và
bảng forced-command luôn trùng khớp.

---

## 5. Safety Gate

### 5.1. Các kiểm tra, theo đúng thứ tự trong code

`SafeExecutionGate.evaluate()` kiểm tra lần lượt và **dừng ở lỗi đầu tiên** (`core/safety/safe_execution_gate.py`):

| # | Kiểm tra | Từ chối khi |
|---|---|---|
| 1 | Schema của yêu cầu và phê duyệt | Sai kiểu, thiếu trường, có trường lạ |
| 2 | Kill switch | Đang bật |
| 3 | Độ tin cậy hợp lệ | Không phải số hữu hạn trong [0, 1] (boolean bị từ chối) |
| 4 | Idempotency | Khoá đã qua Gate trước đó |
| 5 | Số bằng chứng | Ít hơn **3** tham chiếu khác nhau |
| 6 | Tham số | Có tham số do người gọi đưa vào (executor cố định không nhận) |
| 7 | Catalog và timeout | Hành động không có trong catalog, hoặc timeout vượt giới hạn |
| 8 | Rollback | Thiếu rollback đối ứng, hoặc rollback không phải hành động staging nghịch đảo |
| 9 | Tính hoàn tác | Khác với catalog; hành động thay đổi trạng thái mà thiếu kế hoạch rollback |
| 10 | Kiểu hành động | Khác kiểu đã review trong catalog |
| 11 | RBAC, phạm vi, môi trường | Ngoài kịch bản, phạm vi, môi trường hoặc vai trò được phép |
| 12 | Mục tiêu | Tài nguyên ngoài phạm vi, hoặc lớp tài nguyên không hợp lệ |
| 13 | Ngưỡng độ tin cậy | Dưới **0.65** |
| 14 | Phê duyệt | Cần duyệt nếu catalog yêu cầu, **hoặc** độ tin cậy < 0.80, **hoặc** blast radius khác `LOW`. Khi cần, phải có chữ ký hợp lệ gắn đúng action hash, chưa hết hạn |

Đạt hết thì Gate trả `ALLOW` và giữ chỗ idempotency key một cách nguyên tử, để hai yêu cầu song song không cùng
được cho qua.

### 5.2. Gate tự giải thích (`explain`)

Khi chuẩn bị yêu cầu (`/prepare`), Gate chạy `explain()`: gọi `evaluate()` **không kèm chữ ký**, nên không thể cho
qua một hành động cần duyệt. Khi `gate_preview` trả `REQUIRE_APPROVAL`, tin Telegram liệt kê 8 nhóm đã đạt: cấu trúc
yêu cầu; kill switch; bằng chứng; catalog và timeout; rollback; vai trò, phạm vi và môi trường; mục tiêu và loại tài
nguyên; độ tin cậy chẩn đoán. Đây là kiểm tra trước duyệt, chưa phải `ALLOW`.

### 5.3. Một chữ ký Operator

Operator gửi `approve` trong nhóm hoặc reply tin yêu cầu của bot. Cầu nối chỉ chấp nhận Telegram user ID trong allowlist,
đúng chat, tin gửi sau yêu cầu; mỗi nhóm chỉ có một yêu cầu chờ trên máy vận hành. Sau đó máy Operator ký Ed25519 lên digest gồm môi trường staging, kịch bản, hành động catalog, phạm
vi và idempotency key. Chữ ký hết hạn sau 5 phút. Safety Gate trong Agent và Executor API trên monitor cùng kiểm tra
chữ ký bằng khóa công khai Operator. Gate vẫn kiểm tra toàn bộ yêu cầu, bằng chứng, catalog, rollback và kill switch;
Executor vẫn chỉ chạy hành động cố định trong allowlist. Agent không giữ khóa riêng.

**Giới hạn:** chỉ có một người phê duyệt nên không có kiểm tra chéo của người thứ hai. Cần bảo vệ khóa riêng Operator
trên máy vận hành và giữ `TELEGRAM_ADMIN_USER_IDS` tối thiểu. Independent Verifier dùng khóa riêng khác để ký kết quả
khôi phục; chỉ attestation hợp lệ mới chuyển incident sang `RESOLVED`.

### 5.4. Handoff của operator

`core/moodle/live_handoff.py`:
- `prepare`: lấy candidate còn hợp lệ, dựng yêu cầu với `request_id` và `idempotency_key` mới, trả
  `status = approval_required`, `execution_permitted = false`, hai digest và `gate_preview`. **Không bao giờ dispatch.**
- `execute`: đọc lại candidate, dựng lại yêu cầu, **so khớp tuyệt đối** với yêu cầu đã duyệt; khác là từ chối. Chỉ
  chạy khi công tắc `AIOPS_SAFE_ACTION_LIVE_ENABLED` bật. `SafeActionLiveWorkflow` ghi audit chuỗi hash, gọi Executor,
  và điều phối rollback có điều kiện khi thực thi một phần.
- Nếu bằng chứng ban đầu đã cũ, `execute` thu thập lại probe chỉ đọc; chỉ tiếp tục khi chẩn đoán mới vẫn khớp cùng
  hành động, phạm vi và mục tiêu đã duyệt. Bằng chứng mới được ghi vào snapshot audit; thiếu probe hoặc khác hành động
  thì từ chối, không thực thi.

---

## 6. Safe Executor

| Lớp bảo vệ | Cơ chế (`automation/moodle-safe-executor-api.py`) |
|---|---|
| Công tắc live | `EXECUTOR_LIVE_ENABLED` (mặc định tắt); tắt thì trả `423 disabled` |
| Xác thực Agent | HMAC-SHA256 trên `POST`, đường dẫn, timestamp, nonce và hash body; so sánh thời gian hằng |
| Chống replay | Nonce dùng một lần (lưu SQLite) + lệch giờ tối đa **30 s** |
| Allowlist | Đúng 14 bộ (kịch bản, hành động, phạm vi) → node đích do server quyết định, không lấy từ người gọi |
| Chữ ký operator | Kiểm tra lại độc lập bằng khoá công khai riêng; sai, thiếu, hết hạn hoặc khác hành động → `403 awaiting_approval` |
| Idempotency bền | Khoá idempotency lưu cùng hash body; gửi lại cùng khoá với body khác bị từ chối |
| Log | Không ghi body, phê duyệt, chữ ký hay query string |
| Trên node | SSH bằng key **forced-command**: wrapper `/usr/local/sbin/moodle-safe-executor` chỉ biết các hành động đã review; lệnh khác → `forced command rejected`, **exit 64** |

Executor **không có shell**. Hành động chỉ-người (ví dụ `restore_moodle_oauth2_issuer`, mọi thay đổi SG của RDS)
không nằm trong allowlist lẫn wrapper; CI kiểm tra `moodle-scenario-verify.sh SEC-01` trả 64.

---

## 7. Independent Verifier

**Ai:** `automation/moodle-independent-verifier.py`, chạy trên máy operator, **không có** khoá hay credential của
Executor, không có LLM trong vòng lặp.

**Làm gì:**
1. Đọc bản export audit (chỉ đọc), kiểm tra chuỗi hash, tìm **đúng một** biên nhận `executed`, `mutated=true` khớp
   incident, request và kịch bản.
2. Chạy probe đã review (`moodle-scenario-verify.sh`) lặp lại: cửa sổ ≥ 120 s, mẫu cách nhau ≤ 60 s (mặc định 30 s,
   5 mẫu). Probe phải phủ **đúng** hợp đồng; thiếu hay thừa đều bị từ chối.
3. Đọc lại audit ở cuối cửa sổ, để audit bị sửa giữa chừng không lọt qua.
4. Ký attestation Ed25519 trên payload chuẩn hoá, gắn với `receipt_event_hash` của biên nhận.

**Hợp đồng probe:**

| Nhóm | Nghĩa | Probe |
|---|---|---|
| allowed | phải hoạt động | `common_synthetic_transaction` (BIZ dùng probe riêng, ví dụ `student_course_view_http_200`) |
| forbidden | phải **bị chặn** | `internet_to_postgresql_blocked` (Route 53 health check phải thất bại) |
| related | liên quan sự cố | `scenario_alert_resolved` + probe riêng: CON-01 `moodle_app_b_container_healthy`, `both_alb_targets_healthy`; DB-01 TLS DB của hai node, hai target; RES-01 `named_cpu_fixture_absent`; NET-01 DNS DB của hai node, hai target; BIZ-01 `moodle_cli_maintenance_disabled`; BIZ-02 `moodle_cron_service_active`, `overdue_adhoc_tasks_zero` |

**Agent chấp nhận attestation** (`POST /api/moodle/verifications/{request_id}`) chỉ khi: chữ ký đúng khoá Verifier,
attestation ký chưa quá **120 s**, các mẫu tăng dần theo thời gian, phủ ≥ 120 s, khoảng cách ≤ 60 s, gắn đúng biên
nhận đang chờ xác minh. State machine (`core/verification/state_machine.py`) chỉ cho `RESOLVED` qua
`resolve_verified` với kết quả đủ điều kiện; mọi đường khác bị từ chối.

**Vì sao không để "health check xanh" là đủ:** một hệ thống có thể trả 200 trong khi cổng DB đang mở ra Internet, hoặc
alert vẫn firing ở node khác. Probe *phải bị chặn* và probe *liên quan* bắt được những trường hợp đó.

**Giới hạn:** tình huống chỉ-người (SSO-13, SEC-01) không có biên nhận `executed`, nên hiện **không** có attestation
ký và không chuyển `RESOLVED`. SEC-01 dùng `moodle-human-remediation-verify.sh` (chỉ đọc) để kiểm tra sau khi người
sửa.

---

## 8. Phê duyệt và báo cáo qua Telegram

**Cầu nối phê duyệt** (`automation/moodle-telegram-approval.py`, chạy trên máy operator):
- Trong drill, Agent trả kết quả `/prepare` cho cầu nối và cầu nối gửi một tin duy nhất gồm kết quả Safety Gate và yêu cầu duyệt. Operator gửi `approve` hoặc `reject` trong nhóm, hoặc reply tin yêu cầu. Khi gọi `/prepare` độc lập, Agent vẫn gửi thông báo; Gate từ chối vẫn được thông báo.
- Chỉ nhận tin của user thuộc `TELEGRAM_ADMIN_USER_IDS` trong đúng `TELEGRAM_CHAT_ID`, có message ID mới hơn tin yêu cầu. Nếu là reply thì phải reply đúng tin. Khóa cục bộ giới hạn một yêu cầu đang chờ cho mỗi nhóm trên máy vận hành; không chạy cầu nối của cùng bot từ máy khác cùng lúc.
- Cầu nối chờ không giới hạn theo mặc định trong khi tiến trình đang chạy; `MOODLE_APPROVAL_TIMEOUT_SECONDS` có thể đặt thời hạn riêng. Sau `approve`, drill dùng **một** khóa riêng Ed25519 trên máy operator ký hành động; Safety Gate và Executor cùng kiểm tra chữ ký. Từ chối, lỗi Telegram hoặc dừng tiến trình sẽ dừng luồng. Phiên AWS vẫn cần còn hạn cho các bước AWS/Verifier.
- Mã thoát: 0 duyệt, 10 từ chối, 11 hết giờ, 2 lỗi cấu hình hoặc Telegram.

**Tin báo theo bước:**

| Bước | Tin | Gửi từ |
|---|---|---|
| ② ③ | 🚨 ALERT ngắn: lỗi, nơi lỗi, nguyên nhân, giải pháp tập trung và mã sự cố; báo cáo chi tiết vẫn ở evidence | Celery worker |
| ④ | 🛡 SAFETY GATE: 8 kiểm tra, hành động, phạm vi và yêu cầu gửi `approve` | Cầu nối trên máy operator; Agent gửi riêng nếu Gate chặn |
| ⑤ | ⚙️ EXECUTOR ĐÃ THỰC THI / ⛔ KHÔNG THỰC THI / ⛔ BỊ TỪ CHỐI | Agent API `/execute` |
| ⑥ | 🔎 Verifier bắt đầu (hợp đồng) và từng mẫu | Verifier trên laptop (`--telegram-progress`) |
| ⑥ | 🏁 RESOLVED hoặc ⛔ attestation bị từ chối; tổng kết probe, RTO đo được và RPO | Agent API và drill trên máy operator |
| Hết alert | ✅ hết firing, "chưa phải RESOLVED"; CON-01 khớp sự cố đã dispatch được gộp vào tổng kết cuối, với tin dự phòng sau 10 phút nếu chưa xác minh hoặc gửi thất bại | Celery worker |

Tin chỉ chứa ID, quyết định và tiền tố hash; không có chữ ký, token hay body yêu cầu. Tin ④–⑥ gửi **sau** khi API đã
trả lời (`BackgroundTasks`), nên Telegram chậm hay lỗi không đổi quyết định an toàn. Callback tiến độ của Verifier lỗi
cũng không đổi kết luận (có test). RTO được đo từ lúc alert được phát hiện đến lúc runtime nhận `RESOLVED`; RPO ghi
"không áp dụng" cho các kịch bản khôi phục tính sẵn sàng mà không restore dữ liệu.

---

## 9. Hermes Agent và mô hình đa AI

**Hermes Agent** (Nous Research, MIT) là khung tác nhân tự vận hành dài hạn: tự sinh kỹ năng, bộ nhớ phân tầng, cổng
nhắn tin đa nền tảng, là MCP client, có hook `pre_tool_call`, có bộ lọc lệnh nguy hiểm với các chế độ
`manual`/`smart`/`off`/`--yolo`.

**Hermes có tự làm Safety Gate được không? Không.**

| Tiêu chí | Cơ chế của Hermes | Yêu cầu của đề tài |
|---|---|---|
| Vị trí | Trong tiến trình Agent | Dịch vụ ngoài tiến trình AI |
| Bất biến | Tắt được bằng `--yolo` hoặc `mode: off` | Không thể bỏ qua dù AI sinh prompt gì |
| Prompt injection | `smart approval` dùng LLM nên có thể bị lừa | Kiểm tra catalog hữu hạn và chữ ký mật mã |
| Định danh người duyệt | stdin hoặc tin nhắn thường | Telegram user ID allowlist và một chữ ký Ed25519 gắn action digest |

**Hermes có tự kết luận `RESOLVED` được không? Không.** LLM có xu hướng tự xác nhận hành động của mình thành công;
Verifier của đề tài đòi đo định lượng nhiều mẫu, ba nhóm probe và chữ ký riêng.

**Vai trò phù hợp:** *Investigator* (điều tra, đề xuất) kết nối hạ tầng qua MCP **chỉ đọc**. Nguyên mẫu trong
`agent_src/integrations/hermes/`:
- `mcp_server.py`: 4 công cụ `list_scenarios`, `collect_evidence`, `diagnose_alert`, `propose_remediation`; không có
  công cụ thực thi hay xác minh. JSON-RPC 2.0 qua stdio.
- `hooks.py`: `HermesSafetyHook` ở `pre_tool_call` chặn shell, bash, ssh và SQL sửa bảng `mdl_*`. Ranh giới từ (ví dụ
  `truncate_mdl_task_adhoc`) đã được sửa để `_` được coi là dấu tách.
- Mô hình đa AI luân phiên: Investigator (Hermes, chỉ đọc), Critic (AI phản biện, có quyền veto khi vượt ranh giới hạ
  tầng), Operator Liaison (giao diện người-máy, tóm tắt và thu phê duyệt). Verifier luôn là daemon đo đạc, không có LLM.

Kiểm thử: `test_hermes_integration.py` 16/16, `test_moodle_7_scenario_suite.py` 16/16 (24/10/2026). Đây là kiểm thử
mã nguồn, chưa có lần chạy Hermes thật nối với staging.

---

## 10. Mô hình mối đe doạ

**Tài sản:** tính sẵn sàng của Moodle và dữ liệu người học; toàn vẹn RDS/EFS; quyền điều khiển EC2 và container;
credential AWS và secret runtime; toàn vẹn incident và bằng chứng; hàng đợi cảnh báo và kênh thông báo.

**Đầu vào không tin cậy:** JSON, label, annotation của Alertmanager; trích đoạn log; metric và phản hồi probe; metadata
repo ngoài; nội dung runbook truy xuất và phản hồi của admin.

**Ranh giới tin cậy:** ALB công khai → Moodle; node → RDS/EFS; mạng monitor → webhook của Agent; Agent → Redis/ChromaDB;
Agent → Executor API riêng; phê duyệt của người → thực thi. **Ranh giới ground truth:** đáp án kịch bản chỉ để chấm
điểm sau quyết định; không làm ngữ cảnh prompt, bằng chứng, đầu vào chọn hành động hay đầu vào Verifier.

| Mối đe doạ | Kiểm soát hiện có | Rủi ro còn lại |
|---|---|---|
| Alert giả hoặc phát lại làm ngập Agent | Kiểm tra payload, khử trùng lặp theo fingerprint, giới hạn hàng đợi; bearer token 256 bit từ file | Triển khai khác không bật `ALERTMANAGER_WEBHOOK_AUTH_REQUIRED` vẫn không xác thực |
| Label, log, runbook hay phản hồi bị chèn nội dung làm chọn hành động sai | Schema có kiểu, kiểm tra dữ liệu nhạy cảm, collector có giới hạn, catalog hữu hạn; Gemini chỉ tư vấn, có schema đầu ra | Test prompt injection không chứng minh chống được mọi nội dung thù địch |
| Rò đáp án ground truth làm đẹp kết quả | Đường live đọc ground truth đã bị xoá; image runtime không chứa đáp án (CI kiểm tra); replay fixture không được thực thi | Replay offline vẫn dùng đáp án nên phải loại khỏi mọi kết luận thực nghiệm |
| Đầu độc RAG hoặc runbook lỗi thời | Runbook chuẩn và phản hồi đã duyệt tách collection; nguồn, thời điểm, hash được ghi; hành động vẫn qua Gate | Hash chứng minh nội dung, không chứng minh tác giả hay độ mới |
| Cập nhật Telegram giả, thành viên group không tin cậy | Webhook đòi secret header so sánh thời gian hằng; phản hồi và callback đòi user ID trong danh sách admin và đúng chat; cầu nối phê duyệt kiểm tra user ID, chat và nonce | Hiện một người giữ cả hai vai trong demo |
| Lộ secret hoặc dữ liệu người học vào prompt, bằng chứng, log | Loại trường theo khoá, redact bằng regex, trích đoạn có giới hạn, secret qua file mount | Regex có thể bỏ sót định dạng mới; token API từng lộ trong phiên làm việc (23/10) cần đổi |
| Quyền quá rộng hoặc hành động phá huỷ | Catalog và phạm vi staging chính xác, kiểm tra vai trò, phê duyệt ký gắn hành động, idempotency, timeout, kill switch, Executor riêng, chỉ-người cho bảo mật | Khoá của người duyệt phải được giữ bởi người khác nhau khi chạy thật |
| Khôi phục giả từ tín hiệu "health-only" | Verifier kiểm tra allowed, forbidden, related và cửa sổ ổn định; chỉ Verifier được `RESOLVED` | Tình huống chỉ-người chưa có attestation |
| Sửa bằng chứng hoặc phát lại checkpoint | Trigger append-only, hash nội dung, ID idempotent, checkpoint có lease | Quản trị DB cục bộ vẫn sửa được; chưa có kho audit bất biến bên ngoài |
| Nhà cung cấp LLM sập hoặc hết hạn mức | Giới hạn số lần gọi, fallback xác định, escalate rõ ràng | Chưa đo độ sẵn sàng, chi phí, độ trễ thật |

**Điều kiện tối thiểu trước khi bật khắc phục live:** xác thực Alertmanager và giới hạn nguồn tới webhook; bằng chứng
đọc mới nối vào chẩn đoán, có `evidence_refs`, xử lý tín hiệu mâu thuẫn, không nạp đáp án; hành động có kiểu từ catalog
đã review rồi qua Gate độc lập; giữ phê duyệt tách biệt, chữ ký gắn hành động, phạm vi chính xác, idempotency, timeout,
kill switch; chỉ chạy trên staging, chụp trạng thái trước/sau; xác minh allowed/forbidden/related ≥ 120 s; thu đủ dòng
Manual/Ansible/AI cùng các ablation trước khi kết luận câu hỏi nghiên cứu.

---

## 11. Trạng thái bằng chứng

### 11.1. Đã chạy thật trên staging

| Hạng mục | Kết quả |
|---|---|
| CON-01 trọn chuỗi ①→⑥ | Đạt 4 lần (run 5–8, 22–23/10/2026). Run 8 có **người bấm duyệt trên Telegram**; `RESOLVED` bằng attestation ký |
| SSO-13 chỉ-người | Đạt nhiều lần: Agent chẩn đoán đúng lớp, không có candidate, executions không đổi, OIDC 5/5 sau khi người sửa |
| RES-01 tới Safety Gate | Chạy thử 23/10/2026: alert, candidate, Gate 8/8, phê duyệt qua Telegram, không thực thi |
| Thông báo Telegram ①–⑥ | Đã gửi thật trong run 7–8 |

### 11.2. Kiểm thử và replay (không phải bằng chứng khôi phục)

| Hạng mục | Kết quả gần nhất |
|---|---|
| Bộ test Agent | 1091 đạt (25/10/2026, Python 3.11.9); trên macOS có 1 test phụ thuộc GNU `timeout` |
| Replay hợp nhất | 15/15 chấp nhận; không có thực thi bị cấm, không có tuyên bố khôi phục |
| Replay Sprint 2 | 15 `VERIFIED_DRY_RUN`, 0 thực thi bị cấm |
| SEC-01 verify | Bị từ chối, exit 64 (bằng chứng từ chối, không phải kết quả Verifier) |
| Bộ 7 kịch bản, Hermes | 16/16 và 16/16 test mã nguồn |
| Chiến dịch luận văn | Công cụ `thesis-campaign-trial.py` sẵn sàng; bản mô phỏng 105 trial qua mọi bộ kiểm tra chấp nhận **trừ nhãn dữ liệu** (đúng thiết kế: dữ liệu mô phỏng bị từ chối) |

### 11.3. Chưa có

- Chiến dịch thực nghiệm thật: **0 lần chấp nhận** cho cả hai hồ sơ đánh giá. Thiết kế luận văn là 7 kịch bản × 3
  phương pháp (`ai_agent`, `ansible`, `manual`) × 5 lần lặp = 105 trial chính, cộng 35 dòng no-gate và 35 dòng
  health-only. Với 7 cụm, p nhỏ nhất của sign test chính xác là 0.0156, nên sau Holm cho 3 so sánh, RQ1 cần **mọi**
  kịch bản nghiêng về Agent có Gate.
- Drill tổng quát cho DB-01, NET-01, BIZ-01, BIZ-02 chưa chạy thật; RES-01 chưa chạy tới Executor.
- Attestation cho tình huống chỉ-người; kho audit bất biến bên ngoài; lane ERPNext chưa triển khai.
- Đánh giá có che mắt (blind) cho người vận hành thủ công: chưa sẵn sàng tới khi tách quyền truy cập của operator khỏi
  marker, lệnh drill, log điều khiển và ground truth.

### 11.4. Nhật ký kiểm chứng trước đó (tóm tắt)

Từ 05/10 tới 22/10/2026 các lần kiểm tra cục bộ lần lượt đạt 460 → 519 → 907 → 925 → 997 test khi bổ sung: xác thực
webhook, khoá dependency theo hash, cô lập ground truth khỏi Diagnosis/Planner, chế độ investigate có bằng chứng, chống
phản hồi Prometheus sai dạng, checkpoint có lease, kiểm định thống kê (Wilson, sign test theo cụm, Holm) và bộ kiểm
tra chấp nhận ràng buộc SHA-256. Nhiều lần kiểm tra trước 22/10 không chạy được trên staging do credential AWS
(`InvalidClientTokenId`) hoặc thiếu Docker Engine; các mốc đó chỉ là bằng chứng mã nguồn.
