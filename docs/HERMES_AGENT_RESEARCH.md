# Nghiên cứu Hermes Agent và Mô hình Đa AI trong Vận hành Hạ tầng

**Ngày nghiên cứu:** 24-10-2026  
**Chủ đề:** Đánh giá tính khả thi của Hermes Agent (Nous Research), phân tích Cổng an toàn (Safety Gate) & Bộ xác minh độc lập (Independent Verifier), và đề xuất kiến trúc Đa AI luân phiên nhiệm vụ (Multi-AI Task Rotation).  
**Đồ án:** Xây dựng khung tác nhân AI an toàn và có thể kiểm chứng cho vận hành hạ tầng (Moodle on AWS).

---

## 1. Tổng quan về Hermes Agent (Nous Research)

**Hermes Agent** là một khung tác nhân AI nguồn mở (giấy phép MIT) được phát triển bởi **Nous Research**. Khung này được thiết kế để vượt ra khỏi mô hình chatbot phiên tĩnh truyền thống, hướng tới một tác nhân tự vận hành dài hạn (persistent, self-improving agent).

### 1.1. Các đặc tính kiến trúc nổi bật

1. **Vòng lặp học tập & Tự sinh kỹ năng (Skill Creation & Learning Loop):**
   - Khi hoàn thành các chuỗi tác vụ phức tạp, Hermes có thể tự động đúc kết, tối ưu hóa và lưu trữ các quy trình thành "Skills" (tập tin mẫu suy luận tái sử dụng).
   - Qua các chu kỳ vận hành lặp lại, tác nhân tự động tối ưu đường dẫn thực thi của mình.
2. **Bộ nhớ phân tầng bền vững (Tiered & Persistent Memory):**
   - Kết hợp tìm kiếm toàn văn FTS5 trên các phiên làm việc cùng tóm tắt ngữ cảnh bằng LLM.
   - Duy trì thông tin ngữ cảnh hệ thống, lịch sử tương tác và sở thích của người vận hành qua các phiên chạy dài hạn.
3. **Cổng giao tiếp đa nền tảng (Multi-Platform Messaging Gateway):**
   - Một tiến trình Gateway duy nhất có thể kết nối đồng thời tới **Telegram, Discord, Slack, WhatsApp, Signal và CLI**.
   - Cho phép quản trị viên tương tác, theo dõi và ra lệnh bất đồng bộ từ xa.
4. **Hỗ trợ Model Context Protocol (MCP):**
   - Hermes đóng vai trò là một **MCP Client** nguyên bản.
   - Các công cụ ngoài được khai báo trong `~/.hermes/config.yaml` dưới khối `mcp_servers:` (hỗ trợ cả tiến trình cục bộ stdio và từ xa qua SSE/HTTP).
5. **Hệ thống Hook & Can thiệp vòng đời (Hooks Lifecycle):**
   - Cho phép cài đặt các hàm can thiệp tại các điểm nút: `pre_tool_call`, `post_tool_call`, `pre_approval_request`.
   - Plugin có thể trả về directive `{"action": "block", "message": "..."}` để phủ quyết một lệnh gọi công cụ trước khi nó được kích hoạt.
6. **Mô hình an toàn nội bộ (Internal Dangerous Command Approvals):**
   - Tích hợp sẵn bộ lọc phát hiện lệnh shell nguy hiểm (`rm -rf`, `docker stop`, `iptables`,...).
   - Hỗ trợ các chế độ duyệt: `manual` (hỏi người dùng `[o]nce / [s]ession / [a]lways / [d]eny`), `smart` (dùng một LLM phụ trợ đánh giá ngữ cảnh), `off`, hoặc chế độ không kiểm soát `YOLO mode` (`--yolo`).

---

## 2. Kiểm chứng: Hermes Agent có tự đảm nhiệm được Safety Gate và Verification không?

Một câu hỏi cốt lõi đặt ra cho đồ án là: **"Liệu có thể giao toàn bộ trách nhiệm Cổng An toàn (Safety Gate) và Bộ Xác minh (Verification) vào bên trong Hermes Agent được không?"**

