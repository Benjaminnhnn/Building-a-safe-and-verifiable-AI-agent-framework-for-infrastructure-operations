# Historical AIOps Master Runbook (Moodle AWS)

> **Status reviewed 2026-10-05:** This document is a historical procedure collection, not a verified end-to-end reproduction guide. The planned Manual/Ansible/AI matrix remains 0/225 empirical cells; synthetic and shadow records are excluded. Use the current Moodle operations and AI Engineer recovery runbooks, and verify every command against the current script/config before running it.
>
> The 3 methods x 15 scenarios x 3 repetitions are a target design, not completed results. Infrastructure provisioning, live fault injection, and teardown require separate review and are outside AI-only code work.

---

## Contents

- [Provisioning and deployment](#1-provision-aws-infrastructure-terraform)
- [Offline replay and scenario procedures](#5-offline-replay-không-cần-live-infra)
- [Empirical benchmark status](#7-empirical-benchmark-pending)
- [Ablation status](#8-ablation-studies-empirical-execution-pending)
- [Result validation](#9-collect-and-validate-results)
- [Teardown and troubleshooting](#11-teardown)


## 0. Tổng quan và Prerequisites

### 0.1 Sơ đồ kiến trúc

```
                          ┌─────────────────────────────────────────────────────────┐
                          │                     AWS VPC (10.0.0.0/16)               │
                          │                                                          │
   Internet ──── HTTPS ──►│  ┌──────────────────────────────────────────────────┐   │
                          │  │  Application Load Balancer (ALB)                  │   │
                          │  │  Listener: 443/HTTPS → Target Group (port 8080)   │   │
                          │  └──────────────┬───────────────────────────────────┘   │
                          │                 │                                        │
                          │     ┌───────────┴────────────┐                          │
                          │     │   Public Subnet         │                          │
                          │     │  ┌──────────────────┐  │                          │
                          │     │  │  monitor-ai-01   │  │                          │
                          │     │  │  (t3.small)       │  │                          │
                          │     │  │  - Prometheus     │  │                          │
                          │     │  │  - Alertmanager   │  │                          │
                          │     │  │  - Grafana        │  │                          │
                          │     │  │  - AI Agent       │  │                          │
                          │     │  │    (FastAPI +      │  │                          │
                          │     │  │     Celery+Redis)  │  │                          │
                          │     │  └──────────────────┘  │                          │
                          │     └───────────┬────────────┘                          │
                          │                 │                                        │
                          │     ┌───────────┴────────────────────────────┐          │
                          │     │  Private Subnet                         │          │
                          │     │  ┌────────────────┐ ┌────────────────┐ │          │
                          │     │  │  moodle-app-a  │ │  moodle-app-b  │ │          │
                          │     │  │  (t3.small)    │ │  (t3.small)    │ │          │
                          │     │  │  - Moodle      │ │  - Moodle      │ │          │
                          │     │  │    (Docker)    │ │    (Docker)    │ │          │
                          │     │  │  - nginx proxy │ │  - nginx proxy │ │          │
                          │     │  └──────┬─────────┘ └──────┬─────────┘ │          │
                          │     │         │                   │           │          │
                          │     │         └────────┬──────────┘           │          │
                          │     │                  │                       │          │
                          │     │    ┌─────────────▼──────┐               │          │
                          │     │    │  Amazon EFS         │               │          │
                          │     │    │  (moodledata share) │               │          │
                          │     │    └────────────────────┘               │          │
                          │     │                  │                       │          │
                          │     │    ┌─────────────▼──────┐               │          │
                          │     │    │  Amazon RDS          │               │          │
                          │     │    │  PostgreSQL 15       │               │          │
                          │     │    │  (t4g.micro, Multi- │               │          │
                          │     │    │   AZ not needed for  │               │          │
                          │     │    │   staging)           │               │          │
                          │     │    └────────────────────┘               │          │
                          │     └────────────────────────────────────────┘          │
                          └─────────────────────────────────────────────────────────┘

Luồng dữ liệu:
  Browser → ALB → moodle-app-[a|b] → RDS PostgreSQL
                                    → EFS (moodledata)
  Prometheus ← scrape ← moodle-app-[a|b] (node_exporter + custom metrics)
  Alertmanager → webhook → AI Agent (FastAPI) → Celery task queue
  AI Agent: Observer → Diagnosis → Planner → Safety Gate → Execution → Verifier
  AI Agent → Telegram Bot (notification)
```

**Nodes tóm tắt:**

| Node | Role | Instance | IP (ví dụ) |
|------|------|----------|------------|
| `monitor-ai-01` | Monitoring + AI Agent | t3.small | 10.0.1.10 |
| `moodle-app-a` | Moodle primary | t3.small | 10.0.2.10 |
| `moodle-app-b` | Moodle replica/standby | t3.small | 10.0.2.11 |
| RDS | PostgreSQL 15 | t4g.micro | (endpoint từ Terraform output) |
| EFS | Shared moodledata | — | (mount target per AZ) |
| ALB | Load balancer | — | (DNS từ Terraform output) |

---

### 0.2 Tools cần cài

> [!IMPORTANT]
> Kiểm tra **từng công cụ** trước khi bắt đầu. Phiên bản không đúng có thể gây lỗi khó debug.

#### Checklist cài đặt

- ☐ **Terraform** ≥ 1.5.0
- ☐ **Ansible** ≥ 2.14.0
- ☐ **Docker** ≥ 24.0 + Docker Compose v2
- ☐ **Python** 3.11.x
- ☐ **AWS CLI** v2.x
- ☐ **jq** ≥ 1.6
- ☐ **git** ≥ 2.40
- ☐ **ssh-agent** (OpenSSH)

#### Kiểm tra phiên bản

```bash
terraform version
# Expected: Terraform v1.5.x hoặc cao hơn

ansible --version
# Expected: ansible [core 2.14.x]

docker --version && docker compose version
# Expected: Docker version 24.x.x, Docker Compose version v2.x.x

python3 --version
# Expected: Python 3.11.x

aws --version
# Expected: aws-cli/2.x.x Python/3.x.x

jq --version
# Expected: jq-1.6 hoặc cao hơn
```

#### Cài đặt nhanh (Ubuntu/Debian)

```bash
# Terraform
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform

# Ansible
sudo apt update && sudo apt install -y python3-pip
pip3 install ansible==2.14.*

# AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip && sudo ./aws/install

# jq
sudo apt install -y jq

# Python deps
pip3 install -r requirements.txt
```

---

### 0.3 AWS Credentials

> [!WARNING]
> **Không bao giờ** commit AWS credentials vào git. Sử dụng `~/.aws/credentials` hoặc environment variables.

- ☐ Tạo IAM user với quyền: `AmazonEC2FullAccess`, `AmazonRDSFullAccess`, `AmazonEFSFullAccess`, `ElasticLoadBalancingFullAccess`, `AmazonVPCFullAccess`
- ☐ Tạo Access Key → lưu vào `~/.aws/credentials`

```bash
aws configure
# AWS Access Key ID [None]: AKIA...
# AWS Secret Access Key [None]: xxxxxx
# Default region name [None]: ap-southeast-1
# Default output format [None]: json
```

**Kiểm tra credentials:**

```bash
aws sts get-caller-identity
```

**Expected output:**
```json
{
    "UserId": "AIDA...",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/aiops-deploy"
}
```

---

### 0.4 SSH Key Setup

- ☐ Tạo SSH key pair (nếu chưa có):

```bash
ssh-keygen -t ed25519 -C "aiops-staging" -f ~/.ssh/aiops_staging_key
```

- ☐ Import public key vào AWS (hoặc để Terraform tạo key pair):

```bash
aws ec2 import-key-pair \
  --key-name "aiops-staging" \
  --public-key-material fileb://~/.ssh/aiops_staging_key.pub \
  --region ap-southeast-1
```

- ☐ Thêm key vào ssh-agent:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/aiops_staging_key
```

- ☐ Cập nhật `terraform/deployment.tfvars`:

```hcl
key_name = "aiops-staging"
```

---

### 0.5 Ước tính chi phí

> [!NOTE]
> Chi phí ước tính cho môi trường **staging** tại region `ap-southeast-1` (Singapore).

| Resource | Type | Giá/giờ (USD) | Giá/ngày (USD) |
|----------|------|--------------|----------------|
| EC2 monitor-ai-01 | t3.small | ~$0.023 | ~$0.55 |
| EC2 moodle-app-a | t3.small | ~$0.023 | ~$0.55 |
| EC2 moodle-app-b | t3.small | ~$0.023 | ~$0.55 |
| RDS PostgreSQL | t4g.micro | ~$0.016 | ~$0.38 |
| EFS | Standard | ~$0.043/GB-month | ~$0.10 (5GB) |
| ALB | — | ~$0.008/LCU-h | ~$0.50 |
| Data transfer | — | — | ~$0.20 |
| **Tổng** | | | **~$2.83–$5/ngày** |

> [!CAUTION]
> **Luôn chạy `terraform destroy` sau khi hoàn thành thực nghiệm** để tránh phát sinh chi phí không cần thiết. Để quên EC2+RDS chạy 1 tuần có thể tốn ~$35+.

---

## 1. Provision AWS Infrastructure (Terraform)

> [!IMPORTANT]
> Thực hiện từ **thư mục gốc của project**. Đảm bảo AWS credentials đã được cấu hình (xem [0.3](#03-aws-credentials)).

### Bước 1.1 — Khởi tạo Terraform

- ☐ Khởi tạo backend và download providers:

```bash
terraform -chdir=terraform init
```

**Expected output:**
```
Initializing the backend...
Initializing provider plugins...
- Finding hashicorp/aws versions matching "~> 5.0"...
- Installing hashicorp/aws v5.x.x...
Terraform has been successfully initialized!
```

### Bước 1.2 — Tạo Terraform plan

- ☐ Kiểm tra `terraform/deployment.tfvars` đã có đủ biến:
  - `key_name`, `region`, `db_password`, `allowed_ssh_cidr`

- ☐ Tạo plan:

```bash
terraform -chdir=terraform plan \
  -var-file=deployment.tfvars \
  -out=staging.tfplan
```

**Expected output (phần cuối):**
```
Plan: 32 to add, 0 to change, 0 to destroy.
─────────────────────────────────────────────────────────────────────────────
Saved the plan to: staging.tfplan
```

- ☐ **Review plan** — xác nhận các resource sau có mặt:

| Resource Type | Tên dự kiến | Ghi chú |
|---------------|-------------|---------|
| `aws_vpc` | `aiops-staging-vpc` | CIDR 10.0.0.0/16 |
| `aws_subnet` | public + private | ít nhất 2 AZ |
| `aws_instance` (×3) | monitor-ai-01, moodle-app-a, moodle-app-b | t3.small |
| `aws_db_instance` | `aiops-staging-rds` | PostgreSQL 15, t4g.micro |
| `aws_efs_file_system` | `aiops-moodledata` | — |
| `aws_lb` | `aiops-staging-alb` | Application LB |
| `aws_security_group` (×nhiều) | — | Kiểm tra inbound/outbound rules |

> [!WARNING]
> Nếu plan hiển thị > 50 resource hoặc có `destroy`, dừng lại và kiểm tra tfvars. Không apply plan có resource destroy không mong muốn.

### Bước 1.3 — Apply infrastructure

- ☐ Apply plan đã review:

```bash
terraform -chdir=terraform apply staging.tfplan
```

> Quá trình này mất **5–10 phút**. RDS provisioning thường mất lâu nhất (~5 phút).

**Expected output (phần cuối):**
```
Apply complete! Resources: 32 added, 0 changed, 0 destroyed.

Outputs:
alb_dns_name = "aiops-staging-alb-xxxxxxxxxx.ap-southeast-1.elb.amazonaws.com"
monitor_ai_01_public_ip = "54.x.x.x"
moodle_app_a_private_ip = "10.0.2.10"
moodle_app_b_private_ip = "10.0.2.11"
rds_endpoint = "aiops-staging-rds.xxxxxxxxxx.ap-southeast-1.rds.amazonaws.com"
efs_id = "fs-xxxxxxxxxx"
```

### Bước 1.4 — Lưu outputs

- ☐ Đọc và lưu outputs vào artifact directory:

```bash
mkdir -p terraform/.artifacts
terraform -chdir=terraform output -json > terraform/.artifacts/outputs.json
```

- ☐ Trích xuất các giá trị quan trọng:

```bash
export ALB_URL=$(jq -r '.alb_dns_name.value' terraform/.artifacts/outputs.json)
export MONITOR_IP=$(jq -r '.monitor_ai_01_public_ip.value' terraform/.artifacts/outputs.json)
export RDS_ENDPOINT=$(jq -r '.rds_endpoint.value' terraform/.artifacts/outputs.json)
export EFS_ID=$(jq -r '.efs_id.value' terraform/.artifacts/outputs.json)

echo "ALB: $ALB_URL"
echo "Monitor: $MONITOR_IP"
echo "RDS: $RDS_ENDPOINT"
echo "EFS: $EFS_ID"

# Lưu vào file env cho các bước sau
cat > terraform/.artifacts/staging.env << EOF
ALB_URL=$ALB_URL
MONITOR_IP=$MONITOR_IP
RDS_ENDPOINT=$RDS_ENDPOINT
EFS_ID=$EFS_ID
EOF
```

### Bước 1.5 — Acceptance check

- ☐ Kiểm tra ALB healthy:

```bash
aws elbv2 describe-target-health \
  --target-group-arn $(aws elbv2 describe-target-groups \
    --query 'TargetGroups[0].TargetGroupArn' \
    --output text) \
  --region ap-southeast-1
```

**Expected output:**
```json
{
    "TargetHealthDescriptions": [
        {"TargetHealth": {"State": "healthy"}},
        {"TargetHealth": {"State": "healthy"}}
    ]
}
```

- ☐ Kiểm tra SSH đến monitor node:

```bash
ssh -i ~/.ssh/aiops_staging_key ubuntu@$MONITOR_IP "echo 'SSH OK'"
```

**Expected output:**
```
SSH OK
```

---

## 2. Configure Hosts (Ansible)

> [!NOTE]
> Ansible cần SSH access đến tất cả nodes. Đảm bảo `ansible/inventory.ini` đã có IPs từ Terraform output.

### Bước 2.0 — Cập nhật inventory

- ☐ Cập nhật `ansible/inventory.ini` với IPs thực:

```bash
source terraform/.artifacts/staging.env

# Tự động update inventory (hoặc chỉnh sửa thủ công)
sed -i "s/MONITOR_IP_PLACEHOLDER/$MONITOR_IP/g" ansible/inventory.ini
```

**Cấu trúc `ansible/inventory.ini` mẫu:**
```ini
[monitor]
monitor-ai-01 ansible_host=54.x.x.x ansible_user=ubuntu ansible_ssh_private_key_file=~/.ssh/aiops_staging_key

[moodle_app]
moodle-app-a ansible_host=10.0.2.10 ansible_user=ubuntu ansible_ssh_private_key_file=~/.ssh/aiops_staging_key
moodle-app-b ansible_host=10.0.2.11 ansible_user=ubuntu ansible_ssh_private_key_file=~/.ssh/aiops_staging_key

[all:vars]
ansible_python_interpreter=/usr/bin/python3
```

- ☐ Test connectivity:

```bash
ansible -i ansible/inventory.ini all -m ping
```

**Expected output:**
```
monitor-ai-01 | SUCCESS => {"ping": "pong"}
moodle-app-a  | SUCCESS => {"ping": "pong"}
moodle-app-b  | SUCCESS => {"ping": "pong"}
```

### Bước 2.1 — Bootstrap hosts

- ☐ Cài đặt dependencies cơ bản (Docker, node_exporter, v.v.):

```bash
ansible-playbook \
  -i ansible/inventory.ini \
  ansible/playbooks/bootstrap.yml \
  --diff
```

**Expected output (tóm tắt cuối):**
```
PLAY RECAP *********************************************************************
monitor-ai-01  : ok=18  changed=12  unreachable=0  failed=0
moodle-app-a   : ok=15  changed=10  unreachable=0  failed=0
moodle-app-b   : ok=15  changed=10  unreachable=0  failed=0
```

### Bước 2.2 — Configure Moodle monitoring stack

- ☐ Deploy Prometheus, Alertmanager, Grafana, và cấu hình AI Agent webhook:

```bash
ansible-playbook \
  -i ansible/inventory.ini \
  ansible/playbooks/configure-moodle-monitoring.yml \
  --diff
```

**Expected output (tóm tắt cuối):**
```
PLAY RECAP *********************************************************************
monitor-ai-01  : ok=32  changed=20  unreachable=0  failed=0
moodle-app-a   : ok=8   changed=5   unreachable=0  failed=0
moodle-app-b   : ok=8   changed=5   unreachable=0  failed=0
```

### Bước 2.3 — Prepare Moodle runtime

- ☐ Chuẩn bị môi trường runtime cho Moodle (EFS mount, DB init, v.v.):

```bash
bash automation/prepare-moodle-runtime.sh
```

**Expected output:**
```
[INFO] Mounting EFS fs-xxxxxxxxxx on moodle-app-a...OK
[INFO] Mounting EFS fs-xxxxxxxxxx on moodle-app-b...OK
[INFO] Initializing Moodle database schema...OK
[INFO] Setting moodledata permissions...OK
[INFO] Runtime preparation complete.
```

### Bước 2.4 — Acceptance checks

- ☐ Prometheus scraping tất cả targets:

```bash
curl -s http://$MONITOR_IP:9090/api/v1/targets | \
  jq '[.data.activeTargets[] | {job: .labels.job, health: .health}]'
```

**Expected output:**
```json
[
  {"job": "moodle-app-a", "health": "up"},
  {"job": "moodle-app-b", "health": "up"},
  {"job": "node-monitor", "health": "up"},
  {"job": "postgres", "health": "up"}
]
```

- ☐ Grafana accessible (port 3000):

```bash
curl -s -o /dev/null -w "%{http_code}" http://$MONITOR_IP:3000/api/health
```

**Expected output:** `200`

- ☐ AI Agent webhook responds:

```bash
curl -s http://$MONITOR_IP:8000/health | jq .
```

**Expected output:**
```json
{"status": "ok", "celery": "running", "redis": "connected"}
```

---

## 3. Deploy Moodle

### Bước 3.1 — Stage Moodle release

- ☐ Chọn image tag cần deploy (xem GHCR releases):

```bash
export MOODLE_TAG="v1.0.0"  # thay bằng tag thực tế
bash automation/stage-moodle-release.sh $MOODLE_TAG
```

**Expected output:**
```
[INFO] Pulling moodle image ghcr.io/org/moodle:v1.0.0...
[INFO] Image pulled on moodle-app-a...OK
[INFO] Image pulled on moodle-app-b...OK
[INFO] Staging complete for tag v1.0.0
```

### Bước 3.2 — Deploy monitor/AI Agent stack

- ☐ Deploy AI Agent stack với health check và auto-rollback:

```bash
bash automation/app-release-deploy.sh staging $MOODLE_TAG monitor
```

**Expected output:**
```
[DEPLOY] Deploying monitor stack tag=v1.0.0 env=staging
[DEPLOY] Starting containers...
[DEPLOY] Health check attempt 1/5... PASS
[DEPLOY] Monitor stack deployed successfully.
```

### Bước 3.3 — Deploy Moodle web stack

```bash
bash automation/app-release-deploy.sh staging $MOODLE_TAG web
```

**Expected output:**
```
[DEPLOY] Deploying web stack tag=v1.0.0 env=staging
[DEPLOY] Health check attempt 1/5... PASS
[DEPLOY] Web stack deployed successfully.
```

### Bước 3.4 — Verify Moodle health

- ☐ HTTP health check:

```bash
source terraform/.artifacts/staging.env
curl -s -o /dev/null -w "%{http_code}" https://$ALB_URL/healthz.php
```

**Expected output:** `200`

- ☐ Synthetic transaction test:

```bash
python3 automation/moodle-synthetic-transaction.py \
  --url https://$ALB_URL \
  --username admin \
  --password <admin-password>
```

**Expected output:**
```
[SYNTH] Login...PASS
[SYNTH] Dashboard load...PASS
[SYNTH] Course list...PASS
[SYNTH] All synthetic checks passed (3/3)
```

> [!TIP]
> Nếu `healthz.php` trả về 503, kiểm tra ALB target group health trước. Nếu target unhealthy, kiểm tra nginx và php-fpm trên moodle-app nodes.

---

## 4. Environment Baseline

> [!IMPORTANT]
> **Luôn chạy baseline verify trước mỗi experiment run.** Nếu baseline fail, DỪNG lại và điều tra trước khi inject fault.

### Bước 4.1 — Chạy baseline verify

- ☐ Kiểm tra toàn bộ trạng thái môi trường:

```bash
bash automation/moodle-environment-baseline.sh verify
```

**Expected output:**
```
[BASELINE] Checking Moodle HTTP...           [PASS]
[BASELINE] Checking DB connectivity...       [PASS]
[BASELINE] Checking EFS mount on app-a...    [PASS]
[BASELINE] Checking EFS mount on app-b...    [PASS]
[BASELINE] Checking node_exporter metrics... [PASS]
[BASELINE] Checking Prometheus scrape...     [PASS]
[BASELINE] Checking Alertmanager...          [PASS]
[BASELINE] Checking AI Agent health...       [PASS]
[BASELINE] Checking disk usage (<80%)...     [PASS]
[BASELINE] Checking memory usage (<85%)...   [PASS]
[BASELINE] Checking no active alerts...      [PASS]
─────────────────────────────────────────────────
[BASELINE] ALL 11 CHECKS PASSED ✓
Checksum: sha256:a3f8c2...
Saved to: terraform/.artifacts/baseline-20250101-120000.json
```

### Bước 4.2 — Lưu baseline checksum

- ☐ Checksum artifact được lưu tự động. Kiểm tra:

```bash
ls -la terraform/.artifacts/baseline-*.json
```

- ☐ Lưu baseline timestamp để tham chiếu sau:

```bash
export BASELINE_TS=$(date -u +"%Y%m%d-%H%M%S")
echo "Baseline verified at: $BASELINE_TS" | tee -a terraform/.artifacts/experiment.log
```

### Bước 4.3 — Acceptance criteria

| Check | Threshold | Action nếu fail |
|-------|-----------|----------------|
| Moodle HTTP | HTTP 200 | Restart nginx, php-fpm |
| DB connectivity | <100ms latency | Check RDS SG, check credentials |
| EFS mount | Mounted và writable | Remount EFS |
| Disk usage | <80% | Xóa logs cũ, hoặc expand EBS |
| Memory usage | <85% | Restart apps, check memory leak |
| Active alerts | 0 alerts | Điều tra alerts hiện có |

---

## 5. Offline Replay (không cần live infra)

> [!NOTE]
> Offline replay sử dụng recorded traces để test AI Agent pipeline mà không cần AWS infrastructure. Chạy được trên local machine.

### Bước 5.1 — Chạy 15 scenarios offline

- ☐ Replay toàn bộ 15 scenarios:

```bash
bash automation/run-aiops-e2e.sh replay all
```

**Expected output:**
```
[REPLAY] DB-01  PostgreSQL stopped          PASS (MTTD: 32s, action: restart-postgres)
[REPLAY] DB-02  Connection exhaustion        PASS (MTTD: 28s, action: reset-pool)
[REPLAY] DB-03  Wrong DB endpoint           PASS (MTTD: 45s, action: fix-config)
[REPLAY] RES-01 CPU hog                     PASS (MTTD: 19s, action: kill-process)
[REPLAY] RES-02 Memory pressure/OOM         PASS (MTTD: 22s, action: restart-service)
[REPLAY] RES-03 Disk fill                   PASS (MTTD: 15s, action: cleanup-disk)
[REPLAY] NET-01 DNS alias loss              PASS (MTTD: 38s, action: restore-dns)
[REPLAY] NET-02 Port block Moodle→DB        PASS (MTTD: 41s, action: fix-iptables)
[REPLAY] NET-03 Latency/loss via tc netem   PASS (MTTD: 35s, action: remove-netem)
[REPLAY] CON-01 Moodle stopped              PASS (MTTD: 12s, action: start-moodle)
[REPLAY] CON-02 Reverse proxy stopped       PASS (MTTD: 14s, action: start-nginx)
[REPLAY] CON-03 Crash-loop bad image        PASS (MTTD: 55s, action: rollback-image)
[REPLAY] SEC-01 DB port exposure            PASS (MTTD: 62s, action: close-port)
[REPLAY] SEC-02 Moodledata permission       PASS (MTTD: 48s, action: fix-permissions)
[REPLAY] SEC-03 Security config drift       PASS (MTTD: 71s, action: restore-config)
─────────────────────────────────────────────────────────────────────────────
[REPLAY] 15/15 scenarios PASSED
```

### Bước 5.2 — Chạy unit tests

- ☐ Chạy test suite cho AI Agent:

```bash
PYTHONPATH=agent_src pytest -q agent_src/tests/
```

**Expected output:**
```
..........................................
42 passed, 0 failed, 0 errors in 8.32s
```

> [!TIP]
> Nếu một số test fail do missing fixtures, chạy: `bash automation/prepare-test-fixtures.sh` trước.

---

## 6. Chạy từng Scenario

> The fault/reset scripts currently have reviewed pairs for 14 scenarios; `SEC-01` is HUMAN_ONLY. The AI live-execution allowlist is narrower: `DB-01`, `RES-01`, `NET-01`, `CON-01`, and `SEC-02`. A staging smoke/injector pass outside that set does not authorize AI execution. Always check the current allowlist in `automation/moodle-fault-inject.sh`, `automation/moodle-fault-reset.sh`, and `automation/sprint6-acceptance.py` before a live operation.

### 6.1 Quy trình chung

> [!IMPORTANT]
> Áp dụng quy trình này **cho mỗi scenario**, dù chạy AI Agent, Manual, hay Ansible method.

#### Template quy trình

```
┌─────────────────────────────────────────────────┐
│  PRE-FLIGHT (không bỏ qua bước này)              │
│  1. Baseline verify                              │
│  2. Confirm no active alerts                     │
│  3. Open AI Agent log stream                     │
├─────────────────────────────────────────────────┤
│  INJECT                                          │
│  4. Ghi t_inject = $(date -u +%s)               │
│  5. Chạy moodle-fault-inject.sh <SCENARIO>      │
├─────────────────────────────────────────────────┤
│  OBSERVE                                         │
│  6. Watch AI Agent logs / Grafana alerts         │
│  7. Ghi t_detect khi alert fire                 │
│  8. Ghi t_incident khi incident created         │
│  9. Ghi t_plan khi plan generated               │
│  10. Ghi t_gate khi gate decision               │
│  11. Ghi t_execute_start khi action bắt đầu    │
│  12. Ghi t_execute_end khi action hoàn thành   │
│  13. Ghi t_verify khi verifier chạy            │
│  14. Ghi t_resolved khi service restored        │
├─────────────────────────────────────────────────┤
│  RESET & STABILITY                               │
│  15. Chạy moodle-fault-reset.sh <SCENARIO>      │
│  16. Wait 120 giây stability window              │
│  17. Baseline verify lại                         │
├─────────────────────────────────────────────────┤
│  RECORD                                          │
│  18. Ghi timestamps vào CSV                      │
└─────────────────────────────────────────────────┘
```

#### Bước chi tiết

**Pre-flight:**

- ☐ Verify baseline:

```bash
bash automation/moodle-environment-baseline.sh verify
```

- ☐ Confirm no active alerts:

```bash
curl -s http://$MONITOR_IP:9093/api/v2/alerts | jq 'length'
# Expected: 0
```

- ☐ Mở terminal riêng để stream AI Agent logs:

```bash
ssh ubuntu@$MONITOR_IP "docker logs -f aiops-agent --since=0m"
```

**Inject fault:**

- ☐ Ghi timestamp inject:

```bash
T_INJECT=$(date -u +%s)
echo "t_inject=$T_INJECT"
```

- ☐ Inject fault (thay `<SCENARIO>` bằng ID cụ thể, ví dụ `DB-01`):

```bash
MOODLE_FAULT_CONFIRM=staging bash automation/moodle-fault-inject.sh <SCENARIO>
```

**Observe (quan sát AI Agent log):**

```bash
# Terminal 1: AI Agent logs
ssh ubuntu@$MONITOR_IP "docker logs -f aiops-agent"

# Terminal 2: Alertmanager alerts
watch -n 5 'curl -s http://$MONITOR_IP:9093/api/v2/alerts | jq "[.[] | {name: .labels.alertname, state: .status.state}]"'
```

**Ghi timestamps theo schema:**

```bash
# Ghi vào file CSV (chạy khi từng event xảy ra)
cat >> terraform/.artifacts/timestamps.csv << EOF
<SCENARIO>,<METHOD>,<RUN>,<T_INJECT>,<T_DETECT>,<T_INCIDENT>,<T_PLAN>,<T_GATE>,<T_EXECUTE_START>,<T_EXECUTE_END>,<T_VERIFY>,<T_RESOLVED>
EOF
```

**CSV Header (tạo một lần):**
```bash
echo "scenario,method,run,t_inject,t_detect,t_incident,t_plan,t_gate,t_execute_start,t_execute_end,t_verify,t_resolved" \
  > terraform/.artifacts/timestamps.csv
```

**Reset:**

- ☐ Reset fault về trạng thái bình thường:

```bash
bash automation/moodle-fault-reset.sh <SCENARIO>
```

- ☐ Đợi 120 giây stability window:

```bash
echo "Waiting 120s stability window..."
sleep 120
```

- ☐ Verify baseline sau reset:

```bash
bash automation/moodle-environment-baseline.sh verify
```

**Expected output sau reset:** `ALL 11 CHECKS PASSED ✓`

---

### 6.2 Bảng tham khảo 15 Scenarios

| # | Scenario ID | Mô tả | Inject Command | Expected Alert | Expected Action | Reset Command | Expected MTTD |
|---|-------------|--------|---------------|----------------|-----------------|---------------|---------------|
| 1 | **DB-01** | PostgreSQL service stopped | `moodle-fault-inject.sh DB-01` | `PostgreSQLDown` | `restart-postgres` | `moodle-fault-reset.sh DB-01` | <60s |
| 2 | **DB-02** | Connection pool exhaustion | `moodle-fault-inject.sh DB-02` | `DBConnectionExhausted` | `reset-connection-pool` | `moodle-fault-reset.sh DB-02` | <45s |
| 3 | **DB-03** | Wrong DB endpoint/config | `moodle-fault-inject.sh DB-03` | `MoodleDBConfigError` | `fix-db-config` | `moodle-fault-reset.sh DB-03` | <90s |
| 4 | **RES-01** | CPU hog (stress process) | `moodle-fault-inject.sh RES-01` | `HighCPUUsage` | `kill-cpu-hog` | `moodle-fault-reset.sh RES-01` | <30s |
| 5 | **RES-02** | Memory pressure / OOM risk | `moodle-fault-inject.sh RES-02` | `HighMemoryUsage` | `restart-moodle-service` | `moodle-fault-reset.sh RES-02` | <45s |
| 6 | **RES-03** | Disk fill (generate large files) | `moodle-fault-inject.sh RES-03` | `DiskSpaceCritical` | `cleanup-disk` | `moodle-fault-reset.sh RES-03` | <30s |
| 7 | **NET-01** | DNS alias loss (Route53 record delete) | `moodle-fault-inject.sh NET-01` | `MoodleDNSFailure` | `restore-dns-alias` | `moodle-fault-reset.sh NET-01` | <60s |
| 8 | **NET-02** | Port block Moodle→DB (iptables DROP) | `moodle-fault-inject.sh NET-02` | `MoodleDBConnectFail` | `fix-iptables-rule` | `moodle-fault-reset.sh NET-02` | <45s |
| 9 | **NET-03** | Network latency/loss (tc netem) | `moodle-fault-inject.sh NET-03` | `HighResponseLatency` | `remove-netem-rule` | `moodle-fault-reset.sh NET-03` | <60s |
| 10 | **CON-01** | Moodle container stopped | `moodle-fault-inject.sh CON-01` | `MoodleServiceDown` | `start-moodle-container` | `moodle-fault-reset.sh CON-01` | <20s |
| 11 | **CON-02** | Reverse proxy (nginx) stopped | `moodle-fault-inject.sh CON-02` | `NginxServiceDown` | `start-nginx` | `moodle-fault-reset.sh CON-02` | <20s |
| 12 | **CON-03** | Crash-loop with bad image tag | `moodle-fault-inject.sh CON-03` | `MoodleCrashLoop` | `rollback-to-stable-image` | `moodle-fault-reset.sh CON-03` | <90s |
| 13 | **SEC-01** | DB port (5432) exposed to 0.0.0.0/0 | `moodle-fault-inject.sh SEC-01` | `DBPortExposed` | `close-rds-public-access` | `moodle-fault-reset.sh SEC-01` | <120s |
| 14 | **SEC-02** | moodledata wrong permissions (777) | `moodle-fault-inject.sh SEC-02` | `MoodleDataPermissionRisk` | `fix-moodledata-permissions` | `moodle-fault-reset.sh SEC-02` | <90s |
| 15 | **SEC-03** | Security config drift (debug=ON) | `moodle-fault-inject.sh SEC-03` | `MoodleSecurityConfigDrift` | `restore-secure-config` | `moodle-fault-reset.sh SEC-03` | <120s |

> [!NOTE]
> **Expected MTTD** là thời gian từ `t_inject` đến `t_detect` (alert fire). MTTD thực tế phụ thuộc vào Prometheus scrape interval (mặc định 15s) và alert evaluation interval (15s).

#### Smoke trial cho 1 scenario đơn lẻ

```bash
# Test nhanh 1 scenario không cần ghi timestamp đầy đủ
bash automation/moodle-fault-trial.sh DB-01
```

---

## 7. Empirical benchmark (pending)

The default target design is 15 Moodle scenarios x 3 methods (Manual, Ansible, AI) x 5
repetitions = 225 matched empirical runs. The latest default acceptance report records
0/225 valid empirical runs. A reduced n=3 main matrix requires an explicit written
limitation; it does not lower the 50-run minimum for either ablation. Individual
staging fault/reset smokes and AI shadow runs are not matched benchmark cells and
must remain separate.

This checkout does not provide verified per-scenario Manual SOP records or the
`<scenario>-remediate.yml` Ansible playbooks described in older drafts. Do not
run guessed commands or count assumed outcomes. Infrastructure and experiment
owners must approve and document the method procedures, execution authority,
reset, verifier evidence, and timestamps before collection. The AI benchmark
acceptance checker is `automation/sprint6-acceptance.py`; it audits submitted
records and does not create results.

The simulation harness `automation/run-benchmark.py` is for scorer development
only. It does not perform any of the three methods or an empirical ablation;
see Section 8.

---

## 8. Ablation Studies (empirical execution pending)

The historical ablation commands/results below were removed because the current
`automation/run-benchmark.py` only generates randomized fixture simulations. It
does not disable a live Safety Gate, execute Moodle recovery, or run a
health-only production decision. Never use its `--ablation` options against AWS
or describe their output as measured trials.

The empirical no-gate and no-verifier comparisons remain **not collected**.
They require a separately reviewed staging protocol, matched scenarios and
methods, a safe counterfactual that cannot dispatch forbidden actions, raw
provenance-bearing records, and independent scoring. Current Sprint 6 coverage
is 0/225 empirical cells; existing shadow runs are excluded. See
`docs/SPRINT3_SPRINT4_COMPLETION_REPORT.md` and
`automation/sprint6-acceptance.py` for the acceptance boundary.

For local harness/scorer development only:

```bash
PYTHONPATH=agent_src python3 automation/run-benchmark.py \
  --moodle-only --repetitions 1 --synthetic-smoke \
  --output-dir terraform/.artifacts/sprint6-benchmark-simulation/
```

The generated output is `synthetic_simulation_not_empirical`; do not cite it
for RQ1/RQ2, recovery, or ablation acceptance.

## 9. Collect and validate results

### 9.1 Keep source and evidence class attached

The harvester accepts producer-marked runtime `result.json` records and can
ingest fixture-harness `benchmark_results.jsonl` only with the explicit
`simulated_fixture_benchmark` evidence class. It does not fill missing
timestamps, infer recovery from health, or label a harness reset as an AI
action. The CSV preserves `evidence_class`, `method`, and `execution_authority`;
synthetic rows remain excluded from empirical acceptance and analysis.

The Sprint 2 AWS drill observes the AI in shadow mode; the test harness resets
the fault. Its records are `staging_runtime_shadow`, not evidence of AI
self-remediation. `automation/run-benchmark.py` uses fixture truth and
RNG-derived outcomes; its records are labeled `simulated_fixture_benchmark`
and must not be presented as a live benchmark. Keep these evidence classes
separate in analysis.

Historical audit on 2026-10-04 found the former checked-in synthetic
`benchmark_results.jsonl` had 990 rows but only 270 unique `(method, run_id)`
keys, including ERPNext fixture rows mixed with Moodle fixtures. The former
CSV had 225 synthetic rows. These generated result files were removed from the
active result directory on 2026-10-05; the counts document the reason for
removal and are not trial observations. The collector rejects duplicate keys.
The generator assigns a unique campaign prefix, and `--include-erpnext`
explicitly controls the optional fixture set; generated output remains
simulation-only. Fresh empirical acceptance must use separate provenance-
bearing runtime records.

```bash
python3 automation/harvest-benchmark-results.py \
  --campaign-dir terraform/.artifacts/moodle-sprint2-live/ \
  --method ai_agent_shadow \
  --output evaluation/benchmark/results/ai-agent-shadow.csv

python3 automation/harvest-benchmark-results.py \
  --campaign-dir terraform/.artifacts/manual-runs/ \
  --method manual \
  --output evaluation/benchmark/results/manual-runtime.csv

python3 automation/harvest-benchmark-results.py \
  --merge \
  --inputs evaluation/benchmark/results/ai-agent-shadow.csv \
          evaluation/benchmark/results/manual-runtime.csv \
  --output evaluation/benchmark/results/collected.csv
```

A campaign with no supported records fails instead of writing an empty CSV.
Missing producer timestamps remain empty. Only compare methods, scenarios,
and repetitions when independent raw trials exist; the harvester does not
assert a run target or synthesize rows to fill missing groups; the acceptance gate
requires the 225-run default or a documented reduced n=3 main design.

## 10. ERPNext (Nếu Applicable)

> [!NOTE]
> ERPNext scenarios là optional — chạy nếu có thời gian sau khi hoàn thành 135 Moodle runs.

### Bước 10.1 — Deploy ERPNext

- ☐ Deploy ERPNext stack:

```bash
docker compose -f release/erpnext/docker-compose.yml up -d
```

**Expected output:**
```
[+] Running 5/5
 ✔ Container erpnext-mariadb    Started
 ✔ Container erpnext-redis      Started
 ✔ Container erpnext-nginx      Started
 ✔ Container erpnext-app        Started
 ✔ Container erpnext-worker     Started
```

- ☐ Verify ERPNext healthy:

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080
# Expected: 200
```

### Bước 10.2 — ERPNext Scenarios

| # | Scenario | Inject | Expected Action | Reset |
|---|----------|--------|-----------------|-------|
| ERP-01 | MariaDB stop | `moodle-fault-inject.sh ERP-01` | `start-mariadb` | `moodle-fault-reset.sh ERP-01` |
| ERP-02 | Redis stop | `moodle-fault-inject.sh ERP-02` | `start-redis` | `moodle-fault-reset.sh ERP-02` |
| ERP-03 | Nginx config drift | `moodle-fault-inject.sh ERP-03` | `restore-nginx-config` | `moodle-fault-reset.sh ERP-03` |

```bash
# Chạy ERPNext scenarios
for SCENARIO in ERP-01 ERP-02 ERP-03; do
  echo "=== Running $SCENARIO ==="
  MOODLE_FAULT_CONFIRM=staging bash automation/moodle-fault-inject.sh $SCENARIO
  sleep 90  # Chờ detect
  bash automation/moodle-fault-reset.sh $SCENARIO
  sleep 120  # Stability window
  bash automation/moodle-environment-baseline.sh verify
done
```

### Bước 10.3 — Teardown ERPNext

```bash
docker compose -f release/erpnext/docker-compose.yml down -v
```

---

## 11. Teardown

> [!CAUTION]
> **Backup tất cả artifacts trước khi destroy.** Terraform destroy sẽ xóa toàn bộ infrastructure bao gồm RDS data (nếu không có final snapshot).

### Bước 11.1 — Archive artifacts

- ☐ Copy artifacts ra nơi an toàn TRƯỚC KHI destroy:

```bash
# Tạo archive
ARCHIVE_TS=$(date +"%Y%m%d-%H%M%S")
tar -czf ~/aiops-artifacts-$ARCHIVE_TS.tar.gz \
  terraform/.artifacts/ \
  evaluation/benchmark/results/

# Verify archive
tar -tzf ~/aiops-artifacts-$ARCHIVE_TS.tar.gz | head -20
echo "Archive size: $(du -sh ~/aiops-artifacts-$ARCHIVE_TS.tar.gz)"
```

- ☐ Upload to S3 (optional, recommended):

```bash
aws s3 cp ~/aiops-artifacts-$ARCHIVE_TS.tar.gz \
  s3://your-backup-bucket/aiops-experiments/$ARCHIVE_TS/
```

### Bước 11.2 — Stop Moodle containers

- ☐ Graceful stop Moodle:

```bash
ssh ubuntu@$MONITOR_IP "cd /opt/aiops && docker compose -f release/docker-compose.staging.yml down"
ssh ubuntu@$MONITOR_IP "ssh ubuntu@10.0.2.10 'docker compose -f /opt/moodle/docker-compose.yml down'"
ssh ubuntu@$MONITOR_IP "ssh ubuntu@10.0.2.11 'docker compose -f /opt/moodle/docker-compose.yml down'"
```

### Bước 11.3 — Destroy AWS infrastructure

- ☐ Tạo RDS final snapshot (optional):

```bash
aws rds create-db-snapshot \
  --db-instance-identifier aiops-staging-rds \
  --db-snapshot-identifier aiops-staging-final-$(date +%Y%m%d) \
  --region ap-southeast-1
```

- ☐ Destroy infrastructure:

```bash
terraform -chdir=terraform destroy \
  -var-file=deployment.tfvars \
  -auto-approve
```

> Quá trình này mất **5–10 phút**.

**Expected output:**
```
Destroy complete! Resources: 32 destroyed.
```

### Bước 11.4 — Verify không còn charges

- ☐ Kiểm tra không còn EC2 instances:

```bash
aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=aiops-staging" \
  --query 'Reservations[].Instances[].{ID:InstanceId,State:State.Name}' \
  --output table
```

**Expected output:** *(empty table)*

- ☐ Kiểm tra không còn ELB:

```bash
aws elbv2 describe-load-balancers \
  --query 'LoadBalancers[?contains(LoadBalancerName, `aiops-staging`)].LoadBalancerArn' \
  --output text
```

**Expected output:** *(empty)*

- ☐ Kiểm tra không còn RDS:

```bash
aws rds describe-db-instances \
  --query 'DBInstances[?contains(DBInstanceIdentifier, `aiops-staging`)].DBInstanceStatus' \
  --output text
```

**Expected output:** *(empty)*

---

## 12. Troubleshooting

### 12.1 Moodle không login được

**Triệu chứng:** `403 Forbidden` hoặc trang login báo lỗi.

**Kiểm tra DB connection:**

```bash
# Từ moodle-app-a
ssh ubuntu@$MONITOR_IP -t "ssh ubuntu@10.0.2.10 'docker exec moodle-app php -r \"
\\\$db = new PDO(\\\"pgsql:host=\$RDS_ENDPOINT;dbname=moodle\\\", \\\"moodle\\\", \\\"password\\\");
echo \\\$db ? \\\"DB OK\\\" : \\\"DB FAIL\\\";
\\\"'"
```

**Kiểm tra EFS mount:**

```bash
ssh ubuntu@$MONITOR_IP -t "ssh ubuntu@10.0.2.10 'df -h | grep efs && ls -la /var/moodledata'"
```

**Fix nếu EFS unmounted:**

```bash
ssh ubuntu@$MONITOR_IP -t "ssh ubuntu@10.0.2.10 'sudo mount -t efs -o tls $EFS_ID:/ /var/moodledata'"
```

---

### 12.2 Alert không fire

**Triệu chứng:** Fault đã inject nhưng Alertmanager không tạo alert.

**Kiểm tra Prometheus scrape targets:**

```bash
curl -s http://$MONITOR_IP:9090/api/v1/targets | \
  jq '[.data.activeTargets[] | select(.health != "up") | {job: .labels.job, lastError: .lastError}]'
```

**Kiểm tra alert rules:**

```bash
curl -s http://$MONITOR_IP:9090/api/v1/rules | \
  jq '[.data.groups[].rules[] | select(.type == "alerting") | {name: .name, state: .state}]'
```

**Kiểm tra Alertmanager nhận alerts:**

```bash
curl -s http://$MONITOR_IP:9093/api/v2/alerts | jq 'length'
```

**Fix — reload Prometheus config:**

```bash
curl -X POST http://$MONITOR_IP:9090/-/reload
```

---

### 12.3 AI Agent không nhận webhook

**Triệu chứng:** Alert fire nhưng AI Agent không xử lý.

**Kiểm tra Alertmanager webhook config:**

```bash
ssh ubuntu@$MONITOR_IP "cat /etc/alertmanager/alertmanager.yml | grep webhook_url"
```

**Kiểm tra AI Agent health:**

```bash
curl -s http://$MONITOR_IP:8000/health | jq .
```

**Kiểm tra Celery workers:**

```bash
ssh ubuntu@$MONITOR_IP "docker exec aiops-celery celery -A agent.tasks inspect active"
```

**Kiểm tra Redis:**

```bash
ssh ubuntu@$MONITOR_IP "docker exec aiops-redis redis-cli ping"
# Expected: PONG
```

**Fix — restart AI Agent stack:**

```bash
ssh ubuntu@$MONITOR_IP "cd /opt/aiops && docker compose -f release/docker-compose.staging.yml restart"
```

---

### 12.4 Fault không reset

**Triệu chứng:** `moodle-fault-reset.sh` chạy xong nhưng service vẫn lỗi.

**Manual reset commands per scenario:**

| Scenario | Manual Reset Command |
|----------|---------------------|
| DB-01 | `ssh ubuntu@app-a 'sudo systemctl start postgresql'` hoặc `docker start postgres` |
| DB-02 | `ssh ubuntu@app-a 'docker exec postgres psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid();"'` |
| DB-03 | `ssh ubuntu@app-a 'sudo cp /etc/moodle/config.php.bak /etc/moodle/config.php'` |
| RES-01 | `ssh ubuntu@app-a 'pkill -f stress'` |
| RES-02 | `ssh ubuntu@app-a 'pkill -f stress-ng && sync'` |
| RES-03 | `ssh ubuntu@app-a 'sudo rm -f /tmp/diskfill-*'` |
| NET-01 | `aws route53 change-resource-record-sets --hosted-zone-id <ZONE_ID> --change-batch file://dns-restore.json` |
| NET-02 | `ssh ubuntu@app-a 'sudo iptables -D OUTPUT -p tcp --dport 5432 -j DROP'` |
| NET-03 | `ssh ubuntu@app-a 'sudo tc qdisc del dev eth0 root netem'` |
| CON-01 | `ssh ubuntu@app-a 'docker start moodle-app'` |
| CON-02 | `ssh ubuntu@app-a 'sudo systemctl start nginx'` |
| CON-03 | `ssh ubuntu@app-a 'docker pull ghcr.io/org/moodle:stable && docker compose up -d'` |
| SEC-01 | `aws ec2 revoke-security-group-ingress --group-id <sg-id> --protocol tcp --port 5432 --cidr 0.0.0.0/0` |
| SEC-02 | `ssh ubuntu@app-a 'sudo chmod 750 /var/moodledata && sudo chown -R www-data:www-data /var/moodledata'` |
| SEC-03 | `ssh ubuntu@app-a 'sudo cp /etc/moodle/config.php.secure.bak /etc/moodle/config.php'` |

---

### 12.5 SSH timeout

**Triệu chứng:** `ssh: connect to host xx.xx.xx.xx port 22: Connection timed out`

**Kiểm tra Security Group inbound rules:**

```bash
aws ec2 describe-security-groups \
  --filters "Name=tag:Name,Values=aiops-monitor-sg" \
  --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`]'
```

**Fix — thêm IP của bạn vào SG:**

```bash
MY_IP=$(curl -s https://ipinfo.io/ip)
aws ec2 authorize-security-group-ingress \
  --group-id <monitor-sg-id> \
  --protocol tcp \
  --port 22 \
  --cidr $MY_IP/32
```

---

### 12.6 RDS connection refused

**Triệu chứng:** `psql: error: connection to server at "xxxx.rds.amazonaws.com", port 5432 failed: Connection refused`

**Kiểm tra RDS status:**

```bash
aws rds describe-db-instances \
  --db-instance-identifier aiops-staging-rds \
  --query 'DBInstances[0].DBInstanceStatus'
```

**Kiểm tra Security Group giữa moodle-app và RDS:**

```bash
# Kiểm tra RDS SG có allow inbound từ moodle-app SG chưa
aws ec2 describe-security-groups \
  --filters "Name=tag:Name,Values=aiops-rds-sg" \
  --query 'SecurityGroups[0].IpPermissions[?FromPort==`5432`]'
```

**Fix — thêm inbound rule:**

```bash
aws ec2 authorize-security-group-ingress \
  --group-id <rds-sg-id> \
  --protocol tcp \
  --port 5432 \
  --source-group <moodle-app-sg-id>
```

---

### 12.7 Terraform state lock

**Triệu chứng:** `Error acquiring the state lock: ConditionalCheckFailedException`

```bash
# Xem lock info
terraform -chdir=terraform force-unlock -force <LOCK_ID>
```

> [!CAUTION]
> Chỉ force-unlock nếu chắc chắn không có process Terraform nào khác đang chạy.

---

*Tài liệu này là tài sản của khóa luận tốt nghiệp. Vui lòng không chia sẻ AWS credentials hoặc thông tin infrastructure ra bên ngoài.*
