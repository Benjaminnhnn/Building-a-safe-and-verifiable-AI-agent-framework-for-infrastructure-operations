# AWS Hybrid Infrastructure Deployment Guide

Guide nay danh cho dev dung may khac clone repo va van hanh he thong AWS Hybrid theo luong hien tai:

- Terraform tao/cap nhat ha tang AWS.
- Ansible bootstrap EC2, cai Docker, cau hinh firewall, Node Exporter, Prometheus, Alertmanager, Grafana va release runtime config.
- GitHub Actions build/push image versioned cho AI Agent, backend va frontend.
- EC2 pull image, chay health check va rollback ve tag cu neu release moi fail.

## 1. Tong Quan

Ha tang gom 3 EC2 instances trong region `ap-southeast-1`:

| Nhom | Host | Vai tro | Dich vu |
|------|------|---------|---------|
| `monitor` | `monitor-ai-01` | Monitoring va AI Agent | Prometheus `9090`, Alertmanager `9093`, Grafana `3000`, AI Agent `8000` |
| `web` | `bank-web-01` | Frontend/Nginx | Frontend production `80`, staging `18081`, Node Exporter |
| `core` | `bank-core-01` | Backend/PostgreSQL | Payment API production `8080`, staging `18080`, PostgreSQL, Node Exporter |

Luong chay chinh:

```text
Clone repo
  -> cau hinh AWS profile target-account
  -> cau hinh SSH key
  -> sua terraform/terraform.tfvars theo may moi
  -> terraform apply
  -> cap nhat ansible/inventory.ini
  -> ansible bootstrap EC2 va monitoring/config layer
  -> cap nhat GitHub Secrets cho 3 SSH host va runtime secrets
  -> push develop hoac tag v* de GitHub Actions build/push image
  -> GitHub Actions SSH vao dung role: monitor/core/web
  -> moi EC2 pull image theo role qua app-release-deploy.sh
  -> health check AI Agent, backend, frontend
  -> rollback tag cu neu release moi fail
```

## 2. File Can Chinh

| File | Muc dich |
|------|----------|
| `terraform/terraform.tfvars` | IP may dev, SSH key path, instance type |
| `ansible/inventory.ini` | Public IP va SSH private key path cho Ansible |
| `ansible/playbooks/bootstrap.yml` | Cai package co ban, Docker/Docker Compose, SSH runtime cho EC2 |
| `ansible/playbooks/configure-monitoring-stack.yml` | Cai Node Exporter tren cac node; Prometheus/Alertmanager/Grafana/blackbox tren monitor |
| `.github/workflows/ci.yml` | Lint/test/build local image de validate code, khong deploy EC2 |
| `.github/workflows/cd-staging.yml` | CD staging khi push `develop`; detect file thay doi, chi build/deploy role lien quan |
| `.github/workflows/cd-production.yml` | CD production khi push tag `v*`; build 3 image song song va deploy 3 role |
| `release/.env.example` | Template runtime config cho release stack image-based |
| `release/docker-compose.staging.yml` | Compose staging chua ca 3 role; deploy script chi start service cua role hien tai |
| `release/docker-compose.production.yml` | Compose production chua ca 3 role; deploy script chi start service cua role hien tai |
| `automation/github-deploy-role.sh` | Chay trong GitHub Actions: tao SSH key tam, SCP `release/` + `automation/`, goi deploy script tren EC2 |
| `automation/app-release-deploy.sh` | Chay tren EC2: docker login, pull image, start service theo role, health check, rollback |
| `demo-web/database/*.sql` | Source of truth cua SQL; CI copy vao `release/database/`, khong duplicate SQL trong release |
| GitHub Actions Secrets | Credentials cho CD staging/production |

## 3. Dieu Kien Truoc Khi Chay

Dev moi can co:

- AWS access key/secret key co quyen tao/cap nhat EC2, VPC, EIP, Security Group, Key Pair.
- SSH private key de vao EC2, hoac quyen tao key moi.
- `GEMINI_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
- Docker registry token de push/pull release images, vi du GHCR token.
- Tool local: `terraform`, `aws`, `ansible`, `curl`, `git`, `docker`.

Chay tu root repo:

```bash
cd /path/to/aws-hybrid
```

Kiem tra tool:

```bash
terraform version
aws --version
ansible --version
```

## 4. Cau Hinh AWS Profile

Terraform provider dang dung profile `target-account`, nen may moi phai cau hinh profile nay:

```bash
aws configure --profile target-account
```

Nhap:

```text
AWS Access Key ID: <access-key>
AWS Secret Access Key: <secret-key>
Default region name: ap-southeast-1
Default output format: json
```

Kiem tra:

```bash
aws sts get-caller-identity --profile target-account
```

Lenh dung se tra ve `Account`, `Arn`, `UserId`.

## 5. Cau Hinh SSH Key

Neu tao key moi:

```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/aws-hybrid -N ""
chmod 600 ~/.ssh/aws-hybrid
```

Neu dung key duoc ban giao, copy private key vao may moi, vi du:

```text
~/.ssh/aws-hybrid
```

Sau do:

```bash
chmod 600 ~/.ssh/aws-hybrid
```

Khong commit private key vao repo.

## 6. Sua `terraform/terraform.tfvars`

Lay public IP cua may dev:

```bash
curl -s https://api.ipify.org
```

Sua `terraform/terraform.tfvars`:

```hcl
aws_region = "ap-southeast-1"
my_ip_cidr = "<your-public-ip>/32"
ci_cd_ssh_cidr_blocks = []

