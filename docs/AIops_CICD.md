# CI/CD Moodle và AI Agent

Tài liệu này mô tả **luồng CI/CD đang chạy** cho môi trường Moodle staging và
AI Agent, theo đúng các workflow trong `.github/workflows/`. Khi workflow thay
đổi, cập nhật tài liệu này trong cùng commit.

> **Trạng thái ngày 15/10/2026:** hạ tầng staging đã được destroy (xem
> [Moodle Operations Runbook](MOODLE_OPERATIONS_RUNBOOK.md) §14). CI và việc
> publish image vẫn chạy bình thường; job deploy của CD sẽ không thể chạy cho
> tới khi hạ tầng được dựng lại theo runbook §4–§8.

## 1. Mục tiêu và ranh giới

CI/CD kiểm thử mọi thay đổi, tạo **image Docker bất biến** (gắn theo commit
SHA) và triển khai **Moodle** lên hai application node. GitHub Actions **không**
tạo, sửa hay phá hủy hạ tầng AWS.

| Thành phần | Chủ sở hữu | Cách thay đổi |
| --- | --- | --- |
| VPC, ALB, EC2, RDS, EFS, security group | Terraform, Infrastructure Engineer | `plan` được review rồi `apply`, theo runbook §5 |
| Host runtime và monitoring | Ansible, script trong `automation/` | Chạy từ máy điều khiển tin cậy, theo runbook §6 và §9 |
| Moodle image | GitHub Actions + GHCR | Build theo commit SHA; CD staging triển khai |
| AI Agent image | GitHub Actions + GHCR | Build theo commit SHA; owner triển khai thủ công |
| Runtime secrets | File secret trên host | Stage thủ công (runbook §8); không nằm trong image, repo hay log |

## 2. Luồng theo branch

| Sự kiện | CI (`ci.yml`) | Moodle image | AI Agent image (`agent-image.yml`) | CD staging (`cd-staging.yml`) |
| --- | --- | --- | --- | --- |
| Pull request vào `develop`/`main` | ✅ | — | — | — |
| Push `feature/**` | ✅ | ✅ `moodle-image.yml` (khi `moodle/**` đổi) | ✅ (khi `agent_src/**` đổi) | — |
| Push `develop` | ✅ | ✅ do `cd-staging.yml` build | ✅ (khi `agent_src/**` đổi) | ✅ khi `moodle/**`, `release/moodle/**`, `automation/github-deploy-moodle.sh` hoặc chính workflow đổi |
| Push `main` | ✅ | ✅ `moodle-image.yml` | ✅ | — |
| Chạy tay (`workflow_dispatch`) | — | ✅ | ✅ | ✅ |

Không có CD production. Moodle staging là môi trường triển khai duy nhất.

## 3. Image và registry

| Artifact | Dockerfile và build context | Image bất biến |
| --- | --- | --- |
| Moodle | `moodle/Dockerfile`, context `moodle/` | `ghcr.io/<owner>/moodle:<commit-sha-40-ký-tự>` |
| AI Agent | `agent_src/Dockerfile`, context **thư mục gốc repo** (`docker build -f agent_src/Dockerfile .`) | `ghcr.io/<owner>/moodle-ai-agent:<commit-sha-40-ký-tự>` |

- Commit SHA 40 ký tự là định danh release. Không dùng tag trôi như `latest`.
- Image AI Agent dùng `agent_src/Dockerfile.dockerignore`, nên **không chứa**
  `evaluation/ground_truth/`. CI kiểm tra điều này ở bước 9.
- Package `moodle` hiện là **public**; package `moodle-ai-agent` do GHCR quản lý
  quyền riêng.
- EC2 không build source. Mỗi node chỉ pull image do CI tạo và chạy Docker
  Compose với file runtime đã stage sẵn.

## 4. CI: kiểm thử mọi thay đổi

Workflow [ci.yml](../.github/workflows/ci.yml) chạy một job
`lint-test-build` trên `ubuntu-latest`, gồm các bước theo thứ tự:

