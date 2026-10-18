# Safety Policy, Action Catalog & Gate Enforcement (`agent_src/core/safety/`)

Thư mục này là **lá chắn phòng thủ then chốt** của toàn bộ hệ thống AI Agent Ops.

Mục tiêu tối thượng của phân hệ Safety là đảm bảo nguyên tắc **Fail-Closed (Mặc định từ chối khi không chắc chắn)**: AI Agent không bao giờ được phép tự ý thực thi các hành động tùy tiện, phá hoại hoặc vượt quá ranh giới quyền hạn được cấp phép.

---

## Danh mục các file và chức năng chi tiết

### 1. `action_catalog.py`
- **Tác dụng**: Quản lý danh mục các hành động kỹ thuật an toàn (`ActionCatalog`, `CatalogEntry`). Phân loại hành động theo 4 cấp độ rủi ro: `READ_ONLY`, `WEAK_MUTATION`, `STRONG_MUTATION`, `IRREVERSIBLE`, kèm phạm vi quyền hạn (least-privilege permissions) và tài nguyên mục tiêu.
- **Ảnh hưởng tới toàn bộ AI Agent**: Xác lập ranh giới tuyệt đối cho những gì Agent **được phép** và **không được phép** thực thi. Mọi hành động nằm ngoài danh mục này đều bị từ chối ngay lập tức.

### 2. `safety_policy_engine.py`
- **Tác dụng**: Động cơ thẩm định chính sách an toàn (`SafetyPolicyEngine`). Đánh giá mức độ rủi ro (`BlastRadius`), kiểm tra ranh giới môi trường (chặn can thiệp Production nếu chưa có cờ cấp phép tường minh), phát hiện xung đột tài nguyên và kiểm tra tính hợp lệ của kịch bản khôi phục (Rollback Plan).
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp phán quyết chính sách tĩnh và động cho từng hành động đề xuất trước khi chuyển tới Cổng an toàn.

### 3. `safe_execution_gate.py`
- **Tác dụng**: Cổng phê duyệt thực thi an toàn (`SafeExecutionGate`). Đối chiếu kế hoạch hành động với chính sách và trạng thái phê duyệt của Operator, đưa ra phán quyết cuối cùng: `ALLOW_AUTONOMOUS`, `REQUIRE_HUMAN_APPROVAL`, hoặc `REJECT`. Ký chữ ký băm bảo mật HMAC (`action_digest`) cho các kế hoạch được duyệt.
- **Ảnh hưởng tới toàn bộ AI Agent**: Là chốt chặn quyết định hành động có được đi tiếp hay không. Tách bạch hoàn toàn giữa việc đề xuất hành động (của Planner) và quyền phê duyệt thực thi (của Gate/Con người).

### 4. `safe_action_audit.py`
- **Tác dụng**: Sổ cái kiểm toán an toàn bất biến (`SafeActionAuditStore`) lưu trữ trong SQLite. Ghi nhận toàn bộ yêu cầu hành động, phán quyết của Gate, chữ ký băm, thời gian, và kết quả thực thi dry-run/live.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo khả năng giải trình (accountability) và đối soát pháp lý cho mọi quyết định của AI. Dữ liệu chỉ được ghi nối tiếp (append-only), chống sửa đổi.

### 5. `safe_action_live.py`
- **Tác dụng**: Điều phối quy trình thực thi hành động trực tiếp trên hạ tầng live có kiểm soát rủi ro (`SafeActionLiveWorkflow`). Kiểm tra chữ ký phê duyệt hợp lệ và trạng thái kiểm chứng trước khi gọi client thực thi.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo hạ tầng thực tế chỉ tiếp nhận các hành động đã vượt qua cổng an toàn và có chữ ký xác thực hợp lệ của Operator.

### 6. `safe_executor_client.py`
- **Tác dụng**: Client giao tiếp với daemon thực thi an toàn cục bộ trên host (`safe-executor`) qua Unix domain socket hoặc HTTP mTLS với timeout chặt chẽ.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cách ly quyền hạn cao nhất (root/sudo) ra khỏi container AI. Container AI chỉ gửi yêu cầu thực thi có cấu trúc sang daemon độc lập, ngăn chặn việc container bị chiếm quyền điều khiển.

### 7. `sanitization.py`
- **Tác dụng**: Khử khuẩn và ẩn thông tin nhạy cảm (`sanitize_log_excerpt`, `mask_secrets`, `contains_unredacted_secret`). Tự động tìm kiếm và thay thế mật khẩu, private key, token, credit card, chuỗi kết nối DB bằng `[REDACTED]`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Bảo vệ an toàn dữ liệu, ngăn ngừa rò rỉ bí mật hạ tầng vào logs, LLM context, SQLite store hoặc tin nhắn thông báo Telegram.

### 8. `__init__.py`
- **Tác dụng**: Khởi tạo subpackage `core.safety`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp namespace chuẩn cho toàn bộ phân hệ an toàn.