public_key_path  = "~/.ssh/aws-hybrid.pub"
private_key_path = "~/.ssh/aws-hybrid"
ssh_user         = "ec2-user"

monitor_instance_type = "t3.small"
web_instance_type     = "t3.micro"
core_instance_type    = "t3.micro"
root_volume_size      = 30
```

`my_ip_cidr` phai dung IP public hien tai cua may dev, vi security group dung gia tri nay de mo SSH va cac cong monitoring.

## 7. Chay Terraform Va Cap Nhat Inventory

Chay:

```bash
cd terraform
terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
terraform output -raw ansible_inventory > ../ansible/inventory.ini
cd ..
```

Kiem tra inventory:

```bash
cat ansible/inventory.ini
```

Dang dung:

```ini
[monitor]
monitor-ai-01 ansible_host=<monitor-public-ip> ansible_user=ec2-user

[web]
bank-web-01 ansible_host=<web-public-ip> ansible_user=ec2-user

[core]
bank-core-01 ansible_host=<core-public-ip> ansible_user=ec2-user

[app:children]
web
core

[all:vars]
ansible_python_interpreter=/usr/bin/python3
ansible_ssh_private_key_file=/home/<user>/.ssh/aws-hybrid
```

Neu may dev doi IP sau nay, chay:

```bash
bash automation/update-infrastructure.sh --var-file terraform/terraform.tfvars
terraform -chdir=terraform plan -out=.artifacts/ip-cidr.tfplan
terraform -chdir=terraform show .artifacts/ip-cidr.tfplan
terraform -chdir=terraform apply .artifacts/ip-cidr.tfplan
```

Script chi cap nhat `my_ip_cidr` trong `terraform/terraform.tfvars`.
Terraform plan/apply van la buoc rieng de co the review thay doi; script khong
ghi lai `ansible/inventory.ini` va khong chay Ansible.

## 8. Kiem Tra SSH/Ansible

```bash
ansible all -i ansible/inventory.ini -m ping
```

Ket qua dung:

```text
monitor-ai-01 | SUCCESS
bank-web-01   | SUCCESS
bank-core-01  | SUCCESS
```

Neu `UNREACHABLE`, kiem tra lai:

- `my_ip_cidr` co dung public IP hien tai khong.
- `ansible_ssh_private_key_file` co dung duong dan tren may moi khong.
- Private key da `chmod 600` chua.
- EC2 moi tao co the can cho 1-2 phut de SSH san sang.

## 9. Chay Ansible Dung Vai Tro Bootstrap/Config

### Buoc 9.1: Bootstrap EC2

Playbook nay cai package nen, Docker, Docker Compose, SSH va cac thiet lap co ban:

```bash
ansible-playbook -i ansible/inventory.ini ansible/playbooks/bootstrap.yml
```

### Buoc 9.2: Cau Hinh Monitoring Va Firewall

Playbook `configure-monitoring-stack.yml` chuyen dung cho monitoring/config layer:

- Node Exporter tren cac node.
- Prometheus, Alertmanager, Grafana tren monitor node.
- Firewall host-level.
- Cac cau hinh monitoring va dashboard.

```bash
ansible-playbook -i ansible/inventory.ini ansible/playbooks/configure-monitoring-stack.yml
```

Playbook nay khong deploy backend, frontend hay app release production. Phan do duoc CI/CD xu ly bang release images va `automation/app-release-deploy.sh`.

### Buoc 9.3: Release Runtime Config

Luong hien tai khong can dat san release runtime bang Ansible cho moi lan deploy. GitHub Actions se dung `automation/github-deploy-role.sh` de copy `release/` va `automation/` vao dung EC2 role truoc khi deploy.

Playbook `configure-release-runtime.yml` la cach bootstrap/compatibility cu de dat file release len monitor host:

- `release/docker-compose.staging.yml`
- `release/docker-compose.production.yml`
- `release/.env.example`
- `automation/app-release-deploy.sh`
- thu muc `release/.state`

```bash
ansible-playbook -i ansible/inventory.ini ansible/playbooks/configure-release-runtime.yml
```

Neu deploy qua GitHub Actions, workflow se tu copy cac file nay vao `/home/ec2-user/aws-hybrid` tren `MONITOR_SSH_HOST`, `CORE_SSH_HOST` hoac `WEB_SSH_HOST`.

## 10. Cau Hinh Release Runtime Bang GitHub Secrets

Release `.env.staging` va `.env.production` duoc GitHub Actions tao tam tu `release/.env.example`, sau do nap gia tri tu repository secrets. Khong tao thu cong file secret trong repo.

Template can biet:

```env
GHCR_OWNER=<github-user-or-org>
IMAGE_TAG=<workflow-supplied-tag>
GEMINI_API_KEY=<secret>
TELEGRAM_TOKEN=<secret>
TELEGRAM_CHAT_ID=<secret>
AI_AGENT_PUBLIC_URL=<secret>
DATABASE_URL=<secret>
SECRET_KEY=<secret>
PROMETHEUS_URL=<secret>
ENVIRONMENT=staging-or-production
PAYMENT_API_UPSTREAM=http://<core-private-ip>:8080-or-18080
```

## 11. Duong Deploy Thong Nhat Cho Service Release

Tu luong hien tai, production/staging release deu di qua mot duong duy nhat. GitHub Actions build image len GHCR, sau do SSH vao tung EC2 theo role:

```text
GitHub Actions
  -> lint/test
  -> detect changed role (staging) hoac release full tag (production)
  -> build/push image len GHCR bang Docker Buildx cache
  -> SSH/SCP release config toi dung EC2 role
  -> chay automation/app-release-deploy.sh <env> <tag> <role>
  -> EC2 docker compose pull va up service cua role do
  -> health check role vua deploy
  -> rollback tag cu neu release moi fail