| # | Bước | Kiểm tra gì |
| --- | --- | --- |
| 1–3 | Checkout, Python 3.11, cài dependency | `pip install --require-hashes -r agent_src/requirements.txt`, cộng `pytest`, `ruff`, `httpx<0.28` |
| 4 | Lint (critical rules) | `ruff check agent_src --select E9,F63,F7,F82` (lỗi cú pháp và tên chưa định nghĩa) |
| 5 | Test AI Agent | `pytest -q agent_src/tests` |
| 6 | Validate Moodle experiment and operator automation | `py_compile` các script Python và `bash -n` các script shell trong `automation/`; `moodle-scenario-verify.sh SEC-01` phải bị từ chối với exit 64 (HUMAN_ONLY) |
| 7 | Replay merged unified AI control plane | Replay offline 15 kịch bản. **Kết quả từng kịch bản được ghim**: 13 `fail_closed_missing_rollback`, SEC-01 `fail_closed_human_only`, DB-01 `escalated_without_action`; không có thực thi live |
| 8 | Build AI Agent image | Build local, không push |
| 9 | Verify runtime image excludes evaluation ground truth | Trong image: không có `/app/evaluation/ground_truth`; chế độ `live` phải escalate và không cho thực thi |
| 10 | Build Moodle image | Build local, không push |
| 11 | Test Moodle secret-file handling | Moodle đọc mật khẩu DB qua `MOODLE_DB_PASSWORD_FILE` |
| 12 | Replay Moodle Sprint 2 pipeline | 15 kịch bản `VERIFIED_DRY_RUN`, 0 thực thi bị cấm |
| 13 | Test Moodle public web root layout | `healthz.php`, `synthetic-transaction.php`, DocumentRoot `/var/www/html/public` |
| 14 | Validate Docker Compose files | `docker compose config` cho `release/moodle/docker-compose.yml` |
| 15–16 | Notify admins on failure | Chỉ khi có bước lỗi: gửi Telegram qua `automation/notify.sh` (secret `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) |

Chạy lại các bước này ở máy local theo `AGENTS.md`, mục *Verify before pushing*.

**Lưu ý khi CI đỏ:** CI dừng ở bước lỗi **đầu tiên**, nên lỗi ở bước sau bị
che. Sau khi sửa một bước, bước tiếp theo có thể lộ lỗi mới. Hãy chạy lại toàn
bộ các bước ở local (mỗi lệnh kiểm tra phải trả về exit code 0, đừng để `| tail`
nuốt mã lỗi) trước khi push.

CI không có credential AWS, nên không thể `apply` hay `destroy` hạ tầng.

## 5. Build và publish image

### Moodle

| Workflow | Khi nào chạy | Kết quả |
| --- | --- | --- |
| [moodle-image.yml](../.github/workflows/moodle-image.yml) | Push `feature/**` hoặc `main` khi `moodle/**` đổi, hoặc chạy tay | Push `moodle:<sha>` lên GHCR |
| [cd-staging.yml](../.github/workflows/cd-staging.yml), job `build-moodle` | Push `develop` theo path filter ở mục 2, hoặc chạy tay | Push `moodle:<sha>` rồi chuyển sang job deploy |

### AI Agent

[agent-image.yml](../.github/workflows/agent-image.yml) build và push
`moodle-ai-agent:<sha>`. Workflow này **không tự thay Agent đang chạy**. Owner
chọn SHA đã review và triển khai bằng:

```bash
bash automation/configure-moodle-monitoring.sh \
  --agent-image ghcr.io/<owner>/moodle-ai-agent:<commit-sha>
```

Tách rollout Agent khỏi CD Moodle để việc release ứng dụng không vô tình thay
đổi logic chẩn đoán hay thực thi của agent.

## 6. CD staging Moodle

Workflow [cd-staging.yml](../.github/workflows/cd-staging.yml) có hai job:

```text
push develop (đúng path filter)
  → job build-moodle (ubuntu-latest): build + push ghcr.io/<owner>/moodle:<sha>
  → job deploy-moodle (runner tự host: self-hosted, linux, moodle-staging;
                       GitHub Environment "staging")
      → kiểm tra đủ secret
      → automation/github-deploy-moodle.sh:
          moodle-app-b trước, rồi moodle-app-a
          mỗi node: docker login GHCR → cập nhật MOODLE_IMAGE trong
                    /opt/moodle/release/moodle-runtime.env → compose pull
                    → compose up -d --wait → curl http://127.0.0.1:8080/healthz.php
      → curl $MOODLE_PUBLIC_URL/healthz.php qua ALB
```

- Node B được deploy trước để luôn còn một target healthy phía sau ALB.
- **Runner tự host bắt buộc** (thường đặt trên `monitor-ai-01`, nơi SSH được
  vào hai node private). Không có runner online thì job `deploy-moodle` đứng ở
  trạng thái `queued` rồi bị hủy; image vẫn đã được publish. Khi đó deploy tay
  theo runbook §8.
- Script SSH với `StrictHostKeyChecking=accept-new`: tin host key ở lần kết nối
  đầu tiên. Khác với luồng thủ công trong runbook (bắt buộc đối chiếu fingerprint).

### Secret của GitHub Environment `staging`

| Secret | Mục đích |
| --- | --- |
| `MOODLE_APP_A_HOST`, `MOODLE_APP_B_HOST` | IP private hoặc hostname của hai node (runner phải tới được) |
| `SSH_PRIVATE_KEY` | Key deploy; chỉ ghi ra file tạm trên runner, xóa khi script kết thúc |
| `SSH_PORT` | Không bắt buộc, mặc định 22 |
| `GHCR_USERNAME`, `GHCR_TOKEN` | **Bắt buộc**, vì script luôn `docker login`, kể cả khi package public. Token chỉ cần `read:packages` |
| `MOODLE_PUBLIC_URL` | Không bắt buộc; nếu có thì kiểm tra health qua ALB sau rollout |

**Mỗi lần dựng lại hạ tầng**, IP private của node và DNS của ALB đều đổi: phải
cập nhật `MOODLE_APP_A_HOST`, `MOODLE_APP_B_HOST`, `MOODLE_PUBLIC_URL`.

### Điều kiện trước khi CD chạy được

CD **không** bootstrap Moodle. Trước lần deploy đầu tiên, Infrastructure
Engineer phải hoàn tất runbook:

1. §4–§5: Terraform `apply`, xác minh host key, sinh SSH config và inventory.
2. §6: `prepare-moodle-runtime.sh` (Docker, EFS).
3. §8: preflight, tạo secret, `stage-moodle-release.sh`, cài Moodle một lần,
   khởi động web/cron.
4. §8.1: khôi phục cấu hình Okta OIDC (database mới, DNS ALB mới).
5. Cài runner tự host và cập nhật secret của environment `staging`.

CD chỉ thay `MOODLE_IMAGE` trong `/opt/moodle/release/moodle-runtime.env`; nó
không tạo database, không đổi mật khẩu RDS, không mount EFS và không chạy
Terraform.

## 7. Rollback và xử lý lỗi

- Script deploy **không tự rollback**. Gặp lỗi (pull, compose, health check),
  script dừng ngay: node đã deploy giữ image mới, node chưa tới lượt giữ image cũ.
- Cách rollback: chọn một SHA **đã biết healthy**, rồi deploy lại theo runbook
  §8 (cập nhật `MOODLE_IMAGE`, `compose up -d --wait` lần lượt node B rồi A).
- Khi một node lỗi: giữ node còn healthy trong ALB, xem
  `docker compose ps` và log của node lỗi, xác minh image SHA.
- **Cần xác minh: cron có thể chạy trên cả hai node.** Script gọi
  `compose up -d --wait moodle-web moodle-cron` trên **cả** node B và A. Service
  `moodle-cron` thuộc profile `cron`, nhưng Docker Compose vẫn khởi động một
  service khi nó được gọi đích danh. Nếu đúng như vậy, sau một lần CD sẽ có
  **hai** cron runner, trái với quy tắc "chỉ node A chạy cron" của runbook §8.
  Kiểm tra bằng `docker compose ps` trên node B sau lần CD tiếp theo; nếu thấy
  `moodle-cron`, sửa script để chỉ node A chạy cron.
- Không dùng CD để khôi phục RDS, sửa quyền EFS, tạo lỗi diễn tập hay xóa hạ
  tầng. Các việc này làm theo runbook, có baseline và bằng chứng riêng.

## 8. Kiểm tra sau release

```bash
MOODLE_URL="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"
curl --fail --silent "$MOODLE_URL/healthz.php"

for host in moodle-app-a moodle-app-b; do
  ssh -F terraform/.artifacts/moodle-ssh.config "$host" \
    'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
      -f /opt/moodle/release/docker-compose.yml ps'
done
```

Sau đó kiểm tra target health của ALB (runbook §8), nút đăng nhập Okta
(runbook §8.1), và Prometheus/Grafana nếu monitoring đã triển khai (runbook §9).

## 9. Nguyên tắc an toàn

- Không tự động `apply` Terraform hay Ansible từ GitHub Actions.
- Không build image production trên EC2.
- Không dùng tag `latest`, không mở RDS/EFS ra Internet, không đặt mật khẩu
  trên dòng lệnh.
- Không đưa private key, PAT, Terraform state hay `terraform/.artifacts/` vào Git.
- Chỉ deploy image SHA đã qua CI; ghi lại commit SHA, image reference, thời điểm
  deploy và kết quả health check làm bằng chứng release.