### 2.1. Đánh giá Cổng an toàn (Safety Gate) trên Hermes

| Tiêu chí | Cơ chế duyệt nội bộ của Hermes | Yêu cầu Cổng An toàn Đồ án (Safety Gate) | Kết luận |
|---|---|---|---|
| **Vị trí ranh giới** | Nằm bên trong tiến trình của chính Agent | Nằm ở dịch vụ ủy quyền độc lập bên ngoài tiến trình AI | ❌ Không đạt |
| **Tính bất biến** | Có thể bị tắt bằng cờ `--yolo`, cấu hình `mode: off`, hoặc cấu hình lỏng lẻo | Cố định, không thể bypass dù AI sinh bất kỳ prompt nào | ❌ Không đạt |
| **Miễn nhiễm Prompt Injection** | Rủi ro cao: Prompt injection từ dữ liệu log/cảnh báo có thể đánh lừa LLM (chế độ `smart approval`) | Độc lập hoàn toàn với ngữ cảnh LLM: kiểm tra danh mục hữu hạn và chữ ký số mật mã | ❌ Không đạt |
| **Xác thực định danh người duyệt** | Hỏi CLI stdin hoặc tin nhắn Telegram thông thường | Bắt buộc 2 chữ ký số độc lập bằng thuật toán Ed25519 gắn với hash của hành động | ❌ Không đạt |

> [!CAUTION]
> **Kết luận 1:** **Hermes Agent KHÔNG ĐƯỢC tự làm Cổng an toàn.** Nếu để Safety Gate nằm bên trong Hermes, chỉ cần một lỗ hổng prompt injection hoặc chế độ chạy không giám sát (unattended/cron), agent có thể tự thông qua lệnh hạ tầng nguy hiểm. Cổng an toàn bắt buộc phải là một chốt chặn ngoại vi (External Policy Enforcement Point).

### 2.2. Đánh giá Bộ xác minh (Independent Verifier) trên Hermes

Nguyên lý nền tảng của kỹ nghệ an toàn hệ thống và tiêu chuẩn luận văn là:
$$\text{Actor}(\text{Remediation}) \neq \text{Actor}(\text{Verification})$$
*(Tác nhân đề xuất hoặc thi hành sửa lỗi KHÔNG BAO GIỜ được tự xác minh và kết luận hệ thống đã phục hồi).*

Nếu giao cho Hermes Agent tự xác minh:
1. **Ảo giác xác nhận (Confirmation Bias / Hallucination):** LLM có xu hướng tự biện minh rằng hành động của mình đã thành công.
2. **Thiếu tính định lượng & tính thời gian:** Bộ xác minh độc lập của đồ án đòi hỏi nghiêm ngặt:
   - 5 mẫu đo đạc liên tiếp cách nhau không quá 60s trên cửa sổ tối thiểu 120s.
   - Kiểm tra chéo cả 3 nhóm: `allowed` (giao dịch tổng hợp), `forbidden` (không mở cổng DB), `related` (chỉ số đặc thù của kịch bản).
   - Chỉ chuyển trạng thái `RESOLVED` khi có chữ ký số Ed25519 từ máy chủ đo đạc độc lập.

> [!IMPORTANT]
> **Kết luận 2:** **Hermes Agent KHÔNG ĐƯỢC tự cấp chứng nhận khôi phục (`RESOLVED`).** Việc xác minh phải do dịch vụ đo đạc chuyên biệt (`moodle-independent-verifier.py`) thực hiện dựa trên hợp đồng kiểm chứng trực tiếp từ Prometheus, Blackbox Exporter và Docker API thật.

---

## 3. Kiến trúc Đề xuất: Hermes Agent kết hợp Cổng An toàn & Bộ Xác minh Ngoại vi

Thay vì để Hermes tự làm tất cả, kiến trúc chuẩn hóa định vị Hermes Agent ở đúng thế mạnh lớn nhất của nó: **Khả năng suy luận, điều tra nguyên nhân gốc, tổng hợp thông tin và đề xuất phương án xử lý.**

