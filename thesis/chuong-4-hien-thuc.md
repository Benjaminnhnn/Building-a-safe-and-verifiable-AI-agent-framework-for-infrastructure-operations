# Chương 4. Hiện thực hệ thống

Chương 3 đã đặt ra khung an toàn: đầu ra của mô hình là đề xuất không đáng tin; Safety Gate kiểm tra policy, catalog, phạm vi và độ tươi bằng chứng; Operator ký một chữ ký gắn đúng action; Safe Executor chỉ chạy allowlist; chỉ Independent Verifier được chuyển incident sang `RESOLVED`. Chương này mô tả cách khung đó được hiện thực thành mã nguồn, cấu hình hạ tầng và quy trình vận hành: công nghệ (4.1), hạ tầng dưới dạng mã (4.2), CI/CD (4.3), AI Agent (4.4), bộ cấy lỗi, reset và probe (4.5), hiện thực AIHS trên Moodle (4.6), kiểm thử (4.7) và các vấn đề gặp phải (4.8). Mỗi mục nêu loại bằng chứng đi kèm (kiểm thử đơn vị/hợp đồng, replay offline, lab cục bộ, live staging); số liệu đo nằm ở Chương 5.

## 4.1. Công nghệ sử dụng

Môi trường thực nghiệm chính là Moodle trên AWS EC2: một ALB, hai node ứng dụng, RDS PostgreSQL, EFS dùng chung và một máy `monitor-ai-01` chạy giám sát, Agent và Executor (Bảng 4.1).

**Bảng 4.1.** Thành phần, công nghệ và vai trò

| Thành phần | Công nghệ | Vai trò |
| --- | --- | --- |
| Hạ tầng cloud | Terraform [Terraform] (`>= 1.6.0`, provider AWS `~> 5.0`); AWS [AWS] | Khai báo tài nguyên cloud; `plan` được xem lại trước `apply` |
| Cấu hình máy | Ansible [Ansible] | Cài Docker, mount EFS, triển khai giám sát, Agent, Executor qua SSH |
| Đóng gói, chạy | Docker, Docker Compose [Docker] [DockerCompose] | Chạy Moodle, Agent, Prometheus, Redis theo image cố định |
| Ứng dụng đích | Moodle 5.2.3, PHP 8.4, Apache [Moodle]; PostgreSQL [PostgreSQL] | Hệ thống bị giám sát; `healthz.php`, `synthetic-transaction.php` làm điểm kiểm tra |
| Giám sát | Prometheus, Alertmanager, Grafana, Blackbox/Node Exporter, cAdvisor [Prometheus] [Alertmanager] [Grafana] | Thu metric, 23 alert rule, webhook tới Agent |
| API và hàng đợi | FastAPI [FastAPI], Pydantic [Pydantic]; Celery [Celery], Redis [Redis] | Webhook, endpoint prepare/execute/verify; xử lý bất đồng bộ; khóa dedup |
| Bằng chứng, chữ ký | SQLite [SQLite]; Ed25519 qua `cryptography` [Cryptography]; HMAC-SHA256 | Kho bằng chứng và audit băm; chữ ký Operator, Verifier; xác thực Agent–Executor |
| Tri thức, người dùng | ChromaDB [ChromaDB]; Telegram Bot API [TelegramBot]; Gemini [Gemini] | Runbook và bộ nhớ sự cố; báo cáo, duyệt; review góp ý của admin (chỉ vậy) |
| CI/CD | GitHub Actions [GitHubActions]; GHCR [GHCR] | Lint, test, replay, image bất biến theo SHA, deploy staging |
| AIHS và lab | scikit-learn, LightGBM [ScikitLearn] [LightGBM]; HAProxy [HAProxy] | Mapper LDA, Predictor, Controller theo [AIHS2021]; lab Docker mô phỏng staging |

<!-- nguồn: agent_src/requirements.in; agent_src/Dockerfile; moodle/Dockerfile; ansible/playbooks/configure-moodle-monitoring.yml; evaluation/aihs/requirements-moodle-aihs.txt; lab/moodle-local/docker-compose.yml; docs/BAO_CAO_HUONG_DAN_TRIEN_KHAI.md §2 -->

Agent chạy trên Python 3.11 (`python:3.11-slim`, khởi động bằng `uvicorn core.main:app` cổng 8000). Dependency cài với `--require-hashes`; `httpx` giữ `<0.28` để `TestClient` của FastAPI hoạt động; gói Gemini là `google-genai`. Image giám sát ghim theo tag (ví dụ Prometheus `v2.53.3`, Alertmanager `v0.27.0`). Tooling huấn luyện AIHS (scikit-learn 1.5.2, LightGBM 4.5.0) nằm ngoài `requirements.txt` của Agent vì image Agent không huấn luyện mô hình.

<!-- nguồn: AGENTS.md; agent_src/requirements.in; evaluation/aihs/requirements-moodle-aihs.txt; ansible/playbooks/configure-moodle-monitoring.yml -->

## 4.2. Hạ tầng dưới dạng mã

### 4.2.1. Terraform

Nguyên tắc cứng: Terraform sở hữu tài nguyên cloud, Ansible sở hữu cấu hình bên trong máy, GitHub Actions không chạy Terraform hay Ansible, và không build image trên EC2 (EC2 chỉ kéo image do CI tạo). Thư mục `terraform/` có 77 khối `resource` (gồm lane ERPNext tắt mặc định, ngoài phạm vi đánh giá). Bảng 4.2 tóm tắt theo tệp.

<!-- nguồn: docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §1; docs/BAO_CAO_HUONG_DAN_TRIEN_KHAI.md §1.2 -->

**Bảng 4.2.** Cấu trúc thư mục `terraform/`

| Tệp | Nội dung chính |
| --- | --- |
| `network.tf`, `security.tf`, `moodle-security.tf` | VPC hai AZ, subnet public/application/data/management, mỗi AZ một NAT Gateway, subnet data không có route ra Internet; luồng nội bộ tham chiếu security group thay vì CIDR |
| `alb.tf` | ALB internet-facing, target group cổng 8080, health check `/healthz.php` kỳ vọng 200, không sticky session |
| `moodle-compute.tf`, `compute.tf` | Hai node Moodle (`t3.small`, gp3 mã hoá, IMDSv2 bắt buộc, không public IP) và máy monitor; không có Auto Scaling Group |
| `rds.tf`, `efs.tf` | RDS PostgreSQL 16.10 `db.t4g.micro` Single-AZ, `rds.force_ssl`, mật khẩu master do RDS quản lý; EFS Regional mã hoá, access point `/moodledata`, `prevent_destroy = true` |
| `monitor-iam.tf` | Instance profile chỉ có `ec2:DescribeSecurityGroupRules` cho script kiểm tra security group của RDS |
| `application-outputs.tf`, `tests/` | Output sinh SSH config và inventory Ansible; bốn tệp `*.tftest.hcl` với provider giả |