```

Role mapping khi deploy:

| Role | EC2 secret | Service duoc start |
|------|------------|--------------------|
| `monitor` | `MONITOR_SSH_HOST` | `redis`, `ai-agent`, `celery-worker`, `log-watcher` |
| `core` | `CORE_SSH_HOST` | `postgres`, `payment-api` |
| `web` | `WEB_SSH_HOST` | `frontend-web` |

### Buoc 11.1: Deploy Staging

Trigger:

```bash
git push origin develop
```

Workflow staging detect file thay doi:

- Sua `agent_src/` -> build/push AI Agent va deploy role `monitor`.
- Sua `demo-web/backend/` hoac `demo-web/database/` -> build/push Payment API va deploy role `core`.
- Sua `demo-web/frontend/` -> build/push Frontend va deploy role `web`.
- Sua `release/` hoac `automation/` -> build/push va deploy ca 3 role.
- Chay `workflow_dispatch` -> build/push va deploy ca 3 role.

Image tag staging co dang:

```text
staging-<commit-sha>
staging-latest
```

Sau do workflow chay tren EC2:

```bash
./automation/app-release-deploy.sh staging staging-<commit-sha> <monitor|core|web>
```

Health endpoints:

```text
AI Agent: http://127.0.0.1:18000/health
Backend:  http://127.0.0.1:18080/api/health
Frontend: http://127.0.0.1:18081/health
```

### Buoc 11.2: Deploy Production

Trigger:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Workflow production build/push ca 3 image song song de release tag luon day du:

- `aws-hybrid-ai-agent:v1.0.0`
- `aws-hybrid-payment-api:v1.0.0`
- `aws-hybrid-frontend:v1.0.0`

Sau do workflow chay tren EC2:

```bash
./automation/app-release-deploy.sh production v1.0.0 <monitor|core|web>
```

Health endpoints:

```text
AI Agent: http://127.0.0.1:8000/health
Backend:  http://127.0.0.1:8080/api/health
Frontend: http://127.0.0.1/health
```

Neu mot trong ba health check fail, script se quay ve tag truoc do da luu trong `release/.state/<environment>.tag`.

Luu y: webhook cua AI Agent chi enqueue alert vao Redis. Container `celery-worker` moi la thanh phan xu ly task, goi Gemini va gui Telegram. Neu thieu `celery-worker`, Alertmanager van thay webhook `200 OK` nhung Telegram se khong nhan alert.

Khi sua code AI Agent, backend hoac frontend:

- staging: push `develop`
- production: tao tag `v*`

Khong build image tren EC2, khong copy source release len EC2 de chay production.

## 12. Kiem Tra Sau Deploy

Lay IP public tu inventory:

```bash
cat ansible/inventory.ini
```

Kiem tra container:

```bash
ansible all -i ansible/inventory.ini -m shell -a "docker ps --format '{% raw %}table {{.Names}}\t{{.Status}}\t{{.Ports}}{% endraw %}'"
```

Kiem tra health noi bo cua monitoring/config layer va release runtime:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "curl -s http://localhost:9090/-/ready && echo && curl -s http://localhost:9093/-/ready && echo && curl -s http://localhost:3000/api/health"
ansible monitor -i ansible/inventory.ini -m shell -a "curl -s http://localhost:8000/health"
ansible core -i ansible/inventory.ini -m shell -a "curl -s http://localhost:8080/api/health"
ansible web -i ansible/inventory.ini -m shell -a "curl -s http://localhost/health && echo && curl -s http://localhost/api/health"
```

