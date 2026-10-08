import os
import time
import requests
import json
import socket
from datetime import datetime, timezone

# --- CẤU HÌNH ---
# Danh sách các file log cần giám sát.
# Có thể override bằng LOG_FILES="/path/a,/path/b" trong Docker Compose.
DEFAULT_LOG_FILES = [
    "/var/log/syslog",
    "/var/log/messages",
    "/var/log/cloud-init.log",
    "/var/log/dnf.log",
    "/var/log/nginx/error.log",
    "/var/log/apache2/error.log",
    "/var/log/mysql/error.log",
    "/tmp/test_syslog.log"  # For testing
]
LOG_FILES = [
    path.strip()
    for path in os.getenv("LOG_FILES", ",".join(DEFAULT_LOG_FILES)).split(",")
    if path.strip()
]

# URL của AI Agent Webhook (FastAPI server bạn đã chạy)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://localhost:8000/webhook")

# Từ khóa để nhận diện lỗi
ERROR_KEYWORDS = ["ERROR", "CRITICAL", "FATAL", "EXCEPTION", "FAILED", "PANIC"]
WEBHOOK_MAX_ATTEMPTS = max(int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "3")), 1)
WEBHOOK_RETRY_BASE_SECONDS = float(os.getenv("WEBHOOK_RETRY_BASE_SECONDS", "1"))

# Lấy tên của Server hiện tại
HOSTNAME = socket.gethostname()

def send_alert_to_ai_agent(log_line, file_path):
    """Gửi nội dung log lỗi sang AI Agent dưới định dạng Alertmanager Payload"""
    
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    payload = {
        "receiver": "log-watcher",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "LogFileErrorDetected",
                    "severity": "critical",
                    "instance": HOSTNAME,
                    "service": "log-monitoring",
                    "source_file": file_path
                },
                "annotations": {
                    "summary": f"🚨 Phát hiện lỗi trong file log: {file_path}",
                    "description": log_line.strip()
                },
                "startsAt": timestamp,
                "generatorURL": f"http://{HOSTNAME}/log-watcher"
            }
        ],
        "groupLabels": {"alertname": "LogFileErrorDetected"},
        "commonLabels": {"instance": HOSTNAME},
        "commonAnnotations": {},
        "externalURL": "",
        "version": "4",
        "groupKey": f"log-alert-{HOSTNAME}"
    }

    for attempt in range(1, WEBHOOK_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=5)
            if response.status_code == 200:
                print(f"✅ [{datetime.now()}] Đã gửi báo cáo lỗi sang AI Agent.")
                return
            if response.status_code < 500:
                print(f"❌ [{datetime.now()}] Webhook từ chối alert: HTTP {response.status_code}")
                return
            print(f"⚠️ [{datetime.now()}] Webhook tạm lỗi: HTTP {response.status_code}")
        except requests.RequestException as e:
            print(f"⚠️ [{datetime.now()}] Lỗi khi kết nối tới AI Agent Webhook: {e}")

        if attempt < WEBHOOK_MAX_ATTEMPTS:
            time.sleep(WEBHOOK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

def watch_logs():
    """Vòng lặp giám sát log thời gian thực"""
    print(f"🚀 AI Ops Log Watcher đang khởi động trên {HOSTNAME}...")
    print(f"🔍 Đang giám sát: {', '.join(LOG_FILES)}")

    # Mở và di chuyển tới cuối file log hiện tại
    file_handles = {}
    for path in LOG_FILES:
        try:
            if os.path.exists(path):
                f = open(path, "r")
                f.seek(0, os.SEEK_END)
                file_handles[path] = f
                print(f"✅ Đã kết nối tới: {path}")
            else:
                print(f"⚠️ Cảnh báo: File không tồn tại: {path}")
        except Exception as e:
            print(f"❌ Không thể truy cập {path}: {e}")

    if not file_handles:
        print("🛑 Lỗi: Không có file log nào hợp lệ để giám sát. Vui lòng kiểm tra quyền sudo/root.")
        return

    try:
        while True:
            for path, f in file_handles.items():
                line = f.readline()
                if not line:
                    continue
                
                # Kiểm tra từ khóa lỗi
                if any(keyword.upper() in line.upper() for keyword in ERROR_KEYWORDS):
                    print(f"🚨 Phát hiện lỗi mới: {line.strip()[:100]}...")
                    send_alert_to_ai_agent(line, path)
            
            # Nghỉ 1 giây để tiết kiệm CPU
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n🛑 Đang dừng Log Watcher...")
        for f in file_handles.values():
            f.close()

if __name__ == "__main__":
    # Lưu ý: Chạy script này với quyền sudo nếu cần đọc các file log hệ thống
    watch_logs()
