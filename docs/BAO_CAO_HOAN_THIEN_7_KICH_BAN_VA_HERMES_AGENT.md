# Báo cáo Hoàn thiện 7 Kịch bản Vận hành Hạ tầng và Tích hợp Hermes Agent

**Ngày hoàn thành:** 24-10-2026  
**Nhánh thực hiện:** `AI-Agent-Refactor`  
**Mục tiêu:** 
1. Thống nhất và tối ưu hóa bộ kịch bản thực nghiệm theo định hướng phản biện: *"Quản trị hạ tầng, không quản trị ứng dụng"*.
2. Khóa cứng bộ **7 kịch bản trọng tâm** (2 Nghiệp vụ/Hạ tầng + 5 Kỹ thuật).
3. Giải quyết triệt để 5 vấn đề còn tồn đọng trong Báo cáo rà soát ngày 22-10-2026.
4. Nghiên cứu tính khả thi và xây dựng nguyên mẫu tích hợp **Hermes Agent** (Nous Research), kiểm chứng cơ chế Cổng an toàn (Safety Gate) và Bộ xác minh độc lập (Independent Verifier).

---

## 1. Định hướng Cải tiến và Nguyên tắc Ranh giới

Sau khi tiếp nhận ý kiến phản biện chuyên gia:
- **"Quản trị hạ tầng không quản trị ứng dụng":** AI không được phép can thiệp trực tiếp vào cấu hình ứng dụng, bảng dữ liệu cơ sở dữ liệu (`mdl_*`), phân quyền học vụ, khóa học hay sổ điểm của Moodle. Mọi hành vi tự động sửa đổi hàng trong DB đều tiềm ẩn rủi ro phá vỡ tính toàn vẹn ứng dụng.
- **Phân định rõ ranh giới hành động của AI:**
  1. **Lớp 1 - Thu thập chứng cứ (Evidence Collection):** Chỉ đọc (Read-only), sử dụng Prometheus metrics, Blackbox Exporter và trạng thái Docker.
  2. **Lớp 2 - Chẩn đoán nguyên nhân (Root-Cause Diagnosis):** Bắt buộc phải có bằng chứng đối chiếu (`evidence_refs`).
  3. **Lớp 3 - Đề xuất phương án (Remediation Proposal):** Chỉ đề xuất các hành động có trong danh mục kiểm duyệt; bắt buộc có kế hoạch hoàn tác (Rollback Plan).
  4. **Lớp 4 - Thi hành & Xác minh (Execution & Verification):** Bắt buộc phải thông qua Cổng an toàn với 2 chữ ký số Ed25519 độc lập từ con người. Sau khi thi hành, chỉ có Bộ xác minh độc lập (không có LLM trong vòng lặp) mới được quyền ký chứng nhận khôi phục (`RESOLVED`).

---

## 2. Danh mục 7 Kịch bản Trọng tâm Chuẩn hóa