Kiem tra release containers theo role:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "docker ps --format '{% raw %}table {{.Names}}\t{{.Status}}{% endraw %}' | egrep 'NAMES|ai-agent-prod|redis-prod|celery-worker-prod|log-watcher-prod'"
ansible core -i ansible/inventory.ini -m shell -a "docker ps --format '{% raw %}table {{.Names}}\t{{.Status}}{% endraw %}' | egrep 'NAMES|payment-api-prod|postgres-prod'"
ansible web -i ansible/inventory.ini -m shell -a "docker ps --format '{% raw %}table {{.Names}}\t{{.Status}}{% endraw %}' | egrep 'NAMES|frontend-web-prod'"
ansible monitor -i ansible/inventory.ini -m shell -a "docker exec redis-prod redis-cli llen celery || true"
```

Kiem tra Prometheus co scrape du metric va Grafana co doc duoc data:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "curl -s http://localhost:9090/api/v1/targets | python3 -c \"import sys,json; d=json.load(sys.stdin); [print(t['labels'].get('job'), t['labels'].get('instance'), t['health'], t.get('lastError','')) for t in d['data']['activeTargets']]\""
ansible monitor -i ansible/inventory.ini -m shell -a "curl -sG http://localhost:9090/api/v1/query --data-urlencode 'query=up' | python3 -c \"import sys,json; d=json.load(sys.stdin); print('series='+str(len(d['data']['result']))); [print(r['metric'].get('job'), r['metric'].get('instance'), r['value'][1]) for r in d['data']['result']]\""
ansible monitor -i ansible/inventory.ini -m shell -a "curl -s http://admin:admin123@localhost:3000/api/datasources"
```

Kiem tra URL public tu may local:

```bash
curl -i http://<web-public-ip>/
curl -i http://<web-public-ip>/api/health
curl -i http://<web-public-ip>/api/ready
curl -i http://<web-public-ip>/docs

curl -i http://<monitor-public-ip>:3000/api/health
curl -i http://<monitor-public-ip>:9090/-/ready
curl -i http://<monitor-public-ip>:9093/-/ready
```

URLs truy cap:

```text
Web UI demo:  http://<web-public-ip>
API health:   http://<web-public-ip>/api/health
API ready:    http://<web-public-ip>/api/ready
API docs:     http://<web-public-ip>/docs
Grafana:      http://<monitor-public-ip>:3000
Prometheus:   http://<monitor-public-ip>:9090
Alertmanager: http://<monitor-public-ip>:9093
AI Agent:     http://<monitor-public-ip>:8000/health  # neu security group cho phep public access
Frontend release health noi bo tren web host: http://127.0.0.1/health
Backend release health noi bo tren core host: http://127.0.0.1:8080/api/health
Backend release readiness noi bo tren core host: http://127.0.0.1:8080/api/ready
```

`/api/health` la liveness cua API process. `/api/ready` kiem tra them ket noi PostgreSQL va duoc blackbox probe dung de phat hien dependency/configuration failure.

URLs theo inventory hien tai:

```text
Web UI demo:  http://3.1.78.149
API health:   http://3.1.78.149/api/health
API docs:     http://3.1.78.149/docs
Grafana:      http://54.151.146.219:3000
Prometheus:   http://54.151.146.219:9090
Alertmanager: http://54.151.146.219:9093
AI Agent:     http://54.151.146.219:8000/health
```

Grafana:

```text
Username: admin
Password: admin123
```

Neu Grafana/Prometheus/Alertmanager public URL bi timeout, kiem tra IP public hien tai:

```bash
curl -s https://api.ipify.org
rg -n "my_ip_cidr" terraform/terraform.tfvars
```

Neu IP hien tai khac `my_ip_cidr`, chay:

```bash
bash automation/update-infrastructure.sh --var-file terraform/terraform.tfvars
terraform -chdir=terraform plan -out=.artifacts/ip-cidr.tfplan
terraform -chdir=terraform show .artifacts/ip-cidr.tfplan
terraform -chdir=terraform apply .artifacts/ip-cidr.tfplan
```

## 13. Cap Nhat GitHub Secrets Truoc Khi Chay CI/CD

Can cap nhat GitHub Secrets sau khi doi server/IP/key, truoc khi push `develop`, tao tag `v*`, hoac chay CD workflow thu cong.

Workflow lien quan:

- `.github/workflows/ci.yml`: lint/test/build, khong can deploy secret.
- `.github/workflows/cd-staging.yml`: deploy khi push `develop` hoac `workflow_dispatch`.
- `.github/workflows/cd-production.yml`: deploy khi push tag `v*` hoac `workflow_dispatch`.

### Chinh Secrets Tren GitHub UI

Vao repo tren GitHub:

```text
Settings -> Secrets and variables -> Actions -> Repository secrets
```

Tao secret moi:

1. Bam `New repository secret`.
2. Nhap `Name`, vi du `MONITOR_SSH_HOST`, `CORE_SSH_HOST` hoac `WEB_SSH_HOST`.
3. Paste gia tri vao `Secret`.
4. Bam `Add secret`.

Sua secret da co:

1. Tim secret trong `Repository secrets`.
2. Bam `Update`.
3. Paste gia tri moi.
4. Bam `Update secret`.

GitHub se khong hien lai gia tri sau khi luu. Neu paste sai, update lai secret do.

### Secrets Can Co

