# Báo cáo kiến trúc hạ tầng Moodle staging trên AWS

**Cập nhật:** 26/10/2026 · **Region:** `ap-southeast-1` (Singapore) · **Môi trường:** staging
**Đối tượng đọc:** người cần hiểu hệ thống đang chạy gồm những gì, vì sao được thiết kế như vậy, và giới hạn của nó.

Bộ tài liệu gồm 4 báo cáo:

| Báo cáo | Trả lời câu hỏi |
|---|---|
| **BAO_CAO_KIEN_TRUC_HA_TANG** (tài liệu này) | Hệ thống gồm những gì, nối với nhau thế nào, vì sao |
| [BAO_CAO_HUONG_DAN_TRIEN_KHAI](BAO_CAO_HUONG_DAN_TRIEN_KHAI.md) | Dựng, release, vận hành và gỡ hệ thống bằng lệnh nào |
| [BAO_CAO_KICH_BAN_LOI](BAO_CAO_KICH_BAN_LOI.md) | Các kịch bản lỗi, cách diễn tập, demo và kết quả |
| [BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN](BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN.md) | AI Agent, Safety Gate, Executor, Verifier hoạt động ra sao |

Nguồn sự thật là mã nguồn trong `terraform/`, `ansible/`, `release/moodle/`, `.github/workflows/`. Khi tài liệu và
mã nguồn khác nhau, **mã nguồn thắng**; hãy sửa tài liệu.

---

## Mục lục

