# Báo cáo hướng dẫn triển khai và vận hành Moodle staging

**Cập nhật:** 26/10/2026
**Dành cho:** DevOps mới vào dự án, đã biết Linux, Git và Docker ở mức cơ bản, chưa từng deploy hệ thống này.
**Mục tiêu:** sau khi đọc xong, bạn **tự dựng được** toàn bộ hạ tầng Moodle staging trên AWS từ con số 0, hiểu
**vì sao** từng bước tồn tại, và biết cách tự chẩn đoán khi một bước thất bại.

Tài liệu này gộp hướng dẫn deploy, quy trình vận hành, hướng dẫn phân quyền AWS IAM và các bước cấu hình Okta/Entra.
Kiến trúc và lý do thiết kế nằm ở [BAO_CAO_KIEN_TRUC_HA_TANG](BAO_CAO_KIEN_TRUC_HA_TANG.md); diễn tập sự cố và demo
nằm ở [BAO_CAO_KICH_BAN_LOI](BAO_CAO_KICH_BAN_LOI.md); Agent, Safety Gate, Executor và Verifier nằm ở
[BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN](BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN.md). Khi tài liệu khác mã nguồn,
mã nguồn thắng.

> ⚠️ **Chi phí:** hạ tầng này **tính tiền theo giờ** ngay sau `terraform apply`
> (2 NAT Gateway, ALB, RDS, EFS, 3 EC2). NAT Gateway là khoản lớn nhất và vẫn
> tính tiền khi EC2 đã stop. Chỉ dựng khi có kế hoạch dùng, và destroy theo
> phụ lục D khi xong.

---

## Mục lục

