# Infrastructure Monitoring Daemons (`agent_src/monitoring/`)

Thư mục này chứa các **tiến trình giám sát nền (Monitoring Daemons)** chạy trực tiếp trên các host hạ tầng hoặc trong container nhằm phát hiện sớm các dấu hiệu bất thường về log, cổng dịch vụ, và mạng, sau đó chuẩn hóa thành payload Alertmanager và gửi về Webhook của AI Agent (`core/main.py: /webhook`).

---

## Danh mục các file và chức năng chi tiết

### 1. `log_watcher.py`
- **Tác dụng**: Giám sát thời gian thực các file log hệ thống và ứng dụng (`/var/log/syslog`, `/var/log/nginx/error.log`, v.v.) bằng thư viện `watchdog` (bắt sự kiện file hệ điều hành thay vì polling liên tục). Tự động nhận diện các dòng log chứa từ khóa nghiêm trọng (`ERROR`, `CRITICAL`, `FATAL`, `EXCEPTION`, `FAILED`, `PANIC`), xử lý hiện tượng log rotation/truncation, đóng gói thành cảnh báo `LogFileErrorDetected` và gửi về AI Agent với cơ chế lũy tiến số mũ (exponential backoff retry) và cooldown chống flood.
- **Ảnh hưởng tới toàn bộ AI Agent**: Là nguồn phát tín hiệu sớm (early warning signal) cho các lỗi phần mềm hoặc crash hệ thống trước khi Prometheus kịp cào metric. Cung cấp dữ liệu log trích đoạn (log excerpt) đóng vai trò là bằng chứng kỹ thuật đầu vào (`Evidence`) cho quá trình chẩn đoán.

### 2. `service_monitor.py`
- **Tác dụng**: Tiến trình giám sát dịch vụ hợp nhất (`UnifiedServiceMonitor`) chạy vòng lặp 10 giây/lần:
  - **Port Checker**: Giám sát tính khả dụng của các port TCP nội bộ (Port 80 Nginx, 5432 PostgreSQL, v.v.), chỉ phát cảnh báo `ServicePortDown` khi trạng thái chuyển từ `UP` sang `DOWN`.
  - **Network Checker**: Đo lường sự thay đổi (delta) của `psutil.net_io_counters()`, kiểm tra tỷ lệ rớt gói (`packet_drop_rate`), tỷ lệ lỗi gói (`error_rate`), số lượng socket bị ứ đọng ở trạng thái `TIME_WAIT` và `CLOSE_WAIT` (phát hiện rò rỉ socket/connection leak).
  - Tự động nạp cấu hình dịch vụ từ `config/services_config.json` và bắn cảnh báo tới `AI_AGENT_WEBHOOK_URL`.
- **Ảnh hưởng tới toàn bộ AI Agent**: Đảm bảo phát hiện ngay lập tức khi một thành phần mạng hoặc tiến trình hạ tầng then chốt bị gián đoạn, kích hoạt quy trình điều tra sự cố của Agent mà không phụ thuộc hoàn toàn vào Alertmanager bên ngoài.

