# Data Contracts & Schemas (`agent_src/core/schema/`)

Thư mục này chứa toàn bộ các **hợp đồng dữ liệu Pydantic (Pydantic Schemas)** được sử dụng làm ranh giới truyền tải dữ liệu (Data Transfer Objects / Boundary Contracts) trong toàn bộ hệ thống AI Agent Ops.

Lớp schema đảm bảo tính toàn vẹn dữ liệu, kiểm tra hợp lệ kiểu (type validation), ngăn chặn việc truyền dữ liệu thiếu hoặc dữ liệu độc hại giữa các giai đoạn xử lý và qua các API ranh giới.

---

## Danh mục các file và chức năng chi tiết

### 1. `action.py`
- **Tác dụng**: Định nghĩa Pydantic model cho `TypedAction` (hành động kỹ thuật đã định kiểu, tham số rõ ràng, tài nguyên mục tiêu) và `RollbackPlan` (kịch bản khôi phục khi hành động thất bại).
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo các hành động do Planner sinh ra không chứa tham số tùy tiện hay độc hại. Mọi tham số đều được validate nghiêm ngặt trước khi chuyển đến Safety Gate.

### 2. `audit.py`
- **Tác dụng**: Định nghĩa cấu trúc cho các bản ghi nhật ký kiểm toán bất biến (`AuditEvent`, `StageAuditEvent`), lưu trữ vết chuyển đổi trạng thái, dấu vết thời gian, chữ ký điện tử hoặc băm bảo mật.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo tính minh bạch và khả năng giải trình (accountability). Bất kỳ can thiệp nào của hệ thống đều có thể được truy vết và đối soát phục vụ kiểm toán bảo mật.

### 3. `common.py`
- **Tác dụng**: Cung cấp các kiểu liệt kê cơ sở và model dữ liệu dùng chung (`ActionType`, `Environment`, `ActionDecision`, `IncidentStatus`, `EvidenceType`).
- **Ảnh hưởng tới toàn bộ AI Agent**: Là nền tảng chia sẻ định nghĩa kiểu cho tất cả các schema khác, loại bỏ xung đột định nghĩa giữa các tầng.

### 4. `diagnosis.py`
- **Tác dụng**: Định nghĩa model `DiagnosisResult`, biểu diễn kết luận chẩn đoán nguyên nhân gốc rễ, điểm số tin cậy (confidence score), các giả thuyết đã xem xét và bằng chứng hỗ trợ cho từng giả thuyết.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo kết quả chẩn đoán có cấu trúc rõ ràng, giúp Planner có căn cứ chính xác để xây dựng kế hoạch ứng cứu.

### 5. `evidence.py`
- **Tác dụng**: Định nghĩa schema Pydantic cho `Evidence`, thực hiện kiểm tra định dạng dữ liệu, thời gian thu thập, nguồn gốc (Prometheus, logs, API) và tính toán băm nội dung để chống sửa đổi.
- **Ảnh hưởng tới toàn bộ AI Agent**: Là chốt chặn xác thực tính hợp lệ của mọi bằng chứng kỹ thuật trước khi đưa vào kho lưu trữ `EvidenceStore`.

### 6. `incident.py`
- **Tác dụng**: Định nghĩa schema Pydantic cho `Incident`, kiểm soát dữ liệu đầu vào của sự cố tiếp nhận từ Webhook hoặc hệ thống giám sát.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo mọi sự cố mới được tiếp nhận đều có đầy đủ thông tin định danh, ngữ cảnh và mức độ nghiêm trọng hợp lệ.

### 7. `resource.py`
- **Tác dụng**: Định nghĩa schema cho `Resource`, bao gồm các thuộc tính tài nguyên, môi trường triển khai, ranh giới an toàn và các nhãn (tags) liên quan.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo Agent chỉ làm việc trên các tài nguyên đã được khai báo và nhận diện đầy đủ trong hệ thống.

### 8. `safety.py`
- **Tác dụng**: Định nghĩa model cho `SafetyDecision`, lưu trữ phán quyết của cổng an toàn (APPROVE, REJECT, ESCALATE), mức độ rủi ro, và danh sách các vi phạm chính sách nếu có.
- **Ảnh hưởng tới toàn bộ AI Agent**: Chuẩn hóa đầu ra của Safety Gate, là cơ sở bắt buộc để quyết định xem hành động có được phép đi tiếp hay bị chặn đứng.

### 9. `scenario.py`
- **Tác dụng**: Định nghĩa model `ScenarioGroundTruth` phục vụ cho việc chấm điểm và đánh giá hiệu năng agent trong các bài thử nghiệm benchmark và replay.
- **Ảnh hưởng tới toàn bộ AI Agent**: Lưu trữ đáp án chuẩn của chuyên gia cho từng kịch bản sự cố, tuyệt đối chỉ dùng cho mục đích chấm điểm (scoring), không bao giờ được dùng làm đầu vào ủy quyền hành động.

### 10. `verification.py`
- **Tác dụng**: Định nghĩa model `VerificationResult`, ghi nhận kết quả kiểm tra độc lập sau xử lý sự cố (đạt/không đạt, danh sách các đầu dò đã kiểm tra, điểm sức khỏe dịch vụ).
- **Ảnh hưởng tới toàn bộ AI Agent**: Là căn cứ dữ liệu duy nhất cho phép chuyển trạng thái sự cố sang `RESOLVED`.

### 11. `contract_validation.py`
- **Tác dụng**: Cung cấp các hàm kiểm tra tính toàn vẹn và tính nhất quán giữa các hợp đồng dữ liệu trong pipeline (`validate_pipeline_references`). Kiểm tra liên kết chéo giữa Incident ID, Evidence IDs và Action IDs.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo các đối tượng dữ liệu truyền qua các bước không bị giả mạo hoặc mất liên kết ngữ cảnh.

### 12. `event_schema.py`
- **Tác dụng**: Chuẩn hóa cấu trúc alert thô từ Alertmanager thành `NormalizedAlertEvent` (`normalize_alertmanager_payload`, `normalize_alert`), bóc tách nhãn (labels), chú thích (annotations), fingerprint và thời điểm bắt đầu (`startsAt`).
- **Ảnh hưởng tới toàn bộ AI Agent**: Làm sạch và đồng nhất dữ liệu đầu vào cho toàn bộ hệ thống, loại bỏ sự khác biệt giữa các phiên bản Alertmanager.

### 13. `__init__.py`
- **Tác dụng**: Export các schema chính và hàm validate cho package `core.schema`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Giúp việc import các model Pydantic và hợp đồng diễn ra thống nhất.