| Secret | Gia tri |
|--------|---------|
| `GHCR_USERNAME` | GitHub username/org co quyen push GHCR |
| `GHCR_TOKEN` | GitHub PAT co scope `write:packages`, them `read:packages` neu image private |
| `MONITOR_SSH_HOST` | Public IP/DNS cua `monitor-ai-01` |
| `CORE_SSH_HOST` | Public IP/DNS cua `bank-core-01` |
| `WEB_SSH_HOST` | Public IP/DNS cua `bank-web-01` |
| `SSH_PORT` | `22` |
| `SSH_PRIVATE_KEY` | Noi dung private key, khong phai duong dan file |
| `GEMINI_API_KEY` | Gemini API key |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `AI_AGENT_PUBLIC_URL` | `http://<monitor-public-ip>:8000` neu dung AI Agent public |
| `DATABASE_URL` | Database URL cho release stack |
| `SECRET_KEY` | Secret key cho backend/API |
| `PROMETHEUS_URL` | `http://<monitor-public-ip>:9090` |
| `PAYMENT_API_UPSTREAM` | Frontend upstream toi core API, vi du production `http://<core-private-ip>:8080`, staging `http://<core-private-ip>:18080` |

Workflow hien tai SSH bang user `ec2-user`. Neu server dung user khac, phai sua workflow truoc khi chay CD.

### Cach Paste `SSH_PRIVATE_KEY`

Lay key tren may local:

```bash
cat ~/.ssh/aws-hybrid
```

Paste toan bo noi dung vao secret `SSH_PRIVATE_KEY`, bao gom:

```text
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
```

Khong paste file `.pub`, khong them dau ngoac kep, khong thay newline bang `\n`.

### Cach Tao `GHCR_TOKEN`

Tren GitHub:

```text
Avatar -> Settings -> Developer settings -> Personal access tokens -> Tokens (classic)
```

Tao token classic moi, tick:

```text
write:packages
read:packages
```

Copy token ngay sau khi tao va luu vao secret `GHCR_TOKEN`.

### Gia Tri Can Lay Tu Ha Tang Moi

```text
MONITOR_SSH_HOST=<monitor-public-ip>
CORE_SSH_HOST=<core-public-ip>
WEB_SSH_HOST=<web-public-ip>
SSH_PORT=22
AI_AGENT_PUBLIC_URL=http://<monitor-public-ip>:8000
PROMETHEUS_URL=http://<monitor-public-ip>:9090
PAYMENT_API_UPSTREAM=http://<core-private-ip>:8080
```

Luu y: CD workflow copy `release/` va `automation/` vao `/home/ec2-user/aws-hybrid` tren tung host theo role, sau do chay `automation/app-release-deploy.sh <environment> <image-tag> <role>`.

### Kiem Tra Truoc Khi Trigger CD

Test SSH tu may local:

```bash
ssh -i ~/.ssh/aws-hybrid -p 22 ec2-user@<monitor-public-ip> 'echo auth-ok'
ssh -i ~/.ssh/aws-hybrid -p 22 ec2-user@<core-public-ip> 'echo auth-ok'
ssh -i ~/.ssh/aws-hybrid -p 22 ec2-user@<web-public-ip> 'echo auth-ok'
```

Test GHCR token:

```bash
echo "<GHCR_TOKEN>" | docker login ghcr.io -u "<GHCR_USERNAME>" --password-stdin
```

Trigger staging:

```bash
git push origin develop
```

Trigger production:

```bash
git tag v<version>
git push origin v<version>
```

## 14. Lenh Nhanh

Lan dau tren may moi:

```bash
cd /path/to/aws-hybrid
aws configure --profile target-account
ssh-keygen -t rsa -b 4096 -f ~/.ssh/aws-hybrid -N ""
chmod 600 ~/.ssh/aws-hybrid
curl -s https://api.ipify.org
# sua terraform/terraform.tfvars
cd terraform
terraform init
terraform validate
terraform plan -out=tfplan
terraform apply tfplan
terraform output -raw ansible_inventory > ../ansible/inventory.ini
cd ..
ansible all -i ansible/inventory.ini -m ping
ansible-playbook -i ansible/inventory.ini ansible/playbooks/bootstrap.yml
ansible-playbook -i ansible/inventory.ini ansible/playbooks/configure-monitoring-stack.yml
ansible-playbook -i ansible/inventory.ini ansible/playbooks/configure-release-runtime.yml
# cap nhat GitHub Secrets
# push develop de deploy staging, hoac tao tag v* de deploy production
```

Khi da co ha tang va chi can cap nhat IP/deploy lai:

```bash
cd /path/to/aws-hybrid
bash automation/update-infrastructure.sh --var-file terraform/terraform.tfvars
terraform -chdir=terraform plan -out=.artifacts/ip-cidr.tfplan
terraform -chdir=terraform show .artifacts/ip-cidr.tfplan
terraform -chdir=terraform apply .artifacts/ip-cidr.tfplan
ansible all -i ansible/inventory.ini -m ping
ansible-playbook -i ansible/inventory.ini ansible/playbooks/configure-monitoring-stack.yml
ansible-playbook -i ansible/inventory.ini ansible/playbooks/configure-release-runtime.yml
```

Khi chi cap nhat app release:

```bash
cd /path/to/aws-hybrid
git push origin develop
# hoac
git tag v<version>
git push origin v<version>
```

## 15. Troubleshooting

Terraform loi credentials:

```bash
aws sts get-caller-identity --profile target-account
aws configure --profile target-account
```

Ansible `UNREACHABLE`:

