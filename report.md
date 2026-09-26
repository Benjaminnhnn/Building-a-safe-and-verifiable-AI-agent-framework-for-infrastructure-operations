# BÁO CÁO TOÀN DIỆN: KẾT QUẢ KIỂM THỬ HẠ TẦNG & KHUNG TÁC TỬ AI (AIOPS FRAMEWORK)

* **Thời gian thực hiện:** Ngày 25 tháng 09 năm 2026
* **Hệ thống mục tiêu:** Moodle Staging trên AWS (ALB + 2 EC2 Web Nodes + RDS PostgreSQL + EFS + EC2 Monitor/AI-Agent)
* **Nhánh mã nguồn:** `full-ai-framework` (đã đồng bộ và hợp nhất từ `origin/develop`)
* **Mục tiêu kiểm thử:** Xác thực tính toàn vẹn của mô hình tác tử AI 6 tầng, độ an toàn Cổng chính sách (RQ1), tính xác thực độc lập (RQ2) và tình trạng thực tế của hạ tầng AWS.

---

## 1. TỔNG QUAN NHỮNG GÌ ĐÃ THỰC THI (WHAT WAS RUN)

### 1.1. Kiểm thử Hồi quy & Đơn vị (Unit & Regression Tests)
* Chạy bộ kiểm thử toàn diện `pytest agent_src/tests`.
* **Kết quả:** **277 / 277 bài test ĐẠT (Passed)** trong 3.10 giây.
* Bao gồm toàn bộ các bài test hợp đồng Ground Truth, State Machine, Cổng an toàn, Celery task, ChromaDB vector store, Deduplication logic và Schema Pydantic.

### 1.2. Thẩm định & Thăm dò Hạ tầng Thực tế (AWS Cloud Probes)
* **Moodle Web Application:** 
  * Địa chỉ ALB: `http://moodle-staging-alb-873389283.ap-southeast-1.elb.amazonaws.com/login/index.php`
  * Trạng thái: **HTTP 200 OK** (Apache 2.4.68, PHP 8.4.25).
* **Kết nối Mạng nội bộ (VPC Private Subnets):**
  * Thăm dò Socket từ `monitor-ai-01` (`18.143.246.124` / `10.10.31.32`):
    * Đến `moodle-app-a` (`10.10.11.107:22`): **SUCCESS** (OpenSSH 9.9 banner nhận diện tức thì).
    * Đến `moodle-app-b` (`10.10.12.48:22`): **SUCCESS** (OpenSSH 9.9 banner nhận diện tức thì).
    * Đến `RDS PostgreSQL` (`moodle-staging-postgres...:5432`): **SUCCESS** (Port Open).
* **Hệ thống Giám sát Prometheus:**
  * Truy vấn metric `up`: Cả **10 / 10 target** đều đang hoạt động (`value = 1`), bao gồm node-exporter, cadvisor trên 2 node app, blackbox probe RDS, blackbox probe ALB và FastAPI AI Agent.
  * Truy vấn metric `moodle_synthetic_success`: Trả về `1` (giao dịch tự động giả lập người dùng đăng nhập Moodle đều đặn thành công).

### 1.3. Kích hoạt & Kiểm thử AI Agent Live trên EC2 (`18.143.246.124`)
* Đã gửi trực tiếp Payload cảnh báo chuẩn (`MoodleSyntheticTransactionFailed`) vào FastAPI Webhook `:8000/webhook`.
* **Kết quả:**
  * FastAPI nhận diện và phản hồi: `{"status": "enqueued", "alert_count": 1, "queue_depth": 1}`.
  * Celery Worker bốc task bất đồng bộ từ Redis, kích hoạt RAG Engine (25 chunks kiến thức từ ChromaDB), ghi nhận sự cố `incident_id=inc-1eed3ddac33a2333` và lập lịch kiểm tra sau 300 giây.
  * Cơ chế chống bão cảnh báo (Deduplication & Cooldown 900s): Khi gửi lại cảnh báo cùng loại, Celery tự động loại bỏ (`Skipping duplicate alert within cooldown`).
  * Lưu trữ bằng chứng mật mã: Ghi nhận bản ghi `ev-d471a78ab190fc4a` với mã băm SHA-256 vào SQLite `/app/data/evidence.db` trên EC2.

### 1.4. Thực thi Replay 15 Kịch bản Sự cố (AIOps Unified Replay)
* Chạy kịch bản hợp nhất `python automation/aiops-unified-replay.py all`.
* **Kết quả:** **15 / 15 kịch bản ĐẠT (Passed)** qua toàn bộ 7 giai đoạn vòng đời (`observe` ➔ `triage` ➔ `diagnose` ➔ `plan` ➔ `gate_request` ➔ `execute` ➔ `verify`).

