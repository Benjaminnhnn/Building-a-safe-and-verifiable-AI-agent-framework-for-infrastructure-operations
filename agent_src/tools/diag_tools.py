# (Toolbox) Các hàm mà AI có thể tự gọi để thu thập thông tin hệ thống, chẩn đoán sự cố mạng, và đề xuất hành động sửa lỗi. 
# Đây là những công cụ mà AI sẽ sử dụng trong quá trình phân tích và xử lý sự cố.
import os
import psutil
import subprocess
import json
import logging
import socket
import re

logger = logging.getLogger(__name__)

# Pattern validation for security
IP_HOSTNAME_PATTERN = r"^[a-zA-Z0-9.-]+$"

def is_valid_host(host: str) -> bool:
    """Kiểm tra host có phải là IP hoặc tên miền hợp lệ không (chặn command injection)."""
    return bool(re.match(IP_HOSTNAME_PATTERN, host))

# System tools for AI agent chẩn đoán sự cố mạng, thu thập thông tin hệ thống, và đề xuất hành động sửa lỗi. AI có thể gọi các hàm này khi cần thiết trong quá trình phân tích và xử lý sự cố.
def get_system_metrics():
    """
    Thu thập metric hệ thống thời gian thực (CPU, RAM, Disk).
    Được AI gọi khi cần chẩn đoán tài nguyên.
    """
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        metrics = {
            "cpu_percent": cpu_usage,
            "memory_percent": memory.percent,
            "memory_available_mb": memory.available / (1024 * 1024),
            "disk_percent": disk.percent,
            "disk_free_gb": disk.free / (1024 * 1024 * 1024)
        }
        return json.dumps(metrics)
    except Exception as e:
        return f"Lỗi thu thập metric: {str(e)}"

def list_running_services():
    """
    Liệt kê các dịch vụ đang chạy trên hệ thống.
    """
    try:
        # Trong demo container, liệt kê các process quan trọng
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'status']):
            processes.append(proc.info)
        return json.dumps(processes[:20]) # Trả về 20 process đầu tiên để tránh quá tải token
    except Exception as e:
        return f"Lỗi liệt kê process: {str(e)}"

def read_service_logs(service_name: str, lines: int = 20):
    """
    Đọc các dòng cuối của file log dịch vụ.
    """
    log_paths = {
        "nginx": "/var/log/nginx/error.log",
        "app": "/app/logs/agent.log",
        "syslog": "/var/log/syslog"
    }
    
    # Validation: Chỉ cho phép đọc các dịch vụ trong whitelist
    service_name = service_name.lower()
    if service_name not in log_paths:
        return f"Từ chối truy cập: Dịch vụ '{service_name}' không nằm trong danh sách được phép."
    
    # Validation: Giới hạn số dòng đọc tối đa
    lines = min(max(int(lines), 1), 100)
    
    path = log_paths.get(service_name)
    if not path or not os.path.exists(path):
        return f"Không tìm thấy log cho dịch vụ {service_name} tại {path}"
    
    try:
        # Sử dụng tham số dưới dạng list để tránh shell injection
        result = subprocess.run(["tail", "-n", str(lines), path], capture_output=True, text=True, timeout=5)
        return result.stdout or result.stderr
    except Exception as e:
        return f"Lỗi đọc log: {str(e)}"

def propose_remediation(action: str, reason: str, host: str):
    """
    AI gọi hàm này khi muốn đề xuất một hành động sửa lỗi cần phê duyệt.
    """
    # Validation
    if not is_valid_host(host):
        return f"Lỗi: Host '{host}' không hợp lệ."

    proposal = {
        "type": "REMEDIATION_PROPOSAL",
        "action": action,
        "reason": reason,
        "host": host
    }
    return json.dumps(proposal)

# Network diagnostic tools

def get_network_metrics():
    """
    Đo network I/O, packet errors, drops, và TCP connection states.
    Gọi khi cần chẩn đoán vấn đề mạng: bandwidth bất thường, 
    packet loss, hoặc quá nhiều connection treo.
    """
    try:
        net   = psutil.net_io_counters()
        conns = psutil.net_connections()

        # Đếm TCP states
        tcp_states = {}
        for c in conns:
            state = c.status if c.status else "NONE"
            tcp_states[state] = tcp_states.get(state, 0) + 1

        return json.dumps({
            # Throughput
            "bytes_sent_mb":      round(net.bytes_sent / 1e6, 2),
            "bytes_recv_mb":      round(net.bytes_recv / 1e6, 2),
            "packets_sent":       net.packets_sent,
            "packets_recv":       net.packets_recv,
            # Errors & drops
            "errin":              net.errin,
            "errout":             net.errout,
            "dropin":             net.dropin,
            "dropout":            net.dropout,
            # TCP states (các state quan trọng)
            "tcp_established":    tcp_states.get("ESTABLISHED", 0),
            "tcp_time_wait":      tcp_states.get("TIME_WAIT", 0),
            "tcp_close_wait":     tcp_states.get("CLOSE_WAIT", 0),
            "tcp_listen":         tcp_states.get("LISTEN", 0),
            "total_connections":  len(conns)
        })
    except psutil.AccessDenied:
        # Thử lại không có net_connections nếu thiếu quyền
        try:
            net = psutil.net_io_counters()
            return json.dumps({
                "bytes_sent_mb": round(net.bytes_sent / 1e6, 2),
                "bytes_recv_mb": round(net.bytes_recv / 1e6, 2),
                "packets_sent":  net.packets_sent,
                "packets_recv":  net.packets_recv,
                "errin":         net.errin,
                "errout":        net.errout,
                "dropin":        net.dropin,
                "dropout":       net.dropout,
                "note":          "TCP states không khả dụng (cần quyền root)"
            })
        except Exception as e:
            return f"Lỗi đọc network metrics: {str(e)}"
    except Exception as e:
        return f"Lỗi đọc network metrics: {str(e)}"