1. [Tóm tắt](#1-tóm-tắt)
2. [Kiến trúc tổng thể](#2-kiến-trúc-tổng-thể)
3. [Mạng](#3-mạng)
4. [Security group: ai được nói chuyện với ai](#4-security-group-ai-được-nói-chuyện-với-ai)
5. [Máy chủ EC2 và cách truy cập](#5-máy-chủ-ec2-và-cách-truy-cập)
6. [Dữ liệu: RDS PostgreSQL và EFS](#6-dữ-liệu-rds-postgresql-và-efs)
7. [Ứng dụng Moodle](#7-ứng-dụng-moodle)
8. [Giám sát và cảnh báo](#8-giám-sát-và-cảnh-báo)
9. [Xác thực: Entra ID → Okta → Moodle](#9-xác-thực-entra-id--okta--moodle)
10. [Phân quyền AWS IAM](#10-phân-quyền-aws-iam)
11. [CI/CD](#11-cicd)
12. [Tính sẵn sàng, chi phí và giới hạn](#12-tính-sẵn-sàng-chi-phí-và-giới-hạn)
13. [Bằng chứng kiểm chứng và vấn đề còn mở](#13-bằng-chứng-kiểm-chứng-và-vấn-đề-còn-mở)

---

## 1. Tóm tắt

Moodle chạy trên **hai EC2 cố định** ở hai Availability Zone sau một **ALB**. Dữ liệu nghiệp vụ nằm trên **RDS
PostgreSQL** (private, Single-AZ), file người dùng nằm trên **EFS** dùng chung. Một máy **`monitor-ai-01`** làm
bastion, chạy toàn bộ giám sát và AI Agent.

| Lớp | Công cụ quản lý | Nguồn |
|---|---|---|
| Tài nguyên cloud (VPC, ALB, EC2, RDS, EFS, IAM role cho monitor) | Terraform, state cục bộ | `terraform/` (77 khối `resource`, gồm cả lane ERPNext tắt mặc định) |
| Cấu hình bên trong máy (Docker, EFS mount, giám sát, executor) | Ansible + script `automation/` | `ansible/` |
| Ứng dụng | Image Docker bất biến trên GHCR, tag theo commit SHA | `moodle/`, `release/moodle/` |
| Agent | Image `moodle-ai-agent` trên GHCR, deploy có chủ đích | `agent_src/` |

**Ba quy tắc cứng:** GitHub Actions không chạy Terraform hay Ansible; không build image trên EC2; Terraform chỉ lo
tài nguyên cloud, Ansible chỉ lo cấu hình host.

---

## 2. Kiến trúc tổng thể

Sơ đồ chuẩn: [`diagram/moodle-aiops-alb-rds-efs-redis.drawio`](../diagram/moodle-aiops-alb-rds-efs-redis.drawio).
Tên file giữ "redis" vì lý do lịch sử; Redis **không** nằm trên đường dữ liệu của Moodle.

```text
Internet (người dùng)
  └─► ALB internet-facing (Public A + Public B)
        ├─► moodle-app-a  (EC2, private App A, AZ A)  web + cron
        └─► moodle-app-b  (EC2, private App B, AZ B)  chỉ web
                │
                ├─► RDS PostgreSQL 16.10 (private Data A, Single-AZ, TLS verify-full)
                └─► EFS Regional (access point /moodledata, mount target ở mỗi AZ, TLS)

Người vận hành (IP /32 được phép)
  └─► monitor-ai-01 (EIP public, SSH + Grafana chỉ mở cho IP quản trị)
        ├─ Prometheus, Alertmanager, Grafana, Blackbox Exporter, synthetic transaction
        ├─ AI Agent (FastAPI) + Celery worker + Redis (hàng đợi riêng của Agent)
        ├─ Safe Executor API (127.0.0.1/172.17.0.1:8765), audit RDS security group (IAM chỉ đọc)
        └─ SSH ProxyJump ─► hai node Moodle

Moodle server ──(NAT, egress 443)──► GHCR (pull image), Okta (token, userinfo)
```

**Luồng dữ liệu chính:**

| Luồng | Đi qua | Ghi chú |
|---|---|---|
| Người dùng → Moodle | ALB :80 (hoặc :443 khi có ACM) → node :8080 | Health check ALB: `/healthz.php` |
| Moodle → DB | Node → RDS :5432, `sslmode=verify-full` | User `moodle_app` quyền tối thiểu |
| Moodle → file | Node → EFS :2049, TLS, access point | `moodledata` dùng chung |
| Moodle → Okta | Node → NAT → Internet :443 | Bước đổi `code` lấy token, đọc userinfo |
| Giám sát | Monitor → node :9100 (Node Exporter), :8088 (cAdvisor); Monitor → RDS :5432 (exporter chỉ đọc) | Probe node ghi qua textfile collector |
| Cảnh báo | Prometheus → Alertmanager → `POST /webhook` của Agent (bearer token) | Agent chỉ nghe trên loopback của monitor |
| Sửa lỗi có duyệt | Agent → Executor API (HMAC) → SSH forced-command tới node | Chỉ khi công tắc live bật |

---

## 3. Mạng

**VPC:** `10.10.0.0/16`, hai AZ.

| Tầng | AZ | CIDR | Public IP khi launch | Route mặc định |
|---|---|---|---|---|
| Public | A | `10.10.1.0/24` | Có | Internet Gateway |
| Public | B | `10.10.2.0/24` | Có | Internet Gateway |
| Application | A | `10.10.11.0/24` | Không | NAT Gateway A |
| Application | B | `10.10.12.0/24` | Không | NAT Gateway B |
| Data | A | `10.10.21.0/24` | Không | **Không có** route ra Internet (chỉ VPC local) |
| Data | B | `10.10.22.0/24` | Không | Không có |
| Management | A | `10.10.31.0/24` | Không; monitor dùng EIP | Internet Gateway |

- **Mỗi AZ một NAT Gateway**, để mất một AZ không làm node còn lại mất đường ra Internet.
- Data route table khai báo `route = []` để Terraform quản lý việc *không có* route tuỳ chỉnh.
- EFS mount target nằm ở Data A và Data B, cùng AZ với node dùng nó; node tới bằng VPC local route.
- RDS subnet group gồm Data A và Data B (yêu cầu của RDS), dù instance chỉ chạy ở Data A.
- Monitor đặt ở Management A khi `monitor_use_management_subnet = true`. Đổi subnet của một EC2 đang chạy sẽ **thay
  máy**.

**Đặt tên và tag.** Tài nguyên dùng tiền tố `moodle-staging`:

| Tài nguyên | Tên logic | Tag `Name` |
|---|---|---|
| VPC | `main` | `moodle-staging-vpc` |
| Subnet | `public_a/b`, `app_a/b`, `data_a/b` | `moodle-staging-public-a`… |
| EC2 | `moodle_a`, `moodle_b`, `monitor` | `moodle-app-a`, `moodle-app-b`, `monitor-ai-01` |
| ALB / target group | `moodle` | `moodle-staging-alb`, `moodle-staging-tg` |
| RDS | `moodle` | `moodle-staging-postgres` |
| EFS | `moodledata` | `moodle-staging-efs` |

Tag bắt buộc: `Project=moodle-aiops`, `Environment=staging`, `ManagedBy=Terraform`, `Owner=Infrastructure`,
`ExperimentScope=thesis`.

---

## 4. Security group: ai được nói chuyện với ai

Luồng nội bộ dùng **tham chiếu security group**, không dùng CIDR.

| Nguồn | Đích | Cổng TCP | Mục đích |
|---|---|---:|---|
| Internet | ALB SG | 80, 443 | Cửa vào Moodle |
| ALB SG | Moodle SG | 8080 | Web và health check |
| IP quản trị (`my_ip_cidr`), CIDR CI hợp lệ | Monitor SG | 22 | Bastion |
| IP quản trị | Monitor SG | 3000 | Grafana |
| IP của AI Engineer (tuỳ chọn) | Monitor SG | 22 | Chỉ vào monitor |
| Monitor SG | Moodle SG | 22, 9100, 8088 | Ansible/SSH, Node Exporter, cAdvisor |
| Moodle SG | RDS SG | 5432 | Database |
| Monitor SG | RDS SG | 5432 | Exporter chỉ đọc |
| Moodle SG | EFS SG | 2049 | `moodledata` |
| Moodle SG | Internet qua NAT | 443 | GHCR, kho gói HTTPS, Okta |

**Bị cấm:**
- Internet → node Moodle, RDS, EFS.
- Internet → Prometheus, Alertmanager, exporter, AI Agent API. Xem các dịch vụ này qua SSH tunnel.
- Node Moodle → cổng SSH/quản trị của monitor.
- Mọi ingress vào RDS không đến từ Moodle SG hoặc exporter của monitor. Rule lạ ở đây chính là kịch bản **SEC-01**.

**Ghi chú triển khai:**
- SG mới dùng resource ingress/egress riêng; SG cũ giữ inline rule. Không trộn hai cách trên cùng một SG.
- RDS và EFS SG không có egress chủ động; SG là stateful nên traffic trả lời vẫn đi được.
- Moodle chỉ có egress 443, 5432, 2049. Dùng mirror HTTP hoặc NTP ngoài AWS thì phải thêm rule có chủ đích.
- SG không chặn được VPC DNS và link-local metadata; các kiểm soát đó nằm ở host và container.
- Muốn đổi IP quản trị: chạy `automation/update-infrastructure.sh`, rồi `plan`/`apply`. Plan chỉ được đổi rule SSH và
  Grafana.

---

## 5. Máy chủ EC2 và cách truy cập

| Máy | Loại | Ổ đĩa | Subnet | Vai trò |
|---|---|---|---|---|
| `moodle-app-a` | `t3.small` | 30 GiB gp3 mã hoá | App A | Moodle web + **cron duy nhất** |
| `moodle-app-b` | `t3.small` | 30 GiB gp3 mã hoá | App B | Moodle web |
| `monitor-ai-01` | `t3.small` | 30 GiB gp3 mã hoá | Management A (hoặc Public A) | Bastion, giám sát, Agent, executor |

- **Không có Auto Scaling Group.** Một node dừng sẽ không được tự thay, để thí nghiệm sự cố đo được đúng phần khắc
  phục của Agent.
- **AMI phải được ghim** (`ec2_ami_id`). Không ghim thì mỗi khi AWS ra AMI Amazon Linux 2023 mới, `plan` sẽ đòi thay
  cả ba máy. Terraform chỉ đọc SSM Parameter Store công khai để tra AMI; dự án **không** dùng Systems Manager để quản
  lý máy.
- **Truy cập:** máy vận hành → `monitor-ai-01` → IP private của node, bằng `ProxyJump` trong file SSH config do
  Terraform sinh (`terraform output -raw moodle_ssh_config`). Private key ở lại máy vận hành, không chép lên monitor.
  SSH config đặt `StrictHostKeyChecking yes`; host key được xác minh qua console output của EC2.
- **IAM role cho monitor (mới, `terraform/monitor-iam.tf`):** instance profile `monitor_audit` chỉ có
  `ec2:DescribeSecurityGroupRules`, dùng cho script `moodle-rds-ingress-audit.sh` phát hiện rule lạ trong SG của RDS
  (kịch bản SEC-01). Việc **sửa** SG vẫn là chỉ-người. Monitor bật `instance_metadata_tags`.

---

## 6. Dữ liệu: RDS PostgreSQL và EFS

### 6.1. RDS PostgreSQL

| Thuộc tính | Giá trị |
|---|---|
| Resource / identifier | `aws_db_instance.moodle` / `moodle-staging-postgres` |
| Engine, lớp | PostgreSQL 16.10, `db.t4g.micro` |
| Sẵn sàng | **Single-AZ** ở Data A; không replica, không failover |
| Mạng | Private, public access tắt, SG `rds_sg` |
| Lưu trữ | 20 GiB gp3, mã hoá |
| Backup | Tự động 1 ngày |
| Truyền tải | `rds.force_ssl=1`; Moodle dùng `sslmode=verify-full` với CA bundle |
| Mật khẩu master | `manage_master_user_password=true`: RDS tự giữ trong Secrets Manager; Terraform chỉ xuất ARN |
| User ứng dụng | `moodle_app`, tạo bởi `stage-moodle-release.sh`; Moodle không bao giờ dùng master |
| Log | PostgreSQL log trên CloudWatch, giữ 14 ngày |
| Dọn dẹp | `deletion_protection=false`, `skip_final_snapshot=true`, `delete_automated_backups=true` (chỉ phù hợp môi trường test ngắn hạn) |

`db.t4g.micro` và Single-AZ được chọn cho đợt thử nghiệm ngắn. Các lỗi mức database (ví dụ cạn kết nối) vẫn quan sát
được, nhưng **failover database không nằm trong phạm vi đánh giá**.

**Route 53 health check ngoài (tuỳ chọn).** Biến `moodle_external_rds_probe_enabled` tạo một health check TCP từ
Internet tới endpoint RDS. Verifier dùng nó cho probe *phải bị chặn* `internet_to_postgresql_blocked`: health check
**phải thất bại**, chứng minh Internet không vào được PostgreSQL. Health check có phí nhỏ; bật trước khi chạy
Verifier và tắt sau đó. Role `InfraEngineer` cần quyền Route 53 health check, nằm ngoài lệnh chặn region.

### 6.2. EFS

- EFS Regional, General Purpose, có lifecycle policy và backup.
- Mỗi Data subnet có một mount target. Node A và B mount cùng access point `/moodledata`, có TLS, chủ sở hữu
  `1000:1000`, quyền `0770`.
- `terraform/efs.tf` có `lifecycle.prevent_destroy = true` để chống mất dữ liệu. Muốn gỡ hẳn phải tạm bỏ khối này
  (xem báo cáo triển khai).

### 6.3. Trạng thái của Moodle nằm ở đâu

| Dữ liệu | Vị trí | Quyết định |
|---|---|---|
| Mã ứng dụng, plugin, theme | Image Docker bất biến | Cùng image trên hai node; không sửa tay trên từng máy |
| Cấu hình | Sinh từ file env và secret khi stage | Cùng cấu hình logic trên hai node |
| Dữ liệu nghiệp vụ | RDS | Không có DB container trên EC2 |
| `moodledata` | EFS | Mount qua access point |
| Session web | File trên EFS | Redis để sau, nếu khoá session hoặc độ trễ EFS thành nút cổ chai |
| Cron | Chỉ `moodle-app-a` | Tránh chạy trùng tác vụ định kỳ |

Hệ thống **gần như stateless**, không hoàn toàn stateless: session và file tải lên phụ thuộc EFS.

---

## 7. Ứng dụng Moodle

- **Phiên bản:** Moodle 5.2.3, image `ghcr.io/benjaminnhnn/moodle:<SHA 40 ký tự>`.
- **Web server:** Apache, DocumentRoot `/var/www/html/public`, cổng 8080.
- **Điểm kiểm tra công khai:** `healthz.php` (sẵn sàng, nhanh, ALB dùng), `synthetic-transaction.php` (giao dịch
  tổng hợp có đăng nhập, đọc/ghi qua DB và EFS).
- **Secret:** mật khẩu DB đọc qua `MOODLE_DB_PASSWORD_FILE`; secret nằm trong file được mount, không trên dòng lệnh hay
  trong image.
- **Compose** (`release/moodle/docker-compose.yml`): service `moodle-web`, `moodle-cron` (profile `cron`, chỉ node
  A), `moodle-install` (profile `installer`, chạy một lần trên node A).
- **Định danh của web worker:** uid `1000`, gid `1999`. Mọi lệnh PHP CLI phải chạy với `-u 1000:1999`; `-u moodle`
  thiếu nhóm đọc secret và CA, nên báo `Database connection failed`.
- **TLS ngoài:** ưu tiên HTTPS 443 bằng ACM, HTTP 80 chuyển hướng sang HTTPS. Staging hiện chạy **HTTP**
  (`moodle_allow_http = true`), chỉ dùng cho kiểm thử chức năng.

---

## 8. Giám sát và cảnh báo

### 8.1. Thành phần

| Thành phần | Chạy ở đâu | Thu gì |
|---|---|---|
| Node Exporter, cAdvisor | Node A, B | CPU, bộ nhớ, đĩa; container |
| `moodle-node-probes.sh` (timer) | Node A, B | Web container chạy không, router Apache, kết nối RDS từ node, DNS, EFS, maintenance flag, upgrade đang chạy, cron, số ad-hoc task quá hạn (chỉ đọc) |
| Blackbox Exporter | Monitor | Trang công khai, trang login (`probe=login`), tuỳ chọn SSO (`http_moodle_sso_option`), RDS TCP |
| Synthetic transaction (timer 30 s) | Monitor | Đăng nhập admin, đọc/ghi fixture qua ALB |
| `moodle-rds-ingress-audit.sh` | Monitor | Số rule ingress chưa duyệt trong SG của RDS (IAM chỉ đọc) |
| Prometheus, Alertmanager, Grafana | Monitor | Lưu metric, đánh giá rule, gửi cảnh báo, dashboard `moodle-infrastructure` |

Mọi dashboard chỉ truy cập qua SSH tunnel (3000, 9090, 9093, 8000). Prometheus rule được kiểm tra bằng
`promtool check rules` mỗi lần triển khai.

### 8.2. Danh sách 24 alert (`ansible/config/moodle_alert_rules.yml`)

| Nhóm | Alert |
|---|---|
| Thu thập | `MoodleNodeExporterDown`, `MoodleCAdvisorDown`, `MoodleNodeProbeStale`, `MoodleSyntheticTransactionStale`, `MoodleAdhocQueueProbeFailed`, `MoodleRdsIngressAuditFailed` |
| Cửa vào | `MoodlePublicProbeFailed`, `MoodleSsoLoginOptionMissing`, `MoodleSyntheticTransactionFailed` |
| Container và router | `MoodleWebContainerMissing`, `MoodleWebContainerRestarting`, `MoodleApacheRouterMissing`, `MoodleNodeRouterFallbackFailed`, `MoodleTrustedProxyConfigDrift` |
| Database | `MoodleRdsTcpProbeFailed`, `MoodleNodeRdsTcpFailed`, `MoodleNodeRdsTcpLatencyHigh` |
| Tài nguyên | `MoodleNodeCpuHigh`, `MoodleNodeMemoryPressure`, `MoodleScratchEnospc`, `MoodleEfsMountMissing` |
| Vận hành Moodle | `MoodleMaintenanceModeStuck`, `MoodleAdhocTaskQueueStuck` |
| Bảo mật | `MoodleDatabasePortExposure` |

**Nguyên tắc thiết kế:**
- Alert **không mang mã kịch bản**. Marker kịch bản chỉ nằm trong file root trên host, để Agent không thể "nhìn đáp án".
- `MoodleSsoLoginOptionMissing` chỉ bắn khi trang login vẫn lên mà thiếu nút SSO
  (`probe_success{job="blackbox_moodle_sso"} == 0 and on() probe_success{...probe="login"} == 1`, `for: 1m`). Nhờ vậy
  nó không trùng với alert khi Moodle sập hẳn.
- `MoodleNodeMemoryPressure` đo độ **giảm** bộ nhớ khả dụng hơn 32 MiB trong 1 phút. Mỗi lần container khởi động lại
  nó có thể bắn trong 10–80 s; đây là cảnh báo nhạy quá mức, đã ghi nhận.

### 8.3. Alertmanager → Agent

Alertmanager gửi tới `http://ai-agent:8000/webhook`, `group_wait 5s`, `group_interval 10s`, `repeat_interval 4h`,
`send_resolved: true`. Xác thực bằng bearer token 256 bit sinh **trên chính monitor**, đọc từ file mount chỉ đọc ở cả
hai phía. Đổi token phải deploy lại cả Alertmanager lẫn Agent cùng lúc.

---

## 9. Xác thực: Entra ID → Okta → Moodle

### 9.1. Phân chia trách nhiệm

| Hệ thống | Trả lời câu hỏi | Không làm |
|---|---|---|
| **Microsoft Entra ID** | Người này là ai, tài khoản còn hoạt động không? | Không cấp session Moodle, không phân quyền course |
| **Okta** | Có đúng là người này không, có được dùng Moodle không? Mật khẩu, MFA, phát hành token OIDC | Không quản lý course hay role Moodle |
| **Moodle** | Vào rồi thì được làm gì? Account, session, role, ghi danh | Không nhận và không lưu mật khẩu Entra hay Okta |

Khi có sự cố, hỏi *"câu hỏi nào đang được trả lời sai"* là biết lỗi nằm ở hệ thống nào.

### 9.2. Luồng đồng bộ (nền)

```text
Entra ID ──(Microsoft Graph, Okta chủ động import)──► Okta Universal Directory
user + group                                           bản sao user + group
```

Chỉ đồng bộ profile và group, **không đồng bộ mật khẩu**. Thay đổi ở Entra chỉ sang Okta ở lần import kế tiếp; user bị
disable chỉ được phát hiện bởi **Full import**.

### 9.3. Luồng đăng nhập (OIDC Authorization Code)

```text
 ①  Trình duyệt → Moodle /login/index.php
 ②  Bấm "Sign in by Okta" → 302 → Okta /oauth2/v1/authorize?client_id&redirect_uri&state&scope
 ③  Okta: mật khẩu + Okta Verify; kiểm tra user Active, được assign app, policy MFA
 ④  Okta → 302 → Moodle /admin/oauth2callback.php?code=…&state=…
 ⑤  Moodle server → Okta /oauth2/v1/token (code + client secret; server-to-server, qua NAT)
 ⑥  Moodle server → Okta /oauth2/v1/userinfo → sub, email, given_name, family_name
 ⑦  Moodle tìm linked login theo (issuer, sub): có thì đăng nhập, chưa có thì tạo account
 ⑧  Cookie MoodleSession, role và ghi danh
```

Mật khẩu chỉ đi giữa trình duyệt và Okta. `code` đi qua trình duyệt nhưng vô dụng nếu thiếu client secret, vốn chỉ nằm
trên server Moodle.

### 9.4. Giá trị cấu hình

| Thành phần | Giá trị |
|---|---|
| Entra tenant | `m365x22866088.onmicrosoft.com` (tenant demo) |
| Group (Assigned, không lồng) | `moodle-students`, `moodle-teachers`, `moodle-denied` |
| User demo | `student.test`, `teacher.test`, một user thuộc `moodle-denied` |
| **Okta domain thật** | **`https://trial-3489054.okta.com`** (admin: `trial-3489054-admin.okta.com`) |
| Tên hiển thị của org (không phải domain) | `galaxyfinx-trial-3489054` |
| Authorization server | Org authorization server `/oauth2/v1/...` (không dùng `/oauth2/default`) |
| App Okta | `Moodle`, `oidc_client` (Web), chỉ Authorization Code, client secret, không PKCE |
| Quyền vào app | Chỉ group `moodle-students`, `moodle-teachers` |
| Policy | `Moodle Demo - MFA`: hai group trên cần mật khẩu + Okta Verify, xác thực lại sau 2 giờ; Catch-all **Denied** |
| Issuer trong Moodle | `Okta`, endpoint nhập tay, scope `openid profile email`, login domain của tenant, chỉ dùng cho trang login |
| Field mapping | `given_name`→`firstname`, `family_name`→`lastname`, `email`→`email`, **`sub`→`username`** |
| Redirect URI | `<URL Moodle>/admin/oauth2callback.php` (đổi mỗi khi dựng lại ALB) |

### 9.5. Vì sao thiết kế như vậy

| Quyết định | Lý do |
|---|---|
| OIDC Authorization Code, không SAML, không đồng bộ mật khẩu | Moodle có sẵn client OAuth 2; token ở phía server; Moodle không bao giờ thấy mật khẩu |
| Liên kết account theo `(issuer, sub)` | `sub` bất biến; email có thể đổi hoặc trùng. Kết hợp login domain chặn chiếm account qua trùng email |
| Mật khẩu và MFA ở Okta | Mật khẩu không rời IdP. Hai lớp chặn: assignment của app và policy (Catch-all Deny) |
| Group Okta chỉ quyết định "được vào Moodle hay không" | Role Teacher/Student do Moodle quản lý, để IdP không vô tình cấp quyền sửa course |
| Không dùng WS-Federation | WS-Fed chỉ cho đăng nhập Microsoft 365; bật nó sẽ federate cả domain. Domain `.onmicrosoft.com` cũng không federate được |
| Cấu hình Okta bằng tay | Module Terraform cho Okta từng bị bỏ vì lỗi sai domain (xem 13.2). Với domain đúng, Terraform cho Okta nhiều khả năng dùng lại được |
| Endpoint nhập tay | Quyết định khi discovery trả 403, cũng do sai domain. Trên domain đúng có thể dùng *Service base URL*; endpoint tay vẫn đúng |
| Org authorization server | Có ở mọi org, không cần add-on API Access Management |
| HTTP tạm thời | Kiểm chứng luồng sớm. `code` và cookie đi qua kết nối không mã hoá, nên **không dùng cho demo chính thức**. Chuyển HTTPS chỉ cần sửa redirect URI và `wwwroot`; account liên kết theo `sub` không đổi |

---

## 10. Phân quyền AWS IAM

```text
IAM user <infra-user> (access key + MFA) ──► Role InfraEngineer (MFA, phiên ≤ 4 giờ) ─┐
                                                                                       ├─► Policy InfraEngineerMoodle
IAM user <ai-user> (mật khẩu Console + MFA) ─► Role AIEngineer (MFA, phiên ≤ 4 giờ) ──┘   (chỉ ap-southeast-1, không quản lý IAM)
```

| Quyết định | Lý do |
|---|---|
| Không dùng root hay `AdministratorAccess` | Một lỗi thao tác hay một key bị lộ không được phép phá cả account |
| Quyền nằm ở **role**, không ở user | Credential role là tạm thời và bắt buộc MFA. Key lâu dài của user một mình không làm được gì |
| Mỗi vai trò một role, dùng chung policy | CloudTrail ghi rõ ai làm (`assumed-role/AIEngineer/...`); thu hồi một người không ảnh hưởng người kia |
| Chặn mọi region khác Singapore (trừ IAM, STS và các action Route 53 health check) | Giới hạn thiệt hại, tránh tài nguyên mồ côi vẫn tính tiền ở region khác |
| Không có quyền quản lý IAM | Thay đổi IAM là **chỉ-người**: kể cả Agent hay người dùng role cũng không tự nâng quyền được |
| KMS chỉ dùng "qua" RDS, Secrets Manager, EFS (`kms:ViaService`) | Mã hoá vẫn hoạt động nhưng không ai dùng khoá trực tiếp cho việc khác |
| Secrets Manager chỉ với `rds!*` | Chỉ đọc được secret master do RDS tự quản lý |
| Monitor có instance role riêng, chỉ `ec2:DescribeSecurityGroupRules` | Đủ để phát hiện SEC-01, không đủ để sửa |

**Rủi ro chấp nhận:** policy `InfraEngineerMoodle` cho phép terminate EC2, xoá RDS, xoá EFS trong staging. Nếu AI
Engineer chỉ cần quan sát, gắn policy chỉ đọc cho role `AIEngineer`. Quyền AWS và quyền SSH vào monitor là **hai lớp
độc lập**. Toàn văn policy và cách tạo nằm ở báo cáo triển khai, phụ lục A.

---

## 11. CI/CD

### 11.1. Ranh giới

| Thành phần | Chủ sở hữu | Thay đổi bằng |
|---|---|---|
| VPC, ALB, EC2, RDS, EFS, SG, IAM role monitor | Terraform, Infrastructure Engineer | `plan` được review rồi `apply` |
| Host runtime, giám sát, executor | Ansible và `automation/` | Chạy từ máy điều khiển tin cậy |
| Image Moodle | GitHub Actions + GHCR | Build theo SHA; CD staging triển khai |
| Image Agent | GitHub Actions + GHCR | Build theo SHA; **người** chọn và triển khai |
| Secret runtime | File trên host | Stage tay; không trong image, repo hay log |

### 11.2. Workflow theo sự kiện

| Sự kiện | `ci.yml` | `moodle-image.yml` | `agent-image.yml` | `cd-staging.yml` |
|---|---|---|---|---|
| PR vào `develop`/`main` | ✅ | — | — | — |
| Push `feature/**` | ✅ | ✅ khi `moodle/**` đổi | ✅ khi `agent_src/**` đổi | — |
| Push `develop` | ✅ | (CD tự build) | ✅ khi `agent_src/**` đổi | ✅ khi `moodle/**`, `release/moodle/**`, script deploy hoặc workflow đổi |
| Push `main` | ✅ | ✅ | ✅ | — |
| Chạy tay | — | ✅ | ✅ | ✅ |

Không có CD production. Workflow build image chạy **độc lập** với CI: có image không có nghĩa là đã qua CI.

### 11.3. Các bước CI (`lint-test-build`)

| Bước | Kiểm tra |
|---|---|
| Terraform | `fmt`, `validate`, `terraform test` với provider giả |
| Python | Cài dependency theo hash; `ruff --select E9,F63,F7,F82`; `pytest agent_src/tests` |
| Automation | `py_compile` các script Python, `bash -n` script shell; `moodle-scenario-verify.sh SEC-01` phải trả **64** |
| Replay | Replay offline 15 kịch bản với kết quả từng kịch bản được ghim (fail-closed cũng là đúng); không có thực thi live |
| Image Agent | Build; image **không** chứa `evaluation/ground_truth`; chế độ `live` phải escalate |
| Image Moodle | Build; đọc mật khẩu qua file; layout web root công khai |
| Sprint 2 replay | 15 `VERIFIED_DRY_RUN`, 0 thực thi bị cấm |
| Compose | `docker compose config` cho Moodle |
| Thông báo | Gửi Telegram khi có bước lỗi |

### 11.4. CD staging Moodle

```text
push develop (đúng path filter)
  → build-moodle (runner GitHub): build + push ghcr.io/<owner>/moodle:<sha>
  → deploy-moodle (runner tự host trên monitor-ai-01, nhãn self-hosted/linux/moodle-staging, environment "staging")
      → automation/github-deploy-moodle.sh: node B trước, rồi node A
          mỗi node: docker login GHCR → đổi MOODLE_IMAGE → compose pull → up -d --wait → healthz 127.0.0.1:8080
          node B: chỉ moodle-web (gỡ moodle-cron nếu sót); node A: moodle-web + moodle-cron
      → healthz qua ALB
```

- **Node B trước** để luôn còn một target healthy (rolling).
- **Runner tự host bắt buộc**, vì hai node nằm trong private subnet. Runner chạy bằng user `github-runner` không sudo;
  repo public nên PR từ fork phải được duyệt (`all_external_contributors`).
- CD **chỉ đổi image**: không tạo database, không mount EFS, không chạy Terraform. Script **không tự rollback**;
  rollback là deploy lại một SHA đã biết healthy.

---

## 12. Tính sẵn sàng, chi phí và giới hạn

| Chủ đề | Hiện trạng |
|---|---|
| Mất một node web | ALB vẫn phục vụ qua node còn lại; không có ASG tự thay |
| Mất node A | Web vẫn chạy trên B, **cron tạm dừng** tới khi A phục hồi |
| Database | Single-AZ, không failover |
| Giám sát | Một máy duy nhất. Mất monitor ảnh hưởng quan sát và Agent, không ảnh hưởng đường phục vụ người dùng |
| HTTPS | Chưa có; staging chạy HTTP |
| Chi phí liên tục | 2 NAT Gateway (lớn nhất, vẫn tính khi EC2 dừng), ALB, RDS, EFS, EIP, 3 EC2. Dừng EC2 không xoá các khoản còn lại |
| Terraform state | Cục bộ (`terraform/terraform.tfstate`), không remote backend: một người, một máy quản lý một môi trường |

---

## 13. Bằng chứng kiểm chứng và vấn đề còn mở

### 13.1. Đã kiểm chứng

| Hạng mục | Kết quả |
|---|---|
| Kiểm thử Terraform với provider giả | `terraform test` đạt (22/22 theo lần kiểm tra ngày 25/10/2026) |
| Dựng hạ tầng thật | Đã dựng và vận hành; `terraform plan` cuối phiên trả "No changes" |
| Topology | CIDR không chồng nhau; app route qua NAT cùng AZ; data subnet không có route Internet; không còn ingress `0.0.0.0/0` vào máy |
| Runtime | 2 target ALB `healthy`; EFS mount `nfs4` trên cả hai node; chỉ node A có cron |
| SSO | Đăng nhập đầu-cuối `student.test` qua Okta đã xác nhận trong DB (22/10/2026); `moodle-denied` bị Okta chặn |

### 13.2. Sự cố đã gặp khi dựng hạ tầng và SSO

| Sự cố | Nguyên nhân gốc | Bài học |
|---|---|---|
| Okta trả 400 trống, API token "invalid", discovery 403 | Dùng **tên hiển thị** `galaxyfinx-trial-3489054` làm subdomain; domain thật là `trial-3489054` | Lấy định danh từ thanh địa chỉ hoặc output API, không từ nhãn giao diện. Khi **mọi thứ** cùng hỏng, nghi ngờ thứ chung nhất trước |
| Chẩn đoán sai "org Okta bị hỏng", đề xuất tạo org mới | Thiếu giả thuyết "đang gọi nhầm đối tượng" | Tìm nguyên nhân gốc trước khi làm lại |
| `RunInstances` báo `PendingVerification` | AWS đang xác minh account mới | Chờ email xác minh rồi plan lại; không destroy |
| Ansible `UNREACHABLE` | Host key EC2 mới chưa được tin | Không tắt kiểm tra; đối chiếu fingerprint qua console output |
| Secret bị lộ qua ảnh chụp và editor | Gõ token trên dòng lệnh, mở CSV access key | Thu hồi và thay mới, không chỉ xoá ảnh; luôn `git check-ignore` file chứa bí mật |

### 13.3. Vấn đề còn mở

| # | Vấn đề | Ưu tiên |
|---|---|---|
| 1 | HTTPS cho ALB (ACM + hostname cố định), cập nhật redirect URI | Cao, trước demo chính thức |
| 2 | Redirect URI Okta phải sửa tay sau mỗi lần dựng lại (DNS ALB đổi) | Trung bình; giải bằng hostname cố định hoặc Terraform cho Okta |
| 3 | `MoodleNodeMemoryPressure` nhạy quá mức khi container khởi động | Trung bình |
| 4 | Một lần 504 không tái hiện được (ALB và target đều healthy, không có trong access log) | Thấp; ghi URL và thời điểm nếu lặp lại |
| 5 | Monitor đơn lẻ, RDS Single-AZ | Giới hạn chấp nhận cho staging |
| 6 | Runner CD cài tay | Nên đưa vào playbook |

Các request tới `wp-login.php` trong access log là bot quét lỗ hổng WordPress, vô hại với Moodle.