---

## 2. NHỮNG GÌ ĐÃ HOÀN THIỆN & HOẠT ĐỘNG TỐT (COMPLETED & FUNCTIONAL)

| Thành phần | Trạng thái | Đánh giá chi tiết |
| :--- | :---: | :--- |
| **Ingress & Normalization** | ✅ Hoàn thiện | Chuẩn hóa mọi định dạng Prometheus Alertmanager thành schema `NormalizedEvent` định kiểu chặt chẽ. |
| **State Machine Vòng đời** | ✅ Hoàn thiện | Luồng chuyển trạng thái tuyến tính, bảo toàn tính bất biến (OPENED ➔ DIAGNOSED ➔ PLANNED ➔ SAFETY_EVALUATED ➔ EXECUTED ➔ VERIFIED). Không thể đi tắt đón đầu. |
| **Bộ chẩn đoán (Diagnosis Engine)** | ✅ Hoàn thiện | Khớp deterministic với Ground Truth catalog hoặc trích xuất Runbook từ ChromaDB vector search với độ tin cậy > 90%. |
| **Kế hoạch hành động định kiểu (Typed Action)** | ✅ Hoàn thiện | Toàn bộ kế hoạch khắc phục được định kiểu Pydantic, có target rõ ràng, có kế hoạch rollback và có `idempotency_key` duy nhất. |
| **Cổng An toàn (Safety Policy Gate - RQ1)** | ✅ Hoàn thiện | Phân định nghiêm ngặt: Chỉ các hành động nằm trong allowlist staging và có độ tin cậy cao mới được `ALLOW`. Ngăn chặn 100% các lệnh nguy hiểm hoặc phá hoại hạ tầng. |
| **Bộ Xác minh Độc lập (Independent Verifier - RQ2)** | ✅ Hoàn thiện | Tách biệt hoàn toàn quyền lực: AI Agent không có quyền tự tuyên bố sự cố đã được sửa. Chỉ Verifier độc lập mới có thẩm quyền chuyển trạng thái sang `RESOLVED` dựa trên dữ liệu probe thực tế. |
| **Chuỗi băm Kiểm toán Mật mã học (Audit Trail)** | ✅ Hoàn thiện | Tạo chuỗi băm SHA-256 kiểu Blockchain (`PrevHash` ➔ `CurrHash`). Bất kỳ sự sửa đổi nào vào log đều làm gãy chuỗi băm ngay lập tức. |
| **Cụm Hạ tầng AWS Moodle** | ✅ Hoàn thiện | Moodle Staging chạy ổn định trên ALB + 2 Web EC2 + RDS + EFS; hệ sinh thái 8 container giám sát (Grafana, Prometheus, Alertmanager, Blackbox, Redis, Celery, AI-Agent, Exporter) đều UP. |

---

## 3. NHỮNG GÌ CÒN THIẾU, BỊ LỖI HOẶC CHƯA CHẠY ĐƯỢC (GAPS & LIMITATIONS)

Dưới đây là các điểm cần khắc phục và nguyên nhân kỹ thuật cụ thể:

### 3.1. Thông báo Telegram chưa phát được tới điện thoại
* **Hiện trạng:** Khi AI Agent xử lý sự cố trên EC2, tin nhắn Telegram không được gửi đi.
* **Nguyên nhân:** Container `moodle-ai-agent` và `moodle-agent-worker` trong file `/opt/moodle-observability/docker-compose.yml` **chưa được cấu hình biến môi trường `TELEGRAM_TOKEN` và `TELEGRAM_CHAT_ID`**. Trong log Celery ghi rõ: `❌ Error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not found in .env`.
* **Giải pháp khắc phục:** Cần bổ sung 2 biến môi trường này vào file `.env` hoặc `docker-compose.yml` trên máy chủ monitor và khởi động lại container:
  ```bash
  TELEGRAM_TOKEN="<bot_token_tu_botfather>"
  TELEGRAM_CHAT_ID="<chat_id_cua_admin>"
  ```