| Mã | Tên kịch bản | Cảnh báo kích hoạt | Bản chất vấn đề | Ranh giới hạ tầng (AI đề xuất sau duyệt) | Hành vi ngoài phạm vi (Chuyển người) |
|---|---|---|---|---|---|
| **BIZ-01** | Kẹt chế độ bảo trì Moodle sau cập nhật | `MoodleMaintenanceModeStuck` | File cờ `climaintenance.html` tồn đọng sau khi cập nhật mã nguồn hoàn tất | Tắt chế độ bảo trì bằng CLI (`maintenance.php --disable`) — **chỉ khi `upgrade.php` không còn chạy** | Tiến trình cập nhật đang chạy dở hoặc lỗi dữ liệu |
| **BIZ-02** | Hàng đợi tác vụ nền (Ad-hoc) bị kẹt | `MoodleAdhocTaskQueueStuck` | Container `moodle-cron` trên node A bị dừng hoặc crash | **Khởi động lại container `moodle-cron` trên node A** (Mức hạ tầng Docker Compose) | **Tuyệt đối cấm** chạy câu lệnh SQL xóa/sửa khóa trong bảng `mdl_task_adhoc` |
| **CON-01** | Web Moodle dừng trên 1 node | `MoodleWebContainerMissing` | Container web trên node B bị dừng, làm giảm năng lực phục vụ của cụm ALB | Khởi động lại service web theo Compose chuẩn đã duyệt | Image hỏng hoặc lỗi logic lặp lại |
| **DB-01** | Mất kết nối TCP app $\rightarrow$ RDS | `MoodleNodeRdsTcpFailed` | Luật iptables chặn có gắn nhãn sự cố trên nút ứng dụng | Gỡ bỏ đúng luật chặn iptables có gắn nhãn | RDS ngừng hoạt động, cấu hình Security Group/Route thay đổi |
| **RES-01** | Quá tải CPU nút ứng dụng | `MoodleNodeCpuHigh` | Tiến trình / container phụ tải nhân tạo chiếm dụng CPU | Dừng đúng container gây tải phụ đã biết tên | Tải thật từ người dùng tăng đột biến $\rightarrow$ Cần người quyết định scale |
| **NET-01** | Lệch phân giải tên nội bộ | `MoodleSyntheticTransactionFailed` | Ghi đè hosts sai lệch bên trong container làm sai địa chỉ phụ thuộc | Tạo lại container web từ Compose chuẩn đã được kiểm duyệt | Cấu hình DNS Route 53 / VPC thay đổi |
| **SEC-01** | Nguồn lạ trong Security Group của RDS | `MoodleDatabasePortExposure` | Ingress rule mở cổng 5432 ra Internet (`0.0.0.0/0`) | **HUMAN_ONLY** — AI chỉ tạo cảnh báo và phân tích; **tuyệt đối cấm tự động sửa đổi** | Luôn chuyển cho Kỹ sư An ninh xử lý thủ công |

> [!NOTE]
> **Các kịch bản đã loại bỏ:**
> - `BIZ-03` (Bật lại phương thức đăng nhập): Loại bỏ vì can thiệp sâu vào cấu hình plugin Moodle.
> - `BIZ-04` (Khôi phục tự ghi danh): Loại bỏ vì can thiệp nghiệp vụ tuyển sinh/ghi danh.
> - `BIZ-05` (Bất thường sổ điểm / bài thi): Giữ nguyên phân loại Human-only ở tài liệu hướng dẫn nhưng không đưa vào đường thi hành AI.

---

## 3. Khắc phục Triệt để 5 Lỗ hổng từ Báo cáo Rà soát

1. **Bằng chứng cảnh báo nghiệp vụ vào bộ thu thập thật:**
   Đã bổ sung `BUSINESS_METRIC_QUERIES_BY_ALERT` vào `agent_src/core/evidence/evidence_collectors.py` và cập nhật `MoodlePlatformAdapter.get_alert_queries()`. Cả `MoodleMaintenanceModeStuck` và `MoodleAdhocTaskQueueStuck` đều có định nghĩa truy vấn PromQL rõ ràng.
2. **Hành động nghiệp vụ được đưa vào Danh mục đường thi hành:**
   Đã xây dựng hàm `load_moodle_7_scenario_catalog()` trong `agent_src/core/safety/action_catalog.py`, sáp nhập các hành động S3.3 cùng `disable_moodle_cli_maintenance_mode`, `restart_moodle_cron_service`, và `remove_tagged_database_ingress_rule`. Cập nhật `MoodlePlatformAdapter.get_action_catalog()` trả về danh mục 7 kịch bản này.
3. **Bộ xác minh độc lập hỗ trợ đầy đủ các kịch bản mới:**
   Cập nhật `automation/moodle-independent-verifier.py` hỗ trợ cờ `--scenario BIZ-01` và `BIZ-02`. Cập nhật kịch bản kiểm tra `automation/moodle-scenario-verify.sh`, bảo đảm `SEC-01` trả về mã lỗi 64 khi bị yêu cầu xác minh tự động (chính sách `HUMAN_ONLY`).
4. **Bộ chạy mô phỏng minh bạch hóa tính chất dữ liệu:**
   Đã xóa `run-moodle-10-scenarios.py` và thay thế bằng `automation/run-moodle-7-scenarios.py`. Toàn bộ dữ liệu giả lập được gắn cờ rõ ràng `simulated=True`, không ký nhận `RESOLVED` từ dữ liệu giả, bảo đảm tính liêm chính học thuật.
