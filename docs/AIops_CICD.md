# CI/CD Moodle và AI Agent

Tài liệu này mô tả luồng delivery hiện tại của môi trường Moodle staging. Nó
thay thế thiết kế CI/CD của delivery stack ứng dụng cũ.

## 1. Mục tiêu và ranh giới

CI/CD tạo các image Docker bất biến, kiểm thử chúng và triển khai **Moodle**
vào hai Moodle application node. Hạ tầng AWS không được GitHub Actions tự ý
tạo, thay đổi hoặc phá hủy.

| Thành phần | Chủ sở hữu | Cách thay đổi |
| --- | --- | --- |
| VPC, ALB, EC2, RDS, EFS, security group | Terraform + Infrastructure Engineer | <code>plan</code> được review, rồi <code>apply</code> rõ ràng |
| Host runtime và monitoring | Ansible + automation | Chạy từ máy điều khiển tin cậy |
| Moodle image | GitHub Actions + GHCR | Build theo commit SHA; CD staging triển khai |
| AI Agent image | GitHub Actions + GHCR | Build theo commit SHA; owner phê duyệt rollout monitoring |
| Runtime secrets | Secret files trên host | Stage thủ công; không nằm trong image, repo hay workflow log |

## 2. Image và registry

Hai artifact chính được phát hành lên GitHub Container Registry (GHCR):

| Artifact | Build context | Image immutable |
| --- | --- | --- |
| Moodle | <code>moodle/</code> | <code>ghcr.io/&lt;owner&gt;/moodle:&lt;commit-sha&gt;</code> |
| AI Agent | <code>agent_src/</code> | <code>ghcr.io/&lt;owner&gt;/moodle-ai-agent:&lt;commit-sha&gt;</code> |

Commit SHA gồm 40 ký tự là định danh release. Không dùng image tag trôi như
<code>latest</code> để triển khai staging.

EC2 không build source application. Mỗi node chỉ xác thực GHCR, pull image đã
được CI tạo, rồi chạy Docker Compose với file runtime đã stage sẵn.

## 3. CI: kiểm thử mọi thay đổi

