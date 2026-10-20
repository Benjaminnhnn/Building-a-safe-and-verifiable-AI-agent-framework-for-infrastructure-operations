# Domain Models (`agent_src/core/domain/`)

Thư mục này chứa các **thực thể miền (Domain Entities)** trung lập hoàn toàn với nền tảng của hệ thống AI Agent Ops.

Đây là lớp nghiệp vụ cốt lõi, hoàn toàn độc lập với các công nghệ hạ tầng cụ thể (Moodle, AWS, Kubernetes, Postgres, v.v.). Mọi thành phần trong Agent Core chỉ giao tiếp và thao tác trên các hợp đồng dữ liệu chuẩn hóa này.

---

## Danh mục các file và chức năng chi tiết

### 1. `incident.py`
- **Tác dụng**: Định nghĩa thực thể `Incident` đại diện cho một sự cố kỹ thuật trong toàn bộ vòng đời của nó, bao gồm định danh (`incident_id`), tiêu đề, dịch vụ bị ảnh hưởng, mức độ nghiêm trọng (`severity`), trạng thái hiện tại (`status`), dòng thời gian (`timeline`) và siêu dữ liệu bổ trợ (`metadata`).
- **Ảnh hưởng tới toàn bộ AI Agent**: Là đối tượng dữ liệu trung tâm xuyên suốt chu trình xử lý của Agent: từ tiếp nhận cảnh báo, điều tra bằng chứng, lập kế hoạch, kiểm duyệt an toàn, cho tới khi hoàn tất kiểm chứng và đóng sự cố.

### 2. `resource.py`
- **Tác dụng**: Định nghĩa thực thể `Resource` biểu diễn một tài nguyên hạ tầng trừu tượng (như node máy chủ, container, cơ sở dữ liệu, dịch vụ mạng, ổ đĩa lưu trữ) cùng với trạng thái hoạt động và mối quan hệ phụ thuộc (`dependencies`) với các tài nguyên khác.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cho phép xây dựng đồ thị phụ thuộc (`DependencyGraph`) để tính toán phạm vi ảnh hưởng (blast radius) và khoanh vùng tài nguyên khi xảy ra sự cố.

### 3. `evidence.py`
- **Tác dụng**: Định nghĩa thực thể `Evidence` biểu diễn các bằng chứng kỹ thuật khách quan được thu thập (metric từ Prometheus, log trích xuất, kết quả ping/curl, trạng thái container). Hỗ trợ băm SHA256 (`content_hash`) để đảm bảo tính bất biến và phát hiện dữ liệu giả mạo/mâu thuẫn.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo nguyên tắc "ra quyết định dựa trên bằng chứng" (evidence-backed operations). AI Agent tuyệt đối không được suy đoán hoặc hành động nếu không có `Evidence` hợp lệ bảo chứng.

### 4. `action_plan.py`
- **Tác dụng**: Định nghĩa thực thể `ActionPlan` đại diện cho kế hoạch hành động ứng cứu do Planner sinh ra, bao gồm danh sách các hành động có thứ tự (`actions`), kế hoạch hoàn tác (`rollback_actions`), ước lượng thời gian thực thi và đánh giá mức độ rủi ro sơ bộ.
- **Ảnh hưởng tới toàn bộ AI Agent**: Là đầu vào quan trọng nhất cho Cổng an toàn (`SafetyGate`). Nếu kế hoạch thiếu các thông tin kiểm định rủi ro hoặc thiếu kế hoạch rollback, cổng an toàn sẽ từ chối phê duyệt.

### 5. `dry_run.py`
- **Tác dụng**: Cung cấp công cụ chạy giả lập kế hoạch (`DryRunner`) và cấu trúc kết quả giả lập (`DryRunDecision`). Đánh giá xem nếu kế hoạch hành động được thực thi thì tài nguyên nào sẽ bị thay đổi, có gây gián đoạn dịch vụ hoặc vi phạm chính sách hay không.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp cơ chế xác minh không can thiệp (zero-mutation verification) cho các chế độ Replay và Shadow. Đảm bảo mọi quyết định của Agent có thể được thẩm định an toàn trước khi xem xét cho phép can thiệp live.