```bash
bash automation/update-infrastructure.sh --var-file terraform/terraform.tfvars
terraform -chdir=terraform plan -out=.artifacts/ip-cidr.tfplan
terraform -chdir=terraform show .artifacts/ip-cidr.tfplan
terraform -chdir=terraform apply .artifacts/ip-cidr.tfplan
chmod 600 ~/.ssh/aws-hybrid
ansible all -i ansible/inventory.ini -m ping
```

Khong pull duoc release image:

```bash
echo "<GHCR_TOKEN>" | docker login ghcr.io \
  -u "<GHCR_USERNAME>" \
  --password-stdin
docker pull ghcr.io/<owner>/aws-hybrid-ai-agent:<tag>
docker pull ghcr.io/<owner>/aws-hybrid-payment-api:<tag>
docker pull ghcr.io/<owner>/aws-hybrid-frontend:<tag>
```

Docker format bi loi `unexpected '.'`: dung lenh da boc `{% raw %}` trong phan kiem tra, vi Ansible co the parse `{{.Names}}` nhu Jinja.

### Alertmanager webhook `200 OK` nhung Telegram khong nhan alert

Dau hieu:

- Alertmanager log co `Notify success` hoac AI Agent log co `POST /webhook HTTP/1.1" 200 OK`.
- Telegram bot khong nhan alert.
- Redis queue `celery` tang len hoac co task ton dong.
- `docker ps` khong co container `celery-worker-prod`.

Nguyen nhan:

- `core/main.py` chi nhan webhook va goi `process_alerts_task.delay(...)` de enqueue task vao Redis.
- Task gui Telegram nam trong Celery worker (`process_alerts_task` trong `agent_src/core/tasks.py`).
- Neu release stack chi chay `ai-agent-prod` va `redis-prod`, alert se nam trong queue nhung khong co worker xu ly.

Kiem tra nhanh tren monitor host:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "docker ps --format '{% raw %}table {{.Names}}\t{{.Status}}{% endraw %}' | egrep 'NAMES|alertmanager|ai-agent-prod|redis-prod|celery-worker-prod'"
ansible monitor -i ansible/inventory.ini -m shell -a "docker exec ai-agent-prod sh -c 'for v in TELEGRAM_TOKEN TELEGRAM_CHAT_ID GEMINI_API_KEY; do if [ -n \"\$(printenv \$v)\" ]; then echo \"\$v=set\"; else echo \"\$v=missing\"; fi; done'"
ansible monitor -i ansible/inventory.ini -m shell -a "docker exec redis-prod redis-cli --scan | head -50 && docker exec redis-prod redis-cli llen celery"
ansible monitor -i ansible/inventory.ini -m shell -a "docker logs --since 5m ai-agent-prod 2>&1 | egrep 'POST /webhook|ERROR|WARNING' || true"
ansible monitor -i ansible/inventory.ini -m shell -a "docker logs --since 5m celery-worker-prod 2>&1 | egrep 'Task process_alerts_task|Message sent|All .* Telegram|ERROR|WARNING|succeeded|failed' || true"
```

Kiem tra Telegram token/chat id truc tiep, khong in token ra output:

```bash
ssh -i ~/.ssh/aws-hybrid ec2-user@<monitor-public-ip>

docker exec ai-agent-prod sh -c 'MSG="AIOps Telegram direct test at $(date -u +%Y-%m-%dT%H:%M:%SZ)"; curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" -d chat_id="$TELEGRAM_CHAT_ID" -d text="$MSG" | python -c "import sys,json; d=json.load(sys.stdin); print({k:d.get(k) for k in (\"ok\",\"description\",\"error_code\")})"'
```

Ket qua dung:

```text
{'ok': True, 'description': None, 'error_code': None}
```

Gui alert test qua Alertmanager:

```bash
ssh -i ~/.ssh/aws-hybrid ec2-user@<monitor-public-ip>

START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u -d "+2 minutes" +%Y-%m-%dT%H:%M:%SZ)
curl -sS -X POST http://localhost:9093/api/v2/alerts \
  -H "Content-Type: application/json" \
  -d "[{\"labels\":{\"alertname\":\"TelegramPipelineWorkerTest\",\"severity\":\"warning\",\"service\":\"availability\",\"instance\":\"monitor-ai-01\"},\"annotations\":{\"summary\":\"Telegram worker pipeline test\",\"description\":\"Synthetic alert after celery-worker startup\"},\"startsAt\":\"$START\",\"endsAt\":\"$END\",\"generatorURL\":\"http://manual-test/telegram-worker-pipeline\"}]"