### 3.1. Sơ đồ phân tách trách nhiệm (Separation of Concerns)

```mermaid
flowchart TD
    subgraph TrinhNhanTin["1. Lớp Tiếp nhận & Cảnh báo"]
        AM[Alertmanager] -->|Webhook / Alert JSON| GW[Hermes Messaging Gateway]
        GW -->|Prompt / Session Context| HA[Hermes Cognitive Agent]
    end

    subgraph DienDieuTra["2. Vùng Điều tra Nhận thức (Hermes Domain)"]
        HA <-->|MCP Protocol (Read-Only)| MCP[Moodle Read-Only MCP Server]
        MCP -->|Collect Metrics| PROM[Prometheus / Exporters]
        MCP -->|Read Health| DOCK[Docker / System Telemetry]
        HA -->|Suy luận & Chẩn đoán| DIAG[Tạo Giả thuyết & Đề xuất Hành động]
    end

    subgraph CongKiemSoat["3. Vùng Kiểm soát An toàn Ngoại vi (Safety Boundary)"]
        DIAG -->|Tạo yêu cầu SafeLiveActionRequest| GATE[Cổng An toàn Độc lập - External Gate]
        OP1[Kỹ sư Vận hành 1] -->|Ký số Ed25519| GATE
        OP2[Kỹ sư An toàn 2] -->|Ký số Ed25519| GATE
        GATE -->|Kiểm tra Danh mục & 2 Chữ ký| PASS{Đạt kiểm chuẩn?}
        PASS -->|Từ chối / Vi phạm| DENY[Ghi Log Từ chối / Chặn hoàn toàn]
    end

    subgraph VungThiHanh["4. Vùng Thi hành & Đo đạc Độc lập"]
        PASS -->|Chấp thuận| EXEC[Safe Executor Staging Boundary]
        EXEC -->|Hành động có thể hoàn tác| INFRA[Moodle Infrastructure Nodes]
        EXEC -->|Ghi biên nhận thi hành| AUDIT[(Append-Only Audit DB)]
        
        IV[Independent Verifier Daemon] -->|Đo đạc 5 mẫu trong 120s| INFRA
        IV -->|Kiểm tra Biên nhận Thi hành| AUDIT
        IV -->|Ký Chứng chỉ Khôi phục Ed25519| ATTEST[Verifier Attestation]
        ATTEST -->|Chấp thuận vào Hệ thống| RESOLVED[Trạng thái: RESOLVED]
    end
```

---

## 4. Mô hình Đa AI Luân phiên Thực hiện Task (Multi-AI Task Rotation)

Để tối ưu hóa độ tin cậy và loại bỏ điểm lỗi đơn lẻ trong suy luận (Single Point of Failure in Reasoning), đồ án đề xuất mô hình **3 AI luân phiên phối hợp** (Collaborative Red-Teaming):

### 4.1. Bảng phân công vai trò 3 Tác nhân AI

| Vai trò | Mô hình / Nền tảng | Nhiệm vụ chính | Ranh giới kiểm soát |
|---|---|---|---|
| **1. AI Điều tra (Investigator)** | Hermes Agent (ví dụ: Claude Opus / GPT-4o / Hermes-3) | - Nhận alert từ Alertmanager.<br>- Gọi MCP Server đọc chứng cứ Prometheus.<br>- Chẩn đoán nguyên nhân gốc và đề xuất kế hoạch khắc phục. | Chỉ có quyền **READ-ONLY**; không có quyền thực thi shell hay sửa đổi hệ thống. |
| **2. AI Phản biện (Auditor / Critic)** | AI phụ trợ độc lập (Model đối trọng) | - Rà soát đề xuất của Investigator.<br>- Kiểm tra ranh giới: "Hành động có đụng vào ứng dụng Moodle không?"<br>- Kiểm tra an toàn: "Có tiến trình upgrade đang chạy không (BIZ-01)?", "Đây có phải hành động phá hủy không?" | Nếu phát hiện vi phạm ranh giới hạ tầng $\rightarrow$ **Veto** đề xuất và chuyển người. |
| **3. AI Giám sát Giao tiếp (Operator Liaison)** | Hermes Telegram Gateway Bot | - Tóm tắt sự cố bằng tiếng Việt dễ hiểu cho kỹ sư vận hành.<br>- Hiển thị kế hoạch hoàn tác (Rollback Plan) và độ tin cậy.<br>- Thu thập phản hồi `/approve` hoặc chữ ký số từ kỹ sư qua kênh an toàn. | Không tham gia tính toán chẩn đoán; chỉ làm giao diện người-máy. |