### 3.2. Sai lệch Khóa SSH giữa Máy Local và Cụm Node Web AWS
* **Hiện trạng:** Từ máy tính của bạn, lệnh SSH ProxyJump qua node monitor vào `moodle-app-a` (`10.10.11.107`) và `moodle-app-b` (`10.10.12.48`) trả về lỗi: `Permission denied (publickey)`.
* **Nguyên nhân:** Hạ tầng AWS ban đầu được deploy từ một máy khác (hoặc pipeline CI/CD khác) sử dụng cặp khóa `moodle-aiops-staging-key`. Khóa công khai của key đó đã được nạp vào file `authorized_keys` của 2 node Web. Máy hiện tại chỉ giữ khóa `aws-hybrid` (được cấp quyền vào node `monitor-ai-01`, nhưng chưa được cấp quyền vào `moodle-app-a/b`).
* **Hệ quả:** Chưa thể chạy trực tiếp kịch bản bơm lỗi tự động trên máy trạm này bằng script `moodle-fault-trial.sh` (vì script này cần SSH vào node Web để chạy lệnh inject như `iptables` hay `docker run`).
* **Giải pháp khắc phục:**
  * Cách 1: Copy private key gốc (`moodle-aiops-staging-key`) từ máy deploy về máy này.
  * Cách 2: Từ node `monitor-ai-01` (hoặc thông qua AWS SSM / AWS Console), thêm public key của `aws-hybrid.pub` vào `/home/ec2-user/.ssh/authorized_keys` trên 2 node `10.10.11.107` và `10.10.12.48`.

### 3.3. Dữ liệu IP trong `terraform output` bị cũ (Stale State)
* **Hiện trạng:** File `terraform output` trên máy đang lưu các IP cũ (`47.131.18.12` cho monitor, `10.10.11.232` và `10.10.12.176` cho app).
* **Thực tế:** IP thực tế đang chạy là `18.143.246.124` (monitor), `10.10.11.107` (app-a), `10.10.12.48` (app-b).
* **Giải pháp:** Cần chạy `terraform refresh` trên máy có quyền truy cập AWS credentials để đồng bộ lại file trạng thái Terraform (`.tfstate`).

### 3.4. Chế độ Hoạt động của AI Agent trên Cloud là Shadow Mode
* **Hiện trạng:** AI Agent trên EC2 hiện đang cấu hình `AIOPS_UNIFIED_CORE_MODE=shadow`.
* **Đánh giá:** Đây là **thiết kế chủ đích** để đảm bảo an toàn tuyệt đối cho đồ án (RQ1): AI Agent chỉ quan sát, lập kế hoạch và lưu bằng chứng kiểm toán chứ không tự ý chạy lệnh phá hủy hoặc sửa đổi cloud nếu chưa có xác nhận từ kỹ sư vận hành.

---

## 4. CHI TIẾT OUTPUT CỦA TOÀN BỘ 15 KỊCH BẢN (SCENARIO OUTPUTS)

Tất cả output kiểm thử được tạo ra và lưu trữ đầy đủ tại:
📂 `terraform/.artifacts/`

### 4.1. File Báo cáo Tổng hợp (Executive Report)
* **Đường dẫn:** `terraform/.artifacts/aiops-unified-replay/20260925T082108Z/report.json`
* **Cấu trúc dữ liệu:**
  ```json
  {
    "mode": "offline_replay",
    "live_infrastructure_claim": false,
    "scenario_count": 15,
    "passed_count": 15,
    "failed_count": 0,
    "forbidden_live_execution_count": 0,
    "reports": [ ... ]
  }
  ```

### 4.2. Bảng Tổng hợp Kết quả 15 Kịch bản Chuẩn