Workflow [ci.yml](../.github/workflows/ci.yml) chạy cho pull request và push
vào <code>feature/**</code>, <code>develop</code>, <code>main</code>.

Nó thực hiện:

1. Ruff critical rules và pytest cho <code>agent_src</code>.
2. Python/Bash syntax validation cho Moodle automation và replay.
3. Build local image AI Agent và Moodle.
4. Kiểm tra Moodle đọc database password qua
   <code>MOODLE_DB_PASSWORD_FILE</code>, PHP syntax, Apache public document root và
   Docker Compose configuration.
5. Replay control-plane AI ở chế độ không mutation.

CI fail phải được sửa trước khi merge; CI không có quyền Terraform hoặc AWS
credential để <code>apply</code>/<code>destroy</code>.

> **Trạng thái chuyển tiếp cần biết:** workflow hiện tại vẫn validate hai Compose
> file root <code>release/docker-compose.staging.yml</code> và
> <code>release/docker-compose.production.yml</code> ngoài Moodle Compose. Đây
> là validation còn lại của release stack cũ, không phải CD path Moodle và nên
> được bỏ cùng đợt dọn source legacy. Không coi chúng là source of truth cho
> Moodle staging.

## 4. Build Moodle và Agent images

### Moodle

| Workflow | Trigger | Kết quả |
| --- | --- | --- |
| [moodle-image.yml](../.github/workflows/moodle-image.yml) | Push vào <code>feature/**</code> hoặc <code>main</code> khi <code>moodle/</code> thay đổi | Push image Moodle immutable lên GHCR |
| [cd-staging.yml](../.github/workflows/cd-staging.yml) | Push vào <code>develop</code> khi <code>moodle/</code>, <code>release/moodle/</code> hoặc Moodle deploy script thay đổi | Build/push image rồi deploy Moodle staging |

### AI Agent

[agent-image.yml](../.github/workflows/agent-image.yml) build/push AI Agent
khi <code>agent_src/</code> thay đổi trên <code>feature/**</code>, <code>develop</code> hoặc
<code>main</code>. Workflow này **không tự thay Agent đang chạy**. Infrastructure
owner chọn image SHA đã review và chạy:

~~~bash
bash automation/configure-moodle-monitoring.sh \
  --agent-image ghcr.io/&lt;owner&gt;/moodle-ai-agent:&lt;commit-sha&gt;
~~~

Tách Agent rollout khỏi Moodle CD giúp tránh thay đổi logic diagnosis/execution
ngoài ý muốn khi chỉ release ứng dụng Moodle.

## 5. CD staging Moodle

Sau một push hợp lệ vào <code>develop</code>, <code>CD Staging Moodle</code> chạy theo trình tự:

~~~text
push to develop
  → GitHub Actions: test/build Moodle image
  → GHCR: publish ghcr.io/&lt;owner&gt;/moodle:&lt;commit-sha&gt;
  → self-hosted runner on monitor-ai-01
  → SSH deploy moodle-app-b, then moodle-app-a
  → each node: docker pull + compose up --wait + local health check
  → ALB /healthz.php check
~~~

Deploy node B trước để duy trì một target healthy phía sau ALB. Script
[github-deploy-moodle.sh](../automation/github-deploy-moodle.sh) fail ngay nếu
runtime chưa được stage, image không immutable, Docker pull/Compose/health
check thất bại hoặc ALB không healthy.

### GitHub Secrets bắt buộc cho CD

| Secret | Mục đích |
| --- | --- |
| <code>MOODLE_APP_A_HOST</code>, <code>MOODLE_APP_B_HOST</code> | Target SSH private address/hostname |
| <code>SSH_PRIVATE_KEY</code> | Private key deploy, chỉ dùng tạm thời trong runner |
| <code>SSH_PORT</code> | Port SSH, mặc định 22 nếu không khai báo |
| <code>GHCR_USERNAME</code>, <code>GHCR_TOKEN</code> | Quyền pull package GHCR |
| <code>MOODLE_PUBLIC_URL</code> | Kiểm tra health qua ALB sau rollout |

<code>GHCR_TOKEN</code> chỉ cần scope <code>read:packages</code>. Không ghi bất kỳ secret nào vào
repository, Docker image, Terraform output công khai hay evidence artifacts.

## 6. Điều kiện trước CD

CD không bootstrap Moodle từ đầu. Trước lần deploy đầu tiên, Infrastructure
Engineer phải hoàn tất theo
[Moodle Operations Runbook](MOODLE_OPERATIONS_RUNBOOK.md):

1. Apply Terraform đã review và tạo inventory/SSH config.
2. Chạy <code>prepare-moodle-runtime.sh</code>.
3. Cấu hình quyền GHCR trên cả hai Moodle nodes.
4. Stage RDS app role, database secret, admin secret và Compose runtime.
5. Chạy installer Moodle một lần.
6. Xác nhận ALB health, RDS, EFS và preflight pass.

CD chỉ thay <code>MOODLE_IMAGE</code> trong <code>/opt/moodle/release/moodle-runtime.env</code>; nó
không tạo database, không thay RDS password, không mount EFS và không chạy
Terraform.

## 7. Monitoring, rollback và xử lý lỗi

- Moodle deployment phải giữ health endpoint <code>healthz.php</code> hợp lệ trên mỗi
  node và qua ALB.
- Prometheus, Alertmanager, Grafana, Redis và AI Agent được cấu hình bằng
  <code>configure-moodle-monitoring.sh</code>; chúng không phải là service của Moodle
  release Compose.
- Khi rollout fail, dừng tại node lỗi, giữ node còn healthy trong ALB, xem
  Docker Compose status/log và xác minh image SHA. Chỉ rollback bằng một SHA
  đã biết healthy sau khi review.
- Không dùng CD để xử lý RDS restore, EFS permission repair, fault injection
  hay Terraform destruction. Các thao tác này phải theo runbook và có
  evidence/baseline riêng.

## 8. Kiểm tra release

Sau một CD thành công:

~~~bash
curl --fail --silent "$MOODLE_PUBLIC_URL/healthz.php"

for host in moodle-app-a moodle-app-b; do
  ssh -F terraform/.artifacts/moodle-ssh.config "$host" \
    'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
      -f /opt/moodle/release/docker-compose.yml ps'
done
~~~

Sau đó kiểm tra target health của ALB, Prometheus targets và Grafana theo
[Moodle Operations Runbook](MOODLE_OPERATIONS_RUNBOOK.md).

## 9. Nguyên tắc an toàn

- Không auto-apply Terraform/Ansible từ GitHub Actions.
- Không build production image trên EC2.
- Không dùng <code>latest</code>, public RDS/EFS, hoặc password trên command line.
- Không đưa private key, PAT, Terraform state hay <code>.artifacts</code> vào Git.
- Chỉ deploy image SHA được review; ghi lại commit SHA, image reference,
  thời điểm deploy và kết quả health check trong release evidence.