5. **Xử lý triệt để xung đột di trú giữa bộ cũ và bộ mới:**
   Tách biệt hoàn toàn `SCENARIO_REMEDIATION_DISPOSITION` (giữ nguyên cho 15 kịch bản kỹ thuật phục vụ CI replay) và `BUSINESS_SCENARIO_REMEDIATION_DISPOSITION` (phục vụ bộ kịch bản luận văn). Test suite kiểm tra 15 kịch bản cũ trong CI chạy mượt mà không còn bị fail.

---

## 4. Nghiên cứu và Tích hợp Hermes Agent

### 4.1. Kết luận Nghiên cứu (Xem chi tiết tại `docs/HERMES_AGENT_RESEARCH.md`)
1. **Về Cổng An toàn (Safety Gate):** Cơ chế duyệt nội bộ của Hermes nằm bên trong tiến trình và có thể bị vô hiệu hóa bởi cờ `--yolo` hoặc bị đánh lừa bởi Prompt Injection. Do đó, Hermes **không được phép tự làm Safety Gate**. Cổng An toàn bắt buộc phải là dịch vụ ngoại vi đòi hỏi 2 chữ ký số Ed25519.
2. **Về Bộ Xác minh (Independent Verifier):** Tuân thủ bất biến $\text{Actor}(\text{Remediation}) \neq \text{Actor}(\text{Verification})$. Hermes không được tự kết luận `RESOLVED`. Việc xác minh phải do daemon đo đạc độc lập thực hiện qua 5 mẫu đo trong 120 giây.
3. **Kiến trúc Phân tầng:** Hermes Agent đóng vai trò là **Bộ não Điều tra (Investigator)** kết nối với hạ tầng thông qua máy chủ MCP chỉ-đọc.

### 4.2. Hiện thực Nguyên mẫu Tích hợp (`agent_src/integrations/hermes/`)
- **`mcp_server.py`:**
  - Cung cấp 4 công cụ chuẩn MCP: `list_scenarios`, `collect_evidence`, `diagnose_alert`, `propose_remediation`.
  - **Không cung cấp** bất kỳ công cụ thực thi hay xác minh nào (`execute_action`, `resolve_incident`). Mọi yêu cầu trái phép đều bị từ chối ngay tại tầng protocol.
  - Hỗ trợ đầy đủ giao thức JSON-RPC 2.0 (stdio) cho Hermes Agent.
- **`hooks.py`:**
  - Cài đặt `HermesSafetyHook` tại vòng đời `pre_tool_call`.
  - Chặn đứng mọi lệnh gọi terminal, shell, bash, ssh.
  - Chặn đứng mọi câu lệnh SQL cố tình sửa đổi các bảng ứng dụng Moodle (`mdl_*`).
- **Mô hình Đa AI Luân phiên (`validate_multi_ai_task_rotation`):**
  - Định nghĩa 3 vai trò: Điều tra (Hermes), Phản biện ranh giới (Critic), và Giao tiếp Vận hành (Operator Liaison).
  - Khóa cứng yêu cầu Verifier là daemon đo đạc độc lập, không có LLM.

---

## 5. Kết quả Kiểm thử và Đánh giá

1. **Kiểm thử Đơn vị & Tích hợp Bộ 7 Kịch bản (`test_moodle_7_scenario_suite.py`):**
   - Đạt **16/16 test case (100%)**.
   - Kiểm tra đầy đủ 4 giai đoạn an toàn: Thu thập $\rightarrow$ Chẩn đoán $\rightarrow$ Cổng an toàn (2 chữ ký) $\rightarrow$ Xác minh độc lập.
2. **Kiểm thử Nguyên mẫu Hermes Agent (`test_hermes_integration.py`):**
   - Đạt **16/16 test case (100%)**.
   - Kiểm tra tính bất biến của MCP Server, khả năng chặn đứng lệnh shell và SQL injection, cùng giao thức JSON-RPC 2.0.
3. **Bộ chạy Tổng hợp 7 Kịch bản (`automation/run-moodle-7-scenarios.py`):**
   - Chạy thành công 7/7 kịch bản qua các giai đoạn mô phỏng, minh bạch hoá nhãn `simulated=True`.
