# Evidence Collection & Tamper-Resistant Storage (`agent_src/core/evidence/`)

Thư mục này chịu trách nhiệm về **thu thập, chuẩn hóa và bảo vệ tính toàn vẹn của bằng chứng kỹ thuật** trong toàn bộ vòng đời ứng cứu sự cố.

Mọi quyết định chẩn đoán và hành động của AI Agent bắt buộc phải dựa trên bằng chứng thực nghiệm có thể kiểm chứng được, không chấp nhận việc suy diễn không có căn cứ.

---

## Danh mục các file và chức năng chi tiết

### 1. `evidence_store.py`
- **Tác dụng**: Kho lưu trữ bằng chứng bất biến (`SQLiteEvidenceStore`) dựa trên SQLite. Mỗi mẩu bằng chứng (`EvidenceRecord`) khi ghi nhận đều được tính toán băm nội dung SHA-256 (`evidence_content_hash`) và chuỗi liên kết băm để chống giả mạo hoặc sửa đổi.
- **Ảnh hưởng tới toàn bộ AI Agent**: Là nguồn chân lý kỹ thuật cho các phân hệ Diagnosis, Planner và Verifier. Nếu phát hiện bằng chứng bị can thiệp trái phép, hệ thống lập tức báo lỗi toàn vẹn (`EvidenceIntegrityError`) và dừng xử lý.

### 2. `evidence_collectors.py`
- **Tác dụng**: Động cơ thu thập bằng chứng từ nhiều nguồn: truy vấn Prometheus metrics theo alert (`METRIC_QUERIES_BY_ALERT`), bóc tách log hệ thống, truy vấn endpoint kiểm tra sức khỏe và lấy dữ liệu giao dịch giả lập (`synthetic transactions`).
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp bức tranh toàn diện và khách quan về trạng thái của hệ thống tại thời điểm xảy ra sự cố, làm dữ liệu đầu vào trực tiếp cho Agent chẩn đoán.

### 3. `adapters.py`
- **Tác dụng**: Lớp adapter chuẩn hóa giao tiếp thu thập dữ liệu và thực thi hành động (`ActionResult`, `SafeActionRequest`, `sanitize_adapter_output`).
- **Ảnh hưởng tới toàn bộ AI Agent**: Đóng gói các tương tác với hệ thống bên ngoài thành các giao thức chuẩn, tự động khử khuẩn dữ liệu nhạy cảm đầu ra trước khi nạp vào chu trình phân tích.

### 4. `dependency_graph.py`
- **Tác dụng**: Xây dựng và duyệt đồ thị phụ thuộc topo (`DependencyGraph`) giữa các dịch vụ, tiến trình và tài nguyên hạ tầng (ví dụ: Moodle App phụ thuộc vào RDS PostgreSQL và EFS Storage).
- **Ảnh hưởng tới toàn bộ AI Agent**: Giúp phân hệ chẩn đoán lần theo chuỗi lan truyền sự cố, khoanh vùng nguyên nhân gốc rễ và đánh giá phạm vi ảnh hưởng (blast radius) của sự cố.

### 5. `__init__.py`
- **Tác dụng**: Khởi tạo subpackage `core.evidence`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp namespace chuẩn cho toàn bộ phân hệ quản lý bằng chứng.