### 6. `approval.py`
- **Tác dụng**: Định nghĩa `ApprovalRecord`, `ApprovalScope`, kho lưu trữ nối tiếp không sửa xóa `ApprovalStore` (append-only) và cổng người duyệt `HumanGate`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Quản lý phê duyệt cho các hành động có trạng thái `REQUIRE_APPROVAL`. Ràng buộc chặt chẽ theo `plan_hash`, người phê duyệt có thẩm quyền (`authorized_approvers`), phạm vi hành động (`scope`) và thời hạn hiệu lực (`expires_at`).

### 7. `gate_pass.py`
- **Tác dụng**: Định nghĩa value object `GatePass` bất biến. Chỉ được khởi tạo nội bộ bởi `SafetyGate` sau khi vượt qua cả 3 bước kiểm tra (Policy, Dry-run, Approval).
- **Ảnh hưởng tới toàn bộ AI Agent**: Là tấm thẻ ủy quyền bắt buộc duy nhất mà `SafeExecutor` chấp nhận trước khi thực thi thay đổi trên hạ tầng. Constructor kiểm tra token nội bộ bảo vệ khỏi việc giả mạo.

### 8. `audit_log.py`
- **Tác dụng**: Định nghĩa `AuditEntry` và `SafetyAuditLogger` ghi log định dạng JSONL strictly append-only.
- **Ảnh hưởng tới toàn bộ AI Agent**: Ghi lại đầy đủ mọi quyết định, bao gồm: `timestamp`, `plan_hash`, `policy_decision`, `reason_code`, tóm tắt diff (`diff_summary`), người duyệt (`approver`), và kết quả (`result`). Đảm bảo 100% mọi trường hợp từ chối đều có dòng audit tương ứng.

### 9. `safety_gate.py`
- **Tác dụng**: Cung cấp thực thể `SafetyGate` thực hiện chu trình kiểm duyệt 3 bước:
  1. Đánh giá chính sách (Policy check)
  2. Chạy giả lập không can thiệp (Dry-run check & Diff generation)
  3. Xác thực phê duyệt của con người (Approval verification & Hash matching)
- **Ảnh hưởng tới toàn bộ AI Agent**: Đóng vai trò chốt chặn then chốt, chỉ cấp phát `GatePass` khi cả ba bước đạt tiêu chuẩn an toàn.

### 10. `executor.py`
- **Tác dụng**: Định nghĩa `SafeExecutor` thực thi kế hoạch an toàn.
- **Ảnh hưởng tới toàn bộ AI Agent**: Yêu cầu bắt buộc phải có `GatePass` hợp lệ. Ngay trước khi can thiệp hạ tầng, thực hiện quy trình **Just-In-Time Re-check**: kiểm tra lại `plan_hash`, hạn phê duyệt, quyền người duyệt, trạng thái tài nguyên đích còn tồn tại hay không, và toàn bộ điều kiện tiên quyết (`preconditions`). Bất kỳ sai lệch nào đều kích hoạt hủy bỏ thực thi ngay lập tức (`abort`) và ghi nhận vào audit log.

### 11. `enums.py`
- **Tác dụng**: Định nghĩa các kiểu liệt kê chuẩn mực (Enums) dùng chung toàn hệ thống, bao gồm: `IncidentStatus`, `Severity`, `ActionType`, `Environment`, và `EvidenceSource`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo tính nhất quán về mặt ngữ nghĩa và kiểu dữ liệu cho toàn bộ các module trong hệ thống, loại bỏ lỗi do gõ sai chuỗi (string typing error).

### 12. `__init__.py`
- **Tác dụng**: Export toàn bộ các class và enum trên để các module khác có thể import ngắn gọn qua `from core.domain import ...`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Giúp chuẩn hóa giao diện import của tầng Domain.