1. [Bức tranh tổng thể](#1-bức-tranh-tổng-thể)
2. [Bộ công cụ và vai trò của từng công cụ](#2-bộ-công-cụ-và-vai-trò-của-từng-công-cụ)
3. [Luồng deploy từ đầu đến cuối](#3-luồng-deploy-từ-đầu-đến-cuối)
4. [Giai đoạn 0: Chuẩn bị máy điều khiển](#4-giai-đoạn-0-chuẩn-bị-máy-điều-khiển)
5. [Giai đoạn 1: Lấy quyền AWS có MFA](#5-giai-đoạn-1-lấy-quyền-aws-có-mfa)
6. [Giai đoạn 2: Khai báo đầu vào Terraform](#6-giai-đoạn-2-khai-báo-đầu-vào-terraform)
7. [Giai đoạn 3: Dựng hạ tầng bằng Terraform](#7-giai-đoạn-3-dựng-hạ-tầng-bằng-terraform)
8. [Giai đoạn 4: Tin cậy host key và kiểm tra kết nối](#8-giai-đoạn-4-tin-cậy-host-key-và-kiểm-tra-kết-nối)
9. [Giai đoạn 5: Cấu hình host bằng Ansible](#9-giai-đoạn-5-cấu-hình-host-bằng-ansible)
10. [Giai đoạn 6: Chọn image Moodle](#10-giai-đoạn-6-chọn-image-moodle)
11. [Giai đoạn 7: Secret, stage release và khởi động Moodle](#11-giai-đoạn-7-secret-stage-release-và-khởi-động-moodle)
12. [Giai đoạn 8: Nghiệm thu](#12-giai-đoạn-8-nghiệm-thu)
13. [Giai đoạn 9: Khôi phục đăng nhập Okta](#13-giai-đoạn-9-khôi-phục-đăng-nhập-okta)
14. [Giai đoạn 10: Monitoring và AI Agent](#14-giai-đoạn-10-monitoring-và-ai-agent)
15. [Giai đoạn 11: Bật CD cho các lần release sau](#15-giai-đoạn-11-bật-cd-cho-các-lần-release-sau)
16. [Vận hành hằng ngày: release, rollback, kiểm tra drift](#16-vận-hành-hằng-ngày-release-rollback-kiểm-tra-drift)
17. [Gỡ toàn bộ hạ tầng](#17-gỡ-toàn-bộ-hạ-tầng)
18. [Tư duy xử lý sự cố](#18-tư-duy-xử-lý-sự-cố)
19. [Checklist một trang](#19-checklist-một-trang)
20. [Phụ lục A: Phân quyền AWS IAM (dành cho admin)](#phụ-lục-a-phân-quyền-aws-iam-dành-cho-admin)
21. [Phụ lục B: Cấu hình Entra ID, Okta và OAuth 2 của Moodle](#phụ-lục-b-cấu-hình-entra-id-okta-và-oauth-2-của-moodle)
22. [Phụ lục C: Kết thúc phiên và bàn giao](#phụ-lục-c-kết-thúc-phiên-và-bàn-giao)
23. [Phụ lục D: Gỡ toàn bộ hạ tầng (chi tiết)](#phụ-lục-d-gỡ-toàn-bộ-hạ-tầng-chi-tiết)

---

## 1. Bức tranh tổng thể

### 1.1. Hệ thống trông như thế nào

```text
Người dùng (Internet)
  └─► ALB (Application Load Balancer) ở 2 public subnet, 2 AZ
        ├─► moodle-app-a (EC2, private subnet App A) ─┐
        └─► moodle-app-b (EC2, private subnet App B) ─┤
                                                      ├─► RDS PostgreSQL (private, Single-AZ)
                                                      └─► EFS (moodledata dùng chung, 1 mount target / AZ)

Quản trị viên (máy của bạn)
  └─► monitor-ai-01 (EC2 có IP public, SSH chỉ mở cho IP của bạn)
        ├─ Prometheus, Alertmanager, Grafana, Blackbox, AI Agent, Celery, Redis
        └─ SSH ProxyJump ─► 2 node Moodle private
```

Ba điều cần nhớ:

- **Hai node Moodle nằm trong private subnet.** Bạn không SSH thẳng vào được;
  mọi kết nối đi qua `monitor-ai-01` (bastion). Ra Internet (pull image) đi qua
  NAT Gateway.
- **Dữ liệu có hai nơi:** PostgreSQL (RDS) là nguồn sự thật; EFS chứa file
  người dùng tải lên (`moodledata`) để hai node thấy cùng một dữ liệu.
- **Chỉ node A chạy cron.** Hai cron chạy song song sẽ làm Moodle thực hiện
  trùng các tác vụ định kỳ.

### 1.2. Bốn lớp, bốn công cụ, ranh giới rõ ràng

| Lớp | Thứ được tạo | Công cụ | Ai chạy |
|---|---|---|---|
| **Hạ tầng cloud** | VPC, subnet, NAT, ALB, EC2, RDS, EFS, security group | Terraform | Bạn, từ máy điều khiển |
| **Cấu hình host** | Docker, mount EFS, file cấu hình, monitoring stack | Ansible + script `automation/` | Bạn, từ máy điều khiển |
| **Ứng dụng** | Image Moodle / AI Agent | GitHub Actions build, đẩy lên GHCR | Tự động khi push code |
| **Release** | Đổi image đang chạy trên 2 node | Script thủ công hoặc CD staging | Bạn, hoặc CD qua runner tự host |

**Vì sao phải tách lớp?** Mỗi lớp có tốc độ thay đổi và mức rủi ro khác nhau.
Release Moodle diễn ra nhiều lần một tuần; tạo/xóa RDS thì hiếm và có thể mất
dữ liệu. Nếu gộp chung, một lần release nhỏ có thể vô tình đụng vào database.
Vì vậy dự án có quy tắc cứng:

- GitHub Actions **không bao giờ** chạy Terraform hay Ansible.
- **Không build image trên EC2.** EC2 chỉ pull image CI đã build và test.
- Terraform chỉ lo tài nguyên cloud, Ansible chỉ lo cấu hình bên trong máy.

---

## 2. Bộ công cụ và vai trò của từng công cụ

| Công cụ | Dùng để làm gì trong dự án | Vì sao chọn nó |
|---|---|---|
| **Git + GitHub** | Lưu code; mọi thay đổi đi qua branch và pull request | Có lịch sử, review, và kích hoạt CI |
| **GitHub Actions** | CI (lint, test, replay, build kiểm tra) và CD (build + deploy Moodle) | Gắn liền với repo, không cần server CI riêng |
| **GHCR** (`ghcr.io`) | Registry chứa image, tag theo commit SHA 40 ký tự | Image bất biến: biết chính xác code nào đang chạy |
| **Docker / Docker Compose** | Đóng gói và chạy Moodle, monitoring trên EC2 | Môi trường chạy giống nhau ở CI và EC2 |
| **Terraform** (>= 1.6, provider AWS ~> 5.0) | Khai báo toàn bộ tài nguyên AWS trong `terraform/` | Infrastructure as Code: `plan` cho thấy trước sẽ thay đổi gì |
| **Ansible** | Cài Docker, mount EFS, triển khai monitoring trong `ansible/` | Không cần agent trên máy đích, chỉ cần SSH; chạy lại nhiều lần vẫn an toàn (idempotent) |
| **AWS CLI v2** | Lấy credential có MFA, kiểm tra tài nguyên, xem console output EC2 | Kênh kiểm chứng độc lập với Terraform |
| **OpenSSH** | Truy cập node qua bastion bằng file SSH config do Terraform sinh | Không mở cổng quản trị ra Internet |
| **jq, curl, openssl** | Đọc output JSON, gọi health check, sinh mật khẩu ngẫu nhiên | Dùng trong hầu hết script `automation/` |
| **psql** (PostgreSQL client >= 15) | `stage-moodle-release.sh` tạo role DB; chạy trong Docker hoặc client local | Cần khi máy không có Docker daemon |
| **Okta + Entra ID** | Đăng nhập SSO cho Moodle qua OIDC | Phần xác thực của đề tài |

**Terraform state nằm ở máy local** (`terraform/terraform.tfstate`, không có
remote backend). Hệ quả:

- Chỉ **một người, một máy** quản lý một môi trường tại một thời điểm.
- Mất file state đồng nghĩa Terraform "quên" toàn bộ tài nguyên đang chạy.
  Hãy giữ nó an toàn và **không bao giờ commit** nó.

---

## 3. Luồng deploy từ đầu đến cuối

```text
 [0] Chuẩn bị máy ──► [1] Quyền AWS (MFA) ──► [2] deployment.tfvars
                                                     │
 [5] Ansible chuẩn bị host ◄── [4] Host key + ping ◄─┴── [3] Terraform plan → apply
        │
        ▼
 [6] Chọn image SHA trên GHCR ──► [7] Secret → stage → install → start
                                                     │
 [11] Bật CD ◄── [10] Monitoring + Agent ◄── [9] Okta ◄── [8] Nghiệm thu
```

Thời gian tham khảo cho lần đầu: khoảng 1–2 giờ. Riêng `terraform apply` mất
khoảng 15–25 phút, chủ yếu chờ RDS và NAT Gateway.

**Mỗi giai đoạn có một "cổng kiểm tra" (✅).** Chưa qua cổng thì không làm
bước kế tiếp. Lỗi ở bước sau thường là hậu quả của một bước trước đã hỏng mà
không ai để ý; dừng đúng chỗ giúp bạn biết lỗi nằm ở lớp nào.

> Chạy **mọi lệnh từ thư mục gốc của repo**. Script dùng đường dẫn tương đối;
> chạy từ `automation/` sẽ tạo file ở chỗ không được `.gitignore` bảo vệ.

---

## 4. Giai đoạn 0: Chuẩn bị máy điều khiển

"Máy điều khiển" là máy tin cậy của bạn: nó giữ private key, Terraform state
và các file secret.

```bash
# macOS (Homebrew). Linux: dùng trình quản lý gói tương đương.
brew install terraform awscli ansible jq openssl gh   # gh: GitHub CLI, có thể cài cách khác nếu brew bị khoá quyền
brew install libpq coreutils          # psql khi không có Docker; GNU timeout cho test

# libpq là gói "keg-only": Homebrew KHÔNG tự đưa psql vào PATH.
# Ghi vào ~/.zshrc để mọi terminal mới đều có, rồi nạp lại:
echo 'export PATH="/opt/homebrew/opt/libpq/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

git clone <repo-url> && cd <repo>
git switch develop && git pull
```

**Đăng nhập GitHub CLI (`gh`).** Cần cho việc đọc kết quả CI khi repo private,
cho `gh secret set` và `gh workflow run` ở mục 15. Nếu `brew install gh` báo
lỗi quyền (`sudo chown -R ... /opt/homebrew`) vì Homebrew do tài khoản IT quản
lý, **đừng chạy lệnh `chown` đó**. Hãy cài `gh` theo cách khác, ví dụ bản
binary đặt trong `~/.local/bin`.

```bash
gh auth login
# GitHub.com → HTTPS → Authenticate Git: Y → Login with a web browser
# Terminal in ra:  ! First copy your one-time code: ABCD-1234
#                  Press Enter to open https://github.com/login/device ...
```

Đây là *device flow*. Mã **chỉ hiện trong terminal**, không được gửi tới app
hay email nào. Bạn tự chép mã đó sang trang `github.com/login/device` trên
trình duyệt đã đăng nhập, bấm **Continue** rồi **Authorize**. GitHub thiết kế
như vậy để chỉ người đang ngồi trước cả terminal lẫn trình duyệt mới hoàn tất
được; vì thế trang ghi *"Never use a code sent by someone else"*. Lỗi thường
gặp:

- Mở trang device bằng tay trong khi `gh auth login` không còn chạy: không có
  mã nào để nhập. Chạy lại `gh auth login`.
- Mã chỉ sống khoảng 15 phút. Hết hạn thì `Ctrl+C` và chạy lại.

**Docker Desktop hay psql local?** `stage-moodle-release.sh` cần `psql` để tạo
role database. Nó dùng container postgres khi Docker daemon đang chạy
(`docker info` thành công), và dùng `psql` local (>= 15) khi không có Docker.
Bạn chỉ cần **một trong hai**. Nếu chỉ `export PATH` trong một terminal mà
không ghi vào `~/.zshrc`, terminal mới sẽ không thấy `psql`.

Tạo SSH key cho hạ tầng (chỉ lần đầu). Public key được Terraform đẩy lên AWS;
private key **không bao giờ** rời máy bạn:

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/moodle-aiops -C "you@example.com"
chmod 600 ~/.ssh/moodle-aiops
```

✅ **Cổng kiểm tra:**

```bash
terraform version          # >= 1.6
aws --version              # aws-cli/2.x
ansible-playbook --version
jq --version
psql --version             # >= 15, HOẶC docker info chạy được (Docker Desktop đang mở)
gh auth status             # đã đăng nhập GitHub
```

---

## 5. Giai đoạn 1: Lấy quyền AWS có MFA

Bạn **không** dùng root hay `AdministratorAccess`. Admin cấp cho bạn một IAM
user gần như không có quyền, chỉ được assume role `InfraEngineer` khi có MFA
(xem phụ lục A.2). Quyền thật nằm ở
role, credential tạm thời tối đa 4 giờ, và chỉ dùng được ở region
`ap-southeast-1`.

Cấu hình profile `target-account` trong `~/.aws/config` (giá trị lấy từ admin):

```ini
[profile target-account]
role_arn = arn:aws:iam::<ACCOUNT_ID>:role/InfraEngineer
source_profile = <base-profile>
mfa_serial = arn:aws:iam::<ACCOUNT_ID>:mfa/<mfa-device>
region = ap-southeast-1
duration_seconds = 14400
```

**Vấn đề:** Terraform không hỏi được mã MFA. **Giải pháp:** để AWS CLI hỏi
MFA, rồi xuất credential tạm ra biến môi trường cho Terraform dùng:

```bash
aws sts get-caller-identity --profile target-account      # nhập mã MFA
eval "$(aws configure export-credentials --profile target-account --format env)"
unset AWS_PROFILE                 # nếu không, Terraform lại đòi MFA và báo lỗi
export AWS_REGION=ap-southeast-1
```

✅ **Cổng kiểm tra:**

```bash
aws sts get-caller-identity
# "Arn" phải chứa  assumed-role/InfraEngineer/...
# Nếu thấy  user/...  thì bạn CHƯA vào role.
```

### Khi nào phải chạy lại khối lệnh trên

Các biến `AWS_*` do `eval` tạo ra **chỉ sống trong đúng terminal đó**. Chúng
mất khi bạn:

- mở terminal hoặc tab mới, hay mở lại VS Code;
- để credential hết hạn (tối đa 4 giờ);
- chạy `unset AWS_PROFILE` mà quên bước `eval` phía trước.

Thói quen an toàn: **trước mỗi lệnh `terraform plan`/`apply` và mỗi script
`automation/`**, chạy `aws sts get-caller-identity` (không kèm `--profile`).
Nếu nó không trả về `assumed-role/InfraEngineer/...`, chạy lại khối lệnh trên.
Trước một lần `apply` dài (khoảng 20 phút), hãy lấy credential mới để nó không
hết hạn giữa chừng.

### Đọc lỗi credential: mỗi thông báo chỉ tới một nguyên nhân

Terraform tìm credential theo thứ tự: biến môi trường → `AWS_PROFILE` / file
`~/.aws` → IMDS (dịch vụ metadata, **chỉ có bên trong EC2**). Thông báo lỗi
cho biết nó dừng ở bước nào:

| Thông báo | Nghĩa là | Cách sửa |
|---|---|---|
| `No valid credential sources found` kèm `169.254.169.254 ... host is down` | Terminal **không có biến `AWS_*` và không có `AWS_PROFILE`**. Terraform phải thử tới IMDS và thất bại, vì máy bạn không phải EC2 | Chạy lại khối lệnh `eval` ở trên **trong chính terminal đó** |
| `AssumeRoleTokenProvider session option not set` | Vẫn còn `AWS_PROFILE` trỏ tới role cần MFA; Terraform không hỏi được MFA | `eval ...` rồi `unset AWS_PROFILE` |
| `ExpiredToken` / `The security token included in the request is expired` | Đã export, nhưng quá 4 giờ | Chạy lại khối lệnh `eval` |
| `InvalidClientTokenId` (ngay ở lệnh `aws sts ... --profile target-account`) | Access key gốc của IAM user đã bị vô hiệu hoá hoặc xoay vòng | `aws configure --profile <base-profile>` với key mới |
| `MultiFactorAuthentication failed` | Mã MFA sai, đã dùng rồi, hoặc đồng hồ máy bị lệch | Đợi mã mới; kiểm tra giờ hệ thống |

Dòng `169.254.169.254` **không phải** nguyên nhân gốc. Đó chỉ là nguồn cuối
cùng Terraform thử sau khi mọi nguồn khác đều trống. Đừng sửa mạng hay IMDS.

---

## 6. Giai đoạn 2: Khai báo đầu vào Terraform

```bash
cp -n terraform/deployment.tfvars.example terraform/deployment.tfvars
bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars
```

Script thứ hai tự lấy IP public hiện tại của bạn và ghi vào `my_ip_cidr`.
Security group chỉ mở SSH/Grafana cho đúng IP này. **Đổi mạng (về nhà, quán
cà phê) thì chạy lại script này rồi `plan` + `apply`**, nếu không SSH sẽ
timeout.

Mở `terraform/deployment.tfvars` và kiểm tra:

| Biến | Đặt thế nào | Vì sao |
|---|---|---|
| `public_key_path`, `private_key_path` | `~/.ssh/moodle-aiops.pub`, `~/.ssh/moodle-aiops` | Key vừa tạo ở giai đoạn 0 |
| `ec2_ami_id` | **Bắt buộc ghim** (lệnh bên dưới); file example chưa có dòng này, hãy thêm vào | Không ghim thì mỗi khi AWS ra AMI mới, `plan` sẽ đòi **thay cả 3 EC2** |
| `my_ip_cidr` | `x.x.x.x/32` (script đã điền) | Chỉ một IP, không bao giờ `0.0.0.0/0` |
| `moodle_allow_http` | `true` cho staging ngắn hạn | HTTPS cần chứng chỉ ACM và hostname |

```bash
aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text
# Ghi kết quả vào deployment.tfvars:  ec2_ami_id = "ami-..."
```

`deployment.tfvars` bị Git ignore. **Không commit** file này.

---

## 7. Giai đoạn 3: Dựng hạ tầng bằng Terraform

### 7.1. Kiểm tra code trước khi đụng tới AWS

```bash
terraform -chdir=terraform init -input=false
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform test          # test với provider giả, không tạo tài nguyên
```

### 7.2. Plan, đọc plan, rồi mới apply

```bash
umask 077                                 # file mới chỉ bạn đọc được
mkdir -p terraform/.artifacts

terraform -chdir=terraform plan -input=false \
  -var-file=deployment.tfvars -out=.artifacts/deployment.tfplan

terraform -chdir=terraform show -no-color .artifacts/deployment.tfplan | less
```

**Cách đọc plan** (đây là kỹ năng quan trọng nhất của giai đoạn này):

| Ký hiệu | Nghĩa | Phản ứng |
|---|---|---|
| `+ create` | Tạo mới | Bình thường khi dựng lần đầu |
| `~ update in-place` | Sửa tại chỗ | Đọc kỹ thuộc tính nào đổi |
| `-/+ replace` | **Xóa rồi tạo lại** | ⚠️ Dừng lại. Với EC2/RDS/EFS thì có thể **mất dữ liệu**. Tìm dòng `# forces replacement` để biết nguyên nhân |
| `- destroy` | Xóa | ⚠️ Chỉ chấp nhận khi bạn chủ động gỡ |

Với môi trường mới, dòng cuối nên là `Plan: N to add, 0 to change, 0 to destroy`.
Nếu thấy `to destroy` > 0 trong khi bạn chỉ muốn dựng thêm, **đừng apply**;
hãy hỏi người phụ trách.

Apply **đúng file plan vừa review** (không chạy `terraform apply` trần, vì
lệnh đó sẽ tính lại plan và có thể khác cái bạn đã đọc):

```bash
terraform -chdir=terraform apply .artifacts/deployment.tfplan
```

**Không ngắt (Ctrl+C) khi đang apply.** Nếu bị ngắt, AWS có thể đã tạo tài
nguyên mà state chưa ghi nhận. Xem mục 18, lỗi "apply bị ngắt".

✅ **Cổng kiểm tra:**

```bash
terraform -chdir=terraform output -json moodle_application | jq .   # url, nodes, target_group_arn
terraform -chdir=terraform output -json moodle_database | jq .
terraform -chdir=terraform output -json moodle_filesystem | jq .
```

Lúc này ALB target **đang unhealthy**. Đó là **đúng**, vì chưa có gì chạy
trên cổng 8080. Đừng "sửa" bằng cách dựng một web server giả.

---

## 8. Giai đoạn 4: Tin cậy host key và kiểm tra kết nối

Terraform sinh sẵn SSH config và inventory Ansible:

```bash
umask 077
terraform -chdir=terraform output -raw moodle_ssh_config        > terraform/.artifacts/moodle-ssh.config
terraform -chdir=terraform output -raw moodle_ansible_inventory > terraform/.artifacts/moodle-inventory.yml
chmod 600 terraform/.artifacts/moodle-ssh.config
```

### 8.1. Bước này để làm gì

Mỗi EC2 tự sinh một **host key** ở lần boot đầu tiên. Đây là "chứng minh thư"
của máy. SSH config do Terraform sinh
([moodle-ssh.tftpl](../terraform/templates/moodle-ssh.tftpl)) đặt
`StrictHostKeyChecking yes`, nên máy có key chưa nằm trong `~/.ssh/known_hosts`
bị **từ chối thẳng**. Ansible khi đó báo
`Connection closed by UNKNOWN port 65535`.

**Vì sao không gõ "yes" hay tắt kiểm tra cho nhanh?** Làm vậy là tin bất cứ
máy nào trả lời đầu tiên. Nếu có kẻ đứng giữa (man-in-the-middle), lần chạy
Ansible đầu tiên sẽ gửi cho nó quyền `sudo`, mật khẩu DB và các secret. Cách
đúng là **đối chiếu qua hai kênh độc lập**:

| Kênh | Lệnh | Vai trò |
|---|---|---|
| AWS API (HTTPS, IAM + MFA, không đi qua SSH) | `aws ec2 get-console-output` | Fingerprint **chuẩn**, do chính EC2 in ra lúc boot |
| Mạng SSH | `ssh-keyscan` | Key mà máy đang trả lời SSH đưa ra, tức thứ bạn **sắp tin** |

Hai giá trị `SHA256:` khớp nhau thì mới thêm key vào `known_hosts`.

### 8.2. Ba host, ba IP

| Host | IP dùng trong `known_hosts` | Scan từ đâu | Lấy IP / instance-id |
|---|---|---|---|
| `monitor-ai-01` | Elastic IP **public** | Máy bạn, scan trực tiếp | `terraform -chdir=terraform output -raw monitor_public_ip` |
| `moodle-app-a` | IP **private** (subnet App A) | **Qua monitor**, vì máy bạn không tới được IP private | `... output -json moodle_application \| jq -r '.nodes.a.private_ip, .nodes.a.id'` |
| `moodle-app-b` | IP **private** (subnet App B) | Qua monitor | `... \| jq -r '.nodes.b.private_ip, .nodes.b.id'` |

**Thứ tự bắt buộc: monitor trước, A/B sau.** Kết quả scan A/B do monitor trả
về, nên nó chỉ đáng tin khi bạn **đã xác minh monitor**. Logic giống chuỗi
chứng chỉ: tin gốc trước, rồi mới tin những gì đi qua gốc.

### 8.3. Quy trình

Kiểm tra credential còn hạn trước (`aws sts get-caller-identity` phải ra
`assumed-role/InfraEngineer`; nếu không, làm lại mục 5).

```bash
TF="terraform -chdir=terraform"
SSH_CFG=terraform/.artifacts/moodle-ssh.config
MONITOR_IP="$($TF output -raw monitor_public_ip)"
MONITOR_ID="$(aws ec2 describe-instances --filters "Name=ip-address,Values=$MONITOR_IP" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
APP_A_IP="$($TF output -json moodle_application | jq -r .nodes.a.private_ip)"
APP_A_ID="$($TF output -json moodle_application | jq -r .nodes.a.id)"
APP_B_IP="$($TF output -json moodle_application | jq -r .nodes.b.private_ip)"
APP_B_ID="$($TF output -json moodle_application | jq -r .nodes.b.id)"
echo "monitor $MONITOR_ID $MONITOR_IP | A $APP_A_ID $APP_A_IP | B $APP_B_ID $APP_B_IP"

# Hàm in fingerprint "thật" của một instance từ console output (kênh AWS API)
console_fp() {
  aws ec2 get-console-output --latest --instance-id "$1" --output text --query Output \
    | sed -n '/BEGIN SSH HOST KEY FINGERPRINTS/,/END SSH HOST KEY FINGERPRINTS/p' | grep ED25519
}

# 1. Xóa key cũ của các IP có thể đã được tái sử dụng từ lần dựng trước
for ip in "$MONITOR_IP" "$APP_A_IP" "$APP_B_IP"; do ssh-keygen -R "$ip" >/dev/null 2>&1 || true; done
```

**Bước 2: monitor.** Scan vào file tạm, kiểm tra file không rỗng, rồi so:

```bash
ssh-keyscan -T 10 -t ed25519 "$MONITOR_IP" 2>/dev/null > /tmp/monitor.key
test -s /tmp/monitor.key && ssh-keygen -lf /tmp/monitor.key || echo "RỖNG: chưa scan được, đừng thêm"
console_fp "$MONITOR_ID"
# Hai SHA256 KHỚP → thêm và xác nhận đã vào file:
cat /tmp/monitor.key >> ~/.ssh/known_hosts && rm /tmp/monitor.key
ssh-keygen -F "$MONITOR_IP"                 # phải thấy dòng key
```

**Bước 3: app A và app B.** Cùng quy trình, nhưng `ssh-keyscan` chạy **trên
monitor**:

```bash
scan_node() {   # $1 = A|B, $2 = instance-id, $3 = IP private (chạy được cả bash và zsh)
  echo "== app $1 ($2, $3)"
  ssh -F "$SSH_CFG" monitor-ai-01 "ssh-keyscan -T 10 -t ed25519 $3" 2>/dev/null > "/tmp/app-$1.key"
  test -s "/tmp/app-$1.key" && ssh-keygen -lf "/tmp/app-$1.key" || echo "RỖNG: chưa scan được, đừng thêm"
  console_fp "$2"
}
scan_node A "$APP_A_ID" "$APP_A_IP"
scan_node B "$APP_B_ID" "$APP_B_IP"
```

So **từng cặp**. Chỉ cặp nào khớp thì mới thêm:

```bash
cat /tmp/app-A.key >> ~/.ssh/known_hosts && rm /tmp/app-A.key && ssh-keygen -F "$APP_A_IP"
cat /tmp/app-B.key >> ~/.ssh/known_hosts && rm /tmp/app-B.key && ssh-keygen -F "$APP_B_IP"
```

**Vì sao phải dùng file tạm và `test -s`?** Nếu `ssh-keyscan` không nhận được
gì (node chưa bật xong `sshd`, timeout), thì lệnh dạng `... >> known_hosts`
sẽ **ghi chuỗi rỗng mà không báo lỗi**. Trông như thành công, nhưng không có
key nào được thêm. Dự án đã gặp đúng tình huống này với app A. Luôn xác nhận
bằng `ssh-keygen -F <ip>` sau khi thêm.

### 8.4. Các tình huống thường gặp

| Hiện tượng | Nghĩa là | Làm gì |
|---|---|---|
| `WARNING: connection is not using a post-quantum key exchange algorithm` | Máy bạn chạy OpenSSH ≥ 10, còn server AL2023 không đưa ra thuật toán trao đổi khoá chống lượng tử. Kết nối **vẫn được mã hoá và xác thực đầy đủ** | Chỉ là cảnh báo, bỏ qua với staging. Nó in ra stderr, không lọt vào `known_hosts` |
| Bước scan in `RỖNG` | Không nhận được key | Phép thử: `ssh -F "$SSH_CFG" monitor-ai-01 "timeout 5 bash -c '</dev/tcp/<ip>/22' && echo open"`. Thấy `open` thì `sshd` chưa sẵn sàng: đợi rồi scan lại. Không thấy `open` thì kiểm tra security group và route |
| `console_fp` không in gì | Console output chưa có (thường trong vài phút đầu sau boot) | Đợi rồi chạy lại |
| Sau lần SSH đầu, `known_hosts` có thêm dòng **ECDSA** | OpenSSH (`UpdateHostKeys`) tự học thêm các key khác của server **qua kết nối đã được xác thực bằng key ED25519** bạn vừa tin | Bình thường, an toàn |
| Hai SHA256 **không khớp** | Thường là sai thao tác: ghép nhầm IP ↔ instance-id, so nhầm loại key (ECDSA với ED25519, MD5 với SHA256), hoặc output Terraform đã cũ sau khi instance bị thay | **Không thêm key.** Loại lần lượt từng nguyên nhân sai thao tác. Không giải thích được thì coi là sự cố an ninh: dừng, giữ bằng chứng, báo admin, thay instance bằng `terraform plan -replace=aws_instance.<tên>` rồi làm lại mục 8. Nếu nghi monitor thì thay **monitor trước** |

### 8.5. Cổng kiểm tra

```bash
for ip in "$MONITOR_IP" "$APP_A_IP" "$APP_B_IP"; do ssh-keygen -F "$ip" | grep -c ED25519; done   # mỗi dòng ≥ 1
ssh -F "$SSH_CFG" moodle-app-a hostname      # ip-<IP A dạng gạch nối>...
ssh -F "$SSH_CFG" moodle-app-b hostname      # ip-<IP B dạng gạch nối>...
ansible -i terraform/.artifacts/moodle-inventory.yml all -m ping
# Cả 3 host phải trả về "pong"
```

Ba `pong` chứng minh cùng lúc bốn điều: mạng thông, security group cho phép IP
của bạn, private key đúng, và host key đã được tin đúng cách.

Làm lại toàn bộ mục 8 mỗi khi Terraform tạo hoặc thay một instance.

---

## 9. Giai đoạn 5: Cấu hình host bằng Ansible

```bash
bash automation/prepare-moodle-runtime.sh
```

Script này lấy output Terraform, rồi chạy playbook
`ansible/playbooks/prepare-moodle-runtime.yml` để:

- cài Docker và Docker Compose;
- cài công cụ EFS và mount EFS (có TLS, qua access point) vào
  `/mnt/efs/moodledata`;
- chuẩn bị thư mục dữ liệu dùng chung.

Nó **không** build image, **không** lấy mật khẩu RDS, **không** khởi động
Moodle. Playbook là idempotent: chạy lại nhiều lần cũng an toàn.

✅ **Cổng kiểm tra:** EFS phải thực sự được mount (nếu không, Moodle sẽ ghi
file vào ổ cục bộ của từng node và hai node lệch dữ liệu mà không báo lỗi):

```bash
for host in moodle-app-a moodle-app-b; do
  ssh -F terraform/.artifacts/moodle-ssh.config "$host" \
    'findmnt --noheadings --output FSTYPE,TARGET --target /mnt/efs/moodledata'
done
# Kỳ vọng FSTYPE là nfs4 hoặc efs
```

---

## 10. Giai đoạn 6: Chọn image Moodle

Bạn không build image. CI đã build và test, rồi đẩy lên GHCR với tag là
**commit SHA 40 ký tự**. Không dùng `latest`: tag trôi khiến bạn không biết
chính xác code nào đang chạy, và không rollback được.

### 10.1. Hai sự thật cần biết trước khi chọn SHA

1. **Tag là SHA của commit cuối trong một lần push, không phải của từng
   commit.** Workflow gắn tag `${{ github.sha }}`. Push 3 commit một lúc thì
   chỉ commit cuối có image. Vì vậy cách "lấy commit cuối cùng có sửa
   `moodle/`" (`git log -1 -- moodle/`) có thể trỏ vào một commit nằm giữa lần
   push, và commit đó **không có image** (registry trả 404). Dự án đã gặp đúng
   lỗi này: `405093f` không có image, còn image có cùng mã nguồn mang tag
   `02abe35`.
2. **Có image không có nghĩa là đã qua CI.** Workflow build image
   (`moodle-image.yml`, `agent-image.yml`) chạy **song song và độc lập** với
   `ci.yml`. CI đỏ thì image vẫn được push. Phải kiểm tra CI riêng.

Nguyên tắc: **hỏi registry xem có những tag nào, đừng đoán từ git.** Sau đó
chọn tag mới nhất thoả ba điều kiện:

- commit đã nằm trong `develop`;
- mã nguồn của thành phần đó (`moodle/` hoặc `agent_src/`) **giống hệt**
  `develop` hiện tại;
- CI của đúng commit đó **thành công**.

### 10.2. Nạp hàm chọn image (dùng chung cho Moodle và Agent)

Hai hàm nằm trong [automation/lib/image-select.sh](../automation/lib/image-select.sh).
Nạp chúng vào terminal **từ thư mục gốc repo**:

```bash
source automation/lib/image-select.sh
type pick_image ci_ok >/dev/null && echo "đã nạp"
```

| Hàm | Làm gì |
|---|---|
| `pick_image <package> <thư mục>` | Hỏi GHCR danh sách tag, rồi in ra tag mới nhất **đã nằm trong develop** và có thư mục mã nguồn **giống hệt develop**. Không có tag nào thoả thì in chuỗi rỗng |
| `ci_ok <sha>` | Thành công chỉ khi **mọi** lần chạy `ci.yml` của đúng commit đó đều `success`. Dùng `gh` nếu đã đăng nhập; nếu không, dùng GitHub REST API công khai (chạy được khi repo public, giới hạn 60 lần/giờ). Trả mã **2** khi không hỏi được nguồn nào, để lỗi công cụ không bị hiểu nhầm thành "CI đỏ" |

Nếu repo chuyển sang private, `ci_ok` cần `gh auth login` (giai đoạn 0).

**Hàm chỉ tồn tại trong terminal đã `source` file.** Mở terminal mới thì phải
`source` lại. Nếu quên, bạn nhận `command not found: pick_image` và biến SHA
**rỗng**. Vì vậy các khối lệnh bên dưới luôn kiểm tra biến không rỗng trước
khi `export`.

### 10.3. Chọn image Moodle

```bash
SHA="$(pick_image moodle moodle)"
if [ -z "$SHA" ]; then
  echo "SHA rỗng: chưa source automation/lib/image-select.sh (mục 10.2), hoặc không có image khớp moodle/ của develop"
else
  ci_ok "$SHA" && export MOODLE_IMAGE="ghcr.io/benjaminnhnn/moodle:$SHA" && echo "MOODLE_IMAGE=$MOODLE_IMAGE"
fi
```

✅ **Cổng kiểm tra:** dòng `MOODLE_IMAGE=...:<40 ký tự>` được in ra. Biến
chỉ được `export` khi có SHA **và** CI là `success`.

- **Không có image:** `moodle/` trên develop đã đổi mà chưa có lần build nào
  khớp. Chạy tay workflow **moodle-image** (Actions → Run workflow trên
  `develop`) rồi chọn lại. **Không** lấy image cũ có mã nguồn khác.
- **CI chưa xanh:** không deploy image đó. Ví dụ thật ngày 18/10: tag mới
  nhất khớp `moodle/` là `02abe35`, nhưng CI của nó là `failure` (trước đợt
  sửa CI). Commit đầu develop `2229a30` có CI `success` và cùng mã nguồn
  `moodle/`, nhưng chưa có image Moodle vì lần push đó không sửa `moodle/`.
  Cách xử lý: chạy tay **Actions → Build Moodle image → Run workflow** trên
  `develop` (hoặc `gh workflow run moodle-image.yml --ref develop`), đợi nó
  xong, rồi chọn lại. Khi đó `pick_image` sẽ ra `2229a30` với CI xanh.

Không có `gh` thì kiểm tra trên web: mở
`https://github.com/<owner>/<repo>/commit/<SHA>` và xem dấu ✓ xanh của
workflow **CI**.

### 10.4. Preflight

Preflight kiểm tra trước khi deploy, **chỉ đọc, không thay đổi gì**:

```bash
bash automation/moodle-preflight.sh "$MOODLE_IMAGE"
```

| Kiểm tra | Chạy ở đâu | Chứng minh được |
|---|---|---|
| RDS và EFS ở trạng thái `available` | AWS API (control plane) | Tài nguyên tồn tại và đang chạy. **Không** chứng minh node tới được cổng 5432 |
| `docker compose version`, `findmnt /mnt/efs/moodledata`, các thư mục `/opt/moodle/*` | Trên từng node | Docker sẵn sàng; EFS **thực sự đang mount** (tức NFS 2049 thông) |
| `docker manifest inspect <image>` | Trên từng node | DNS, NAT, egress 443 và quyền đọc image đều ổn. Chỉ đọc manifest, chưa tải layer |

Kết nối mạng thật từ node tới RDS được kiểm tra ở bước kế tiếp
(`stage-moodle-release.sh`, qua SSH tunnel và node A). Muốn kiểm tra sớm cho
**cả hai node**:

```bash
DB_HOST="$(terraform -chdir=terraform output -json moodle_database | jq -r .endpoint)"   # xem tên field: ... | jq keys
ansible -i terraform/.artifacts/moodle-inventory.yml moodle -m shell \
  -a "timeout 5 bash -c '</dev/tcp/$DB_HOST/5432' && echo 'RDS 5432 open'"
```

Preflight đẩy output Ansible vào `/dev/null`. Khi nó dừng mà không rõ lý do,
chạy lại đúng lệnh `ansible ...` trong
[moodle-preflight.sh](../automation/moodle-preflight.sh) **không có**
`>/dev/null` để thấy lỗi.

(Package `moodle` đang public. Nếu nó chuyển sang private, chạy thêm
`bash automation/configure-moodle-ghcr-access.sh "$MOODLE_IMAGE"` với PAT chỉ
có quyền `read:packages`.)

---

## 11. Giai đoạn 7: Secret, stage release và khởi động Moodle

### 11.1. Tạo mật khẩu mới cho môi trường mới

```bash
umask 077
mkdir -p terraform/.artifacts/moodle-secrets
openssl rand -hex 32    > terraform/.artifacts/moodle-secrets/moodle-db-password
openssl rand -base64 32 > terraform/.artifacts/moodle-secrets/moodle-admin-password
chmod 600 terraform/.artifacts/moodle-secrets/moodle-*-password
```

Mật khẩu nằm **trong file**, không nằm trên dòng lệnh. Lý do: tham số dòng
lệnh bị lưu vào shell history và hiện trong `ps` cho người dùng khác trên máy.

### 11.2. Stage release (chưa chạy Moodle)

Trước khi chạy, trong **cùng terminal**:

```bash
echo "$MOODLE_IMAGE"                 # phải có giá trị (biến mất khi mở terminal mới)
aws sts get-caller-identity          # còn hạn, assumed-role/InfraEngineer
docker info >/dev/null 2>&1 && echo "dùng Docker" || psql --version   # cần MỘT trong hai
```

Script chọn cách chạy `psql` theo thứ tự: container (Docker daemon chạy được)
→ `psql` local (có trên PATH) → `docker.exe` cho Windows/WSL. Trên macOS
thiếu cả Docker lẫn `psql`, script rơi xuống nhánh WSL và báo
`Docker CLI is unavailable. Enable Docker Desktop WSL integration...`. Thông
báo này **gây hiểu nhầm**: bạn không cần WSL, chỉ cần mở Docker Desktop hoặc
đưa `psql` vào PATH (giai đoạn 0).

```bash
bash automation/stage-moodle-release.sh \
  "$MOODLE_IMAGE" \
  terraform/.artifacts/moodle-secrets/moodle-db-password \
  terraform/.artifacts/moodle-secrets/moodle-admin-password
```

Script này:

1. lấy mật khẩu master RDS từ Secrets Manager, **chỉ giữ trong bộ nhớ**;
2. tạo (hoặc xoay vòng) role PostgreSQL `moodle_app` với quyền tối thiểu;
3. kiểm tra kết nối TLS tới RDS;
4. chép CA bundle, file secret, `docker-compose.yml` và file env runtime vào
   `/opt/moodle/release/` trên cả hai node.

Ứng dụng chỉ dùng `moodle_app`, không bao giờ dùng tài khoản master. Nếu ứng
dụng bị chiếm quyền, thiệt hại giới hạn trong database Moodle.

### 11.3. Cài Moodle một lần duy nhất, trên node A

```bash
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
  'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
     -f /opt/moodle/release/docker-compose.yml \
     --profile installer run --rm moodle-install'
```

Installer tạo bảng trong database dùng chung. **Không chạy trên node B, không
chạy lại** trên database đã có dữ liệu. Nếu nó báo bảng đã tồn tại, đó là
dấu hiệu tốt (database đã được cài), không phải lỗi cần ép qua.

### 11.4. Khởi động: node A chạy web + cron, node B chỉ chạy web

```bash
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
  'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
     -f /opt/moodle/release/docker-compose.yml \
     --profile cron up -d --wait moodle-web moodle-cron'

ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-b \
  'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
     -f /opt/moodle/release/docker-compose.yml \
     up -d --wait moodle-web'
```

---

## 12. Giai đoạn 8: Nghiệm thu

```bash
MOODLE_URL="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"
curl --fail --silent --show-error "$MOODLE_URL/healthz.php"

TARGET_GROUP_ARN="$(terraform -chdir=terraform output -json moodle_application | jq -r .target_group_arn)"
aws elbv2 describe-target-health --target-group-arn "$TARGET_GROUP_ARN" \
  --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output table
```

✅ **Tiêu chí đạt:**

- [ ] `healthz.php` trả HTTP 200 qua ALB.
- [ ] **Cả hai** target ở trạng thái `healthy`.
- [ ] Mở `$MOODLE_URL/login/index.php`, đăng nhập `admin` bằng mật khẩu trong
      `terraform/.artifacts/moodle-secrets/moodle-admin-password`.
- [ ] `docker compose ps` trên node B **không có** `moodle-cron`.

Luôn lấy URL từ `terraform output`. DNS của ALB **đổi sau mỗi lần dựng lại**;
URL cũ trong tài liệu hay tin nhắn đều có thể đã chết.

---

## 13. Giai đoạn 9: Khôi phục đăng nhập Okta

Database mới không có cấu hình OAuth 2, và ALB mới có DNS mới. Sau mỗi lần
dựng lại cần:

1. Trong Okta admin (`https://trial-3489054-admin.okta.com`; chú ý domain là
   `trial-3489054`, **không phải** tên hiển thị `galaxyfinx-trial-3489054`):
   sửa app `Moodle`, đặt sign-in redirect URI thành
   `$MOODLE_URL/admin/oauth2callback.php` và sign-out URI thành `$MOODLE_URL/`.
2. Trong Moodle: tạo lại OAuth 2 service `Okta`, endpoint, field mapping (có
   `sub` → `username`), bật plugin OAuth 2, khóa trường name/email theo
   phụ lục B, Bước 10. Bản xuất
   cấu hình cũ (nếu có) nằm ở `terraform/.artifacts/preserve/moodle-oauth2-okta.json`.
   File này **chứa client secret**: giữ mode 600, không dán vào chat.
3. ✅ Trang đăng nhập có nút Okta; bấm vào thì chuyển tới
   `trial-3489054.okta.com` và hiện form đăng nhập, không phải lỗi 400.

---

## 14. Giai đoạn 10: Monitoring và AI Agent

### 14.1. Chọn image Agent đã qua CI

Image Agent do `agent-image.yml` build khi `agent_src/**` thay đổi. Workflow
này **không tự deploy** và **không phụ thuộc CI**, nên áp dụng đúng nguyên tắc
ở mục 10.1. Nạp hàm như mục 10.2 (`source automation/lib/image-select.sh`) rồi:

```bash
AGENT_SHA="$(pick_image moodle-ai-agent agent_src)"
if [ -z "$AGENT_SHA" ]; then
  echo "AGENT_SHA rỗng: chưa source automation/lib/image-select.sh (mục 10.2), hoặc không có image khớp agent_src/ của develop"
else
  ci_ok "$AGENT_SHA" && export AGENT_IMAGE="ghcr.io/benjaminnhnn/moodle-ai-agent:$AGENT_SHA" && echo "AGENT_IMAGE=$AGENT_IMAGE"
fi
```

Image Agent được build từ thư mục gốc của repo, nhưng `agent_src/Dockerfile`
chỉ `COPY agent_src/`. Vì vậy so cây `agent_src/` là đủ để biết image có
đúng mã nguồn hay không.

**Vì sao CI quan trọng hơn với Agent?** CI kiểm tra những thứ ảnh hưởng trực
tiếp tới an toàn: replay 15 kịch bản phải fail-closed đúng kết quả đã ghim, và
image **không được chứa** `evaluation/ground_truth`. Một image Agent chưa qua
CI có thể ra quyết định sai trên hạ tầng thật.

✅ **Cổng kiểm tra:** dòng `AGENT_IMAGE=...:<40 ký tự>` được in ra.

### 14.2. Triển khai

```bash
bash automation/configure-moodle-monitoring.sh --agent-image "$AGENT_IMAGE"
```

Script cài Node Exporter/cAdvisor trên hai node Moodle, và Prometheus,
Alertmanager, Grafana, Blackbox, Redis, AI Agent, Celery trên `monitor-ai-01`.
Nó tự sinh bearer token giữa Alertmanager và Agent ngay trên host; token
không đi qua máy bạn.

Dashboard **không mở ra Internet**. Truy cập qua SSH tunnel:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config -N \
  -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 \
  -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 monitor-ai-01
```

✅ **Cổng kiểm tra:** Grafana `http://127.0.0.1:3000`, Prometheus Targets
(`http://127.0.0.1:9090/targets`) toàn bộ `UP`, Agent
`http://127.0.0.1:8000/health` trả OK.

Cuối cùng chụp baseline để mọi thí nghiệm sau có mốc so sánh:

```bash
bash automation/moodle-environment-baseline.sh capture
bash automation/moodle-environment-baseline.sh verify
```


### 14.3. Vận hành giám sát

Wrapper `configure-moodle-monitoring.sh` lấy URL ALB, endpoint RDS, inventory và mật khẩu synthetic từ state và
artifact Terraform cục bộ. Nó tạo token bearer 256 bit giữa Alertmanager và Agent **ngay trên monitor** (nếu chưa
có), cấu hình Alertmanager đọc token từ file, và mount cùng file đó chỉ đọc vào Agent. Agent từ chối mọi webhook
thiếu token trước khi xếp hàng. Không chép token vào inventory, tham số dòng lệnh, repo hay log. Muốn đổi token,
phải deploy lại **cả hai** service; đổi một phía sẽ làm mất cảnh báo.

Playbook kiểm tra Compose trước khi khởi động, chạy `promtool check rules` cho rule Prometheus, và
`amtool check-config` cho Alertmanager.

Khôi phục giám sát sau khi bị dừng tay:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  'sudo docker compose -f /opt/moodle-observability/docker-compose.yml up -d && \
   sudo systemctl enable --now moodle-synthetic-transaction.timer && \
   sudo systemctl status --no-pager moodle-synthetic-transaction.timer'
```

| Dịch vụ | URL qua tunnel |
|---|---|
| Grafana | `http://127.0.0.1:3000` |
| Prometheus | `http://127.0.0.1:9090` |
| Alertmanager | `http://127.0.0.1:9093` |
| AI Agent health | `http://127.0.0.1:8000/health` |

Mật khẩu Grafana chỉ đọc từ terminal của người phụ trách:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  'sudo cat /opt/moodle-observability/secrets/grafana-admin-password'
```

Code mới (25/10/2026) thêm probe BIZ và audit security group RDS. Sau khi kéo code mới, cần **apply Terraform**
(IAM role chỉ đọc `terraform/monitor-iam.tf` và tag RDS trên monitor) rồi chạy lại playbook giám sát và playbook
executor. Kiểm tra `promtool check rules` đạt trong lúc chạy play.

### 14.4. Safe Executor và các cặp khoá ký

Executor là dịch vụ riêng trên monitor; nó SSH vào node bằng key forced-command và chỉ chạy các hành động trong
allowlist. Cài lần đầu theo thứ tự, công tắc live vẫn **tắt**:

```bash
bash automation/configure-moodle-safe-executor.sh
bash automation/configure-moodle-safe-executor-api.sh
bash automation/configure-moodle-monitoring.sh --agent-image "$(cat terraform/.artifacts/moodle-agent-image)"
```

| Kiểm tra | Lệnh | Kỳ vọng |
|---|---|---|
| Image Agent đã ghi nhận | `cat terraform/.artifacts/moodle-agent-image` | Tag SHA 40 ký tự đã qua CI |
| Khoá của executor | `ls terraform/.artifacts/safe-executor/` | `agent-hmac.key`, `safe-action-api-token`, 3 cặp khoá Ed25519 |
| Chế độ Agent | `ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 'sudo docker exec moodle-ai-agent env' \| grep AIOPS_` | `AIOPS_UNIFIED_CORE_MODE=investigate`, `AIOPS_SAFE_ACTION_LIVE_ENABLED=false` |

Có ba cặp khoá Ed25519 **khác nhau**: `gate-approval-ed25519.pub` cho Safety Gate, `approval-ed25519.pub` cho
Executor, `verifier-ed25519.pub` cho Independent Verifier. Mỗi khoá riêng nên do chính người phụ trách vai đó tạo
trên máy của họ; chỉ khoá công khai được chép vào `terraform/.artifacts/safe-executor/`:

```bash
openssl genpkey -algorithm ED25519 -out approver-private.pem
openssl pkey -in approver-private.pem -pubout -out approver-public.pem
```

Khi người duyệt ký tách khỏi drill, mỗi người ký file prepared bằng khoá của mình:

```bash
prepared=/path/to/CON-01-prepared.json
python automation/moodle-safe-gate-approval.py \
  --action-sha256 "$(jq -r .gate_action_sha256 "$prepared")" \
  --private-key ~/.keys/safety-officer.pem --actor safety-officer \
  > gate-approval.json
python automation/moodle-safe-approval.py CON-01 \
  --action-id "$(jq -r .request.catalog_action_id "$prepared")" \
  --idempotency-key "$(jq -r .request.idempotency_key "$prepared")" \
  --private-key ~/.keys/operator.pem --actor operator \
  > executor-approval.json
```

Công cụ từ chối hai actor trùng ID và digest không khớp; Gate và Executor tự kiểm tra từng chữ ký bằng khoá công
khai đã cài. Không bao giờ đặt cả hai khoá riêng của người duyệt trên cùng máy chạy drill khi chạy thật (bản demo
hiện tại là ngoại lệ đã ghi nhận). Khi nghi token API của safe-action bị lộ, thay file
`/etc/aiops-safe-executor/safe-action-api-token` rồi khởi động lại Agent.

### 14.5. Telegram cho Agent và cầu nối phê duyệt

Agent chỉ gửi Telegram khi container có `TELEGRAM_TOKEN` và `TELEGRAM_CHAT_ID`. Điền vào `release/.env`
(Git-ignored, mode 600), rồi deploy lại Agent bằng `configure-moodle-monitoring.sh --agent-image <image>`. Wrapper
chỉ chép các biến `TELEGRAM_*` và `AI_AGENT_PUBLIC_URL` sang
`/opt/moodle-observability/secrets/agent-telegram.env` (root, mode 600); giá trị chỉ dùng trên laptop như
`REDIS_HOST=localhost` không lên staging.

| Biến | Lấy ở đâu |
|---|---|
| `TELEGRAM_TOKEN` | @BotFather (`/newbot` hoặc `/token`) |
| `TELEGRAM_CHAT_ID` | `getUpdates` sau khi bot vào group (group bắt đầu bằng `-100`) |
| `TELEGRAM_ADMIN_USER_IDS` | `python3 automation/moodle-telegram-approval.py whoami`, rồi bấm nút trong group |
| `TELEGRAM_WEBHOOK_SECRET` | `openssl rand -hex 32` (chỉ cần khi dùng `/feedback` qua webhook) |

**Webhook và cầu nối loại trừ nhau.** Telegram chỉ cho một cách nhận cập nhật: webhook **hoặc** `getUpdates`. Cầu
nối phê duyệt dùng `getUpdates`; nếu bot đang có webhook, cầu nối báo `409 Conflict` và dừng (không duyệt gì).
Kiểm tra bằng `getWebhookInfo`; webhook ngrok cũ đã được xoá ngày 23/10/2026 sau khi xác nhận tunnel offline
(`ERR_NGROK_3200`). Agent chỉ tự đăng ký webhook khi có cả `AI_AGENT_PUBLIC_URL` (HTTPS) lẫn
`TELEGRAM_WEBHOOK_SECRET`.

Thử gửi tin từ worker (không in token):

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  "sudo docker exec moodle-agent-worker python -c 'from core.moodle.safe_action_notify import notify; print(notify(\"test\"))'"
```

Khoanh vùng khi không thấy tin: log worker có `TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not found` → biến chưa vào
container; `403`/`400` → bot hoặc chat sai; không có dòng nào → alert chưa tới Agent.

### 14.6. Cấp quyền SSH vào monitor cho AI Engineer

Thêm IP public của họ dạng `/32` vào `ai_engineer_monitor_ssh_cidr_blocks`, review plan, apply, rồi chỉ cài
**public key** của họ bằng playbook riêng. Không bao giờ mở SSH cho mọi địa chỉ Internet.

```hcl
ai_engineer_monitor_ssh_cidr_blocks = ["ENGINEER_PUBLIC_IP/32"]
```

```bash
bash automation/authorize-ai-engineer.sh ~/.ssh/ai-engineer.pub
```

Quyền AWS (phụ lục A) và quyền SSH vào monitor là hai lớp độc lập.

---

## 15. Giai đoạn 11: Bật CD cho các lần release sau

### 15.1. CD làm gì, và không làm gì

Sau khi bật, mỗi lần push vào `develop` có thay đổi ở `moodle/**`,
`release/moodle/**`, `automation/github-deploy-moodle.sh` hoặc
`.github/workflows/cd-staging.yml`, workflow `cd-staging.yml` sẽ chạy. Bạn
cũng có thể chạy tay nó (`workflow_dispatch`).

```text
job build-moodle  (runner của GitHub)  → build + push ghcr.io/benjaminnhnn/moodle:<sha>
job deploy-moodle (runner TỰ HOST trên monitor-ai-01, nhãn self-hosted/linux/moodle-staging,
                   GitHub Environment "staging")
    → kiểm tra đủ secret
    → automation/github-deploy-moodle.sh: node B trước, rồi node A
        mỗi node: docker login GHCR → sửa MOODLE_IMAGE trong /opt/moodle/release/moodle-runtime.env
                  → compose pull → compose up -d --wait → curl 127.0.0.1:8080/healthz.php
    → curl $MOODLE_PUBLIC_URL/healthz.php qua ALB
```

CD **chỉ đổi image**. Nó không tạo database, không đổi mật khẩu, không mount
EFS, không chạy Terraform. Vì vậy phải làm xong mục 4–12 trước khi bật CD.

**Vì sao cần runner tự host?** Hai node Moodle nằm trong private subnet. Runner
của GitHub chạy ngoài Internet, và không được phép mở SSH từ Internet vào node
chỉ để deploy. Runner phải nằm **trong VPC**, ở `monitor-ai-01`. Monitor đã có
security group cho phép SSH tới hai node (`monitor_ssh`).

**Vì sao node B trước?** Trong lúc B cập nhật, A vẫn phục vụ phía sau ALB
(rolling deploy). Script dừng ngay khi có lỗi, nên nếu B hỏng thì A vẫn chạy
image cũ.

Hiện trạng lịch sử: mọi lần chạy `deploy-moodle` gần đây đều `cancelled`, vì
**chưa có runner online**. Job đứng `queued` rồi bị hủy, nhưng image vẫn được
publish.

### 15.2. Hai việc phải xử lý trước khi bật

**(a) Lỗi cron chạy trên cả hai node: đã sửa ngày 19/10/2026.** Trước đây
[github-deploy-moodle.sh](../automation/github-deploy-moodle.sh) gọi
`compose pull/up ... moodle-web moodle-cron` cho **cả** B và A. `moodle-cron`
thuộc profile `cron`, nhưng Docker Compose v2 **luôn khởi động service được gọi
đích danh trên dòng lệnh, bất kể profile**. Hệ quả: mỗi lần CD sẽ tạo **hai
cron runner**, và Moodle chạy trùng các tác vụ định kỳ.

Cách kiểm chứng lỗi mà không thay đổi gì là chạy chế độ `--dry-run` trên
node B:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-b \
  'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
     -f /opt/moodle/release/docker-compose.yml --dry-run up -d moodle-web moodle-cron' | grep cron
# Thấy "Container release-moodle-cron-1 Created/Started" → lệnh đó sẽ tạo cron trên B
```

Bản sửa: hàm `remote_deploy` nhận thêm chế độ cho từng node. Node B
(`no-cron`) chỉ pull/up `moodle-web` và gỡ `moodle-cron` nếu còn sót; node A
(`cron`) chạy cả hai.

**(b) Repo đang public mà runner tự host lại nằm trong VPC.** Với sự kiện
`pull_request`, GitHub chạy workflow theo **nội dung file workflow trong chính
PR**. Một PR từ fork có thể thêm `runs-on: [self-hosted, ...]` và chạy lệnh
tùy ý trên `monitor-ai-01`, tức là bên trong VPC. GitHub khuyến cáo không dùng
runner tự host cho repo public. Chọn một trong hai:

| Cách | Làm gì | Đánh giá |
|---|---|---|
| **Chuyển repo sang private** | Settings → General → Danger Zone → Change visibility | An toàn nhất. Hệ quả: ai muốn xem phải được mời; `ci_ok` khi đó cần `gh auth login` |
| Giữ public, giảm rủi ro | Settings → Actions → General → *Fork pull request workflows*: chọn **Require approval for all external contributors**; runner chạy bằng user riêng **không có sudo**; chỉ gắn runner vào repo này | Giảm rủi ro nhưng không loại bỏ: chỉ cần bấm duyệt nhầm một PR |

**Đã chọn ngày 19/10/2026: giữ public và chặn PR từ fork.** Chính sách duyệt
đã được đổi từ `first_time_contributors` (mặc định, chỉ chặn người đóng góp
lần đầu) sang `all_external_contributors`. Kiểm tra lại bất cứ lúc nào:

```bash
REPO=Benjaminnhnn/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations
gh api "repos/$REPO/actions/permissions/fork-pr-contributor-approval"   # {"approval_policy":"all_external_contributors"}
gh api "repos/$REPO/actions/permissions/workflow"                       # default_workflow_permissions: read
```

**Quy tắc khi review PR từ fork:** trước khi bấm *Approve and run*, mở tab
**Files changed** và xem mọi thay đổi trong `.github/workflows/`. PR nào thêm
`runs-on: [self-hosted ...]` hoặc sửa workflow có `pull_request` thì **không
duyệt**. Với người bên ngoài, đó là dấu hiệu muốn chạy code trong VPC.

### 15.3. Tạo deploy key riêng (không dùng key quản trị)

Đừng đưa `~/.ssh/moodle-aiops` (key quản trị của bạn) lên GitHub. Tạo một key
**chỉ để deploy**. Nếu nó bị lộ, bạn thu hồi riêng nó mà không ảnh hưởng quyền
quản trị.

```bash
ssh-keygen -t ed25519 -a 100 -N '' -f ~/.ssh/moodle-cd-deploy -C "moodle-staging-cd"
chmod 600 ~/.ssh/moodle-cd-deploy

# IP private của monitor (nơi đặt runner); key chỉ được dùng khi kết nối đến từ IP này
MONITOR_PRIV_IP="$(ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  "ip -4 -o addr show | awk '\$4 ~ /^10\\./ {sub(/\\/.*/, \"\", \$4); print \$4}'")"
echo "$MONITOR_PRIV_IP"        # một IP private 10.10.x.x của subnet management

# Cài public key lên 2 node Moodle (Ansible quản lý cấu hình host)
ansible -i terraform/.artifacts/moodle-inventory.yml moodle -b \
  -m ansible.posix.authorized_key \
  -a "user=ec2-user state=present key='$(cat ~/.ssh/moodle-cd-deploy.pub)' key_options='from=\"$MONITOR_PRIV_IP\"'"
```

`from="<IP monitor>"` giới hạn key: kể cả khi `SSH_PRIVATE_KEY` bị lộ từ
GitHub, nó cũng chỉ dùng được khi kết nối xuất phát từ monitor.

Key không có passphrase (`-N ''`) vì runner phải dùng nó không cần người. Đây
là lý do nó phải là key **riêng**, chỉ có trên 2 node Moodle.

### 15.4. Cài runner trên monitor-ai-01

Runner chạy bằng user riêng `github-runner`, **không** dùng `ec2-user` (user
này có sudo):

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01
# --- trên monitor ---
sudo useradd --create-home --shell /bin/bash github-runner
sudo -iu github-runner
mkdir actions-runner && cd actions-runner
```

Lấy lệnh cài **từ GitHub**, không chép phiên bản từ tài liệu (phiên bản và
checksum đổi theo thời gian): repo → **Settings → Actions → Runners → New
self-hosted runner → Linux, x64**. Chạy phần **Download** (có bước kiểm tra
checksum `shasum -a 256 -c`), rồi phần **Configure** với thêm hai tham số:

```bash
# Vẫn là user github-runner trên monitor. <TOKEN> lấy từ trang New self-hosted runner
# (hết hạn sau khoảng 1 giờ, chỉ dùng để đăng ký).
./config.sh --url https://github.com/Benjaminnhnn/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations \
  --token <TOKEN> --name monitor-ai-01 --labels moodle-staging --unattended
exit                                  # quay về ec2-user
```

Nhãn `self-hosted` và `linux` được gắn tự động. Nhãn `moodle-staging` thì
**phải tự thêm**, vì job deploy chỉ chọn runner có **đủ cả ba** nhãn.

Cài thư viện cần thiết và chạy runner như một dịch vụ systemd, với quyền của
`github-runner`:

```bash
cd /home/github-runner/actions-runner
sudo dnf install -y git libicu                # AL2023 mặc định thiếu: git cho actions/checkout, libicu cho runner .NET
sudo ./svc.sh install github-runner
sudo ./svc.sh start
sudo ./svc.sh status                          # active (running)
```

**Cho runner tin host key của 2 node.** Script deploy dùng
`StrictHostKeyChecking=accept-new`, tức tin key ở **lần kết nối đầu**. Bạn đã
xác minh key của 2 node ở mục 8, nên nạp sẵn chính các key đó cho
`github-runner` để không phải tin mù. Chạy từ **máy bạn**:

```bash
for ip in "$APP_A_IP" "$APP_B_IP"; do ssh-keygen -F "$ip" | grep -v '^#'; done \
  | ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
      'sudo -u github-runner bash -c "mkdir -p -m 700 ~/.ssh && cat >> ~/.ssh/known_hosts && chmod 600 ~/.ssh/known_hosts"'
```

✅ **Cổng kiểm tra:** trang **Settings → Actions → Runners** thấy
`monitor-ai-01` **Idle** với nhãn `self-hosted, linux, moodle-staging`.

Việc cài runner hiện làm tay. Theo nguyên tắc "Ansible quản lý cấu hình
host", về sau nên đưa nó vào một playbook.

### 15.5. Tạo Environment `staging` và nạp secret

**Settings → Environments → New environment → `staging`.** Nên bật
**Required reviewers** (chọn chính bạn). Khi đó mỗi lần deploy phải có người
bấm duyệt. Đây là nguyên tắc của dự án: phê duyệt tách biệt với thực thi.

Nạp secret bằng `gh`, đọc từ file hoặc stdin. Cách này không phải dán secret
vào trình duyệt, và secret không nằm trong shell history:

```bash
REPO=Benjaminnhnn/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations
MOODLE_URL="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"

gh secret set MOODLE_APP_A_HOST --repo "$REPO" --env staging --body "$APP_A_IP"
gh secret set MOODLE_APP_B_HOST --repo "$REPO" --env staging --body "$APP_B_IP"
gh secret set MOODLE_PUBLIC_URL --repo "$REPO" --env staging --body "$MOODLE_URL"
gh secret set SSH_PRIVATE_KEY   --repo "$REPO" --env staging < ~/.ssh/moodle-cd-deploy
gh secret set GHCR_USERNAME     --repo "$REPO" --env staging --body "Benjaminnhnn"
gh secret set GHCR_TOKEN        --repo "$REPO" --env staging      # dán PAT khi được hỏi
gh secret list --repo "$REPO" --env staging                       # chỉ thấy tên, không thấy giá trị
```

| Secret | Giá trị | Ghi chú |
|---|---|---|
| `MOODLE_APP_A_HOST`, `MOODLE_APP_B_HOST` | IP **private** của 2 node | Runner nằm trong VPC nên dùng IP private |
| `MOODLE_PUBLIC_URL` | URL ALB từ `terraform output` | Không bắt buộc, nhưng nên có để CD kiểm tra health qua ALB |
| `SSH_PRIVATE_KEY` | Nội dung `~/.ssh/moodle-cd-deploy` | Script ghi ra file tạm trên runner rồi `shred` khi xong |
| `GHCR_USERNAME`, `GHCR_TOKEN` | User GitHub + **PAT classic chỉ có `read:packages`** | **Bắt buộc** dù package public, vì script luôn `docker login`. Tạo PAT ở GitHub → Settings → Developer settings → Personal access tokens (classic), đặt ngày hết hạn |
| `SSH_PORT` | Bỏ trống | Mặc định 22 |

`gh secret set` cần `gh` đã đăng nhập (giai đoạn 0) với quyền admin trên repo.

### 15.6. Chạy thử lần đầu

```bash
gh workflow run cd-staging.yml --repo "$REPO" --ref develop
gh run watch --repo "$REPO"       # nếu bật Required reviewers: vào trang run, bấm Review deployments → Approve
```

Lần chạy tay này build image từ commit mới nhất của `develop`, rồi deploy nó.
Đây cũng là cách tạo image Moodle **có SHA trùng với một commit CI xanh**.

✅ **Cổng kiểm tra sau CD:**

```bash
curl --fail --silent "$MOODLE_URL/healthz.php" && echo " ALB OK"
for h in moodle-app-a moodle-app-b; do
  echo "== $h"
  ssh -F terraform/.artifacts/moodle-ssh.config "$h" \
    'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
       -f /opt/moodle/release/docker-compose.yml ps --format "{{.Service}} {{.Image}} {{.Status}}"'
done
# Kỳ vọng: cả hai node chạy moodle-web với image :<sha mới>; CHỈ node A có moodle-cron.
```

Nếu node B có `moodle-cron`, nghĩa là runner đang dùng bản script cũ (trước 15.2(a)). Dừng nó ngay:
`sudo docker compose ... stop moodle-cron && sudo docker compose ... rm -f moodle-cron`.

### 15.7. Khi CD lỗi

| Hiện tượng | Lớp | Hướng điều tra |
|---|---|---|
| `deploy-moodle` đứng **Queued** rồi `cancelled` | Runner | Runner offline: `sudo ./svc.sh status` trên monitor; nhãn thiếu `moodle-staging` |
| Đứng **Waiting** | Phê duyệt | Environment có Required reviewers: vào run bấm **Review deployments** |
| `Missing required staging secret: X` | Secret | Secret X chưa nạp vào **environment** `staging` (nạp nhầm ở mức repo cũng không được) |
| `Permission denied (publickey)` | SSH | Public key deploy chưa có trên node (15.3), hoặc `SSH_PRIVATE_KEY` không khớp |
| `Host key verification failed` | SSH | IP node đổi sau khi dựng lại, nhưng `known_hosts` của `github-runner` vẫn giữ key cũ: xóa key cũ và nạp lại như 15.4 |
| `Moodle runtime has not been staged` (exit 66) | Host | Chưa chạy `stage-moodle-release.sh` (mục 11) trên node đó |
| `docker login` lỗi `unauthorized` | Quyền | `GHCR_TOKEN` hết hạn hoặc thiếu `read:packages` |
| Health check `127.0.0.1:8080` thất bại | Ứng dụng | `docker compose logs moodle-web` trên node lỗi; rollback theo 16.2 |

### 15.8. Sau mỗi lần dựng lại hoặc destroy hạ tầng

- **Dựng lại:** IP node, DNS ALB và host key **đều đổi**. Cập nhật 3 secret
  host/URL, cài lại deploy key (15.3), cài lại runner (15.4; monitor là máy
  mới), nạp lại `known_hosts` cho runner.
- **Destroy:** gỡ runner khỏi GitHub (**Settings → Actions → Runners →
  monitor-ai-01 → Remove**), vì máy đã mất. Nếu không, các lần push sau vẫn
  tạo job `queued` vô ích. Xóa hoặc vô hiệu PAT `GHCR_TOKEN` nếu không còn
  dùng.

---

## 16. Vận hành hằng ngày: release, rollback, kiểm tra drift

### 16.1. Release phiên bản mới

Khuyến nghị dùng CD (mục 15). Khi phải làm tay: chọn SHA (mục 10), rồi trên
**từng node, B trước A sau**, cập nhật `MOODLE_IMAGE` trong
`/opt/moodle/release/moodle-runtime.env`, chạy `compose up -d --wait`, kiểm
tra health, rồi mới sang node kia. Node A dùng `--profile cron ... moodle-web
moodle-cron`, node B chỉ `moodle-web` (như mục 11.4).

### 16.2. Rollback

Script deploy **không tự rollback**. Khi release lỗi:

1. Giữ nguyên node còn healthy trong ALB.
2. Xem nguyên nhân trên node lỗi: `docker compose ps`, `docker compose logs`.
3. Chọn một SHA **đã biết là healthy** (lần release trước) và deploy lại SHA đó
   theo đúng thứ tự B rồi A.

Vì image bất biến và tag theo SHA, rollback chỉ là "deploy lại một SHA cũ".
Đây là lý do không bao giờ dùng tag `latest`.

### 16.3. Kiểm tra drift cuối mỗi phiên làm việc

```bash
terraform -chdir=terraform plan -input=false -var-file=deployment.tfvars -detailed-exitcode
# exit 0 = không lệch; exit 2 = có ai đó đã sửa tay trên Console → điều tra trước khi apply
```

---

## 17. Gỡ toàn bộ hạ tầng

Làm theo **phụ lục D**, không làm tắt. Tóm tắt các điểm dễ sai:

1. Thu thập bằng chứng cần cho báo cáo **trước**.
2. RDS cấu hình `skip_final_snapshot=true`: destroy **không** tạo snapshot
   cuối. Cần giữ dữ liệu thì tạo snapshot thủ công trước.
3. EFS có `prevent_destroy = true`. Tạm gỡ khối `lifecycle` trong
   `terraform/efs.tf`, destroy, rồi **khôi phục lại** (`git diff --exit-code
   terraform/efs.tf` phải sạch). Không dùng `-target` để lách.
4. Dùng plan đã lưu: `plan -destroy -out=...`, review, rồi `apply` file đó.
5. **Kiểm tra trong AWS**, không chỉ tin `terraform state list` rỗng: RDS,
   NAT, ALB, EFS, EIP, VPC không mặc định, snapshot RDS, recovery point của AWS
   Backup. Tài nguyên tạo ngoài Terraform (ví dụ một EC2 tạo tay trong VPC mặc
   định) vẫn tính tiền và Terraform không thấy nó.
6. Xóa recovery point EFS cần quyền `backup:*`, mà role `InfraEngineer`
   **không có**. Nhờ admin xóa, hoặc để chúng tự hết hạn.

---

## 18. Tư duy xử lý sự cố

### 18.1. Phương pháp: đừng sửa khi chưa biết lỗi nằm ở lớp nào

Mỗi khi một bước thất bại, đi qua sáu câu hỏi theo thứ tự:

1. **Triệu chứng chính xác là gì?** Chép nguyên văn thông báo lỗi và exit
   code. Đừng diễn giải ("không kết nối được" là diễn giải;
   `Connection closed by UNKNOWN port 65535` là triệu chứng).
2. **Lỗi thuộc lớp nào?** Quyền AWS → Terraform/AWS → mạng/SSH → host
   (Ansible) → ứng dụng (container) → tích hợp (ALB, Okta). Bước gần nhất đã
   qua cổng kiểm tra cho biết các lớp bên dưới còn tốt.
3. **Có những giả thuyết nào?** Liệt kê ít nhất hai.
4. **Phép thử nào phân biệt được chúng?** Chọn lệnh nhỏ nhất cho kết quả khác
   nhau tùy giả thuyết. Ví dụ: `ssh -v` cho biết lỗi ở bước TCP, bước host key
   hay bước xác thực.
5. **Nguyên nhân gốc, kèm bằng chứng.** Một kết luận chưa có bằng chứng chỉ
   là giả thuyết.
6. **Sửa, rồi chạy lại đúng lệnh đã lỗi** để xác nhận. Kiểm tra cả exit code:
   `cmd | tail` sẽ nuốt mã lỗi của `cmd` và cho bạn một "PASS" giả.

**Ví dụ áp dụng.** `ansible all -m ping` báo
`Connection closed by UNKNOWN port 65535`.

- *Lớp:* bước trước (Terraform apply) đã qua, nên lỗi ở lớp mạng/SSH.
- *Giả thuyết:* (a) security group chặn IP của bạn; (b) host key chưa được tin
  cậy; (c) sai private key.
- *Phép thử:* `ssh -v -F terraform/.artifacts/moodle-ssh.config monitor-ai-01`.
  Nếu thấy `Host key verification failed` thì loại (a), vì TCP đã thông.
- *Gốc:* EC2 mới, host key chưa có trong `known_hosts`, mà config dùng
  `StrictHostKeyChecking yes`.
- *Sửa:* làm mục 8 (so fingerprint), chạy lại `ansible ... -m ping`.

### 18.2. Các lỗi đã thực sự gặp trong dự án

| Triệu chứng | Lớp | Nguyên nhân gốc | Cách xử lý |
|---|---|---|---|
| `No valid credential sources found` … `169.254.169.254 … host is down` | Quyền | Terminal không có biến `AWS_*` (mở terminal mới, hoặc quên `eval`) | Chạy lại khối lệnh ở mục 5 trong chính terminal đó; kiểm tra bằng `aws sts get-caller-identity` |
| `AssumeRoleTokenProvider session option not set` | Quyền | Terraform không hỏi được MFA, mà `AWS_PROFILE` vẫn trỏ tới role có MFA | `export-credentials` rồi `unset AWS_PROFILE` (mục 5) |
| `ExpiredToken` giữa chừng | Quyền | Credential role hết hạn (tối đa 4 giờ) | Lấy lại credential, chạy lại lệnh. Tránh để hết hạn giữa lúc `apply` |
| `no identity-based policy allows the X action` | Quyền | Policy thiếu action X | Gửi admin đúng action đó; không xin wildcard. Xem phụ lục A.8 |
| `explicit deny` khi gọi API ở region khác | Quyền | Policy chặn mọi region ngoài `ap-southeast-1` | Đúng thiết kế; đặt `AWS_REGION=ap-southeast-1` |
| `plan` đòi `-/+ replace` cả 3 EC2 dù không sửa gì | Terraform | `ec2_ami_id` không ghim; AWS ra AMI mới | Ghim AMI (mục 6). Không apply plan đó |
| `Saved plan is stale` | Terraform | Code, input hoặc state đổi sau khi plan | Tạo plan mới, review lại; không lách |
| `RunInstances` báo `PendingVerification` | AWS account | Account mới đang được AWS xác minh | Chờ email xác minh, rồi plan + apply lại; tài nguyên đã tạo vẫn trong state |
| Apply bị ngắt; lần sau báo tài nguyên **đã tồn tại** (ví dụ DB identifier trùng) | Terraform | AWS đã tạo tài nguyên nhưng state chưa ghi nhận (tài nguyên mồ côi) | **Import** chứ không xóa tay: `terraform -chdir=terraform import -var-file=deployment.tfvars aws_db_instance.moodle moodle-staging-postgres` |
| `Host key verification failed` / `Connection closed by UNKNOWN port 65535` | SSH | EC2 mới, hoặc IP được tái sử dụng với key cũ | Mục 8: xóa key cũ, so fingerprint, thêm key |
| SSH **timeout** (không có lỗi host key) | Mạng | IP public của bạn đổi, security group không còn cho phép | `update-infrastructure.sh` rồi plan + apply |
| ALB target `unhealthy` ngay sau `apply` | Ứng dụng | Chưa có Moodle chạy trên 8080 | Đúng kỳ vọng; deploy Moodle (mục 11) |
| Kiểm tra manifest GHCR trả **404** | Image | SHA lấy bằng `git log -1 -- moodle/` là commit nằm giữa một lần push; chỉ commit cuối của lần push có image | Chọn SHA bằng `pick_image` (mục 10.2), tức là hỏi registry chứ không đoán từ git |
| `stage-moodle-release.sh` báo `Docker CLI is unavailable. Enable Docker Desktop WSL integration...` trên macOS | Công cụ | Không có Docker daemon **và** `psql` không nằm trên PATH (libpq là keg-only) | Mở Docker Desktop, hoặc `export PATH="/opt/homebrew/opt/libpq/bin:$PATH"` (ghi vào `~/.zshrc`) |
| `findmnt` trống trên node | Host | EFS chưa mount | Kiểm tra mount target, SG cổng 2049, access point; chạy lại `prepare-moodle-runtime.sh` |
| Okta trả **400** hoặc API token "invalid" | Tích hợp | Dùng nhầm tên hiển thị `galaxyfinx-trial-3489054` thay vì domain `trial-3489054` | Lấy domain từ thanh địa chỉ trình duyệt của trang Okta admin |
| Okta báo `redirect_uri` không hợp lệ sau khi dựng lại | Tích hợp | DNS ALB đã đổi | Cập nhật redirect URI trong Okta (mục 13) |
| Job `deploy-moodle` đứng `queued` | CI/CD | Không có runner tự host online | Image vẫn đã publish; deploy tay hoặc cài runner |
| CI đỏ, sửa xong lại đỏ ở bước khác | CI/CD | CI dừng ở bước lỗi **đầu tiên**, lỗi phía sau bị che | Chạy toàn bộ các bước trong `AGENTS.md` ở local trước khi push |
| `AccessDenied` khi xóa recovery point EFS | Quyền | Role không có `backup:*` | Nhờ admin, hoặc chờ hết hạn |

### 18.3. Khi nào dừng và hỏi

- `plan` có `destroy` hoặc `replace` mà bạn không chủ động yêu cầu.
- Một lệnh đòi bạn tắt kiểm tra an toàn (`StrictHostKeyChecking no`, mở
  `0.0.0.0/0`, thêm quyền `*`).
- Bạn sắp xóa thứ gì đó bằng tay trên AWS Console. Thứ do Terraform quản lý
  thì xóa bằng Terraform.

---

## 19. Checklist một trang

```text
[ ] 0  Công cụ đã cài, SSH key ~/.ssh/moodle-aiops đã có
[ ] 1  aws sts get-caller-identity → assumed-role/InfraEngineer
[ ] 2  deployment.tfvars: my_ip_cidr đúng, ec2_ami_id đã ghim
[ ]    Trước mỗi plan/apply/script: aws sts get-caller-identity → assumed-role/InfraEngineer
[ ] 3  init / fmt / validate / test → plan đã đọc (0 destroy) → apply file plan
[ ] 4  Fingerprint khớp → ansible all -m ping = pong cả 3 host
[ ] 5  prepare-moodle-runtime.sh → findmnt thấy nfs4/efs trên A và B
[ ] 6  MOODLE_IMAGE = pick_image moodle moodle, CI của SHA đó xanh, preflight OK
[ ] 7  Secret trong file mode 600 → stage → install (chỉ A, 1 lần) → start (A: web+cron, B: web)
[ ] 8  healthz 200 qua ALB, 2 target healthy, đăng nhập admin được, B không có cron
[ ] 9  Okta redirect URI cập nhật, nút Okta đăng nhập được
[ ] 10 AGENT_IMAGE = pick_image moodle-ai-agent agent_src, CI xanh → Monitoring + Agent chạy, Prometheus targets UP, baseline capture + verify
[ ] 11 (CD) deploy key riêng
       → runner monitor-ai-01 Idle (3 nhãn) → environment staging + secret → chạy thử,
       chỉ node A có moodle-cron
[ ] Cuối phiên: plan -detailed-exitcode = 0; ghi lại SHA image và kết quả
```

**Quy tắc vàng:**

- Không commit `deployment.tfvars`, `terraform.tfstate*`, `terraform/.artifacts/`,
  private key, `.env*`, PAT hay mật khẩu.
- Không dán secret vào chat hay ảnh chụp màn hình.
- Không chạy Terraform/Ansible từ GitHub Actions; không build image trên EC2.
- Không dùng tag `latest`; mọi release là một commit SHA đã qua CI.
- Không tắt kiểm tra an toàn để "cho chạy được". Tìm nguyên nhân gốc.

---

## Phụ lục A: Phân quyền AWS IAM (dành cho admin)

**Người thực hiện:** quản trị viên AWS. Các role trong phụ lục này **không** có quyền quản lý IAM. Mô hình phân quyền
và lý do thiết kế nằm ở [BAO_CAO_KIEN_TRUC_HA_TANG, mục 10](BAO_CAO_KIEN_TRUC_HA_TANG.md#10-phân-quyền-aws-iam).
Thay `<ACCOUNT_ID>`, `<infra-user>`, `<ai-user>`, `<mfa-device>` bằng giá trị thật khi làm; **không** commit access
key, mật khẩu hay link kích hoạt.

**Route 53 health check:** Route 53 là dịch vụ toàn cầu, nên statement `DenyOutsideSingapore` chặn nó. Để bật health
check cho probe `internet_to_postgresql_blocked`, admin đã thêm các action health check của Route 53 vào `NotAction`
của statement chặn region, cùng một statement `Allow` riêng cho chúng. Bổ sung tương ứng vào policy dưới đây khi tạo
mới.

### A.1. Phần chung: policy `InfraEngineerMoodle`

Tạo **một lần**, dùng chung cho cả hai role.

**IAM → Policies → Create policy → tab JSON**, xóa nội dung mẫu, dán policy dưới đây (đã thay `<ACCOUNT_ID>`). Bấm **Next**, đặt tên `InfraEngineerMoodle`, rồi **Create policy**.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "DenyOutsideSingapore", "Effect": "Deny", "NotAction": ["iam:*", "sts:*"], "Resource": "*",
      "Condition": { "StringNotEquals": { "aws:RequestedRegion": "ap-southeast-1" } } },
    { "Sid": "NetworkAndCompute", "Effect": "Allow", "Resource": "*", "Action": [
        "ec2:Describe*", "ec2:Get*", "ec2:Create*", "ec2:Delete*", "ec2:Modify*",
        "ec2:Associate*", "ec2:Disassociate*", "ec2:Attach*", "ec2:Detach*", "ec2:Replace*",
        "ec2:AllocateAddress", "ec2:ReleaseAddress",
        "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
        "ec2:UpdateSecurityGroupRuleDescriptions*",
        "ec2:RunInstances", "ec2:TerminateInstances", "ec2:StartInstances",
        "ec2:StopInstances", "ec2:RebootInstances", "ec2:ImportKeyPair" ] },
    { "Sid": "LoadBalancer", "Effect": "Allow", "Resource": "*", "Action": [
        "elasticloadbalancing:Describe*", "elasticloadbalancing:Create*",
        "elasticloadbalancing:Delete*", "elasticloadbalancing:Modify*",
        "elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DeregisterTargets",
        "elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags",
        "elasticloadbalancing:SetSecurityGroups", "elasticloadbalancing:SetSubnets",
        "acm:DescribeCertificate", "acm:ListCertificates" ] },
    { "Sid": "Database", "Effect": "Allow", "Resource": "*", "Action": [
        "rds:Describe*", "rds:ListTagsForResource", "rds:Create*", "rds:Delete*",
        "rds:Modify*", "rds:AddTagsToResource", "rds:RemoveTagsFromResource",
        "rds:RebootDBInstance", "rds:RestoreDBInstanceFromDBSnapshot" ] },
    { "Sid": "RdsManagedMasterSecret", "Effect": "Allow",
      "Resource": "arn:aws:secretsmanager:ap-southeast-1:<ACCOUNT_ID>:secret:rds!*", "Action": [
        "secretsmanager:CreateSecret", "secretsmanager:TagResource",
        "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue",
        "secretsmanager:RotateSecret", "secretsmanager:DeleteSecret" ] },
    { "Sid": "EncryptionViaServicesOnly", "Effect": "Allow", "Resource": "*",
      "Action": ["kms:DescribeKey", "kms:CreateGrant", "kms:ListAliases"],
      "Condition": { "StringEquals": { "kms:ViaService": [
        "rds.ap-southeast-1.amazonaws.com",
        "secretsmanager.ap-southeast-1.amazonaws.com",
        "elasticfilesystem.ap-southeast-1.amazonaws.com" ] } } },
    { "Sid": "FileStorage", "Effect": "Allow", "Resource": "*", "Action": [
        "elasticfilesystem:Describe*", "elasticfilesystem:Create*",
        "elasticfilesystem:Delete*", "elasticfilesystem:Put*", "elasticfilesystem:Update*",
        "elasticfilesystem:TagResource", "elasticfilesystem:UntagResource",
        "elasticfilesystem:ListTagsForResource" ] },
    { "Sid": "RdsLogGroups", "Effect": "Allow",
      "Resource": "arn:aws:logs:ap-southeast-1:<ACCOUNT_ID>:log-group:/aws/rds/*", "Action": [
        "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy",
        "logs:DeleteRetentionPolicy", "logs:TagResource", "logs:UntagResource",
        "logs:TagLogGroup", "logs:ListTagsForResource", "logs:ListTagsLogGroup" ] },
    { "Sid": "DescribeLogGroups", "Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": "*" },
    { "Sid": "PublicAmiLookup", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters"],
      "Resource": "arn:aws:ssm:ap-southeast-1::parameter/aws/service/ami-amazon-linux-latest/*" },
    { "Sid": "ServiceLinkedRolesOnly", "Effect": "Allow", "Action": "iam:CreateServiceLinkedRole", "Resource": "*",
      "Condition": { "StringEquals": { "iam:AWSServiceName": [
        "rds.amazonaws.com", "elasticloadbalancing.amazonaws.com",
        "elasticfilesystem.amazonaws.com", "backup.amazonaws.com" ] } } },
    { "Sid": "ReadOnlyInventory", "Effect": "Allow",
      "Action": ["tag:GetResources", "ssm:DescribeInstanceInformation"], "Resource": "*" },
    { "Sid": "ReadUserPolicyAttachments", "Effect": "Allow",
      "Action": "iam:ListAttachedUserPolicies", "Resource": "arn:aws:iam::<ACCOUNT_ID>:user/*" }
  ]
}
```

**Ghi chú:**
- ARN của SSM là `arn:aws:ssm:ap-southeast-1::parameter/...` (**không có Account ID**). Đúng như vậy, vì parameter AMI là tài nguyên công khai của AWS.
- Ô JSON chỉ nhận **nội dung policy**. Không dán câu lệnh shell vào đây. Policy này **không có** trường `Principal`.
- Policy được suy ra từ các resource Terraform đang dùng. Nếu Terraform báo `not authorized to perform: <action>`, chỉ bổ sung **đúng action đó** (xem mục A.8).

---

### A.2. Infrastructure Engineer: CLI và Terraform

Infra Engineer làm việc chủ yếu bằng **AWS CLI và Terraform**, nên dùng **access key + MFA**.

#### A.2.1. Tạo IAM user

**IAM → Users → Create user**
- **User name:** `<infra-user>`
- **Không** tick quyền truy cập Console (trừ khi cần)
- **Không** gán policy, **không** thêm vào group

#### A.2.2. Gán MFA

User → **Security credentials → Assign MFA device → Authenticator app**. Quét QR, nhập 2 mã liên tiếp, rồi **ghi lại MFA ARN**: `arn:aws:iam::<ACCOUNT_ID>:mfa/<mfa-device>`.

#### A.2.3. Tạo role `InfraEngineer`

**IAM → Roles → Create role → Custom trust policy**. Đây là **trust policy** (ai được vào role), **có** trường `Principal`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<ACCOUNT_ID>:user/<infra-user>" },
    "Action": "sts:AssumeRole",
    "Condition": { "Bool": { "aws:MultiFactorAuthPresent": "true" } }
  }]
}
```

1. **Next** → trang **Add permissions**: tìm `InfraEngineerMoodle` (lọc *Customer managed* nếu không thấy), **tick ô vuông**, rồi **Next**.
2. **Role name:** `InfraEngineer` → **Create role**.
3. Mở role → **Edit** → **Maximum session duration = 4 hours**.

Trang trust policy **không có chỗ tick policy**. Ô để tick nằm ở **trang kế tiếp**.

#### A.2.4. Cho user quyền assume role

User `<infra-user>` → **Permissions → Add permissions → Create inline policy → JSON**, đặt tên `AssumeInfraEngineer`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/InfraEngineer"
  }]
}
```

Tab Permissions của user chỉ nên có **duy nhất** policy này.

#### A.2.5. Access key và AWS CLI

User → **Security credentials → Create access key → Command Line Interface (CLI)**. Secret chỉ hiện **một lần**. **Gõ thẳng vào `aws configure`**. Không mở file CSV trong editor, không chụp màn hình, không dán vào chat.

```bash
brew install awscli
aws configure --profile <base-profile>     # nhập access key, region ap-southeast-1, output json
```

Thêm vào `~/.aws/config`:

```ini
[profile target-account]
role_arn = arn:aws:iam::<ACCOUNT_ID>:role/InfraEngineer
source_profile = <base-profile>
mfa_serial = arn:aws:iam::<ACCOUNT_ID>:mfa/<mfa-device>
region = ap-southeast-1
duration_seconds = 14400
```

Tên `target-account` là giá trị mặc định của biến `AWS_PROFILE` trong các script `automation/`.

```bash
aws sts get-caller-identity --profile target-account
# Hỏi mã MFA → "Arn": "arn:aws:sts::<ACCOUNT_ID>:assumed-role/InfraEngineer/..."
```

---

### A.3. AI Engineer: Console có username/mật khẩu

AI Engineer dùng **AWS Console** (đăng nhập bằng username và mật khẩu). Quyền **giống Infra Engineer**, qua một role **riêng**.

Thứ tự bắt buộc: **tạo user (5.1) trước, rồi mới tạo role (5.3)**. Trust policy tham chiếu tới user nào thì user đó phải tồn tại sẵn.

#### A.3.1. Tạo IAM user có quyền đăng nhập Console

**IAM → Users → Create user**
1. **User name:** `<ai-user>` (ví dụ `ai-engineer-01`).
2. Tick **Provide user access to the AWS Management Console** → **I want to create an IAM user**.
3. **Console password:** *Autogenerated password*.
4. Tick **Users must create a new password at next sign-in**. AWS tự gắn policy `IAMUserChangePassword`.
5. **Permissions:** không gán policy nào khác, không thêm vào group.
6. **Create user**. Lưu **Console sign-in URL**, **username** và **mật khẩu tạm**. Mật khẩu chỉ hiện một lần.

#### A.3.2. Policy tự phục vụ và quyền assume role

User `<ai-user>` → **Permissions → Add permissions → Create inline policy → JSON**, đặt tên `AIEngineerSelfServiceAndAssume`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ViewOwnIdentity", "Effect": "Allow",
      "Action": ["iam:GetUser", "iam:ListMFADevices", "iam:ListVirtualMFADevices", "iam:GetAccountPasswordPolicy"],
      "Resource": "*" },
    { "Sid": "ChangeOwnPassword", "Effect": "Allow",
      "Action": "iam:ChangePassword",
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:user/${aws:username}" },
    { "Sid": "ManageOwnMFAWithMFA", "Effect": "Allow",
      "Action": ["iam:CreateVirtualMFADevice", "iam:EnableMFADevice", "iam:ResyncMFADevice",
                 "iam:DeactivateMFADevice", "iam:DeleteVirtualMFADevice"],
      "Resource": ["arn:aws:iam::<ACCOUNT_ID>:user/${aws:username}",
                   "arn:aws:iam::<ACCOUNT_ID>:mfa/*"],
      "Condition": { "Bool": { "aws:MultiFactorAuthPresent": "true" } } },
    { "Sid": "ManageOwnAccessKeysWithMFA", "Effect": "Allow",
      "Action": ["iam:CreateAccessKey", "iam:ListAccessKeys", "iam:UpdateAccessKey",
                 "iam:DeleteAccessKey", "iam:GetAccessKeyLastUsed"],
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:user/${aws:username}",
      "Condition": { "Bool": { "aws:MultiFactorAuthPresent": "true" } } },
    { "Sid": "AssumeAIEngineerWithMFA", "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/AIEngineer",
      "Condition": { "Bool": { "aws:MultiFactorAuthPresent": "true" } } }
  ]
}
```

| Statement | Tác dụng |
|---|---|
| `ViewOwnIdentity` | Xem thông tin của mình, danh sách thiết bị MFA, chính sách mật khẩu của account |
| `ChangeOwnPassword` | Tự đổi mật khẩu của mình. Không cần điều kiện MFA vì API bắt buộc nhập mật khẩu cũ |
| `ManageOwnMFAWithMFA` | Tự quản lý thiết bị MFA của mình (`${aws:username}`), **chỉ khi phiên đã có MFA**. Thiếu điều kiện này, kẻ có access key bị lộ có thể gắn thiết bị MFA của họ rồi assume role. Vì vậy **lần đăng ký MFA đầu tiên do admin làm cùng AI Engineer** (mục A.3.5) |
| `ManageOwnAccessKeysWithMFA` | Tự tạo, xoay vòng, xóa **access key của chính mình** (cho CLI/Terraform), chỉ khi phiên có MFA |
| `AssumeAIEngineerWithMFA` | Chỉ được vào role `AIEngineer`, và chỉ khi **phiên đăng nhập có MFA** |

#### A.3.3. Tạo role `AIEngineer`

**IAM → Roles → Create role → Custom trust policy**:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<ACCOUNT_ID>:user/<ai-user>" },
    "Action": "sts:AssumeRole",
    "Condition": { "Bool": { "aws:MultiFactorAuthPresent": "true" } }
  }]
}
```

1. **Next** → tick **`InfraEngineerMoodle`** → **Next**.
2. **Role name:** `AIEngineer` → **Create role**.
3. Mở role → **Edit** → **Maximum session duration = 4 hours**.

#### A.3.4. Bàn giao thông tin đăng nhập

Gửi **hai mảnh qua hai kênh khác nhau**, không bao giờ gửi chung, và không gửi qua chat công cụ AI:

| Kênh 1 (ví dụ email công việc) | Kênh 2 (ví dụ tin nhắn trực tiếp) |
|---|---|
| Link: `https://<ACCOUNT_ID>.signin.aws.amazon.com/console`, kèm username `<ai-user>` | Mật khẩu tạm |

Lý do: lộ một kênh thì kẻ xấu vẫn thiếu mảnh còn lại. Mật khẩu tạm cũng bị buộc đổi ở lần đăng nhập đầu.

#### A.3.5. AI Engineer đăng nhập lần đầu

1. Mở link, đăng nhập bằng username và mật khẩu tạm, rồi **đặt mật khẩu mới**.
2. MFA đầu tiên: **admin** mở *IAM → Users → `<ai-user>` → Security credentials → Assign MFA device* khi có mặt AI Engineer, và AI Engineer quét QR bằng điện thoại của mình. (Policy cố ý không cho user tự thêm MFA khi chưa có MFA.)
3. **Đăng xuất, đăng nhập lại có MFA.** Bắt buộc: điều kiện `aws:MultiFactorAuthPresent` chỉ đúng khi phiên đăng nhập đã qua MFA.
4. Góc phải → **Switch role**:
   - **Account:** `<ACCOUNT_ID>`
   - **IAM role name:** `AIEngineer`
   - **Display name:** `AIEngineer-Moodle`
5. Đổi region sang **Asia Pacific (Singapore) ap-southeast-1**.

#### A.3.6. (Tùy chọn) AI Engineer dùng AWS CLI hoặc Terraform

1. AI Engineer đăng nhập Console **bằng user, có MFA, chưa Switch role** (nếu đang ở role thì **Switch back**: role không có quyền IAM). Vào **Security credentials → Access keys → Create access key** (use case CLI) và nhập ngay vào `aws configure`; không lưu CSV, không chụp màn hình. Quyền này đến từ statement `ManageOwnAccessKeysWithMFA` ở mục A.3.2.
2. Thêm profile:

```ini
[profile ai-target-account]
role_arn = arn:aws:iam::<ACCOUNT_ID>:role/AIEngineer
source_profile = <profile-chứa-access-key>
mfa_serial = arn:aws:iam::<ACCOUNT_ID>:mfa/<mfa-device-của-ai-engineer>
region = ap-southeast-1
duration_seconds = 14400
```

Khi chạy script trong `automation/`: `AWS_PROFILE=ai-target-account bash automation/<script>.sh`.

**Ranh giới:** quyền AWS (tài liệu này) và quyền **SSH vào `monitor-ai-01`** (`automation/authorize-ai-engineer.sh`) là **hai lớp độc lập**. Cấp lớp này không tự động cấp lớp kia.

---

### A.4. Kiểm chứng quyền

**Đoán trước kết quả**, rồi mới thử. Lần thử nào có kết quả khác với dự đoán cho thấy policy, hoặc cách mình hiểu policy, đang sai.

#### A.4.1. Infra Engineer (CLI)

```bash
export AWS_PROFILE=target-account AWS_PAGER=""
aws sts get-caller-identity --query Arn --output text                   # assumed-role/InfraEngineer/...
aws ec2 describe-vpcs --query 'Vpcs[].VpcId'                             # Được phép
aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text                                  # Được phép (AMI ID)
aws ec2 describe-vpcs --region us-east-1                                 # PHẢI bị chặn: explicit deny
aws iam create-user --user-name should-fail-test                         # PHẢI AccessDenied
aws s3api list-buckets                                                   # PHẢI AccessDenied
```

Kết quả đã đạt khi triển khai (10/10/2026): đúng cả 6 dòng.

#### A.4.2. AI Engineer (Console, sau khi Switch role)

| Thử | Kỳ vọng | Vì sao |
|---|---|---|
| EC2 → Instances (Singapore) | Thấy `monitor-ai-01`, `moodle-app-a`, `moodle-app-b` | `ec2:Describe*` được cho phép |
| Đổi region sang N. Virginia | Báo không có quyền | `DenyOutsideSingapore` |
| IAM → Users → Create user | Access denied | Policy không cho quản lý IAM |
| S3 → Buckets | Access denied | S3 không có trong policy |
| **Trước khi Switch role**, mở EC2 | **Access denied** | Bản thân user không có quyền gì. Quyền chỉ nằm ở role, và vào role thì cần MFA |

Dòng cuối là bài kiểm tra quan trọng nhất của mô hình.

---

### A.5. Dùng với Terraform và các script automation

**Terraform không tự hỏi được mã MFA.** Lấy credential tạm từ AWS CLI rồi đưa cho Terraform:

```bash
aws sts get-caller-identity --profile target-account        # nhập mã MFA nếu phiên đã hết hạn
cd terraform
eval "$(aws configure export-credentials --profile target-account --format env)"
unset AWS_PROFILE
export AWS_REGION=ap-southeast-1
terraform plan -var-file=deployment.tfvars -out=moodle.tfplan
```

- Credential tạm hết hạn sau **4 giờ**. Khi gặp `ExpiredToken`, làm lại hai lệnh đầu.
- Nếu còn để `AWS_PROFILE=target-account` trong terminal, Terraform sẽ báo `AssumeRoleTokenProvider session option not set`. Chạy `unset AWS_PROFILE`.
- Các script automation gọi `aws --profile target-account` trực tiếp, nên AWS CLI **tự hỏi MFA** khi cần.

---

### A.6. Vận hành: xoay vòng key, thu hồi quyền

#### A.6.1. Xoay vòng access key (ví dụ khi key bị lộ)

Làm theo thứ tự này để không bị gián đoạn:

1. **Create access key** mới. Một user được có tối đa 2 key cùng lúc.
2. `aws configure --profile <base-profile>`, nhập key mới (gõ trực tiếp vào terminal).
3. `rm -rf ~/.aws/cli/cache && aws sts get-caller-identity --profile target-account` để chắc chắn đang dùng key mới.
4. Key cũ: **Deactivate**, rồi **Delete**.
5. Xóa mọi file CSV chứa key: `rm ~/Downloads/*accessKeys*.csv`.

#### A.6.2. Thu hồi quyền

| Mục tiêu | Cách làm |
|---|---|
| Tạm khóa một người | User → **Security credentials**: Disable console access, Deactivate access key |
| Thu hồi vĩnh viễn | Xóa user. Role có thể giữ lại cho người thay thế (sửa `Principal` trong trust policy) |
| Thu hẹp quyền AI Engineer | Gỡ `InfraEngineerMoodle` khỏi role `AIEngineer`, gắn policy chỉ đọc. Infra Engineer không bị ảnh hưởng |
| Thu hồi khẩn cấp các phiên đang mở của role | Role → **Revoke active sessions**. Phiên cũ mất hiệu lực, user phải assume lại |

---

### A.7. Lỗi đã gặp và cách xử lý

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| Policy editor báo *"Fix all syntax errors"* | Dán **câu lệnh `sed`** vào ô JSON | Chạy lệnh trong terminal để lấy JSON, rồi dán JSON |
| *"Has prohibited field Principal"* | Dán **trust policy** vào màn hình tạo **permission policy** | Trust policy thuộc *Roles → Create role → Custom trust policy* |
| ARN `user/vietle>` | Còn sót dấu `>` của placeholder | Xóa ký tự thừa. Kiểm tra mọi ARN không còn `<` hay `>` |
| Tạo role không thấy chỗ tick policy | Đang ở trang trust policy | Ô tick nằm ở trang **Add permissions** ngay sau đó |
| Không tìm thấy policy khi tick | Đang lọc *AWS managed* | Lọc **Customer managed** |
| `AssumeRoleTokenProvider session option not set` | Terraform không hỏi được MFA | Dùng `aws configure export-credentials` (mục A.5) |
| Secret access key xuất hiện trong chat/editor | Mở file CSV trong VS Code | Xoay vòng key theo mục A.6.1 |

---

### A.8. Chẩn đoán khi bị "Access Denied"

Áp dụng phương pháp chung (xem [BAO_CAO_KICH_BAN_LOI, mục 1.3](BAO_CAO_KICH_BAN_LOI.md#13-phương-pháp-chẩn-đoán-dùng-chung)):

1. **Ai đang gọi?** `aws sts get-caller-identity`. Nếu kết quả là `user/...` thay vì `assumed-role/...`, bạn **chưa vào role**: thiếu MFA hoặc chưa Switch role.
2. **Ở region nào?** Mọi region khác `ap-southeast-1` đều bị chặn **có chủ đích**.
3. **Thông báo lỗi nói gì?**
   - Có chữ *"explicit deny"* → bị chặn bởi statement `Deny`, thường là chặn region. **Đúng thiết kế.**
   - *"no identity-based policy allows the X action"* → action X **chưa có** trong policy.
4. **Action đó có nên được phép không?** Đối chiếu với thứ Terraform hoặc script thực sự cần. **Chỉ thêm đúng action đó**, không thêm wildcard rộng hơn, rồi cập nhật cả mục A.1 của phụ lục này.
5. **Kiểm chứng lại** bằng đúng lệnh đã bị từ chối.

Muốn thử policy mà không cần chạy thật: **IAM → Policy simulator**, chọn role và action.

---

### A.9. Checklist nhanh

**Tạo mới (admin):**
- [ ] Policy `InfraEngineerMoodle` đã tạo (một lần), không còn `<ACCOUNT_ID>` chưa thay
- [ ] IAM user đã tạo, **không** gán policy rộng, có MFA (Infra) hoặc mật khẩu bắt buộc đổi (AI)
- [ ] Role đã tạo: trust policy trỏ **đúng user**, điều kiện **MFA**, gắn `InfraEngineerMoodle`, phiên ≤ 4 giờ
- [ ] User có inline policy chỉ cho assume **đúng role của mình**
- [ ] Thông tin đăng nhập gửi qua **hai kênh khác nhau**

**Kiểm chứng (người dùng):**
- [ ] `get-caller-identity` (hoặc tên role trên Console) hiện `assumed-role/<Role>`
- [ ] Đọc được EC2 ở Singapore
- [ ] **Bị chặn** ở region khác, ở IAM create user, ở S3
- [ ] **Bị chặn** trước khi vào role

**Định kỳ:**
- [ ] Xoay vòng access key (ví dụ 90 ngày), và xoay ngay khi nghi bị lộ
- [ ] Rà lại danh sách user, gỡ người không còn cần quyền
- [ ] Rà lại policy khi Terraform thêm loại resource mới


---

## Phụ lục B: Cấu hình Entra ID, Okta và OAuth 2 của Moodle

Thực hiện ngày 09–11/10/2026; mục 13 của tài liệu này mô tả phần phải làm lại sau mỗi lần dựng lại hạ tầng. Giá trị
cấu hình và lý do thiết kế nằm ở [BAO_CAO_KIEN_TRUC_HA_TANG, mục 9](BAO_CAO_KIEN_TRUC_HA_TANG.md#9-xác-thực-entra-id--okta--moodle).
Domain Okta **luôn** là `trial-3489054.okta.com` (lấy từ thanh địa chỉ, bỏ `-admin`), **không** phải tên hiển thị
`galaxyfinx-trial-3489054`.

### B.1. Các bước cấu hình

Các bước 6–9 của lần triển khai gốc (IAM, Terraform, Ansible, deploy Moodle) chính là các giai đoạn 1–7 của tài liệu
này nên không lặp lại.

#### Bước 1. Entra ID: tạo user và group demo

1. Trong Entra admin center, tạo 3 group: `moodle-students`, `moodle-teachers`, `moodle-denied`.
   - Membership type là **Assigned** (không dùng dynamic rule).
   - **Không lồng group**, vì Okta không import nested group.
2. Tạo user demo và thêm mỗi user vào đúng group của mình.
3. **Bắt buộc điền** cho từng user (*Edit properties*):
   - Tab **Identity**: **First name** và **Last name**.
   - Tab **Contact information**: **Email**.

   Okta bắt buộc `firstName` và `lastName`. Moodle bắt buộc họ, tên và email. Xem lỗi *Import user thất bại* ở B.2.

#### Bước 2. Okta: import user và group từ Entra

1. **Applications → Browse App Catalog → Microsoft Office 365 → Add Integration**, nhập tenant `m365x22866088.onmicrosoft.com`.
2. **Sign-On**: không federate domain cho Moodle. Domain `.onmicrosoft.com` cũng **không thể federate**. Xem lý do ở báo cáo kiến trúc hạ tầng, mục 9.5.
3. **Provisioning → API Integration**:
   - Tick **Enable API integration** và **Import Groups**.
   - **Bấm "Authenticate with Microsoft Account" trước**, đăng nhập Global Admin, **Accept** quyền cho *Okta Microsoft Graph Client*.
   - **Sau đó** mới bấm **Save**. Xem lỗi *Lưu provisioning trước khi cấp consent* ở B.2.
4. **Provisioning → To Okta**: username format là email; tick **Import unlicensed users** (user demo không có license M365); **bỏ** auto-confirm.
5. **Import → Import Now → Full import**, rồi **chỉ confirm user demo**. Okta kéo về toàn bộ user của tenant; org trial chỉ cho 10 active user.
6. Kiểm tra **Directory → Groups**: 3 group có biểu tượng Office 365, mỗi group 1 thành viên. Các group mẫu như *Tailspin Toys* hay *Contoso Team* là dữ liệu có sẵn của tenant demo, bỏ qua.

#### Bước 3. Okta: kích hoạt user và đặt mật khẩu Okta

User sau khi import ở trạng thái **Staged**: chưa có mật khẩu Okta, và **mật khẩu Entra không dùng được**.

1. **Directory → People →** chọn user → **Activate**. **Bỏ tick** gửi email kích hoạt, vì user demo không có hộp thư.
2. Trạng thái chuyển thành *Pending user action*. Bấm **Set Password & Activate** (hoặc lấy mật khẩu tạm), rồi giao mật khẩu cho user demo.
3. Lần đầu đăng nhập, Okta yêu cầu đổi mật khẩu (nếu dùng mật khẩu tạm) và **đăng ký Okta Verify**.
4. Chỉ cần kích hoạt `student.test` và `teacher.test`. User trong `moodle-denied` không cần.

#### Bước 4. Okta: tạo app OIDC cho Moodle

Phương án Terraform cho Okta đã bị bỏ (xem báo cáo kiến trúc hạ tầng, mục 9.5). App được tạo **bằng tay**:

**Applications → Create App Integration → OIDC - OpenID Connect → Web Application**

| Trường | Giá trị |
|---|---|
| Name | `Moodle` |
| Grant type | **Chỉ Authorization Code** |
| Sign-in redirect URI | `http://<ALB_DNS_NAME>/admin/oauth2callback.php` |
| Sign-out redirect URI | `http://<ALB_DNS_NAME>/` |
| Controlled access | **Limit access to selected groups**: `moodle-students`, `moodle-teachers` |
| Client authentication | **Client secret** |
| Require PKCE | **Tắt** |

Kiểm tra: URL của trang app có dạng `/admin/app/oidc_client/instance/<OKTA_CLIENT_ID>/`, và app ở trạng thái **Active**.

#### Bước 5. Okta: policy MFA cho app

**Security → Authentication Policies → Add a policy** `Moodle Demo - MFA`:

| Priority | Rule | IF | THEN |
|---|---|---|---|
| 1 | Students and teachers require MFA | Group membership: `moodle-students` hoặc `moodle-teachers`. Các điều kiện khác để mặc định (Any) | Allowed after successful authentication; **Password + Another factor** (Okta Verify); re-authentication mỗi 2 giờ |
| 2 | Catch-all Rule | Mọi user còn lại | **Denied** |

Tab **Applications → Add app** → chọn `Moodle`.


#### Bước 10. Moodle: cấu hình OAuth 2 service `Okta`

**a) Tạo service:** **Site administration → Server → OAuth 2 services → Custom**

| Trường | Giá trị | Lý do |
|---|---|---|
| Name | `Okta` | |
| Client ID / Client secret | Lấy từ app `Moodle` trong Okta | |
| Authenticate token requests via HTTP headers | **Tắt** | Gửi secret trong POST body (`client_secret_post`), khớp mặc định của Okta |
| Service base URL | **Để trống** | Endpoint nhập thủ công ở bước b) (xem báo cáo kiến trúc hạ tầng, mục 9.5) |
| This service will be used | **Login page only** | Không cần system account hay internal services |
| Display on login page as | `Sign in by Okta` | |
| Scopes | `openid profile email` | |
| Login domains | `m365x22866088.onmicrosoft.com` | Chỉ chấp nhận email thuộc tenant demo |
| Require email verification | **Tắt** | Moodle chưa cấu hình SMTP |

**b) Endpoint** (biểu tượng 🔍 *Configure endpoints*), **dùng domain thật của org**:

| Name | URL |
|---|---|
| `authorization_endpoint` | `https://trial-3489054.okta.com/oauth2/v1/authorize` |
| `token_endpoint` | `https://trial-3489054.okta.com/oauth2/v1/token` |
| `userinfo_endpoint` | `https://trial-3489054.okta.com/oauth2/v1/userinfo` |

**c) User field mappings** (biểu tượng 👤):

| External (claim của Okta) | Internal (trường Moodle) |
|---|---|
| `given_name` | `firstname` |
| `family_name` | `lastname` |
| `email` | `email` |
| `sub` | `username` |

**d) Bật xác thực:** **Plugins → Authentication → Manage authentication**: bật **OAuth 2**. Đảm bảo *Prevent account creation when authenticating* **tắt**.

**e) Khóa các trường do IdP quản lý:** trong Settings của OAuth 2, đặt `First name`, `Last name`, `Email address` là **Locked**.

**Đã kiểm tra trực tiếp trong database:**
- Issuer `enabled=1`, `baseurl=''`, `basicauth=0`, `requireconfirmation=0`, đúng scope và login domain.
- Đủ 3 endpoint và 4 mapping.
- `auth = email,oauth2`.
- `registerauth=''`, nghĩa là **không** cho tự đăng ký tài khoản.
- Cả 3 trường đều `locked`.
- Cột **Login** của service trong giao diện hiện ✅.

#### Bước 11. Kiểm thử đầu-cuối (đang chờ nghiệm thu)

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| 1 | `student.test`: cửa sổ ẩn danh → *Sign in by Okta* → mật khẩu Okta + Okta Verify | Vào Moodle với họ tên lấy từ Entra; account có `auth = oauth2` |
| 2 | `teacher.test` | Tương tự |
| 3 | User trong `moodle-denied` | Okta chặn, **không có** session Moodle |
| 4 | Kiểm tra liên kết account | `auth_oauth2_linked_login.username` là `sub` của Okta (dạng `00u…`), **không phải** email |
| 5 | Phân quyền | Ghi danh `teacher.test` làm Teacher, `student.test` làm Student. Kiểm tra quyền sửa course |
| 6 | Vòng đời từ Entra | Disable user ở Entra → **Full import** ở Okta → user không đăng nhập mới được nữa |

Logout khỏi Moodle **không** logout khỏi Okta, nên đăng nhập lại ngay sẽ không phải nhập mật khẩu. Đây là hành vi bình thường của SSO.


Trạng thái nghiệm thu (22/10/2026): đăng nhập đầu-cuối `student.test` qua Okta đã xác nhận trong DB (một tài khoản
`auth = oauth2`, username là `sub`, một liên kết tới issuer `Okta`, sự kiện `user_loggedin` đúng thời điểm);
`moodle-denied` bị Okta từ chối và Moodle không tạo tài khoản. Lần đầu đăng nhập, Okta đòi đăng ký Okta Verify; nút
*Set up Okta Verify* trên Mac chỉ chạy khi đã cài ứng dụng Okta Verify for macOS, nên nhanh hơn là *Return to
authenticator list* rồi đăng ký bằng điện thoại.

**Dựng lại issuer bằng CLI.** Bản xuất cấu hình trước khi destroy nằm ở
`terraform/.artifacts/preserve/moodle-oauth2-okta.json` (chứa client secret, mode 600). Chép vào container qua stdin,
chạy script khôi phục, rồi xoá ngay; PHP CLI phải chạy với `-u 1000:1999`:

```bash
C=terraform/.artifacts/moodle-ssh.config
ssh -F $C moodle-app-a 'sudo docker exec -i -u 1000:1999 release-moodle-web-1 sh -c "umask 077; cat > /tmp/okta-issuer.json"' \
  < terraform/.artifacts/preserve/moodle-oauth2-okta.json
ssh -F $C moodle-app-a 'sudo docker exec -i -u 1000:1999 release-moodle-web-1 php' < restore-okta.php
ssh -F $C moodle-app-a 'sudo docker exec -u 1000:1999 release-moodle-web-1 rm -f /tmp/okta-issuer.json'
```

`restore-okta.php` đọc rồi xoá `/tmp/okta-issuer.json`, gọi `core\oauth2\api::create_issuer`, `create_endpoint`,
`create_user_field_mapping`, bật issuer, đặt `auth` (`email,oauth2`), khoá các trường họ tên và email, rồi
`purge_all_caches()`. Nếu đã có issuer cùng tên, script dừng mà không đổi gì. Kết quả mong đợi:
`restored issuer=Okta enabled=1 showonloginpage=2 endpoints=3 mappings=4 auth=email,oauth2`. Container không có
`CAP_FOWNER`: file trong `/tmp` phải được xoá bởi đúng user đã tạo nó.

### B.2. Lỗi đã gặp khi cấu hình SSO

#### Lỗi gốc: dùng nhầm domain của org Okta

| | |
|---|---|
| **Triệu chứng** | API token mới tạo bị báo `E0000011 Invalid token provided`; OAuth service app bị `invalid_client: Invalid value for 'client_id'`; `/.well-known/openid-configuration` và `/oauth2/v1/keys` trả 403 `E0000006`; `/oauth2/v1/authorize` trả **400 trống** với mọi tham số; user đăng nhập báo "Không thể đăng nhập" |
| **Nguyên nhân gốc** | Lấy `galaxyfinx-trial-3489054`, **tên hiển thị** của org ở góc phải Admin Console, làm subdomain. Domain thật là `trial-3489054.okta.com`, xem trên **thanh địa chỉ** (`trial-3489054-admin.okta.com`). Mọi request đều gửi tới **một org khác** |
| **Chẩn đoán sai trước đó** | Kết luận "org trial bị hỏng ở cấp tenant", đề xuất tạo org mới. Kết luận này **sai**. Việc người vận hành yêu cầu tìm root cause thay vì tạo lại org là quyết định đúng |
| **Cách tìm ra** | Bài KB của Okta về lỗi 400 không kèm mô tả trỏ tới lỗi `client_id`. Kiểm tra trang app (đúng loại `oidc_client`, đang Active), thì **URL trên thanh địa chỉ** để lộ domain thật |
| **Xác nhận** | Trên domain đúng: discovery 200, JWKS 200, `/authorize` trả trang đăng nhập, và redirect sai trả lỗi **có mô tả** |
| **Cách sửa** | Đổi 3 endpoint của Moodle sang `trial-3489054.okta.com` |
| **Phòng tránh** | Luôn lấy domain Okta từ **thanh địa chỉ** (bỏ `-admin`). Đối chứng nhanh: `curl https://<domain>/.well-known/openid-configuration` phải trả 200 |

#### Lộ thông tin bí mật trong lúc làm

| Sự cố | Xử lý |
|---|---|
| API token Okta xuất hiện trong ảnh chụp terminal (do gõ `export OKTA_API_TOKEN=...`) | Revoke token; dùng `read -s` để nhập token |
| File `terraform/okta/env` chứa token nhưng **không được Git bỏ qua** | Đổi tên thành `.env` (đã có trong `.gitignore`), `chmod 600` |
| Private key của OAuth service app nằm trong repo | Chuyển ra `~/.okta/`, `chmod 600` |
| Secret access key AWS lộ khi mở file CSV trong editor | Tạo access key mới, vô hiệu hóa và xóa key cũ, xóa file CSV |

**Nguyên tắc:** không gõ secret trên dòng lệnh, không mở file chứa secret trong editor đang được chia sẻ, chụp màn hình trước khi secret hiện ra, và luôn chạy `git check-ignore -v <file>` với mọi file chứa bí mật.

#### Lưu provisioning trước khi cấp consent

| | |
|---|---|
| **Triệu chứng** | *"Please grant consent by authenticating with Microsoft Graph"* |
| **Nguyên nhân** | Bấm **Save** trước khi **Authenticate with Microsoft Account** |
| **Cách sửa** | Authenticate bằng Global Admin, Accept quyền, rồi mới Save |

#### Import user thất bại: firstName / lastName trống

| | |
|---|---|
| **Triệu chứng** | *Confirm Imported User Assignments*: `firstName/lastName – The field cannot be left blank` |
| **Nguyên nhân** | User trên Entra chưa có First name và Last name |
| **Cách sửa** | Điền họ, tên và email trong Entra, rồi **Full import** lại. Sửa ở nguồn (Entra) chứ không vá ở Okta |

#### Không đăng nhập Okta được bằng mật khẩu Entra; không thấy nút đặt mật khẩu

| | |
|---|---|
| **Nguyên nhân** | Import qua Graph **không chép mật khẩu**. User đang **Staged** nên chưa có mật khẩu Okta, và nút đặt mật khẩu chỉ xuất hiện sau khi Activate |
| **Cách sửa** | Activate (không gửi email), rồi **Set Password & Activate** hoặc dùng mật khẩu tạm |


#### Script chỉ chạy trên Linux, lỗi trên macOS

| Script | Lỗi | Cách sửa |
|---|---|---|
| `automation/update-infrastructure.sh` | `chmod --reference` là tùy chọn chỉ có ở GNU | Đọc quyền file bằng `stat -c '%a'` (Linux), nếu không được thì `stat -f '%Lp'` (macOS) |
| `automation/stage-moodle-release.sh` | `stat -c`, `ss -ltn`, chạy `psql` trong Docker (máy không có Docker) | Thêm cách đọc quyền file cho macOS; hàm `port_is_listening` dùng `ss` hoặc `lsof`; thêm nhánh chạy **`psql` trực tiếp** (vẫn qua SSH tunnel, vẫn `sslmode=verify-full`). Hành vi trên Linux/WSL giữ nguyên |

#### Chạy script từ sai thư mục

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `No such file or directory` khi đứng trong `automation/` | Đường dẫn tương đối tính từ thư mục gốc repo | `cd` về thư mục gốc repo |
| Không thấy file mật khẩu admin | Lệnh tạo secret chạy trong `automation/`, nên file nằm ở `automation/terraform/.artifacts/…`, **chỗ này không được Git bỏ qua** | Chuyển về `terraform/.artifacts/moodle-secrets/`, so mã băm với bản trên server để xác nhận trùng |
| `Required command not found: ansible-playbook` | Chưa cài Ansible | `brew install ansible` |


#### Gateway Timeout (504) trên trang Moodle

| | |
|---|---|
| **Triệu chứng** | Người dùng gặp 504 một lần |
| **Điều tra** | ALB và cả 2 target healthy; `/healthz.php` và `/login` trả 200; tải thấp; không có lỗi PHP. Request bị 504 **không có trong access log** |
| **Kết luận** | Không tái hiện được. Khả năng cao là xảy ra trong lúc Moodle đang cài đặt hoặc khởi động. **Chưa đóng**: nếu lặp lại, ghi lại URL và thời điểm để đối chiếu với metric của ALB |

#### Hai phát hiện không phải lỗi

- Access log có request tới `wp-login.php` và `wp-links-opml.php`: đó là **bot quét lỗ hổng WordPress** trên Internet, vô hại với Moodle.
- `m365x22866088` hiện thành `m365×22866088` trên giao diện Okta: do **font** hiển thị chữ "x" giữa các con số. Ký tự thật vẫn là "x".


### B.3. Lệnh kiểm tra nhanh

```bash
# Domain Okta có đúng không (phải trả issuer = https://trial-3489054.okta.com)
curl -s https://trial-3489054.okta.com/.well-known/openid-configuration | jq -r .issuer

# Moodle qua ALB
MOODLE_URL="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"
curl -s -o /dev/null -w '%{http_code}\n' "$MOODLE_URL/healthz.php"

# Hạ tầng có khớp với code không (phải "No changes")
cd terraform && eval "$(aws configure export-credentials --profile target-account --format env)" \
  && terraform plan -var-file=deployment.tfvars -detailed-exitcode

# Log 20 request gần nhất trên mỗi node Moodle (chạy từ thư mục gốc repo)
for h in moodle-app-a moodle-app-b; do
  ssh -F terraform/.artifacts/moodle-ssh.config "$h" \
    'sudo docker logs --since 10m release-moodle-web-1 2>&1 | grep -v healthz | tail -20'
done
```

---

## Phụ lục C: Kết thúc phiên và bàn giao

Nếu một bộ gây lỗi hay chiến dịch thất bại, đừng lặp lại lỗi ngay. Reset các kịch bản trong allowlist và đưa hệ thống
về baseline trước:

```bash
bash automation/moodle-environment-baseline.sh reset
bash automation/moodle-environment-baseline.sh verify
```

Kết thúc mọi phiên hạ tầng hoặc thử nghiệm bằng kiểm tra drift; kỳ vọng `No changes`:

```bash
terraform -chdir=terraform plan \
  -input=false \
  -var-file=deployment.tfvars \
  -detailed-exitcode
```

Ghi lại SHA image Moodle và Agent đang chạy, ID chiến dịch, chỉ số nghiệm thu, đường dẫn artifact kết quả, các quan
sát về alert và kết quả Terraform cuối cùng. Che credential, IP không cần cho báo cáo, ARN của secret và private key.


---

## Phụ lục D: Gỡ toàn bộ hạ tầng (chi tiết)

Đây là thao tác **phá huỷ**. Nó xoá mọi tài nguyên mà state Terraform của staging đang quản lý: hai EC2 Moodle,
monitor và EIP, ALB và target group, RDS, EFS cùng access point và mount target, NAT Gateway và EIP của chúng, subnet,
route, Internet Gateway, security group, IAM role của monitor và tài nguyên CloudWatch do Terraform quản lý. Nó
**không** xoá image trên GitHub, mã nguồn, khoá trên máy bạn, hay tài nguyên không nằm trong state.

### D.1. Trước khi tạo plan destroy

1. Dừng mọi thử nghiệm hoặc soak đang chạy, thu thập bằng chứng cần cho báo cáo.
2. Quyết định là **gỡ có giữ dữ liệu** hay **gỡ hẳn**. RDS đang đặt `skip_final_snapshot=true` và
   `delete_automated_backups=true`, nên destroy bình thường **không** tạo snapshot cuối và **xoá** backup tự động.
3. Cần giữ dữ liệu: tạo snapshot thủ công và đợi nó xong trước. Snapshot có phí lưu trữ và phải được giữ có chủ đích.
4. Gỡ hẳn: liệt kê snapshot RDS thủ công và recovery point AWS Backup của EFS **trước** destroy, rồi chỉ xoá đúng các
   ID đã review **sau khi** Terraform đã gỡ tài nguyên chính.
5. Xác nhận đúng account, workspace và state của staging. Không bao giờ xoá VPC trên Console để "đi tắt" qua state.
6. Gỡ runner CD khỏi GitHub (*Settings → Actions → Runners → monitor-ai-01 → Remove*) và vô hiệu PAT `GHCR_TOKEN`
   nếu không còn dùng.

Snapshot thủ công (tuỳ chọn, chỉ làm sau khi đã xem tên và chi phí):

```bash
export AWS_PROFILE=target-account
export AWS_REGION=ap-southeast-1
DB_IDENTIFIER="$(terraform -chdir=terraform output -json moodle_database | jq -r .instance_identifier)"
SNAPSHOT_ID="${DB_IDENTIFIER}-final-$(date -u +%Y%m%dT%H%M%SZ)"

aws rds create-db-snapshot \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --db-instance-identifier "$DB_IDENTIFIER" \
  --db-snapshot-identifier "$SNAPSHOT_ID"

aws rds wait db-snapshot-available \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --db-snapshot-identifier "$SNAPSHOT_ID"
```

### D.2. Plan destroy, review, rồi apply đúng file đó

EFS có `lifecycle.prevent_destroy = true` để chống mất dữ liệu. Khi gỡ hẳn có chủ đích, tạm xoá khối `lifecycle`
trong `terraform/efs.tf`, chạy plan destroy đã review, rồi **khôi phục** khối đó trong source khi destroy xong. Không
dùng `-target` để lách.

```bash
umask 077
mkdir -p terraform/.artifacts

terraform -chdir=terraform plan \
  -destroy \
  -input=false \
  -var-file=deployment.tfvars \
  -out=.artifacts/moodle-destroy.tfplan

terraform -chdir=terraform show -no-color .artifacts/moodle-destroy.tfplan
```

Plan chỉ được chứa đúng tài nguyên Moodle staging. Sau khi review:

```bash
terraform -chdir=terraform apply .artifacts/moodle-destroy.tfplan
```

### D.3. Kiểm tra không còn gì tính tiền

Xác nhận Terraform không còn theo dõi tài nguyên, đã khôi phục khối `lifecycle` của EFS (`git diff` phải sạch), và
kiểm tra **trong AWS**: một lần apply bị ngắt có thể để lại tài nguyên ngoài state (mục 7.2 và 18.2).

```bash
terraform -chdir=terraform state list
git diff --exit-code terraform/efs.tf
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier'
aws ec2 describe-nat-gateways --filter Name=state,Values=pending,available --query 'NatGateways[].NatGatewayId'
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
aws efs describe-file-systems --query 'FileSystems[].FileSystemId'
aws ec2 describe-addresses --query 'Addresses[].PublicIp'
aws ec2 describe-vpcs --filters Name=is-default,Values=false --query 'Vpcs[].VpcId'
```

Tài nguyên chưa từng nằm trong state (ví dụ một EC2 tạo tay trong VPC mặc định) nằm ngoài phạm vi phụ lục này: báo
cho người sở hữu, đừng tự xoá. Xoá các dòng `~/.ssh/known_hosts` của host đã gỡ trước lần dựng lại kế tiếp (mục 8).

### D.4. Xoá snapshot và recovery point khi gỡ hẳn

Terraform không xoá snapshot RDS thủ công hay recovery point EFS do AWS Backup quản lý. Chỉ làm phần này khi chủ đích
là xoá **toàn bộ** dữ liệu có thể khôi phục, sau khi destroy thành công. Không thể hoàn tác.

Liệt kê đúng mục tiêu trước. Đừng xoá snapshot hay recovery point chỉ vì tên trông giống; đối chiếu database
identifier hoặc ARN của EFS và thời điểm tạo.

```bash
export AWS_PROFILE=target-account
export AWS_REGION=ap-southeast-1
DB_IDENTIFIER='moodle-staging-postgres' # Ghi lại identifier thật trước khi destroy.

aws rds describe-db-snapshots \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --snapshot-type manual \
  --query "DBSnapshots[?DBInstanceIdentifier=='$DB_IDENTIFIER'].{id:DBSnapshotIdentifier,status:Status,created:SnapshotCreateTime}" \
  --output table
```

Chỉ xoá đúng ID snapshot đã review:

```bash
aws rds delete-db-snapshot \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --db-snapshot-identifier 'REVIEWED_MANUAL_SNAPSHOT_ID'
```

Với EFS, ghi ARN trước khi destroy hoặc lấy từ baseline đã chụp. Vòng lặp dưới đây cố ý chỉ nhắm vault backup tự
động của EFS và đúng một ARN EFS đã review. Chạy lệnh liệt kê và xem từng ARN trả về trước, rồi mới chạy vòng xoá.

```bash
EFS_ARN='arn:aws:elasticfilesystem:ap-southeast-1:ACCOUNT_ID:file-system/fs-REVIEWED_ID'
BACKUP_VAULT='aws/efs/automatic-backup-vault'

aws backup list-recovery-points-by-backup-vault \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --backup-vault-name "$BACKUP_VAULT" \
  --query "RecoveryPoints[?ResourceArn=='$EFS_ARN'].{arn:RecoveryPointArn,created:CreationDate,status:Status}" \
  --output table
```

```bash
mapfile -t RECOVERY_POINTS < <(
  aws backup list-recovery-points-by-backup-vault \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --backup-vault-name "$BACKUP_VAULT" \
    --query "RecoveryPoints[?ResourceArn=='$EFS_ARN'].RecoveryPointArn" \
    --output text | tr '\t' '\n'
)

for recovery_point in "${RECOVERY_POINTS[@]}"; do
  test -n "$recovery_point" || continue
  aws backup delete-recovery-point \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --backup-vault-name "$BACKUP_VAULT" \
    --recovery-point-arn "$recovery_point"
done
```

Vault backup mặc định của EFS do AWS quản lý có thể có resource policy `Deny` tường minh cho
`backup:DeleteRecoveryPoint`, và role `InfraEngineer` cũng không có `backup:*`. Nếu bị `AccessDenied ... explicit
deny`, **đừng** tự nới policy của vault dùng chung: nhờ admin review và duyệt một thay đổi hẹp, hoặc để recovery
point tự hết hạn. `mapfile` cần bash ≥ 4; trên macOS (bash 3.2) hãy chạy bằng bash của Homebrew.

Cuối cùng xoá bản sao secret và artifact cục bộ theo chính sách xoá an toàn của máy. Không commit chúng làm "bản sao
lưu".

### D.5. Chi phí khi tạm dừng

Dừng EC2 chỉ giảm tiền máy; RDS, EFS, NAT Gateway, ALB, EIP, backup, snapshot và lưu trữ vẫn tính tiền. Giữ bằng
chứng trước. Khi không cần staging nữa, review một plan destroy tường minh; không dùng thao tác xoá tay rộng trên AWS
thay cho quản lý state của Terraform.
