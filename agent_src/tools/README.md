# Operational Tools (`agent_src/tools/`)

Thư mục này chứa các **tiện ích CLI** cho kỹ sư vận hành và kỹ sư AI. Chúng không nằm trên đường xử lý cảnh báo của Agent: bằng chứng chẩn đoán được thu thập bởi `core/evidence/evidence_collectors.py` với tập truy vấn PromQL đã duyệt.

---

## Danh mục các file và chức năng chi tiết

### 1. `inspect_rag_db.py`
- **Tác dụng**: Công cụ dòng lệnh độc lập để kiểm tra cơ sở dữ liệu vector ChromaDB (`VECTOR_DB_PATH`). Hỗ trợ liệt kê các collection hiện có (`standard_runbooks`, `incident_memory`), đếm số lượng tài liệu, lọc tài liệu theo metadata nguồn (`--source`), và hiển thị nội dung chunk kèm metadata.
- **Khi nào dùng**: Kiểm định việc nạp runbook, gỡ lỗi RAG khi agent không tìm thấy runbook phù hợp, và kiểm tra tri thức học được từ feedback của admin (`incident_memory`).

### 2. `prometheus_check.py`
- **Tác dụng**: Lớp tiện ích `PrometheusChecker` truy vấn trực tiếp Prometheus HTTP API (`/api/v1/query`), bóc tách giá trị metric và kiểm tra một cảnh báo đã hết hay chưa (`is_alert_resolved`).
- **Khi nào dùng**: Kỹ sư kiểm tra nhanh telemetry khi một cảnh báo bị chẩn đoán là thiếu bằng chứng. Agent không gọi công cụ này; việc xác nhận phục hồi thuộc về Independent Verifier.
