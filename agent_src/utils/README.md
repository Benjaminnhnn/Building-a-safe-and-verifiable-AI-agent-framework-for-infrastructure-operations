# Shared Utilities & Integrations (`agent_src/utils/`)

Thư mục này chứa các **tiện ích tích hợp ngoại vi (External Integrations & Utilities)** phục vụ việc tương tác giữa AI Agent và con người (Human-in-the-Loop).

---

## Danh mục các file và chức năng chi tiết

### 1. `telegram_bot.py`
- **Tác dụng**: Tích hợp với Telegram Bot API nhằm tạo kênh giao tiếp hai chiều giữa AI Agent và kỹ sư vận hành (SRE / Sysadmin):
  - **Gửi cảnh báo & báo cáo sự cố**: Tự động gửi báo cáo tóm tắt tình hình sự cố, nguyên nhân chẩn đoán và hành động đề xuất ngay khi có kết quả.
  - **Human-in-the-loop Approval**: Cung cấp các nút bấm tương tác (Inline Keyboards) hoặc lệnh `/approve <id>`, `/reject <id>` để quản trị viên trực tiếp phê duyệt hoặc từ chối các hành động có mức độ rủi ro cao trước khi hệ thống can thiệp.
  - **Thu thập phản hồi quản trị viên (`/feedback`)**: Tiếp nhận phản hồi đánh giá hoặc hướng dẫn sửa sai từ admin (`/feedback <incident_id> <nội dung>`), phân tích bằng LLM và nạp vào bộ nhớ RAG (`incident_memory` trong ChromaDB) để agent tự hoàn thiện trong các lần xử lý sau.
  - **Bảo mật**: Xác thực chặt chẽ `TELEGRAM_CHAT_ID`, từ chối các lệnh đến từ các chat ID không được ủy quyền.
- **Ảnh hưởng tới toàn bộ AI Agent**: Là cầu nối đảm bảo tính an toàn tối thượng theo nguyên tắc "Human-in-the-loop". Ngăn chặn hoàn toàn việc AI tự ý thực thi các hành động đột biến mạnh (strong mutations) khi chưa có sự đồng thuận tường minh của con người.