sleep 20
docker logs --since 1m ai-agent-prod 2>&1 | egrep "POST /webhook|ERROR|WARNING" || true
docker logs --since 1m celery-worker-prod 2>&1 | egrep "TelegramPipelineWorkerTest|Task process_alerts_task|Message sent|All .* Telegram|succeeded|failed|ERROR|WARNING" || true
docker exec redis-prod redis-cli llen celery
```

Ket qua dung:

```text
POST /webhook HTTP/1.1" 200 OK
Task process_alerts_task[...] received
All 1 message parts sent to Telegram!
Task process_alerts_task[...] succeeded
0
```

Fix lau dai:

- `release/docker-compose.staging.yml` va `release/docker-compose.production.yml` phai co service `celery-worker`.
- Worker nen dung command:

```yaml
command: ["celery", "-A", "core.celery_app.celery_app", "worker", "--loglevel=INFO", "--concurrency=1", "--prefetch-multiplier=1"]
```

- `--concurrency=1` va `--prefetch-multiplier=1` giup tranh xu ly backlog qua nhanh lam Gemini bi quota `429 RESOURCE_EXHAUSTED`.
- Neu log worker co `Gemini API error: 429 RESOURCE_EXHAUSTED`, Telegram van co the gui fallback message, nhung nen giam concurrency hoac nang quota Gemini de phan tich AI on dinh hon.
- Sau khi sua compose release, day code len GitHub de CI/CD build lai image va deploy release moi:

```bash
git push origin develop
# hoac
git tag v<version>
git push origin v<version>
```

### Prometheus khong scrape duoc metric / target `blackbox_http_web` down

Dau hieu:

- Prometheus target `blackbox_http_web` hien `down`.
- `lastError` co dang `dial tcp 127.0.0.1:9115: connect: connection refused`.
- Alert `ServiceDown` hoac `ServiceUnreachable` firing, trong khi web endpoint that van tra `200 OK`.

Kiem tra tren monitor host:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "curl -s http://localhost:9090/api/v1/targets | python3 -c \"import sys,json; d=json.load(sys.stdin); [print(t['labels'].get('job'), t['labels'].get('instance'), t['health'], t.get('lastError','')) for t in d['data']['activeTargets']]\""
ansible monitor -i ansible/inventory.ini -m shell -a "ss -ltnp | egrep ':(9090|9093|9100|9115|3000|8000)' || true"
ansible monitor -i ansible/inventory.ini -m shell -a "curl -sS http://localhost:9115/-/healthy || true"
ansible monitor -i ansible/inventory.ini -m shell -a "curl -sS 'http://localhost:9115/probe?module=http_2xx&target=http://<web-public-ip>/health' | egrep 'probe_success|probe_http_status_code|probe_duration_seconds' || true"
```

Nguyen nhan thuong gap:

- Prometheus config co job `blackbox_http_web` tro toi `localhost:9115`, nhung `blackbox-exporter` chua duoc deploy/chua chay.
- Web endpoint `http://<web-public-ip>/health` van tot, nhung Prometheus khong goi duoc exporter trung gian nen target van bi down.

Hotfix runtime tren monitor host:

```bash
ssh -i ~/.ssh/aws-hybrid ec2-user@<monitor-public-ip>

sudo mkdir -p /opt/blackbox
sudo tee /opt/blackbox/blackbox.yml >/dev/null <<'EOF'
modules:
  http_2xx:
    prober: http
    timeout: 5s
    http:
      method: GET
      preferred_ip_protocol: ip4
      valid_status_codes: []
      follow_redirects: true
EOF
sudo chown -R nobody:nobody /opt/blackbox
```

Them service sau vao `/opt/prometheus/docker-compose.yml` cung cap voi `prometheus` va `alertmanager`:

```yaml
  blackbox-exporter:
    image: prom/blackbox-exporter:latest
    container_name: blackbox-exporter
    network_mode: host
    volumes:
      - /opt/blackbox/blackbox.yml:/etc/blackbox_exporter/config.yml:ro
    command:
      - '--config.file=/etc/blackbox_exporter/config.yml'
    restart: unless-stopped
```

Neu service `prometheus` co `depends_on`, them:

```yaml
    depends_on:
      - blackbox-exporter
```

Apply lai stack:

```bash
cd /opt/prometheus
docker-compose up -d
```

Kiem tra ket qua dung:

```bash
curl -sS http://localhost:9115/-/healthy
curl -sS "http://localhost:9115/probe?module=http_2xx&target=http://<web-public-ip>/health" | egrep "probe_success|probe_http_status_code"
curl -s http://localhost:9090/api/v1/alerts
```

Ket qua dung:

```text
probe_http_status_code 200
probe_success 1
```

Fix lau dai:

- `ansible/playbooks/configure-monitoring-stack.yml` phai tao `/opt/blackbox/blackbox.yml`.
- Docker compose Prometheus trong playbook phai co service `blackbox-exporter`.
- `platform-config/docker-compose.dev.yml` cung nen co `blackbox-exporter` de moi truong dev khong bi lech.

### Grafana dashboard hien `No data` do tro sai datasource UID

Dau hieu:

- Prometheus van co data, vi query `up`, CPU, memory deu tra series.
- Grafana datasource `Prometheus` healthy.
- Nhung 3 dashboard Grafana (`Alert Monitoring`, `Network & Performance`, `System Overview`) hien `No data`.
- Dashboard JSON dang tro UID cu, vi du `bfijh14lltg5ce`, trong khi datasource hien tai co UID khac.