def ping_host(host: str, count: int = 4):
    """
    Đo RTT latency đến một host bằng ping.
    Gọi khi nghi ngờ latency cao hoặc packet loss đến một đích cụ thể.
    host: địa chỉ IP hoặc hostname cần kiểm tra (vd: '8.8.8.8', 'google.com')
    count: số lần ping (mặc định 4)
    """
    if not is_valid_host(host):
        return f"Lỗi: Host '{host}' không hợp lệ (không được chứa ký tự đặc biệt)."
    
    # Validation: Giới hạn số lượng ping
    count = min(max(int(count), 1), 10)

    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=15
        )
        output = result.stdout or result.stderr

        # Parse RTT từ output nếu có
        # Dòng cuối thường là: rtt min/avg/max/mdev = 1.2/1.5/1.8/0.1 ms
        rtt_line = [l for l in output.splitlines() if "rtt" in l or "round-trip" in l]
        parsed_rtt = rtt_line[0] if rtt_line else "Không parse được RTT"

        return json.dumps({
            "host":       host,
            "returncode": result.returncode,
            "reachable":  result.returncode == 0,
            "rtt_summary": parsed_rtt,
            "raw_output": output[-800:]   # Giới hạn 800 ký tự để tiết kiệm token
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"host": host, "reachable": False, "error": "Ping timeout sau 15 giây"})
    except FileNotFoundError:
        return json.dumps({"host": host, "error": "Lệnh 'ping' không khả dụng trong container"})
    except Exception as e:
        return f"Lỗi ping: {str(e)}"


def check_dns_resolution(hostname: str):
    """
    Kiểm tra DNS có phân giải được hostname thành IP không.
    Gọi khi service báo 'connection refused' hoặc 'host not found'
    để phân biệt lỗi DNS vs lỗi service thực sự.
    hostname: tên miền cần kiểm tra (vd: 'google.com', 'db.internal')
    """
    if not is_valid_host(hostname):
        return f"Lỗi: Hostname '{hostname}' không hợp lệ."

    try:
        ip = socket.gethostbyname(hostname)
        # Thử resolve thêm để lấy tất cả IPs
        all_ips = socket.getaddrinfo(hostname, None)
        unique_ips = list({info[4][0] for info in all_ips})

        return json.dumps({
            "hostname":   hostname,
            "status":     "ok",
            "primary_ip": ip,
            "all_ips":    unique_ips[:5]   # Tối đa 5 IPs
        })
    except socket.gaierror as e:
        return json.dumps({
            "hostname": hostname,
            "status":   "failed",
            "error":    str(e),
            "hint":     "DNS không phân giải được. Kiểm tra /etc/resolv.conf hoặc DNS server."
        })
    except Exception as e:
        return json.dumps({"hostname": hostname, "status": "error", "error": str(e)})


def check_http_service(url: str = "http://localhost"):
    """
    Kiểm tra trạng thái HTTP của một URL.
    Gọi khi Nginx báo lỗi để xác định mã lỗi (200, 404, 502, 504).
    """
    try:
        import requests
        response = requests.get(url, timeout=5)
        return json.dumps({
            "url": url,
            "status_code": response.status_code,
            "reason": response.reason,
            "is_ok": response.ok
        })
    except Exception as e:
        return json.dumps({"url": url, "error": str(e), "status": "unreachable"})

def check_db_connection():
    """
    Kiểm tra khả năng kết nối đến PostgreSQL cục bộ.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(
            dbname="aiops_db", 
            user="postgres", 
            password="postgres", 
            host="localhost",
            connect_timeout=3
        )
        conn.close()
        return json.dumps({"status": "connected", "database": "postgresql"})
    except Exception as e:
        return json.dumps({"status": "failed", "error": str(e)})

def check_redis_ping():
    """
    Kiểm tra phản hồi PING/PONG từ Redis.
    """
    try:
        import redis
        r = redis.Redis(host='localhost', port=6379, socket_timeout=2)
        return json.dumps({"status": "ok", "ping": r.ping()})
    except Exception as e:
        return json.dumps({"status": "failed", "error": str(e)})

AGENT_TOOLS = [
    get_system_metrics,
    list_running_services,
    get_network_metrics,
]