<!-- nguồn: terraform/*.tf; docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §3–6 -->

Hai chi tiết hiện thực. Thứ nhất, EFS chứa `moodledata` được bảo vệ khỏi xóa nhầm bằng `prevent_destroy`; muốn gỡ hẳn phải tạm bỏ khối này có chủ ý (đã làm khi teardown ngày 17/11/2026, rồi khôi phục).

```hcl
resource "aws_efs_file_system" "moodledata" {
  encrypted = true
  lifecycle { prevent_destroy = true }
}
```

<!-- nguồn: terraform/efs.tf (rút gọn); docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §15 -->

Thứ hai, output Terraform là nguồn duy nhất của inventory Ansible và SSH config; repo không có inventory tĩnh. `moodle_ansible_inventory` bật `ansible_host_key_checking`, chia nhóm `monitor` và `moodle`, đặt `moodle_cron_enabled` chỉ cho node A; `moodle_ssh_config` sinh cấu hình `ProxyJump` qua `monitor-ai-01` với `StrictHostKeyChecking yes`. Khóa riêng ở lại máy vận hành. Hợp đồng hạ tầng được kiểm bằng `terraform test` với provider giả (node dùng subnet private và IMDSv2, ổ root mã hoá, ALB phủ hai subnet public với health check đúng, inventory bật kiểm tra host key); bốn tệp test có 22 khối `run`, báo cáo hạ tầng ghi 22/22 đạt ngày 25/10/2026. Kiểm thử này không tạo tài nguyên thật và không chứng minh trạng thái AWS hiện tại.

<!-- nguồn: terraform/application-outputs.tf; terraform/templates/moodle-ssh.tftpl; terraform/tests/*.tftest.hcl; docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §12.1 -->

### 4.2.2. Ansible

Các playbook trong `ansible/playbooks/` dùng inventory do Terraform sinh: `prepare-moodle-runtime.yml` (kiểm tra Amazon Linux 2023, cài Docker và plugin Compose cố định, mount EFS qua access point với TLS); `configure-moodle-monitoring.yml` (exporter và probe trên node; trên monitor: Prometheus, Alertmanager, Grafana, Blackbox, synthetic transaction, Agent, worker Celery, Redis, bearer token 256 bit cho webhook); `configure-moodle-safe-executor.yml` và `configure-moodle-safe-executor-api.yml` (người dùng `aiops-executor` với khóa SSH forced-command trên node; Executor API trên monitor, công tắc live tắt, ghim host key hai node, cài khóa công khai của Operator và Verifier); `baseline-remediation.yml` (đưa node về baseline sau drill).

<!-- nguồn: ansible/playbooks/*.yml -->

Ba điểm liên quan trực tiếp đến an toàn. (1) Secret không đi qua dòng lệnh hay image: Agent nhận token webhook, khóa HMAC của Executor và khóa **công khai** của Operator và Verifier qua tệp mount chỉ đọc; khóa riêng ở lại máy vận hành. (2) Công tắc live của Agent (`AIOPS_SAFE_ACTION_LIVE_ENABLED`) và của Executor (`EXECUTOR_LIVE_ENABLED`) mặc định tắt; playbook đòi xác nhận staging tường minh mới bật. (3) Agent chạy với `AIOPS_UNIFIED_CORE_MODE=investigate` và `AIOPS_APPROVAL_MODE=operator`.

<!-- nguồn: ansible/playbooks/configure-moodle-monitoring.yml; ansible/playbooks/configure-moodle-safe-executor-api.yml; agent_src/README.md -->

### 4.2.3. Image Moodle, Compose và giám sát

Image Moodle dựa trên `php:8.4-apache-bookworm` ghim bằng digest; mã Moodle 5.2.3 lấy từ một commit Git cố định và kiểm lại bằng `rev-parse`. Apache nghe cổng 8080, `DocumentRoot` là `/var/www/html/public`, tiến trình chạy bằng người dùng không phải root. Hai điểm kiểm tra công khai: `healthz.php` (trả 200, không truy cập DB, dùng cho ALB) và `synthetic-transaction.php` (yêu cầu đăng nhập, ghi và đọc một bản ghi `config_plugins` trong PostgreSQL cùng một tệp fixture trên EFS, nên chạm cả hai phụ thuộc dữ liệu). Mật khẩu DB đọc qua `MOODLE_DB_PASSWORD_FILE`; entrypoint từ chối khởi động nếu tệp thiếu và chuyển giá trị qua `stdin`, nên mật khẩu không nằm trong tham số dòng lệnh hay lớp image.

```bash
required_file MOODLE_DB_PASSWORD_FILE
db_password_literal="$(php -r '$value = rtrim(stream_get_contents(STDIN), "\r\n");
  if ($value === "") { exit(1); } echo var_export($value, true);' < "$MOODLE_DB_PASSWORD_FILE")"
```

<!-- nguồn: moodle/Dockerfile; moodle/healthz.php; moodle/synthetic-transaction.php; moodle/docker-entrypoint.sh (rút gọn) -->

Compose (`release/moodle/docker-compose.yml`) có `moodle-web`, `moodle-cron` (profile `cron`, chỉ node A để tránh chạy trùng tác vụ định kỳ) và `moodle-install` (profile `installer`, chạy một lần). Container chạy với `user: "1000:1999"`, `cap_drop: [ALL]`, `no-new-privileges`, secret mount `:ro`, PostgreSQL `verify-full` và healthcheck gọi `healthz.php`.

Về giám sát, `moodle-node-probes.sh` (timer mười giây sau đợt căn chỉnh ngày 16/11/2026) xuất metric theo node: container web, router Apache, TCP và DNS tới RDS, mount EFS, chế độ bảo trì, cron, ad-hoc task quá hạn, chế độ thư mục fixture. Giao dịch synthetic chạy từ monitor qua ALB theo timer một phút một lần. 23 alert rule được kiểm bằng `promtool check rules` mỗi lần triển khai. Quy tắc thiết kế quan trọng: **alert chỉ mô tả triệu chứng, không mang mã kịch bản**; danh tính kịch bản chỉ nằm trong marker root trên host để Agent không "nhìn đáp án".

<!-- nguồn: release/moodle/docker-compose.yml; ansible/files/moodle-monitoring/moodle-node-probes.sh; ansible/playbooks/configure-moodle-monitoring.yml; ansible/config/moodle_alert_rules.yml; docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §7–8; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §10–11 -->

## 4.3. CI/CD

### 4.3.1. Quy trình CI

Workflow `ci.yml` chạy khi push lên `feature/**`, `develop`, `main` và khi mở pull request vào `develop` hoặc `main`; một job thực hiện các bước ở Bảng 4.3 theo thứ tự và dừng ở bước lỗi đầu tiên.

**Bảng 4.3.** Các bước của `ci.yml`

| Bước | Kiểm tra |
| --- | --- |
| Terraform | `init -backend=false`, `fmt -check`, `validate`, `terraform test` (Terraform 1.15.8) |
| Lint, test | Python 3.11, `--require-hashes`; `ruff check agent_src --select E9,F63,F7,F82`; `pytest -q agent_src/tests` |
| Automation | `py_compile`, `bash -n`; `moodle-scenario-verify.sh SEC-01` **phải thoát mã 64** (kịch bản chỉ người xử lý bị từ chối) |
| Replay hợp nhất | 15 kịch bản, kết quả từng kịch bản được ghim (fail-closed cũng là đúng), 0 thực thi live, 0 tuyên bố phục hồi |
| Image Agent | Build; image **không chứa** `evaluation/ground_truth`; chế độ `live` phải escalate alert có `scenario_id` |
| Image Moodle | Build; đọc mật khẩu qua tệp; replay Sprint 2 (15 `VERIFIED_DRY_RUN`, `resolution_eligible` luôn sai); `healthz.php`, `synthetic-transaction.php`, `php -l`, `DocumentRoot` đúng |
| Compose, thông báo | `docker compose config`; gửi Telegram khi có bước lỗi |

<!-- nguồn: .github/workflows/ci.yml -->

Hai bước thực thi bất biến an toàn ngay trong CI. Bước replay ghim kết quả từng kịch bản: 11 kịch bản `fail_closed_missing_rollback`, CON-01 và RES-01 `verified_dry_run`, DB-01 `escalated_without_action`, SEC-01 `fail_closed_human_only`; 5 kịch bản thuộc allowlist live và 10 chỉ offline/shadow. Thay đổi planner hoặc catalog làm đổi các kết quả này sẽ làm CI đỏ và buộc phải xem lại. Bước kiểm tra image xác nhận image Agent không mang ground truth và đường live đóng:

```bash
docker run --rm local/ai-agent:ci python -c '
import os
from pathlib import Path
from core.moodle.alert_integration import process_moodle_alert
assert not Path("/app/evaluation/ground_truth").exists()
os.environ["AIOPS_UNIFIED_CORE_MODE"] = "live"
r = process_moodle_alert({"status": "firing", "labels": {
    "service": "moodle", "environment": "staging",
    "alertname": "MoodleSyntheticTransactionFailed", "scenario_id": "DB-01"}})
assert r["status"] == "escalated" and "ground-truth" in r["reason"]
assert r["execution_permitted"] is False'
```

<!-- nguồn: .github/workflows/ci.yml (định dạng lại cho dễ đọc) -->

CI chạy replay offline; nó không chứng minh phục hồi live.

### 4.3.2. Build image và CD staging

`agent-image.yml` và `moodle-image.yml` build image và đẩy lên GHCR với tag là commit SHA 40 ký tự (không dùng `latest`). `cd-staging.yml` chạy khi push `develop` đổi `moodle/**`, `release/moodle/**`, script deploy hoặc chính workflow: job `build-moodle` build và đẩy image; job `deploy-moodle` chạy trên **runner tự host** (nhãn `moodle-staging`, môi trường `staging`) đặt trong VPC vì hai node ở subnet private, nên không phải mở SSH từ runner của GitHub. Workflow build image chạy độc lập với CI, nên có image không có nghĩa là đã qua CI. Không có CD production.

Script `automation/github-deploy-moodle.sh` từ chối mọi tham chiếu image không có dạng `ghcr.io/<owner>/moodle:<40 ký tự hex>`, triển khai **node B trước** để luôn còn một target khỏe sau ALB, chọn service theo node (chỉ node A chạy `moodle-cron`) và kiểm tra `healthz` cục bộ lẫn qua ALB. Script không tự rollback; rollback là triển khai lại một SHA đã biết khỏe.

```mermaid
flowchart LR
  P["push hoặc pull request"] --> CI["ci.yml: lint, test, replay, kiểm tra image"]
  P --> IMG["agent-image.yml, moodle-image.yml: build và đẩy image theo SHA"]
  IMG --> R[("GHCR")]
  D["push develop đổi moodle hoặc release"] --> CD["cd-staging.yml: build-moodle"]
  CD --> R
  CD --> DEP["deploy-moodle: runner tự host trong VPC"]
  DEP --> N["github-deploy-moodle.sh: node B rồi node A"]
  R -. "EC2 chỉ kéo image" .-> N
```

**Hình 4.1.** Luồng CI/CD: image được build ngoài EC2 và EC2 chỉ kéo image bất biến

<!-- nguồn: .github/workflows/{agent-image,moodle-image,cd-staging}.yml; automation/github-deploy-moodle.sh; docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §10; docs/BAO_CAO_HUONG_DAN_TRIEN_KHAI.md §10.1 -->

## 4.4. Hiện thực AI Agent

### 4.4.1. Tổ chức mã và đường đi tổng thể

Thư mục `agent_src/core/` là runtime được đóng gói: hợp đồng dữ liệu ở `schema/` (Pydantic, `extra="forbid"`), logic riêng của Moodle ở `moodle/`, cùng `evidence/`, `safety/`, `verification/`, `rag/`, `coordination/`. Replay, bộ điều phối vai trò cũ và ground truth nằm trong `evaluation/` và không vào image. Hình 4.2 mô tả hai đường tách biệt: *đường cảnh báo* chỉ điều tra và đề xuất, không bao giờ thi hành; *đường xử lý* chỉ bắt đầu khi Operator gọi `prepare`.

<!-- nguồn: agent_src/core/README.md; agent_src/README.md -->

```mermaid
flowchart LR
  AM["Alertmanager"] -->|"POST /webhook, bearer token"| API["FastAPI: dedup, giới hạn hàng đợi"]
  API --> Q[("Redis: hàng đợi Celery, khóa cooldown")]
  Q --> W["Celery worker: process_alerts_task"]
  W --> INV["Điều tra chỉ đọc: Prometheus vào evidence store SQLite"]
  INV --> DX["Chẩn đoán theo luật và planner: candidate gắn catalog"]
  DX -. "không thi hành" .-> TG["Telegram: báo cáo cho Operator"]
  OP["Operator gọi prepare"] --> G["Safety Gate: kiểm tra, REQUIRE_APPROVAL"]
  G --> SIG["Operator ký Ed25519 gắn digest action"]
  SIG --> EX["Safe Executor API: HMAC, allowlist, SSH forced command"]
  EX --> AUD[("Audit SQLite chuỗi băm")]
  AUD --> V["Independent Verifier trên máy Operator"]
  V -->|"attestation Ed25519"| ACC["Agent kiểm tra attestation rồi ghi RESOLVED"]
```

**Hình 4.2.** Đường đi trong Agent: đường cảnh báo (không thi hành) và đường xử lý có Operator

### 4.4.2. Tiếp nhận cảnh báo, dedup và hàng đợi

Endpoint `POST /webhook` kiểm tra bearer token đọc từ tệp bằng `hmac.compare_digest`; nếu xác thực bắt buộc mà tệp token thiếu hoặc rỗng, endpoint trả 503 thay vì bỏ qua. Payload được chuẩn hóa rồi lọc trùng ở cửa vào bằng khóa Redis `alert-ingress-cooldown:<identity>` (`SET NX EX`). Danh tính cảnh báo là `fingerprint` của Alertmanager, nếu thiếu thì là hash của `alertname`, `instance`, `job`, `service`, `target`. Khi hàng đợi đầy, webhook trả 503 và nhả các khóa đã giữ để cảnh báo được gửi lại thay vì mất.

Worker Celery chạy các cảnh báo trong một vòng `asyncio.gather(return_exceptions=True)` có giới hạn song song và chỉ retry cả lô khi *tất cả* cảnh báo lỗi; trên monitor worker chạy `--concurrency=1`. Trước khi điều tra, worker giữ khóa `alert-ai-cooldown:<identity>` với TTL `ALERT_AI_COOLDOWN_SECONDS` (mặc định 900 s); nếu Redis lỗi, cooldown chuyển sang bộ nhớ trong tiến trình, trừ khi `ALERT_REDIS_STRICT` bật thì từ chối xử lý. Cảnh báo `resolved` xóa cooldown và chỉ tạo thông báo "hết firing", không phải `RESOLVED`.

<!-- nguồn: agent_src/core/main.py (prometheus_webhook, _filter_ingress_duplicates, _alert_identity); agent_src/core/tasks.py (process_alerts_task, _reserve_alert_processing); agent_src/core/celery_app.py; ansible/playbooks/configure-moodle-monitoring.yml -->

### 4.4.3. Điều tra bằng chứng, chẩn đoán và planner

Cảnh báo `firing` của Moodle đi vào `process_moodle_alert`, chỉ xử lý môi trường staging và chạy theo `AIOPS_UNIFIED_CORE_MODE`: `shadow` (thu bằng chứng, chẩn đoán), `investigate` (thêm đề xuất) hoặc `live` (đóng, luôn escalate); giá trị không hợp lệ rơi về `off`. Cảnh báo không thuộc Moodle không rơi vào trợ lý chung hay Executor; mọi kết quả đều mang `execution_permitted=False` và `resolution_eligible=False`.

Ở chế độ `investigate`, `run_shadow_if_enabled` sinh `incident_id` ổn định từ fingerprint và `startsAt`, ghi bằng chứng cảnh báo vào kho SQLite, giành quyền xử lý bằng `claim_checkpoint_run` (hai worker không điều tra cùng một incident), thu bằng chứng Prometheus bằng truy vấn chỉ đọc, chẩn đoán, lập kế hoạch điều tra chỉ đọc và, nếu đủ điều kiện, một *candidate* sửa lỗi gắn catalog; quyết định được lưu thành bằng chứng `ai_decision`. Mỗi bằng chứng có `content_hash` SHA-256; kho kiểm tra toàn vẹn trước khi dùng và lỗi toàn vẹn làm dừng chẩn đoán. Chẩn đoán (`diagnosis.py`) là bộ luật bảo thủ: chỉ xử lý alert đã có luật được duyệt, đòi quan sát bắt buộc còn tươi (tuổi tối đa 120 s) và trả `insufficient_evidence` khi thiếu hay quá cũ thay vì đoán. Planner (`planner.py`) chỉ tạo candidate khi các quan sát nằm trong cửa sổ 60 s, không trùng lặp theo node và khớp đúng dấu hiệu của fixture đã duyệt; SEC-01 luôn là `human_only_escalation`. Cả hai không đọc ground truth.

<!-- nguồn: agent_src/core/moodle/alert_integration.py; agent_src/core/shadow_pipeline.py; agent_src/core/evidence/evidence_store.py; agent_src/core/moodle/diagnosis.py; agent_src/core/moodle/planner.py -->

### 4.4.4. Action catalog và Safety Gate

`ActionCatalog` định nghĩa từng action bằng một mô hình có kiểu: kiểu action, adapter, vai trò yêu cầu, phạm vi môi trường, `blast_radius`, khả năng đảo ngược, action rollback đối ứng, timeout và loại tài nguyên đích. `is_safe_execution_allowed` chỉ đúng khi bộ ba *(kịch bản, action, phạm vi)* nằm trong bảng đã duyệt, môi trường khớp profile và vai trò hợp lệ.

`SafeExecutionGate.evaluate` trả `ALLOW`, `REQUIRE_APPROVAL` hoặc `DENY` kèm digest SHA-256 của các trường liên quan thi hành; các kiểm tra chạy theo thứ tự và dừng ở lỗi đầu tiên (Bảng 4.4). Ngưỡng nằm trong profile môi trường đã duyệt, không rải rác trong mã.

**Bảng 4.4.** Nhóm kiểm tra của Safety Gate (staging)

| Nhóm | Nội dung | Giá trị staging |
| --- | --- | --- |
| Hợp lệ, công tắc | Schema; kill switch tắt; độ tin cậy hữu hạn trong [0, 1]; idempotency key chưa dùng | – |
| Bằng chứng, tham số | Số tham chiếu bằng chứng khác nhau tối thiểu; không nhận tham số từ caller | ≥ 3; không có tham số |
| Catalog, phạm vi | Action có trong catalog, timeout trong giới hạn, rollback đối ứng đã duyệt; bộ ba kịch bản/action/phạm vi, môi trường, vai trò, tài nguyên đích đều thuộc biên đã duyệt | – |
| Độ tin cậy | Độ tin cậy chẩn đoán tối thiểu | ≥ 0,65 |
| Phê duyệt | Cần chữ ký nếu action yêu cầu, catalog mặc định yêu cầu, độ tin cậy < 0,8 hoặc blast radius không phải LOW; chữ ký gắn đúng digest, actor, TTL ≤ 900 s | ngưỡng tự động 0,8 |

<!-- nguồn: agent_src/core/safety/safe_execution_gate.py; agent_src/config/environment_profiles/staging.json -->

```python
if request.action.parameters:
    return self._deny(action_hash, "caller parameters are not supported by the fixed staging executor")
entry = self._catalog.lookup(request.catalog_action_id)
if entry is None or request.timeout_seconds > entry.timeout_seconds:
    return self._deny(action_hash, "catalog action is unknown or requested timeout exceeds its limit")
...
approval_required = (request.action.requires_approval or bool(entry.default_requires_approval)
                     or confidence < policy.gate.auto_confidence or entry.blast_radius != "LOW")
```

<!-- nguồn: agent_src/core/safety/safe_execution_gate.py (evaluate, rút gọn) -->

Gate cũng có `explain` và `diagnose` chỉ để giải thích cho Operator (quy tắc `HARD` cần đổi catalog/policy, `REMEDIABLE` có thể khắc phục bằng bằng chứng mới); chúng không bao giờ cấp quyền. Idempotency ở Gate nằm trong bộ nhớ; bảo đảm bền vững nằm ở Executor.

### 4.4.5. Handoff cho Operator và chữ ký gắn action

Đường xử lý gồm hai endpoint có bearer riêng: `/api/moodle/remediation/{candidate_id}/prepare` và `/execute`. `prepare` **tính lại** chẩn đoán và kế hoạch từ bằng chứng đã lưu, chỉ chấp nhận candidate nếu kết quả mới bằng đúng candidate cũ, rồi trả yêu cầu bất biến cùng hai digest và bản xem trước của Gate, với `execution_permitted=False`. `execute` kiểm lại độ tươi: nếu bằng chứng cũ, nó thu lại quan sát chỉ đọc và đòi cùng action, cùng đích; yêu cầu Operator gửi phải bằng đúng yêu cầu hiện tại.

Chữ ký của Operator gắn với `action_digest`, SHA-256 của JSON chính tắc gồm môi trường, kịch bản, `catalog_action_id`, phạm vi đích và idempotency key. Ở chế độ `operator` (cấu hình trên staging), Operator ký **một** chữ ký Ed25519 trên digest này, Gate và Executor cùng kiểm lại bằng khóa công khai; chế độ `two_person` (mặc định của mã) đòi hai chữ ký từ hai actor khác nhau. Cầu nối Telegram (`moodle-telegram-approval.py`) chỉ ghi nhận *quyết định của người* (một tin `approve` từ đúng chat và đúng người dùng, bỏ qua tin cũ, tin trả lời nhầm và yêu cầu chạy đồng thời); khóa riêng ở máy vận hành và tin chat tự nó không phải chữ ký.

```python
immutable_action = {"environment": "staging", "scenario_id": scenario_id,
    "catalog_action_id": catalog_action_id, "target_scope": target_scope,
    "idempotency_key": idempotency_key}
return hashlib.sha256(_canonical(immutable_action)).hexdigest()
```

<!-- nguồn: agent_src/core/main.py; agent_src/core/moodle/live_handoff.py; agent_src/core/safety/safe_executor_client.py (action_digest, rút gọn); automation/moodle-telegram-approval.py; ansible/playbooks/configure-moodle-monitoring.yml -->

### 4.4.6. Safe Executor

Executor là một API HTTP nhỏ (`automation/moodle-safe-executor-api.py`) chạy trên monitor, chỉ nghe trên địa chỉ cầu Docker riêng, cổng 8765. Mỗi `POST /v1/execute` qua các lớp theo thứ tự: (1) thân ≤ 16 KiB và JSON hợp lệ; (2) HMAC-SHA256 trên phương thức, đường dẫn, timestamp (lệch ≤ 30 s), nonce và hash thân; (3) tập trường đúng bằng bộ trường yêu cầu, môi trường `staging`; (4) bộ ba *(kịch bản, action, phạm vi)* thuộc `CATALOG` cố định, với danh sách node đích do server quyết định; (5) digest khớp; (6) chữ ký Operator hợp lệ cho đúng digest; (7) công tắc `EXECUTOR_LIVE_ENABLED`; (8) giành nonce và idempotency key bằng giao dịch SQLite trước mọi thay đổi. Công tắc live được kiểm **trước** bước giành khóa nên yêu cầu bị chặn vì công tắc tắt không "đốt" idempotency key. Phản hồi cũng được ký HMAC; client của Agent từ chối phản hồi không xác thực được.

Việc thi hành đi qua SSH tới người dùng `aiops-executor` trên từng node, tuần tự, mỗi node tối đa 45 s. Khóa SSH có tùy chọn `command=` buộc chạy một script chỉ nhận tên action trong allowlist rồi gọi wrapper `sudo` cố định; chuỗi khác bị từ chối với mã 64. Executor không nhận lệnh shell tự do từ mô hình hay Telegram.

```text
authorized_keys: command="/usr/local/sbin/moodle-safe-executor-force",no-agent-forwarding,no-port-forwarding,no-pty ...
case "${SSH_ORIGINAL_COMMAND:-}" in
  CON-01|start_reviewed_compose_service|reconcile_con01_safe_baseline) scenario=CON-01 ;;
  ...
  *) echo 'forced command rejected' >&2; exit 64 ;;
esac
```

<!-- nguồn: automation/moodle-safe-executor-api.py; ansible/playbooks/configure-moodle-safe-executor.yml (rút gọn) -->

Receipt có trạng thái `executed`, `partial_failure` hoặc `failed`, danh sách node hoàn tất và cờ `mutated`; khi lệnh lỗi mà chưa rõ có thay đổi hay chưa, receipt ghi `mutation_state: unknown` để Operator xem xét một compensation đã duyệt. Phía Agent, `SafeActionLiveWorkflow` *chuẩn hóa nghiêm* receipt: node ngoài phạm vi, cờ `mutated` mâu thuẫn với danh sách node hoặc trạng thái lạ đều thành `invalid_response` và incident chuyển người thay vì sang xác minh.

<!-- nguồn: agent_src/core/safety/safe_executor_client.py; agent_src/core/safety/safe_action_live.py (_normalize_executor_result) -->

### 4.4.7. Audit chuỗi băm, Independent Verifier và trạng thái RESOLVED

Mọi bước của đường xử lý được ghi vào `SafeActionAuditStore`: bảng SQLite append-only với hai trigger hủy mọi `UPDATE` và `DELETE`; mỗi sự kiện chứa `previous_hash` và `event_hash` (SHA-256 của JSON chính tắc) theo từng incident; khóa có tên nhạy cảm bị che trước khi ghi; `verify_chain` kiểm tra lại toàn chuỗi.

Independent Verifier (`automation/moodle-independent-verifier.py`) chạy **ngoài Agent**, trên máy Operator, không có credential của Executor và chỉ mở audit ở chế độ chỉ đọc. Nó kiểm tra chuỗi audit và đòi đúng một receipt `executed` với `mutated=true` khớp incident, request và kịch bản; lấy mẫu tại 0, 60 và 120 s bằng script probe chỉ đọc `moodle-scenario-verify.sh`; đòi tập check trả về khớp *chính xác* hợp đồng; kiểm lại audit cuối cửa sổ; dựng `VerificationResult` bằng `VerificationAgent`; rồi ký payload bằng khóa Ed25519 riêng. Hợp đồng có ba nhóm probe: *allowed* (dịch vụ cần hoạt động), *forbidden* (đường phải bị chặn), *related* (thành phần và alert liên quan). Bảng 4.5 cho hợp đồng của sáu kịch bản live; probe `internet_to_postgresql_blocked` dựa trên một Route 53 health check TCP từ Internet tới RDS (phải *thất bại* thì probe mới đạt), còn `both_alb_targets_healthy` so tập target của ALB với đúng các node trong output Terraform.

**Bảng 4.5.** Hợp đồng probe của Verifier; mọi kịch bản chung allowed = `common_synthetic_transaction` và forbidden = `internet_to_postgresql_blocked`

| Kịch bản | Nhóm related |
| --- | --- |
| DB-01 | `scenario_alert_resolved`, `moodle-app-a_database_tls`, `moodle-app-b_database_tls`, `both_alb_targets_healthy` |
| NET-01 | `scenario_alert_resolved`, `moodle-app-a_database_dns`, `moodle-app-b_database_dns`, `both_alb_targets_healthy` |
| RES-01 | `scenario_alert_resolved`, `named_cpu_fixture_absent` |
| CON-01 | `scenario_alert_resolved`, `moodle_app_b_container_healthy`, `both_alb_targets_healthy` |
| SEC-02 | `scenario_alert_resolved`, `fixture_directory_mode_restored`, `efs_mount_metric_is_one` |
| BIZ-02 | `scenario_alert_resolved`, `moodle_cron_service_active`, `overdue_adhoc_tasks_zero` |

<!-- nguồn: agent_src/core/verification/live_contracts.py; automation/moodle-independent-verifier.py; automation/moodle-scenario-verify.sh; docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §6.1 -->

Phía Agent, `POST /api/moodle/verifications/{request_id}` gọi `accept_verifier_attestation`, kiểm tra chữ ký bằng khóa công khai đã cài, tuổi attestation (≤ 120 s), ràng buộc với request và hash receipt, đủ mẫu và probe theo hợp đồng, cửa sổ ổn định ≥ 120 s với tối thiểu 3 mẫu và khe giữa hai mẫu không quá 65 s (3 mẫu) hoặc 60 s. Agent còn đòi mọi mẫu được lấy *sau* thời điểm ghi receipt và audit có verdict `ALLOW` của Gate đứng trước receipt. Kể cả khi đó, audit chỉ là bản ghi: `get_live_incident_status` *kiểm lại chữ ký* mỗi lần tính trạng thái, nên một dòng audit tự khai "resolved" mà thiếu chữ ký hợp lệ không được tính là phục hồi. Ở tầng trạng thái, `IncidentStateMachine.transition` từ chối chuyển sang `RESOLVED`; chỉ `resolve_verified` làm được, và chỉ với kết quả Verifier đầy đủ:

```python
if (verification.incident_id != incident.incident_id
    or verification.verdict != "resolved"
    or not verification.resolution_eligible
    or not verification.health_passed
    or not _has_stable_window(verification)
    or not _has_complete_probe_sets(verification)):
    raise StateTransitionError(incident.status, IncidentStatus.RESOLVED,
        "independent verifier result is missing, ineligible, or failed")
```

<!-- nguồn: agent_src/core/verification/state_machine.py (rút gọn); agent_src/core/safety/verifier_attestation.py; agent_src/core/safety/safe_action_audit.py; agent_src/config/environment_profiles/staging.json -->

### 4.4.8. Telegram, RAG và giới hạn dùng mô hình ngôn ngữ

Webhook Telegram chỉ chấp nhận tin từ `TELEGRAM_CHAT_ID` đã cấu hình; admin gửi `/feedback <incident_id> <nội dung>`. Gemini chỉ được dùng để **review góp ý của admin**, không để định tuyến alert, chẩn đoán hay sửa lỗi. Dữ liệu đưa vào Gemini được khai trong system prompt là không đáng tin; lời gọi tắt gọi hàm từ xa (`maximum_remote_calls=0`); góp ý có thao tác phá hủy hoặc đổi quyền bị từ chối bằng luật tất định *trước* khi gọi mô hình; mô hình lỗi hoặc sai schema thì dùng review tất định. Chỉ góp ý được chấp nhận hoặc chỉnh sửa mới được lưu vào `incident_memory` của ChromaDB; `standard_runbooks` lấy từ `config/knowledge_base/*.md`, chia theo tiêu đề và nạp lúc khởi động. Không có lời gọi mô hình ngoài nào trên đường alert.

<!-- nguồn: agent_src/core/tasks.py (review_admin_feedback, process_admin_feedback); agent_src/core/rag/rag_engine.py; AGENTS.md -->

## 4.5. Bộ cấy lỗi, reset và probe

Các kịch bản live dùng bộ script `automation/moodle-fault-inject.sh`, `moodle-fault-reset.sh`, `moodle-scenario-verify.sh`, `moodle-environment-baseline.sh` và thư viện `lib/moodle-fault-common.sh`. Injector và reset là cặp đã duyệt: cả hai script từ chối kịch bản ngoài danh sách (mã 77); injector đòi `MOODLE_FAULT_CONFIRM=staging` (riêng SEC-01, đổi security group thật, đòi thêm một biến xác nhận); lỗi chỉ đụng đối tượng có tên hoặc tag riêng và reset là idempotent. Bảng 4.6 nêu cơ chế cấy của sáu kịch bản dùng trong chiến dịch AIHS.

**Bảng 4.6.** Cơ chế cấy lỗi và action sửa của sáu kịch bản live

| Kịch bản | Cách cấy lỗi | Action sửa trong catalog |
| --- | --- | --- |
| DB-01 | Rule iptables `REJECT` tới cổng 5432 của RDS trong chuỗi `DOCKER-USER`, gắn comment `moodle-fault-DB-01`, trên hai node | `remove_scoped_db_reject` |
| NET-01 | Ghi đè endpoint RDS về `127.0.0.1` (đánh dấu `moodle-fault-NET-01`) trong `/etc/hosts` của container web, hai node | `recreate_moodle_web_from_reviewed_compose` |
| RES-01 | Container `moodle-fault-res-01` tải CPU (1,8 CPU, 128 MiB) trên node B | `remove_named_cpu_load_container` |
| CON-01 | Dừng container `moodle-web` trên node B | `start_reviewed_compose_service` |
| SEC-02 | Đổi quyền thư mục fixture trên EFS về `0000` | `restore_fixture_directory_mode` |
| BIZ-02 | Dừng `moodle-cron` trên node A và xếp một ad-hoc task đã quá hạn | `restart_moodle_cron_service` |

<!-- nguồn: automation/moodle-fault-inject.sh; evaluation/aihs/action_ladder.py -->

`moodle-scenario-verify.sh` chạy các probe chỉ đọc theo kịch bản và in một đối tượng JSON gồm danh sách check `passed`/`failed`, ràng buộc với mã kịch bản và mã drill; đây là nguồn check chung của Verifier và luồng chấm điểm của harness. SEC-01 không có đường live: script từ chối với mã 64 và CI kiểm đúng điều này. Cho thí nghiệm bổ sung, `aihs-collateral.sh inject|reset web_a_stop` cấy *lỗi phụ có kiểm soát*: dừng `moodle-web` trên node A (khác node mà CON-01 vừa sửa), để ALB vẫn phục vụ qua node B và health vẫn có thể xanh trong khi probe `both_alb_targets_healthy` phải thất bại. Chỉ harness gọi script này, sau khi Executor chạy xong và kết luận health-only đã được ghi.

<!-- nguồn: automation/moodle-scenario-verify.sh; automation/aihs-collateral.sh; .github/workflows/ci.yml; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §16 -->

## 4.6. Hiện thực AIHS trên Moodle

Phần này mô tả công cụ nghiên cứu trong `evaluation/aihs/` và `lab/moodle-local/`: mã ngoại tuyến, không nằm trong image Agent; đầu ra mô hình luôn là đề xuất không đáng tin và không thành phần nào ở đây thi hành action. Hình 4.3 là toàn bộ quy trình.

```mermaid
flowchart TD
  L["Lab Moodle cục bộ: lab_runner"] --> C["lab_collector: snapshot trước quyết định"]
  C --> E["a2_export --source lab: bản ghi theo schema A2"]
  E --> LB["label_tool: gán nhãn mù"]
  LB --> F["b2_freeze_dataset: freeze, kiểm tra rò rỉ"]
  F --> S["train_moodle_aihs split: train, validation, test"]
  S --> T["train: Mapper LDA, Predictor, Controller"]
  T --> K["lock: model-lock.json"]
  K --> P["propose: đề xuất trên test, không mở nhãn"]
  P --> O["score_c0_c3_offline: khóa protocol, mở test một lần"]
  K --> H["live_c0_harness: cấy lỗi, Gate, Operator, Executor, Verifier"]
  H --> A["aihs-live-aggregate: tổng hợp từ run.json"]
```

**Hình 4.3.** Quy trình hiện thực AIHS: từ lab đến chấm offline và chạy live

### 4.6.1. Lab Moodle cục bộ

Lab là một stack Docker Compose mô phỏng staging đủ sát để dùng lại injector, probe và ngữ nghĩa catalog: PostgreSQL 16 (thay RDS), hai node web dùng chung volume `moodledata` (thay EFS), cron ở node A, HAProxy với health check `/healthz.php` (thay ALB), Prometheus với alert rule của staging, giao dịch synthetic nguyên bản và probe theo node xuất các metric `moodle_node_*` như staging; `lab.sh` điều khiển vòng đời (`build`, `up`, `run`, `batch`, `fleet`).

Một episode (`lab_runner.py`) gồm: kiểm baseline, tải nền tùy chọn, cấy một lỗi (hoặc cặp lỗi mơ hồ/chồng lấn), chờ phát hiện, **chụp snapshot trước mọi quyết định**, chọn action theo chính sách hành vi (action khớp cộng khám phá có kiểm soát trong danh sách đã duyệt), thi hành qua `lab_executor.py` (cài lại ngữ nghĩa Executor của staging cho sáu action đã duyệt), probe allowed/forbidden/related trong cửa sổ ổn định, phân loại outcome, reset và kiểm lại baseline. Outcome gồm `success`, `failure`, `not_executed`, `denied`, `timeout`, `cancelled`, `missing_evidence`; chỉ `success` và `failure` có thể thành nhãn huấn luyện. Không gian action của bộ dự đoán nằm ở một nơi duy nhất, `action_ladder.py`, theo chi phí tăng dần: `wait_and_recheck` < `restart_moodle_cron_service` < `start_reviewed_compose_service` < `recreate_moodle_web_from_reviewed_compose` < {`remove_named_cpu_load_container`, `remove_scoped_db_reject`, `restore_fixture_directory_mode`} < `call_human`; action ngoài danh sách làm hàm `spec` nêu lỗi thay vì dùng mặc định.

Lab khác staging ở các điểm sau: PostgreSQL không bật TLS; không có Safety Gate, Operator hay Independent Verifier (phê duyệt là khóa tự động, probe ổn định thay Verifier), nên `gate_decision` là null và `operator_approved` là sai trong bản ghi lab; CPU lấy từ `docker stats`; metric `moodle_fault_*` không được xuất; Prometheus scrape mỗi 5 s. Vì vậy dữ liệu lab mang evidence mode `lab` và không bao giờ cộng gộp với staging, replay hay dữ liệu tổng hợp. Collector (`lab_collector.py`) ghi metric, alert đang firing và log đã che kèm `detected_at`, `evidence_cutoff_at`, thời điểm chụp và SHA-256 từng phần *trước khi* đề xuất ra đời; nó ép `evidence_cutoff_at <= predicted_at` và từ chối marker kịch bản, mã lượt chứa đáp án, ground truth, chữ ký và trường hậu quyết định trong đặc trưng lẫn bằng chứng thô.

<!-- nguồn: lab/moodle-local/lab.sh; lab/moodle-local/docker-compose.yml; lab/moodle-local/lab_runner.py; lab/moodle-local/lab_collector.py; evaluation/aihs/action_ladder.py; agent_src/README.md -->

### 4.6.2. Exporter A2, schema và công cụ gán nhãn

Schema `a2-record.schema.json` quy định **một dòng là một ứng viên action của một incident hoặc phiên**: group ID ẩn danh, provenance, các mốc thời gian, khối `pre_decision` (snapshot, hash bằng chứng, đặc trưng số), đề xuất và khối `post_decision` (Gate, duyệt, action đã chạy, receipt, Verifier). `a2_export.py --source lab` đọc artifact lab, kiểm checksum từng episode và xuất bản ghi hợp schema. Exporter dùng allowlist trường và một regex tên đặc trưng bị cấm (kịch bản, ground truth, outcome, verifier, gate, approval, receipt, action, run id, fault, nhãn…), đồng thời loại IP, ARN, email, khóa.

Nhãn do người gán, độc lập với runner. `label_tool.py` phục vụ giao diện web chỉ nghe trên `127.0.0.1`, mỗi màn hình một episode, đọc từ một **danh sách trắng** tệp (snapshot trước quyết định, action đã chạy và receipt, các vòng probe sau action). `episode.json` (kịch bản, lỗi cấy, outcome của runner, chính sách hành vi) và mọi dự đoán của Gate hay mô hình không bao giờ vào dữ liệu hiển thị, và JSON gửi đi được quét lại bằng `assert_blind`. Câu hỏi Q1 (action đề xuất có an toàn và đúng phạm vi: `yes`/`no`/`unsure`) và Q2 (hệ thống có phục hồi thật không) được ghi nguyên tử sau mỗi câu trả lời. Công cụ có lệnh `agreement` tính Cohen's kappa giữa hai người gán; trong khóa luận chỉ có một người gán nhãn (Chương 5 và 6).

`b2_freeze_dataset.py` đóng băng tập đánh giá hoặc **thất bại có kiểm soát**: nó kiểm lại tên đặc trưng chống rò rỉ, đối chiếu nhãn độc lập với outcome quan sát (khác nhau thì ghi lý do loại `independent_label_disagrees_with_observed_outcome` thay vì ném lỗi) và khi thiếu dữ liệu chỉ ghi manifest `blocked`, không tạo tệp chia tập giả. Tập đóng băng của chiến dịch chính có 277 dòng train, 57 validation và 59 test, chia theo thời gian và theo nhóm incident/phiên; nhóm chồng lấn thời gian qua ranh giới chia bị loại khỏi tập sớm hơn (`purge.py`) thay vì để rò rỉ.

<!-- nguồn: evaluation/aihs/a2-record.schema.json; evaluation/aihs/a2_export_lab.py; evaluation/aihs/label_tool.py; evaluation/aihs/b2_freeze_dataset.py; evaluation/aihs/moodle_aihs/purge.py; docs/BAO_CAO_AIHS.md §4.2–4.3; docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §1 -->

### 4.6.3. Huấn luyện và khóa mô hình

Gói `moodle_aihs/` hiện thực Mapper, Predictor và Controller theo kiến trúc [AIHS2021] trên đặc trưng Moodle. `features.py` rời rạc hóa mỗi thuộc tính thành sáu mức bằng ngưỡng phân vị **chỉ lấy từ tập train** và tạo "tài liệu" trong đó tên thuộc tính lặp lại theo mức; `mapper.py` dùng `CountVectorizer` và `LatentDirichletAllocation` của scikit-learn (α = 0,1, β = 0,01, học theo lô), từ vựng và LDA chỉ khớp trên train. Predictor là LightGBM đa lớp trên phân bố chủ đề; mỗi action có một Controller LightGBM nhị phân, chỉ huấn luyện khi có ít nhất hai dòng thành công và hai dòng thất bại (action thiếu dữ liệu được ghi `controller_missing`). Siêu tham số LightGBM: 200 cây, `learning_rate` 0,05, `num_leaves` 8, `min_child_samples` 2, `class_weight="balanced"`. Số chủ đề K ∈ {5, 10, 20} được chọn **chỉ trên validation**.

Hai baseline không bao giờ trộn kết quả: `aihs-paper-repro-moodle-v1` (C0 của luận văn: Predictor chỉ học trên episode thành công, không thoáng qua; Controller dự phòng thử các action còn lại theo chi phí tăng dần) và `aihs-public-prototype-as-is-v1` (chỉ để tham chiếu, giữ lỗi nhánh dự phòng của prototype công khai: đề xuất bị Controller từ chối thì chuyển thẳng `call_human`). Các khác biệt so với bài báo được ghi trong `model-lock.json`: LDA dùng variational Bayes của scikit-learn thay vì Gibbs sampling; sáu mức là phân vị theo dữ liệu thay vì mức do người vận hành gán. Lệnh `lock` ghi SHA-256 của tệp và nội dung mô hình, định danh mã, phiên bản thư viện, seed, K, đặc tả đặc trưng và SHA-256 của manifest chia tập; việc nạp mô hình luôn kiểm khóa trước (`pickle` chỉ được giải tuần tự hóa với tệp đã xác minh). Nhãn tập test được **niêm phong** trong mã: `Splits.rows("test")` ném `SealedTestError` nếu không có khóa; `propose` chỉ đọc đặc trưng và hash snapshot test, kiểm thời điểm dự đoán không trước `evidence_cutoff_at`, ghi đề xuất mà không mở nhãn hay chấm. `repro-check` huấn luyện lại từ đầu và so hash mô hình lẫn đề xuất. Mô hình C0 được khóa có K = 20 và `model_content_sha256` bắt đầu bằng `0da82b58`.

<!-- nguồn: evaluation/aihs/moodle_aihs/{features,mapper,model,pipeline,data}.py; agent_src/README.md; docs/BAO_CAO_AIHS.md §3.1, §3.3; docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §1 -->

### 4.6.4. Scorer offline với khóa sha256 và marker mở test một lần

`score_c0_c3_offline.py` hiện thực protocol `c0-c3-offline-v1` đã đăng ký trước. `lock-protocol` ghi `protocol-lock-<arm>.json` chứa SHA-256 của tài liệu protocol, của chính scorer, của mười một tệp mã bị khóa (adapter, Gate, catalog, metrics…) và của mọi artifact đầu vào (mô hình, proposal, split, nhãn Q1), và từ chối ghi đè. Lệnh `test` kiểm lại mọi hash, **tạo marker một lần rồi mới đọc nhãn test**; nếu marker đã tồn tại hoặc một hash đổi, scorer thoát với `PROTOCOL REFUSED` (mã 6).

```python
lock = verify_lock(arm_name, arm, phase4=phase4, root=root, files=files)
try:
    with open(marker, "x", encoding="utf-8") as handle:      # "x": thất bại nếu đã tồn tại
        handle.write(json.dumps({"opened_at": now_iso(), "arm": arm_name}) + "\n")
except FileExistsError as exc:
    raise ProtocolError("the test split was already opened for this arm; it is opened exactly once") from exc
# chỉ sau đó mới đọc nhãn: splits.rows("test", {"protocol_lock": lock["locked_at"]})
```

<!-- nguồn: evaluation/aihs/score_c0_c3_offline.py (run_test, rút gọn); docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §3 (D6), §7–8 -->

Tầng Gate của C1 chỉ nhận *proposal* và *đặc trưng trước quyết định*; outcome, nhãn, ground truth và xác suất `p_success` của C0 không bao giờ tới Gate. Ngữ cảnh tin cậy do `derive_context` suy ra chỉ từ đặc trưng alert đang firing; ≥ 2 ứng viên hoặc không có ứng viên thì chặn và chuyển người (fail closed). Độ tin cậy đưa vào Gate là hằng cố định 0,9; đồng hồ quyết định là `evidence_cutoff_at + 1 s`, đồng hồ batch chỉ là đối chứng. C2 và C3 ngoại tuyến được khai báo `OFFLINE_EQUALS_C1_BY_CONSTRUCTION` vì offline không có Operator, Executor hay probe phục hồi, và mọi chỉ số phục hồi là `NOT_MEASURABLE`. Khoảng tin cậy là bootstrap theo nhóm liên thông (B = 2000, seed `20261116`).

<!-- nguồn: evaluation/aihs/score_c0_c3_offline.py; docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §2–4 -->

### 4.6.5. Harness live C0–C3

`live_c0_harness.py` (`live-c0-harness-v1`) điều phối từng lượt live trên staging. Từ **cùng một snapshot trước quyết định**, mọi lượt ghi góc nhìn C0 (đề xuất của mô hình đã khóa, "sẽ thi hành nguyên trạng", không bao giờ thi hành) và góc nhìn C1 (adapter và Safety Gate thật). Chỉ nhánh C2/C3 chạm staging: C2 kết luận phục hồi bằng *health-only* (healthz 200, giao dịch synthetic đạt, alert hết) và không bao giờ ghi `RESOLVED`; C3 thêm Independent Verifier. Theo quyết định đã ghi, mọi lượt thi hành đều chạy ở nhánh C3, còn C2 được tính khi phân tích từ `health_only_view` của chính lượt đó (cùng action, cùng thời điểm).

Mã chia thành ba tệp để kiểm thử được ngoại tuyến. `live_c0_sim.py` cung cấp đồ giả (Prometheus, lỗi, Executor, probe) nhưng giữ **thật** mô hình đã khóa, adapter, Safety Gate, `SafeActionLiveWorkflow`, chuỗi audit, `verify_window` của Verifier và `accept_verifier_attestation` của Agent. `live_c0_live.py` chứa backend live: chỉ gọi lại các script đã duyệt (cấy lỗi, reset, probe, baseline, Verifier, xuất audit) và nói chuyện với Agent qua tunnel SSH do wrapper mở; `KeyFileOperator` hiển thị *thẻ duyệt* chỉ chứa bằng chứng trước quyết định (action, phạm vi, alert đang firing đánh dấu khớp/triệu chứng/khác, bản xem trước của Gate), không bao giờ có lỗi đã cấy, rồi ký bằng khóa Ed25519 khi Operator gõ `APPROVE <12 ký tự đầu của digest>`; gõ `REJECT` thì không thi hành. Wrapper `aihs-live-c0.sh` đòi `AIHS_LIVE_CONFIRM=staging`, chỉ bật công tắc live của Agent và Executor cho nhánh C2/C3 và **luôn tắt lại** trong `trap` khi thoát. Preflight báo `READY` hoặc `BLOCKED` mà không thay đổi gì, với ba nhóm kiểm tra: toàn vẹn (khóa protocol, hash mô hình, mapping đã duyệt, allowlist Executor khớp mapping, hợp đồng Verifier phủ mapping, phiên bản thư viện khớp khóa mô hình, khoảng trống lược đồ metric giữa lab và staging), hạ tầng, và máy Operator (khóa riêng khớp khóa công khai đã cài, tệp secret).

Mỗi lượt ghi `run.json` và `manifest.json` (SHA-256 từng tệp) với từ vựng outcome cố định (ví dụ `RESOLVED_BY_VERIFIER`, `VERIFIER_NOT_RESOLVED`, `OPERATOR_REJECTED`, `HUMAN_HANDOFF`, `HANDOFF_BLOCKED`) và năm bất biến được kiểm sau mỗi lượt; một vi phạm là dừng chiến dịch. Kế hoạch chạy do `build_plan` sinh theo khối ngẫu nhiên hóa (seed `20261116`). Ground truth chỉ đi vào `inject`, `reset` và probe chấm điểm; hàm `decide` không có tham số nào mang đáp án.

```python
return {
    "resolved_only_via_verifier": (not rec["resolved"]) or (rec["arm"] == "C3"
        and rec["resolved_by"] == "independent_verifier_attestation"
        and (rec.get("runtime_status") or {}).get("status") == "resolved"),
    "shadow_arms_never_execute": rec["arm"] in EXECUTING_ARMS or rec.get("execution") is None,
    "no_execution_without_approval": (not executed) or (rec.get("operator") or {}).get("decision") == "approved",
    "approval_bound_to_prepared_digest": ...,   # chữ ký Operator khớp executor_action_sha256 đã chuẩn bị
    "c2_never_resolved": rec["arm"] != "C2" or not rec["resolved"],
}
```

<!-- nguồn: evaluation/aihs/live_c0_harness.py (check_invariants, rút gọn); evaluation/aihs/live_c0_sim.py; evaluation/aihs/live_c0_live.py; automation/aihs-live-c0.sh; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §1, §3–5 -->

Sau lượt chạy, `automation/aihs-live-summary.py` in tóm tắt tiếng Việt và cổng dừng phiên (`--gate`), còn `automation/aihs-live-aggregate.py` tổng hợp chỉ đọc từ các `run.json` thành `aggregate.json` và `aggregate.md`; ground truth chỉ dùng để chấm sau lượt. Số liệu tổng hợp ở Chương 5.

## 4.7. Kiểm thử

Khóa luận phân biệt bốn loại bằng chứng (Bảng 4.7).

**Bảng 4.7.** Loại kiểm thử, công cụ và điều khẳng định được

| Loại | Công cụ | Khẳng định được | Không khẳng định |
| --- | --- | --- | --- |
| Đơn vị và hợp đồng | `pytest agent_src/tests` (104 tệp `test_*.py`); `terraform test` với provider giả | Từng thành phần (Gate, catalog, Executor client, audit, state machine, adapter AIHS, harness, Verifier…) đúng hợp đồng | Hành vi trên staging thật |
| Replay offline | `aiops-unified-replay.py`, `moodle-pipeline-replay.py`; replay shadow của C0 | Đường đi trong mã; kết quả fail-closed được ghim; không thi hành live | Phục hồi thật, RTO |
| Lab cục bộ | `lab/moodle-local`, `lab_runner.py` | Dữ liệu huấn luyện và nhãn; thử injector/probe ở quy mô lớn | Bằng chứng staging (lab không có Gate, Operator, Verifier) |
| Live staging | `live_c0_harness.py` và script `automation/` trên AWS | Hành vi chuỗi Gate, Operator, Executor, Verifier trên các lượt đã chạy | Tần suất tự nhiên của lỗi |

<!-- nguồn: AGENTS.md; README.md; agent_src/tests (đếm số tệp); docs/BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN.md §6 -->

Một số bài kiểm thử mã hóa trực tiếp bất biến an toàn. Ranh giới ground truth được kiểm ở ba tầng: bước CI kiểm image (4.3.1), `test_decision_stage_has_no_ground_truth_parameter` (hàm quyết định của harness không nhận tham số mang đáp án) và `test_answer_bearing_feature_is_rejected` (bước freeze từ chối đặc trưng có tên mang đáp án). Kỷ luật protocol được kiểm bởi `test_refuses_without_lock_and_never_reads_test_labels` và `test_lock_is_not_overwritable_and_requires_locked_model`; `test_slow_approval_with_changed_proposal_blocks_execution` kiểm rằng duyệt chậm khi đề xuất đã đổi thì không thi hành. Về số lượng, tài liệu ghi: 7/7 test hợp đồng AIHS (14/11/2026); 79/79 test của harness live và Verifier, rồi 80/80 sau khi thêm test cho bản sửa race (17/11/2026); 22/22 `terraform test` (25/10/2026). Đây là kết quả của các lần chạy có ghi ngày, không phải tổng số test hiện tại. Replay và CI xanh không chứng minh phục hồi live; harness có chạy thử ngoại tuyến với đồ giả (`dry-run`) trước khi chạm staging, và lượt live đầu tiên của pilot cũng là lượt kiểm chứng wrapper và backend live.

<!-- nguồn: agent_src/tests/*.py; docs/BAO_CAO_AIHS.md §5.3; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §7, §16.5–16.6; docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §12.1 -->

## 4.8. Các vấn đề gặp phải khi hiện thực và cách khắc phục

Nhiều lỗi chỉ lộ ra khi chạy trên staging thật. Quy tắc của chiến dịch là giữ nguyên và công bố các lượt có lỗi tích hợp, sửa phía hạ tầng hoặc harness, không sửa mô hình đã khóa, mapping, nhãn và các tệp bị khóa; các lượt sau khi sửa mới tính vào mẫu chính. Bảng 4.8 nêu lỗi tích hợp khi chạy live AIHS.

**Bảng 4.8.** Lỗi tích hợp khi chạy live AIHS và bản sửa

| # | Vấn đề | Nguyên nhân | Cách khắc phục |
| --- | --- | --- | --- |
| 1 | Khoảng 22/108 đặc trưng `null` trên staging; C0 chọn `call_human`; D1 không tách được NET-01 khỏi SEC-02 | Node probe của staging thiếu metric mà lab phát ra | Thêm 6 chỉ số vào `moodle-node-probes.sh` (cùng tên và ngữ nghĩa với lab); thêm kiểm tra `staging_metric_schema` vào preflight. Mô hình, Gate, protocol không đổi |
| 2 | SEC-02 `HANDOFF_BLOCKED` | Planner chỉ chấp nhận marker mode-0000 ở node A, nhưng EFS dùng chung nên cả hai node đều báo | Sửa planner, phát hành trong Agent image `f79f7c4a`: chấp nhận thêm dạng dùng chung, node A bắt buộc |
| 3 | BIZ-02 `HARNESS_ERROR` | Planner chờ số task quá hạn ở một node, nhưng DB dùng chung nên cả hai node báo; thêm lỗi dấu nháy trong script probe | Planner chấp nhận cả hai node > 0; sửa dấu nháy |
| 4 | NET-01 `HANDOFF_BLOCKED` | Alert `for: 5s` nhưng probe node 30 s mới chạy một lần, nên marker có sau lúc Agent điều tra | Probe chạy mỗi 10 s; alert synthetic đổi `for: 40s` |
| 5 | CON-01 lấy nhầm candidate của RES-01 | Harness tìm candidate trong 30 phút, không lọc theo kịch bản | Tìm đúng kịch bản của lượt, trong 15 phút, lấy bản mới nhất |
| 6 | RES-01 bị tính `OPERATOR_TIMEOUT` | Operator gõ `APPROVED` thay vì `APPROVE <mã>`; bản cũ coi mọi dòng sai là hết giờ | Thẻ duyệt hỏi lại khi gõ sai |
| 7 | Mọi lượt thiếu probe `internet_to_postgresql_blocked` | Health check Route 53 bị tắt khi dựng lại staging | Bật lại trong tfvars và `apply` |
| 8 | Verifier trả `not_stable` dù 3 mẫu đều đạt | Lịch lấy mẫu theo đồng hồ monotonic, cửa sổ đo bằng đồng hồ thực; trên WSL đồng hồ thực chậm nên 119,99 s bị cắt thành 119 s | Verifier chờ đủ offset theo đồng hồ thực trước mỗi mẫu; ngưỡng 120 s giữ nguyên; thêm test |
| 9 | BIZ-02 bị hủy vì reset thất bại | Cron dừng đúng lúc chạy dở một ad-hoc task; claim mồ côi làm số task quá hạn lên 1, Verifier trả `collateral_damage` (đúng) còn health-only vẫn báo khỏe | Reset gỡ claim mồ côi sau khi bật cron; lượt hỏng giữ làm bằng chứng định tính, seq 2 chạy lại |
| 10 | DB-01 bổ sung `HANDOFF_BLOCKED` | Race: harness tra candidate một lần, 5 s trước khi planner của Agent ghi candidate | `ApiAgent.prepare` chờ tối đa 120 s (10 s một lần), hết giờ vẫn fail-closed; thêm test |
| 11 | Ctrl-C để công tắc live của Executor còn bật | Dọn dẹp bị ngắt giữa chừng | `trap` dọn dẹp bỏ qua INT/TERM/HUP khi đang tắt công tắc |
| 12 | Có thể dùng nhầm Prometheus của lab | Cổng tunnel 19090 trùng cổng của lab; `curl` vẫn thành công với nhầm backend | Wrapper đòi chính tiến trình tunnel còn sống |
| 13 | Probe của Verifier treo đến timeout | AWS CLI có thể đọc prompt MFA từ TTY khi stderr bị chụp | Chạy probe với `stdin=DEVNULL` và phiên mới |

<!-- nguồn: docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §10–13, §16.6; automation/aihs-live-c0.sh; automation/moodle-independent-verifier.py -->

Một giới hạn được giữ nguyên thay vì "sửa": ở NET-01, sau khi probe đổi sang chu kỳ 10 s, alert `MoodleNodeRdsTcpFailed` firing trước khi chụp snapshot; `derive_context` (tệp bị khóa) ánh xạ alert này sang DB-01 nên action đúng của NET-01 bị Gate chặn `scope_mismatch` và fail-closed. Alert rule không được chỉnh sau khi đã thấy kết quả; đây là giới hạn thật của việc suy ngữ cảnh từ alert.

<!-- nguồn: docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §12, §16.1 -->

Ở phía hạ tầng, các sự cố đã gặp và cách xử lý: `plan` đòi thay cả ba EC2 vì `ec2_ami_id` không được ghim (ghim AMI); lỗi host key của EC2 mới (không tắt kiểm tra, đối chiếu fingerprint qua console output); `apply` bị ngắt để lại tài nguyên đã tồn tại nhưng chưa vào state (`terraform import` thay vì xóa tay); kéo image trả 404 vì chọn SHA bằng `git log` trúng một commit nằm giữa lần push, mà chỉ commit cuối của lần push có image (hỏi registry thay vì đoán từ git); alert `MoodleNodeMemoryPressure` quá nhạy mỗi khi container khởi động lại (ghi nhận, chưa chỉnh).

<!-- nguồn: docs/BAO_CAO_KIEN_TRUC_HA_TANG.md §12.2–12.3; docs/BAO_CAO_HUONG_DAN_TRIEN_KHAI.md §10.1, §17.2 -->

## 4.9. Tiểu kết chương

Chương này đã mô tả cách khung an toàn được hiện thực. Về hạ tầng, Terraform sở hữu tài nguyên cloud và sinh inventory cùng SSH config, Ansible sở hữu cấu hình máy, và CI/CD bảo đảm image được build ngoài EC2 và bất biến theo SHA. Về Agent, đường cảnh báo chỉ điều tra và đề xuất; đường xử lý tách riêng, đi qua Safety Gate, một chữ ký Operator gắn digest action, Safe Executor (HMAC, allowlist, SSH forced command) và chuỗi audit băm; chỉ attestation Ed25519 của Independent Verifier, được Agent kiểm lại, mới đưa incident tới `RESOLVED`. Công tắc live mặc định tắt và mô hình ngôn ngữ chỉ review góp ý của admin. Về AIHS, quy trình từ lab, exporter, gán nhãn mù, freeze, huấn luyện, khóa mô hình, scorer có khóa sha256 và marker mở test một lần, đến harness live với năm bất biến được hiện thực thành mã có kiểm thử. Bảng 4.8 cho thấy phần lớn công sức tích hợp nằm ở khớp lược đồ quan sát, đồng bộ thời gian giữa harness, Agent và alert, và đo cửa sổ của Verifier, hơn là ở bản thân mô hình. Chương 5 trình bày kết quả đo trên nền hiện thực này; các giới hạn của nó (mẫu nhỏ, một người gán nhãn, Operator cũng là người thiết kế, NET-01 không có lượt thi hành) được bàn ở Chương 5 và 6.

## Tài liệu tham khảo của chương

- [AIHS2021] R. Li và cộng sự, "Automated Intelligent Healing in Cloud-Scale Data Centers", trong *Proc. IEEE 40th International Symposium on Reliable Distributed Systems (SRDS)*, 2021. DOI: 10.1109/SRDS53918.2021.00032.
- [Terraform] HashiCorp, "Terraform Documentation". https://developer.hashicorp.com/terraform
- [Ansible] Red Hat, "Ansible Documentation". https://docs.ansible.com
- [Docker] Docker Inc., "Docker Documentation". https://docs.docker.com
- [DockerCompose] Docker Inc., "Docker Compose Documentation". https://docs.docker.com/compose/
- [Moodle] Moodle Pty Ltd, "Moodle". https://moodle.org
- [PostgreSQL] PostgreSQL Global Development Group, "PostgreSQL Documentation". https://www.postgresql.org/docs/
- [Prometheus] Prometheus Authors, "Prometheus Documentation" (gồm Node Exporter và Blackbox Exporter). https://prometheus.io/docs/
- [Alertmanager] Prometheus Authors, "Alertmanager". https://prometheus.io/docs/alerting/latest/alertmanager/
- [Grafana] Grafana Labs, "Grafana Documentation". https://grafana.com/docs/
- [FastAPI] S. Ramírez, "FastAPI". https://fastapi.tiangolo.com
- [Pydantic] Pydantic Services Inc., "Pydantic Documentation". https://docs.pydantic.dev
- [Celery] Celery Project, "Celery Documentation". https://docs.celeryq.dev
- [Redis] Redis Ltd., "Redis Documentation". https://redis.io/docs/
- [SQLite] SQLite Consortium, "SQLite Documentation". https://www.sqlite.org/docs.html
- [Cryptography] Python Cryptographic Authority, "cryptography Documentation". https://cryptography.io
- [ChromaDB] Chroma, "Chroma Documentation". https://docs.trychroma.com
- [TelegramBot] Telegram, "Telegram Bot API". https://core.telegram.org/bots/api
- [Gemini] Google, "Gemini API Documentation". https://ai.google.dev/gemini-api/docs
- [GitHubActions] GitHub, "GitHub Actions Documentation". https://docs.github.com/actions
- [GHCR] GitHub, "GitHub Packages Documentation" (Container registry). https://docs.github.com/packages
- [AWS] Amazon Web Services, "AWS Documentation". https://docs.aws.amazon.com
- [ScikitLearn] scikit-learn developers, "scikit-learn Documentation". https://scikit-learn.org
- [LightGBM] Microsoft, "LightGBM Documentation". https://lightgbm.readthedocs.io
- [HAProxy] HAProxy Technologies, "HAProxy Documentation". https://www.haproxy.org