Kiem tra Prometheus co data:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "curl -sG http://localhost:9090/api/v1/query --data-urlencode 'query=up' | python3 -c \"import sys,json; d=json.load(sys.stdin); print('series='+str(len(d['data']['result']))); [print(r['metric'].get('job'), r['metric'].get('instance'), r['value'][1]) for r in d['data']['result']]\""
```

Kiem tra datasource UID hien tai:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "curl -s http://admin:admin123@localhost:3000/api/datasources/name/Prometheus"
```

Kiem tra dashboard dang tro UID nao:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "for uid in \$(curl -s 'http://admin:admin123@localhost:3000/api/search?type=dash-db' | python3 -c \"import sys,json; [print(d['uid']) for d in json.load(sys.stdin)]\"); do echo DASH:\$uid; curl -s http://admin:admin123@localhost:3000/api/dashboards/uid/\$uid | python3 -c \"import sys,json; d=json.load(sys.stdin); targets=[t for p in d.get('dashboard',{}).get('panels',[]) for t in p.get('targets',[])]; print(sorted(set(str(t.get('datasource')) for t in targets)))\"; done"
```

Hotfix runtime:

```bash
ssh -i ~/.ssh/aws-hybrid ec2-user@<monitor-public-ip>

python3 - <<'PY'
import base64
import json
import urllib.request

base = "http://localhost:3000"
auth = "Basic " + base64.b64encode(b"admin:admin123").decode()

def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", auth)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

prometheus_uid = request("GET", "/api/datasources/name/Prometheus")["uid"]
dashboards = request("GET", "/api/search?type=dash-db")

for item in dashboards:
    wrapper = request("GET", f"/api/dashboards/uid/{item['uid']}")
    dashboard = wrapper["dashboard"]
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            datasource = target.get("datasource")
            if isinstance(datasource, dict) and datasource.get("type") == "prometheus":
                datasource["uid"] = prometheus_uid
    request("POST", "/api/dashboards/db", {
        "dashboard": dashboard,
        "folderId": wrapper.get("meta", {}).get("folderId", 0),
        "overwrite": True,
        "message": "Fix Prometheus datasource UID",
    })
    print("updated", dashboard.get("title"), item["uid"], "->", prometheus_uid)
PY
```

Kiem tra Grafana query qua datasource API:

```bash
ansible monitor -i ansible/inventory.ini -m shell -a "now=\$(date +%s000); from=\$((now-300000)); curl -s -X POST -H 'Content-Type: application/json' http://admin:admin123@localhost:3000/api/ds/query --data \"{\\\"from\\\":\\\"\$from\\\",\\\"to\\\":\\\"\$now\\\",\\\"queries\\\":[{\\\"refId\\\":\\\"A\\\",\\\"datasource\\\":{\\\"type\\\":\\\"prometheus\\\",\\\"uid\\\":\\\"\$(curl -s http://admin:admin123@localhost:3000/api/datasources/name/Prometheus | python3 -c 'import sys,json; print(json.load(sys.stdin)[\\\"uid\\\"])')\\\"},\\\"expr\\\":\\\"up\\\",\\\"instant\\\":true,\\\"range\\\":false,\\\"format\\\":\\\"time_series\\\",\\\"intervalMs\\\":15000,\\\"maxDataPoints\\\":100}]}\" | python3 -c \"import sys,json; d=json.load(sys.stdin); print('frames='+str(len(d.get('results',{}).get('A',{}).get('frames',[]))))\""
```

Fix lau dai:

- Datasource Prometheus trong `configure-monitoring-stack.yml` nen co UID co dinh, vi du `uid: prometheus`.
- Dashboard templates trong `ansible/templates/*.json` va `ansible/playbooks/templates/*.json` khong nen giu UID cu sinh ngau nhien.
- Truoc khi import dashboard, playbook nen lay UID datasource that tu Grafana API `/api/datasources/name/Prometheus` va patch dashboard JSON tam thoi de tranh lech UID khi datasource da ton tai tu deploy cu.

## 16. Bao Mat Va Ban Giao

Khong commit:

- AWS Access Key/Secret Access Key.
- SSH private key.
- Telegram/Gemini token.
- Docker registry token/GHCR token.
- Terraform state neu state co secret.

Khi ban giao cho dev khac, gui rieng:

- AWS account/profile/role duoc phep deploy.
- SSH private key hoac cach tao key moi.
- Gia tri GitHub Secrets cho runtime release.
- GitHub Secrets can update.
- Public IP hien tai cua ha tang.

## 17. RAG Persistent Storage

The monitor role must deploy `ai-agent` and `celery-worker` with the same persistent ChromaDB volume mounted at `/app/vector_db`. The release compose files set `VECTOR_DB_PATH=/app/vector_db` and share:

- Staging: `vector-db-staging`
- Production: `vector-db-prod`

The database contains two collections:

- `standard_runbooks`: chunks loaded from `agent_src/config/knowledge_base/*.md`.
- `incident_memory`: resolved incident history and accepted/revised admin feedback.

Verify the staging mounts after deployment:

```bash
docker inspect ai-agent-staging --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}'
docker inspect celery-worker-staging --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}'
docker exec celery-worker-staging python tools/inspect_rag_db.py
```

ChromaDB files are runtime data and must not be committed to Git. Back up an existing `/app/vector_db` directory before the first recreate that introduces the shared named volume.
