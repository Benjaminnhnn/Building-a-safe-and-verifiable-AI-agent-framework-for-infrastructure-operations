# System Configuration & Knowledge Base (`agent_src/config/`)

Thư mục này chứa toàn bộ các **tệp cấu hình hạ tầng (Configuration Files)** và **kho tri thức vận hành chuẩn hóa (Knowledge Base Runbooks)** được nạp vào cơ sở dữ liệu vector RAG (`ChromaDB`) khi khởi động hệ thống.

---

## Danh mục các file và chức năng chi tiết

### 1. Cấu hình hệ thống (JSON Configurations)

- **`moodle_capability_contract.json`**:
  - **Tác dụng**: Khai báo danh mục năng lực (capabilities) và các hành động được phép thực thi trên hạ tầng Moodle (ví dụ: `RESTART_SERVICE`, `CLEAR_CACHE`, `SCALE_RESOURCES`), kèm theo danh sách rủi ro và các tham số hợp lệ.
  - **Ảnh hưởng tới toàn bộ AI Agent**: Là nguồn tham chiếu để `SafeExecutionGate` và `ActionCatalog` kiểm tra tính hợp lệ của mọi đề xuất hành động từ AI.
- **`moodle_resource_inventory.json`**:
  - **Tác dụng**: Định nghĩa danh mục tài nguyên thực tế của cụm Moodle trên AWS (Application nodes: `moodle-app-01`, `moodle-app-02`, cơ sở dữ liệu RDS PostgreSQL, hệ thống tệp chia sẻ EFS, và Load Balancer ALB).
  - **Ảnh hưởng tới toàn bộ AI Agent**: Thiết lập ranh giới tài nguyên (Resource Boundary), đảm bảo AI Agent chỉ được phép tương tác với các tài nguyên đã đăng ký.
- **`services_config.json`**:
  - **Tác dụng**: Cấu hình các dịch vụ cần giám sát bởi tiến trình `service_monitor.py` (cổng mạng, trạng thái kích hoạt, thời gian cooldown cảnh báo).
  - **Ảnh hưởng tới toàn bộ AI Agent**: Quyết định những cổng dịch vụ và tiến trình nào trên host sẽ được kiểm tra và kích hoạt cảnh báo khi gặp sự cố.

### 2. Kho tri thức Runbooks (`knowledge_base/`)

Thư mục `knowledge_base/` chứa các tài liệu hướng dẫn vận hành chuẩn (SOP / Runbooks) viết bằng định dạng Markdown. Khi AI Agent khởi động, `runbook_registry.py` sẽ phân tách các tài liệu này theo tiêu đề và đưa vào ChromaDB (`standard_runbooks` collection):

- **Hạ tầng cơ sở**:
  - `runbook_docker.md`: Quy trình xử lý sự cố container Docker, OOMKilled, lỗi daemon, lỗi network bridge.
  - `runbook_nginx.md`: Hướng dẫn chẩn đoán lỗi Web Server Nginx (502 Bad Gateway, 504 Gateway Timeout, cấu hình upstream).
  - `runbook_postgresql.md`: Khắc phục sự cố cơ sở dữ liệu PostgreSQL (kết nối pool cạn kiệt, lock contention, chậm truy vấn, lỗi replication).
  - `runbook_redis.md`: Xử lý sự cố bộ nhớ đệm Redis (memory exhaustion, rớt kết nối cache).

- **Đặc thù ứng dụng Moodle (`knowledge_base/moodle/`)**:
  - `runbook_moodle_connectivity.md`: Chẩn đoán và xử lý mất kết nối giữa Moodle Web Nodes và cơ sở dữ liệu RDS PostgreSQL hoặc Redis Session Cache.
  - `runbook_moodle_monitoring.md`: Hướng dẫn kiểm tra các chỉ số sức khỏe của Moodle qua Prometheus và endpoint `/healthz.php`.
  - `runbook_moodle_security.md`: Quy tắc an toàn, quản lý quyền hạn tệp tin và bí mật chứng thực trên cụm Moodle.
  - `runbook_moodle_service.md`: Hướng dẫn khởi động lại, bảo trì và kiểm tra trạng thái dịch vụ Apache/PHP-FPM của Moodle.
  - `runbook_moodle_storage_resources.md`: Hướng dẫn xử lý sự cố gắn kết EFS (`/var/www/moodledata`), đầy dung lượng ổ đĩa và phân quyền thư mục dữ liệu.

- **Ảnh hưởng tới toàn bộ AI Agent**: Cung cấp tri thức chuyên môn thực nghiệm cho công cụ RAG (`rag_engine.py`), giúp LLM đưa ra các suy luận chẩn đoán và kế hoạch xử lý bám sát quy chuẩn vận hành của doanh nghiệp.