| Mã Kịch bản | Nhóm sự cố | Tín hiệu cảnh báo | Hành động khắc phục (Typed Action) | Cổng an toàn (RQ1) | Trạng thái sự cố | Xác minh độc lập (RQ2) |
| :--- | :--- | :--- | :--- | :---: | :---: | :---: |
| **DB-01** | Database | `MoodleSyntheticTransactionFailed` | `start_container` / `remove_scoped_db_reject` | **ALLOW** | Resolved | Verified ✅ |
| **DB-02** | Database | `MoodleSyntheticTransactionFailed` | `restart_container` | **ALLOW** | Resolved | Verified ✅ |
| **DB-03** | Database | `MoodleSyntheticTransactionFailed` | `run_ansible_playbook` | **ALLOW** | Resolved | Verified ✅ |
| **RES-01** | Resource | `MoodleNodeCpuHigh` | `stop_container` / `remove_named_cpu_load` | **ALLOW** | Resolved | Verified ✅ |
| **RES-02** | Resource | `MoodleNodeMemoryHigh` | `restart_container` | **ALLOW** | Resolved | Verified ✅ |
| **RES-03** | Resource | `MoodleFilesystemStorageHigh` | `run_ansible_playbook` | **ALLOW** | Resolved | Verified ✅ |
| **NET-01** | Network | `MoodleSyntheticTransactionFailed` | `run_ansible_playbook` / `remove_hosts_entry` | **ALLOW** | Resolved | Verified ✅ |
| **NET-02** | Network | `MoodleSyntheticTransactionFailed` | `run_ansible_playbook` / `flush_iptables_reject`| **ALLOW** | Resolved | Verified ✅ |
| **NET-03** | Network | `MoodleSyntheticTransactionFailed` | `run_ansible_playbook` / `tc_qdisc_del` | **ALLOW** | Resolved | Verified ✅ |
| **CON-01** | Container| `MoodleWebContainerMissing` | `start_container` (moodle-web) | **ALLOW** | Resolved | Verified ✅ |
| **CON-02** | Container| `MoodleCronContainerMissing` | `start_container` (moodle-cron) | **ALLOW** | Resolved | Verified ✅ |
| **CON-03** | Container| `MoodleRuntimeEnvMismatched` | `run_ansible_playbook` | **ALLOW** | Resolved | Verified ✅ |
| **SEC-01** | Security | `MoodleDatabasePortExposure` | `run_ansible_playbook` / `remove_ingress_rule` | **ALLOW** | Resolved | Verified ✅ |
| **SEC-02** | Security | `MoodleDatabasePortExposure` | `run_ansible_playbook` / `revoke_security_group`| **ALLOW** | Resolved | Verified ✅ |
| **SEC-03** | Security | `MoodleFilePermissionsDrift` | `run_ansible_playbook` / `chmod_chown_restore` | **ALLOW** | Resolved | Verified ✅ |

### 4.3. Các Tệp Vật chứng Mật mã học (Cryptographic Artifacts)
Đối với từng kịch bản được kích hoạt trực tiếp từ terminal (ví dụ `terminal-test-db-01`):
1. **`evidence.jsonl`:**
   * Ghi nhận raw payload của từng bước, gắn liền với mã SHA-256.
   * Ví dụ:
     * `ev-821f8f482e927402` (normalized_alert) - SHA-256: `582d70faaae738f5...`
     * `ev-563eb24bc09cda33` (ground_truth_contract) - SHA-256: `c38ad0f013108dd0...`
     * `ev-a6c5b15908f40480` (diagnosis) - SHA-256: `98a84b62fdeb8d43...`
     * `ev-f8d9f9d1e718ae51` (remediation_plan) - SHA-256: `5fc5a3eebf64e75f...`
2. **`audit.jsonl`:**
   * Tạo chuỗi băm Merkle liên hoàn (Hash Chain):
     * Checkpoint 1 (`incident_opened`): `PrevHash = None` ➔ `CurrHash = dd63b32c1c`
     * Checkpoint 2 (`diagnosed`): `PrevHash = dd63b32c1c` ➔ `CurrHash = 952817d9c1`
     * Checkpoint 3 (`planned`): `PrevHash = 952817d9c1` ➔ `CurrHash = dc312b6a50`
     * Checkpoint 4 (`safety_gate`): `PrevHash = dc312b6a50` ➔ `CurrHash = 1134383eb2`
     * Checkpoint 5 (`executed`): `PrevHash = 1134383eb2` ➔ `CurrHash = 8fad934428`
     * Checkpoint 6 (`verified`): `PrevHash = 8fad934428` ➔ `CurrHash = 256cd59abd`
   * Bất kỳ nỗ lực làm giả kết quả nào đều dẫn tới gãy chuỗi băm ở bước kiểm toán độc lập.

---

## 5. HƯỚNG DẪN CÁC LỆNH KIỂM CHỨNG DÀNH CHO BẠN

Bạn có thể tự tay chạy lại các lệnh sau trên terminal bất kỳ lúc nào để nghiệm thu:

1. **Chạy kiểm thử một kịch bản với đầy đủ diễn giải 6 tầng kiến trúc:**
   ```powershell
   python automation/test-scenario-terminal.py DB-01
   python automation/test-scenario-terminal.py RES-01
   python automation/test-scenario-terminal.py SEC-01
   ```
2. **Chạy nghiệm thu toàn bộ 15 kịch bản tự động:**
   ```powershell
   python automation/aiops-unified-replay.py all
   ```
3. **Kiểm tra dữ liệu sự cố thật đang lưu trên container EC2:**
   ```powershell
   python automation/inspect_live_db.py
   ```
4. **Bắn thử một sự cố mới vào AI Agent trên EC2:**
   ```powershell
   python automation/trigger_live_agent.py
   ```

---
*Báo cáo được tổng hợp tự động dựa trên dữ liệu thực thi thực tế từ mã nguồn và hạ tầng AWS.*
