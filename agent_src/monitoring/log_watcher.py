# Giám sát Log thời gian thực bằng Watchdog và gửi Alert về AI Agent.
import os
import time
import requests
import json
import socket
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- CẤU HÌNH ---
ENV_LOG_FILES = os.getenv("WATCH_LOG_FILES")
if ENV_LOG_FILES:
    LOG_FILES = [f.strip() for f in ENV_LOG_FILES.split(",")]
else:
    LOG_FILES = ["/var/log/syslog", "/var/log/nginx/error.log", "/tmp/test_syslog.log"]

WEBHOOK_URL = os.getenv("AI_AGENT_WEBHOOK_URL", "http://localhost:8000/webhook")
ERROR_KEYWORDS = ["ERROR", "CRITICAL", "FATAL", "EXCEPTION", "FAILED", "PANIC"]
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "60"))
WEBHOOK_MAX_ATTEMPTS = max(int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "3")), 1)
WEBHOOK_RETRY_BASE_SECONDS = float(os.getenv("WEBHOOK_RETRY_BASE_SECONDS", "1"))
HOSTNAME = socket.gethostname()

last_alerts = {}

def send_alert_to_ai_agent(log_line, file_path):
    now = time.time()
    if file_path in last_alerts and now - last_alerts[file_path] < ALERT_COOLDOWN:
        return

    timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    payload = {
        "receiver": "log-watcher",
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": "LogFileErrorDetected",
                "severity": "critical",
                "instance": HOSTNAME,
                "service": "log-monitoring",
                "source_file": file_path
            },
            "annotations": {
                "summary": f"Phát hiện lỗi trong file log: {file_path}",
                "description": log_line.strip()
            },
            "startsAt": timestamp,
            "generatorURL": f"http://{HOSTNAME}/log-watcher"
        }]
    }

    for attempt in range(1, WEBHOOK_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=5)
            if response.status_code == 200:
                last_alerts[file_path] = now
                print(f"[{datetime.now()}] ✅ Alert sent for {file_path}")
                return
            if response.status_code < 500:
                print(f"❌ [{datetime.now()}] Webhook rejected alert: HTTP {response.status_code}")
                return
            print(f"⚠️ [{datetime.now()}] Webhook temporary error: HTTP {response.status_code}")
        except requests.RequestException as e:
            print(f"⚠️ [{datetime.now()}] Webhook error: {e}")

        if attempt < WEBHOOK_MAX_ATTEMPTS:
            time.sleep(WEBHOOK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

class LogFileHandler(FileSystemEventHandler):
    def __init__(self, file_path):
        self.file_path = file_path
        self.file_handle = open(file_path, "r")
        try:
            self.file_inode = os.stat(file_path).st_ino
        except OSError:
            self.file_inode = 0
        self.file_handle.seek(0, os.SEEK_END)

    def on_modified(self, event):
        if event.src_path == self.file_path:
            # Handle log rotation or truncation
            try:
                stat = os.stat(self.file_path)
                if stat.st_ino != self.file_inode or stat.st_size < self.file_handle.tell():
                    print(f"🔄 Log file {self.file_path} rotated or truncated. Reopening handle.")
                    try:
                        self.file_handle.close()
                    except Exception:
                        pass
                    self.file_handle = open(self.file_path, "r")
                    self.file_inode = stat.st_ino
                    self.file_handle.seek(0)
            except OSError:
                return

            lines = self.file_handle.readlines()
            for line in lines:
                if any(k.upper() in line.upper() for k in ERROR_KEYWORDS):
                    print(f"🔥 Error detected in {self.file_path}: {line.strip()[:100]}...")
                    send_alert_to_ai_agent(line, self.file_path)

def watch_logs():
    print(f"🚀 Log Watcher (Watchdog-based) starting on {HOSTNAME}...")
    observer = Observer()
    handlers = []

    for path in LOG_FILES:
        if os.path.exists(path):
            folder = os.path.dirname(path)
            handler = LogFileHandler(path)
            observer.schedule(handler, folder, recursive=False)
            handlers.append(handler)
            print(f"👁️ Monitoring: {path}")
        else:
            print(f"⚠️ Warning: File not found: {path}")

    if not handlers:
        print("❌ No valid log files to monitor. Exiting.")
        return

    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    watch_logs()
