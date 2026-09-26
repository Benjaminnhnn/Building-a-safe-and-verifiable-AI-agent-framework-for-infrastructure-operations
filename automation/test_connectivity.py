#!/usr/bin/env python3
import subprocess
import sys

script = """
import socket
for name, ip, port in [('moodle-app-a', '10.10.11.107', 22), ('moodle-app-b', '10.10.12.48', 22), ('rds-postgres', 'moodle-staging-postgres.cpk2cwssuno7.ap-southeast-1.rds.amazonaws.com', 5432)]:
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((ip, port))
        banner = s.recv(1024) if port == 22 else b'PORT_OPEN'
        print(f"[{name}] {ip}:{port} -> SUCCESS: {banner.strip()}")
    except Exception as e:
        print(f"[{name}] {ip}:{port} -> FAILED: {e}")
    finally:
        s.close()
"""


cmd = [
    "ssh",
    "-i", r"C:\Users\Qtienle\.ssh\aws-hybrid",
    "-o", "StrictHostKeyChecking=no",
    "ec2-user@18.143.246.124",
    "python3"
]
print("Running socket connectivity probes from monitor node...")
res = subprocess.run(cmd, input=script, capture_output=True, text=True)
print("STDOUT:\n" + res.stdout)
if res.stderr:
    print("STDERR:\n" + res.stderr)
print("RETURNCODE:", res.returncode)

