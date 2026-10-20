# Operational & Diagnostic Tools (`agent_src/tools/`)

Thư mục này chứa các **công cụ chẩn đoán kỹ thuật (Diagnostic Tools)** và **tiện ích quản trị CLI (Admin Tools)** được AI Agent sử dụng trong quá trình điều tra sự cố hoặc được kỹ sư vận hành (SRE) sử dụng để kiểm tra nội bộ hệ thống.

---

## Danh mục các file và chức năng chi tiết

### 1. `diag_tools.py`
- **Tác dụng**: Cung cấp hộp công cụ chẩn đoán an toàn (Safe Diagnostic Toolbox) có thể gọi từ LLM Tool-Calling hoặc các tác tử chẩn đoán (`DiagnosisAgent`). Bao gồm:
  - Thu thập chỉ số tài nguyên máy chủ (`get_system_metrics`: CPU, RAM, Disk).
  - Liệt kê các tiến trình đang thực thi (`list_running_services`).
  - Đọc an toàn các dòng cuối của file log dịch vụ (`read_service_logs` - có whitelist dịch vụ và giới hạn số dòng để tránh quá tải token).
  - Các công cụ kiểm tra mạng (`ping_host`, `check_port_open`, `traceroute_host`, `dns_lookup`, `curl_endpoint`).
  - Toàn bộ tham số đầu vào (IP, hostname, URL) đều được kiểm tra biểu thức chính quy (`is_valid_host`) để triệt tiêu hoàn toàn nguy cơ Command Injection / Shell Injection.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp "tai mắt" cho AI Agent khi tiến hành điều tra chuyên sâu sự cố, biến các suy luận lý thuyết thành các kiểm tra thực nghiệm khách quan trên hệ thống.

### 2. `inspect_rag_db.py`
- **Tác dụng**: Công cụ dòng lệnh (CLI Utility) độc lập dành cho quản trị viên và kỹ sư AI để kiểm tra cơ sở dữ liệu vector ChromaDB (`VECTOR_DB_PATH`). Hỗ trợ liệt kê các collection hiện có (`standard_runbooks`, `incident_memory`), đếm số lượng tài liệu, lọc tài liệu theo metadata nguồn (`--source`), và hiển thị nội dung chunk kèm embedding metadata.
- **Ảnh hưởng tới toàn bộ AI Agent**: Giúp kiểm định chất lượng nạp runbook (ingestion verification), gỡ lỗi RAG khi agent không tìm thấy runbook phù hợp, và kiểm tra các tri thức học được từ feedback của admin (`incident_memory`).

### 3. `prometheus_check.py`
- **Tác dụng**: Cung cấp lớp tiện ích `PrometheusChecker` để truy vấn trực tiếp Prometheus HTTP API (`/api/v1/query`). Kiểm tra các biểu thức PromQL thời gian thực, bóc tách giá trị metric, và hỗ trợ các hàm kiểm tra xem một cảnh báo cụ thể đã thực sự được giải quyết chưa (`is_alert_resolved`).
- **Ảnh hưởng tới toàn bộ AI Agent**: Là công cụ đo lường khách quan được sử dụng bởi các bộ thu thập bằng chứng (`evidence_collectors.py`) và các đầu dò kiểm chứng phục hồi (`probes.py`).