---

## 5. Áp dụng vào 7 Kịch bản Chuẩn hóa của Đồ án

Nguyên tắc xuyên suốt: **"Quản trị hạ tầng không quản trị ứng dụng"**:

1. **BIZ-01 (Kẹt chế độ bảo trì sau update):**
   - *AI làm gì:* Đọc cờ file `climaintenance.html`, kiểm tra xem tiến trình `upgrade.php` còn chạy không.
   - *Quyết định:* Nếu `moodle_upgrade_in_progress == 0.0`, đề xuất chạy CLI tắt bảo trì. Nếu upgrade đang chạy $\rightarrow$ CẤM ĐỤNG, chuyển người.
2. **BIZ-02 (Kẹt hàng đợi tác vụ nền do container cron dừng):**
   - *AI làm gì:* Đo tuổi tác vụ, kiểm tra trạng thái container `moodle-cron` trên node A.
   - *Quyết định:* Đề xuất khởi động lại container `moodle-cron` ở mức hạ tầng Docker. Tuyệt đối KHÔNG chạy câu lệnh SQL xóa khóa trong database Moodle.
3. **CON-01 (Container web dừng trên node B):**
   - *AI làm gì:* Kiểm tra Docker inspect và trạng thái ALB target.
   - *Quyết định:* Đề xuất chạy `start_reviewed_compose_service` trên node B sau khi duyệt.
4. **DB-01 (Mất kết nối TCP tới RDS):**
   - *AI làm gì:* So sánh TCP probe node A và node B, tìm iptables rule có nhãn sự cố.
   - *Quyết định:* Đề xuất gỡ đúng rule iptables có nhãn.
5. **RES-01 (CPU node ứng dụng quá tải):**
   - *AI làm gì:* Kiểm tra CPU từng node, xác định container gây tải nhân tạo đã biết tên.
   - *Quyết định:* Đề xuất dừng đúng container gây nghẽn tài nguyên.
6. **NET-01 (Lệch phân giải tên hosts nội bộ):**
   - *AI làm gì:* Kiểm tra synthetic probe và nội dung hosts override bên trong container.
   - *Quyết định:* Đề xuất tạo lại web container từ Compose chuẩn đã được kiểm duyệt.
7. **SEC-01 (Rò rỉ mở cổng Database RDS):**
   - *AI làm gì:* Phát hiện rule Security Group mở cổng 5432 từ Internet (`0.0.0.0/0`).
   - *Quyết định:* **HUMAN_ONLY**. AI chỉ đưa ra báo cáo và hướng dẫn; tuyệt đối CẤM tạo kế hoạch tự động sửa đổi Security Group trên Cloud.

---

## 6. Kết luận và Lộ trình Triển khai

1. **Về phần 7 Kịch bản:** Đã hoàn thiện toàn bộ trong mã nguồn (`planner.py`, `action_catalog.py`, `live_contracts.py`), kiểm thử tự động 16/16 test case đạt 100%, tích hợp trình chạy ngoại tuyến `automation/run-moodle-7-scenarios.py`.
2. **Về phần Hermes Agent:** Khẳng định Hermes là một công cụ xuất sắc cho vai trò **Investigator & Gateway**, nhưng **bắt buộc phải kết nối với Cổng An toàn và Bộ Xác minh ngoại vi** thông qua chuẩn MCP chỉ-đọc để bảo đảm tính an toàn tuyệt đối và tính kiểm chứng khoa học cho luận văn.
