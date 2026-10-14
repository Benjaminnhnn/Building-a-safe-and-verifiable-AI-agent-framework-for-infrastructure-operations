# Triển khai xác thực Moodle với Microsoft Entra ID và Okta (OIDC)

**Môi trường:** staging, AWS `ap-southeast-1`
**Thời gian triển khai:** 09–11/10/2026
**Tài liệu liên quan:** [Kế hoạch demo](OKTA_ENTRA_MOODLE_OIDC_DEMO_PLAN.md) · [Moodle Operations Runbook](MOODLE_OPERATIONS_RUNBOOK.md)

Tài liệu này ghi lại **đúng những gì đã thực hiện**: các bước triển khai, các lỗi gặp phải cùng nguyên nhân gốc, và lý do của từng quyết định thiết kế. Các giá trị nhạy cảm như Account ID, IP, client secret, mật khẩu và token được thay bằng placeholder dạng `<...>`.

---

## Mục lục

1. [Tóm tắt](#1-tóm-tắt)
2. [Kiến trúc và luồng hoạt động](#2-kiến-trúc-và-luồng-hoạt-động)
3. [Các thành phần và giá trị cấu hình](#3-các-thành-phần-và-giá-trị-cấu-hình)
4. [Các bước triển khai](#4-các-bước-triển-khai)
5. [Các lỗi gặp phải và cách xử lý](#5-các-lỗi-gặp-phải-và-cách-xử-lý)
6. [Vì sao triển khai như vậy](#6-vì-sao-triển-khai-như-vậy)
7. [Trạng thái hiện tại và việc còn lại](#7-trạng-thái-hiện-tại-và-việc-còn-lại)
8. [Bài học rút ra](#8-bài-học-rút-ra)
9. [Phụ lục](#9-phụ-lục)

---

## 1. Tóm tắt

| Hạng mục | Kết quả |
|---|---|
| Hạ tầng AWS cho Moodle (Terraform, 68 resource) | ✅ Dựng lại xong, không có drift |
| Runtime trên EC2 (Ansible) và Moodle 5.2.3 | ✅ Đang chạy, 2 ALB target `healthy` |
| Entra ID: user và group demo | ✅ Đã tạo |
| Okta: import user và group từ Entra | ✅ Đã import |
| Okta: app OIDC `Moodle` và policy MFA | ✅ Đã cấu hình bằng tay |
| Moodle: OAuth 2 service `Okta` | ✅ Đã cấu hình và kiểm tra trong database |
| Đăng nhập đầu-cuối qua Okta | ⏳ **Chưa nghiệm thu.** Trang đăng nhập Okta đã hiện đúng sau khi sửa domain; chưa có user nào đăng nhập xong |
| HTTPS | ⏳ Chưa làm. Đang chạy HTTP, chỉ dùng để test chức năng |

**Lỗi tốn thời gian nhất:** dùng nhầm **tên hiển thị** của org Okta (`galaxyfinx-trial-3489054`) làm **domain**. Domain thật là `trial-3489054.okta.com`. Lỗi này gây ra cả chuỗi triệu chứng (API token "invalid", `invalid_client`, discovery 403, `/authorize` 400) và đã dẫn tới chẩn đoán sai là "org Okta bị hỏng". Xem [mục 5.1](#51-lỗi-gốc-dùng-nhầm-domain-của-org-okta).

---

## 2. Kiến trúc và luồng hoạt động

### 2.1. Phân chia trách nhiệm

| Hệ thống | Trả lời câu hỏi | Không làm |
|---|---|---|
| **Microsoft Entra ID** | *Người này là ai, tài khoản còn hoạt động không?* Quản lý vòng đời user và group | Không cấp session Moodle, không phân quyền course |
| **Okta** | *Có đúng là người này không, có được dùng Moodle không?* Mật khẩu, MFA, phát hành token OIDC | Không quản lý course hay role Moodle |
| **Moodle** | *Vào rồi thì được làm gì?* Account local, session, role, ghi danh course | Không nhận và không lưu mật khẩu Entra hay Okta |

### 2.2. Luồng đồng bộ tài khoản (chạy nền)

```
Microsoft Entra ID ──(Microsoft Graph API, Okta chủ động import)──► Okta Universal Directory
   user + group                                                         bản sao user + group
```

- Chỉ đồng bộ **profile và group**, **không đồng bộ mật khẩu**.
- Thay đổi ở Entra chỉ sang Okta **ở lần import tiếp theo**. User bị **disable** trong Entra chỉ được phát hiện bởi **Full import**, incremental import không thấy.

### 2.3. Luồng đăng nhập (OIDC Authorization Code)

```
Trình duyệt             Moodle (relying party)            Okta (identity provider)
    │ 1. Bấm "Sign in by Okta"                                │
    │──────────────────────►│                                 │
    │ 2. 302 → /oauth2/v1/authorize?client_id&redirect_uri&state&scope
    │◄──────────────────────│                                 │
    │ 3. Nhập mật khẩu Okta + Okta Verify (chỉ tại Okta)       │
    │────────────────────────────────────────────────────────►│
    │                       │      Okta kiểm tra: user Active? được assign app? policy MFA?
    │ 4. 302 → <moodle>/admin/oauth2callback.php?code=…&state=…
    │◄────────────────────────────────────────────────────────│
    │──────────────────────►│                                 │
    │                       │ 5. POST /oauth2/v1/token (code + client secret)  [server → server]
    │                       │────────────────────────────────►│
    │                       │◄──────── access token, id token ─│
    │                       │ 6. GET /oauth2/v1/userinfo → sub, email, given_name, family_name
    │                       │ 7. Tìm linked login theo (issuer, sub): có → đăng nhập; chưa có → tạo account
    │ 8. Cookie MoodleSession│                                 │
    │◄──────────────────────│                                 │
```

Các điểm quan trọng:
- Mật khẩu **chỉ đi giữa trình duyệt và Okta** (bước 3).
- `code` đi qua trình duyệt nhưng **vô dụng nếu không có client secret**, thứ chỉ nằm trên server Moodle (bước 5).
- Bước 5–6 là **Moodle server tự gọi ra Internet** qua NAT Gateway. Security group của Moodle đã có rule egress 443.

---

## 3. Các thành phần và giá trị cấu hình

| Thành phần | Giá trị |
|---|---|
| Entra tenant | `m365x22866088.onmicrosoft.com` (tenant demo Microsoft 365) |
| Group trên Entra (loại Microsoft 365, thành viên Assigned, không lồng nhau) | `moodle-students`, `moodle-teachers`, `moodle-denied` |
| User demo | `student.test@…`, `teacher.test@…`, cùng một user thuộc `moodle-denied` |
| **Okta org (domain thật)** | **`https://trial-3489054.okta.com`** (admin: `trial-3489054-admin.okta.com`) |
| Tên hiển thị của Okta org (**không phải domain**) | `galaxyfinx-trial-3489054` |
| Okta authorization server | **Org authorization server** `/oauth2/v1/...` (không dùng `/oauth2/default`) |
| App OIDC trong Okta | `Moodle`, loại `oidc_client` (Web), Client ID `<OKTA_CLIENT_ID>` |
| Moodle | 5.2.3, image `ghcr.io/benjaminnhnn/moodle:fd4b7482dfd04f5e292dfe2f4063b036c5c6619f` |
| URL Moodle (staging, HTTP) | `http://<ALB_DNS_NAME>` (`terraform output moodle_application`) |
| Redirect URI | `http://<ALB_DNS_NAME>/admin/oauth2callback.php` |
| AWS | Account `<ACCOUNT_ID>`, region `ap-southeast-1`, role `InfraEngineer` |

---

## 4. Các bước triển khai

Các bước được sắp theo **thứ tự phụ thuộc**, không hẳn theo thứ tự thời gian thực tế. Ví dụ: hạ tầng AWS được dựng lại *giữa chừng*, nhưng phải có nó thì Moodle mới có redirect URI.

### Bước 1. Entra ID: tạo user và group demo

1. Trong Entra admin center, tạo 3 group: `moodle-students`, `moodle-teachers`, `moodle-denied`.
   - Membership type là **Assigned** (không dùng dynamic rule).
   - **Không lồng group**, vì Okta không import nested group.
2. Tạo user demo và thêm mỗi user vào đúng group của mình.
3. **Bắt buộc điền** cho từng user (*Edit properties*):
   - Tab **Identity**: **First name** và **Last name**.
   - Tab **Contact information**: **Email**.

   Okta bắt buộc `firstName` và `lastName`. Moodle bắt buộc họ, tên và email. Xem [lỗi 5.6](#56-import-user-thất-bại-firstname--lastname-trống).

### Bước 2. Okta: import user và group từ Entra

1. **Applications → Browse App Catalog → Microsoft Office 365 → Add Integration**, nhập tenant `m365x22866088.onmicrosoft.com`.
2. **Sign-On**: không federate domain cho Moodle. Domain `.onmicrosoft.com` cũng **không thể federate**. Xem [mục 6.4](#64-không-dùng-ws-federation-cho-luồng-moodle).
3. **Provisioning → API Integration**:
   - Tick **Enable API integration** và **Import Groups**.
   - **Bấm "Authenticate with Microsoft Account" trước**, đăng nhập Global Admin, **Accept** quyền cho *Okta Microsoft Graph Client*.
   - **Sau đó** mới bấm **Save**. Xem [lỗi 5.5](#55-lưu-provisioning-trước-khi-cấp-consent).
4. **Provisioning → To Okta**: username format là email; tick **Import unlicensed users** (user demo không có license M365); **bỏ** auto-confirm.
5. **Import → Import Now → Full import**, rồi **chỉ confirm user demo**. Okta kéo về toàn bộ user của tenant; org trial chỉ cho 10 active user.
6. Kiểm tra **Directory → Groups**: 3 group có biểu tượng Office 365, mỗi group 1 thành viên. Các group mẫu như *Tailspin Toys* hay *Contoso Team* là dữ liệu có sẵn của tenant demo, bỏ qua.

### Bước 3. Okta: kích hoạt user và đặt mật khẩu Okta

User sau khi import ở trạng thái **Staged**: chưa có mật khẩu Okta, và **mật khẩu Entra không dùng được**.

1. **Directory → People →** chọn user → **Activate**. **Bỏ tick** gửi email kích hoạt, vì user demo không có hộp thư.
2. Trạng thái chuyển thành *Pending user action*. Bấm **Set Password & Activate** (hoặc lấy mật khẩu tạm), rồi giao mật khẩu cho user demo.
3. Lần đầu đăng nhập, Okta yêu cầu đổi mật khẩu (nếu dùng mật khẩu tạm) và **đăng ký Okta Verify**.
4. Chỉ cần kích hoạt `student.test` và `teacher.test`. User trong `moodle-denied` không cần.

### Bước 4. Okta: tạo app OIDC cho Moodle

Phương án Terraform cho Okta đã bị bỏ (xem [mục 6.7](#67-cấu-hình-okta-bằng-tay-thay-vì-terraform)). App được tạo **bằng tay**:

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

### Bước 5. Okta: policy MFA cho app

**Security → Authentication Policies → Add a policy** `Moodle Demo - MFA`:

| Priority | Rule | IF | THEN |
|---|---|---|---|
| 1 | Students and teachers require MFA | Group membership: `moodle-students` hoặc `moodle-teachers`. Các điều kiện khác để mặc định (Any) | Allowed after successful authentication; **Password + Another factor** (Okta Verify); re-authentication mỗi 2 giờ |
| 2 | Catch-all Rule | Mọi user còn lại | **Denied** |

Tab **Applications → Add app** → chọn `Moodle`.

### Bước 6. AWS: quyền IAM cho Infrastructure Engineer

```
IAM user (người thật, MFA) ──sts:AssumeRole──► Role "InfraEngineer" (phiên 4 giờ, bắt buộc MFA)
                                                 └─ policy "InfraEngineerMoodle" (chỉ ap-southeast-1)
```

1. **Policy `InfraEngineerMoodle`** (IAM → Policies → Create policy → JSON): nội dung đầy đủ ở [Phụ lục A](#a-iam-policy-infraengineermoodle). Policy chỉ chứa **permission**, không có trường `Principal`.
2. **Role `InfraEngineer`** (IAM → Roles → Create role → *Custom trust policy*): trust policy ở [Phụ lục B](#b-trust-policy-của-role-infraengineer), gắn policy `InfraEngineerMoodle`, *Maximum session duration* 4 giờ.
3. **IAM user**: không gán policy rộng nào. Chỉ có MFA, inline policy `AssumeInfraEngineer` (chỉ `sts:AssumeRole` vào role trên), và access key dùng cho CLI.
4. **AWS CLI** trên máy vận hành:
   ```ini
   # ~/.aws/config
   [profile target-account]
   role_arn = arn:aws:iam::<ACCOUNT_ID>:role/InfraEngineer
   source_profile = <base-profile>
   mfa_serial = arn:aws:iam::<ACCOUNT_ID>:mfa/<mfa-device>
   region = ap-southeast-1
   duration_seconds = 14400
   ```
5. **Đã kiểm thử quyền:**

   | Kiểm tra | Kỳ vọng | Kết quả |
   |---|---|---|
   | `sts get-caller-identity` | `assumed-role/InfraEngineer` | ✅ |
   | `ec2 describe-vpcs` ở Singapore | Cho phép | ✅ |
   | Đọc AMI qua SSM | Cho phép | ✅ |
   | `ec2 describe-vpcs --region us-east-1` | Bị chặn bởi explicit deny | ✅ |
   | `iam create-user` | `AccessDenied` | ✅ |
   | `s3api list-buckets` | `AccessDenied` | ✅ |

### Bước 7. AWS: dựng lại hạ tầng bằng Terraform

```bash
cp -n terraform/deployment.tfvars.example terraform/deployment.tfvars   # file này được Git bỏ qua
bash automation/update-infrastructure.sh                                # điền IP hiện tại vào my_ip_cidr
ssh-keygen -t ed25519 -f ~/.ssh/aws-hybrid -C "moodle-aiops"            # máy vận hành chưa có key

cd terraform
eval "$(aws configure export-credentials --profile target-account --format env)"
export AWS_REGION=ap-southeast-1
terraform init && terraform validate && terraform test                  # 19/19 test đạt
terraform plan -var-file=deployment.tfvars -out=moodle.tfplan            # 68 to add
terraform apply moodle.tfplan
terraform plan -var-file=deployment.tfvars                               # "No changes"
```

Kết quả: VPC, 7 subnet, 2 NAT Gateway, ALB, 3 EC2 (`monitor-ai-01`, `moodle-app-a`, `moodle-app-b`), RDS PostgreSQL 16.10 (private, mã hóa), EFS (mã hóa, có backup).

### Bước 8. Ansible: chuẩn bị runtime trên EC2

Chạy **từ thư mục gốc repo**:

```bash
brew install ansible
bash automation/prepare-moodle-runtime.sh
```

Trước khi chạy, phải thêm host key của EC2 mới vào `~/.ssh/known_hosts` **sau khi đối chiếu fingerprint với console output của EC2**. Xem [lỗi 5.12](#512-ansible-unreachable-host-key-verification-failed).

Kết quả: `ok=18 changed=10 unreachable=0 failed=0` trên cả 2 node. Docker và Compose đã cài, EFS đã mount qua TLS.

### Bước 9. Deploy Moodle

Chạy **từ thư mục gốc repo**:

```bash
brew install libpq && export PATH="/opt/homebrew/opt/libpq/bin:$PATH"   # psql >= 15 cho macOS
export MOODLE_IMAGE='ghcr.io/benjaminnhnn/moodle:fd4b7482dfd04f5e292dfe2f4063b036c5c6619f'

bash automation/moodle-preflight.sh "$MOODLE_IMAGE"     # package public, không cần GHCR PAT

umask 077 && mkdir -p terraform/.artifacts/moodle-secrets
openssl rand -hex 32    > terraform/.artifacts/moodle-secrets/moodle-db-password
openssl rand -base64 32 > terraform/.artifacts/moodle-secrets/moodle-admin-password

bash automation/stage-moodle-release.sh "$MOODLE_IMAGE" \
  terraform/.artifacts/moodle-secrets/moodle-db-password \
  terraform/.artifacts/moodle-secrets/moodle-admin-password
```

Sau đó, theo runbook §8:
1. Cài Moodle **một lần**, chỉ trên node A (`--profile installer run --rm moodle-install`).
2. Khởi động **web + cron trên node A**, chỉ **web trên node B**.

Nghiệm thu: `/healthz.php` trả 200, và cả 2 ALB target `healthy`.

### Bước 10. Moodle: cấu hình OAuth 2 service `Okta`

**a) Tạo service:** **Site administration → Server → OAuth 2 services → Custom**

| Trường | Giá trị | Lý do |
|---|---|---|
| Name | `Okta` | |
| Client ID / Client secret | Lấy từ app `Moodle` trong Okta | |
| Authenticate token requests via HTTP headers | **Tắt** | Gửi secret trong POST body (`client_secret_post`), khớp mặc định của Okta |
| Service base URL | **Để trống** | Endpoint nhập thủ công ở bước b) (xem [mục 6.8](#68-endpoint-nhập-thủ-công)) |
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

### Bước 11. Kiểm thử đầu-cuối (đang chờ nghiệm thu)

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| 1 | `student.test`: cửa sổ ẩn danh → *Sign in by Okta* → mật khẩu Okta + Okta Verify | Vào Moodle với họ tên lấy từ Entra; account có `auth = oauth2` |
| 2 | `teacher.test` | Tương tự |
| 3 | User trong `moodle-denied` | Okta chặn, **không có** session Moodle |
| 4 | Kiểm tra liên kết account | `auth_oauth2_linked_login.username` là `sub` của Okta (dạng `00u…`), **không phải** email |
| 5 | Phân quyền | Ghi danh `teacher.test` làm Teacher, `student.test` làm Student. Kiểm tra quyền sửa course |
| 6 | Vòng đời từ Entra | Disable user ở Entra → **Full import** ở Okta → user không đăng nhập mới được nữa |

Logout khỏi Moodle **không** logout khỏi Okta, nên đăng nhập lại ngay sẽ không phải nhập mật khẩu. Đây là hành vi bình thường của SSO.

---

## 5. Các lỗi gặp phải và cách xử lý

### 5.1. Lỗi gốc: dùng nhầm domain của org Okta

| | |
|---|---|
| **Triệu chứng** | API token mới tạo bị báo `E0000011 Invalid token provided`; OAuth service app bị `invalid_client: Invalid value for 'client_id'`; `/.well-known/openid-configuration` và `/oauth2/v1/keys` trả 403 `E0000006`; `/oauth2/v1/authorize` trả **400 trống** với mọi tham số; user đăng nhập báo "Không thể đăng nhập" |
| **Nguyên nhân gốc** | Lấy `galaxyfinx-trial-3489054`, **tên hiển thị** của org ở góc phải Admin Console, làm subdomain. Domain thật là `trial-3489054.okta.com`, xem trên **thanh địa chỉ** (`trial-3489054-admin.okta.com`). Mọi request đều gửi tới **một org khác** |
| **Chẩn đoán sai trước đó** | Kết luận "org trial bị hỏng ở cấp tenant", đề xuất tạo org mới. Kết luận này **sai**. Việc người vận hành yêu cầu tìm root cause thay vì tạo lại org là quyết định đúng |
| **Cách tìm ra** | Bài KB của Okta về lỗi 400 không kèm mô tả trỏ tới lỗi `client_id`. Kiểm tra trang app (đúng loại `oidc_client`, đang Active), thì **URL trên thanh địa chỉ** để lộ domain thật |
| **Xác nhận** | Trên domain đúng: discovery 200, JWKS 200, `/authorize` trả trang đăng nhập, và redirect sai trả lỗi **có mô tả** |
| **Cách sửa** | Đổi 3 endpoint của Moodle sang `trial-3489054.okta.com` |
| **Phòng tránh** | Luôn lấy domain Okta từ **thanh địa chỉ** (bỏ `-admin`). Đối chứng nhanh: `curl https://<domain>/.well-known/openid-configuration` phải trả 200 |

### 5.2. Lộ thông tin bí mật trong lúc làm

| Sự cố | Xử lý |
|---|---|
| API token Okta xuất hiện trong ảnh chụp terminal (do gõ `export OKTA_API_TOKEN=...`) | Revoke token; dùng `read -s` để nhập token |
| File `terraform/okta/env` chứa token nhưng **không được Git bỏ qua** | Đổi tên thành `.env` (đã có trong `.gitignore`), `chmod 600` |
| Private key của OAuth service app nằm trong repo | Chuyển ra `~/.okta/`, `chmod 600` |
| Secret access key AWS lộ khi mở file CSV trong editor | Tạo access key mới, vô hiệu hóa và xóa key cũ, xóa file CSV |

**Nguyên tắc:** không gõ secret trên dòng lệnh, không mở file chứa secret trong editor đang được chia sẻ, chụp màn hình trước khi secret hiện ra, và luôn chạy `git check-ignore -v <file>` với mọi file chứa bí mật.

### 5.3. Terraform không tự hỏi được mã MFA

| | |
|---|---|
| **Triệu chứng** | `assume role with MFA enabled, but AssumeRoleTokenProvider session option not set` |
| **Nguyên nhân** | Provider AWS của Terraform không có cơ chế hỏi mã MFA |
| **Cách sửa** | Dùng AWS CLI lấy credential tạm (đã qua MFA) rồi đưa vào biến môi trường: `eval "$(aws configure export-credentials --profile target-account --format env)"` |

### 5.4. Sai thao tác khi tạo IAM policy và role

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| Lỗi cú pháp JSON | Dán **câu lệnh `sed`** vào ô JSON | Chạy lệnh trong terminal để lấy JSON, rồi dán JSON |
| `Has prohibited field Principal` | Dán **trust policy** vào màn hình tạo **permission policy** | Trust policy thuộc *Roles → Create role → Custom trust policy* |
| ARN `user/vietle>` | Còn sót dấu `>` của placeholder `<iam-user>` | Xóa ký tự thừa |

### 5.5. Lưu provisioning trước khi cấp consent

| | |
|---|---|
| **Triệu chứng** | *"Please grant consent by authenticating with Microsoft Graph"* |
| **Nguyên nhân** | Bấm **Save** trước khi **Authenticate with Microsoft Account** |
| **Cách sửa** | Authenticate bằng Global Admin, Accept quyền, rồi mới Save |

### 5.6. Import user thất bại: firstName / lastName trống

| | |
|---|---|
| **Triệu chứng** | *Confirm Imported User Assignments*: `firstName/lastName – The field cannot be left blank` |
| **Nguyên nhân** | User trên Entra chưa có First name và Last name |
| **Cách sửa** | Điền họ, tên và email trong Entra, rồi **Full import** lại. Sửa ở nguồn (Entra) chứ không vá ở Okta |

### 5.7. Không đăng nhập Okta được bằng mật khẩu Entra; không thấy nút đặt mật khẩu

| | |
|---|---|
| **Nguyên nhân** | Import qua Graph **không chép mật khẩu**. User đang **Staged** nên chưa có mật khẩu Okta, và nút đặt mật khẩu chỉ xuất hiện sau khi Activate |
| **Cách sửa** | Activate (không gửi email), rồi **Set Password & Activate** hoặc dùng mật khẩu tạm |

### 5.8. AWS chặn tạo EC2: `PendingVerification`

| | |
|---|---|
| **Triệu chứng** | `apply` tạo được 66/68 resource; `RunInstances` cho `monitor-ai-01` bị lỗi `PendingVerification` |
| **Nguyên nhân** | AWS xác minh account trước khi cho chạy thêm EC2 ở region. Không liên quan tới IAM hay code |
| **Cách sửa** | Chờ email *"Your Request For Accessing AWS Resources Has Been Validated"*, tạo plan mới (`2 to add`), rồi apply. **Không `destroy`**: resource đã tạo vẫn nằm trong state |

### 5.9. `terraform.tfvars` cũ và thiếu SSH key

| Vấn đề | Cách sửa |
|---|---|
| `terraform.tfvars` (được Git theo dõi) có IP cũ, SSH mở `0.0.0.0/0`, thiếu biến Moodle, `enable_legacy_demo` mặc định `true` | Dùng `deployment.tfvars` tạo từ file mẫu (được Git bỏ qua) |
| Không có `~/.ssh/aws-hybrid` | Tạo key ed25519 mới |

### 5.10. Script chỉ chạy trên Linux, lỗi trên macOS

| Script | Lỗi | Cách sửa |
|---|---|---|
| `automation/update-infrastructure.sh` | `chmod --reference` là tùy chọn chỉ có ở GNU | Đọc quyền file bằng `stat -c '%a'` (Linux), nếu không được thì `stat -f '%Lp'` (macOS) |
| `automation/stage-moodle-release.sh` | `stat -c`, `ss -ltn`, chạy `psql` trong Docker (máy không có Docker) | Thêm cách đọc quyền file cho macOS; hàm `port_is_listening` dùng `ss` hoặc `lsof`; thêm nhánh chạy **`psql` trực tiếp** (vẫn qua SSH tunnel, vẫn `sslmode=verify-full`). Hành vi trên Linux/WSL giữ nguyên |

### 5.11. Chạy script từ sai thư mục

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `No such file or directory` khi đứng trong `automation/` | Đường dẫn tương đối tính từ thư mục gốc repo | `cd` về thư mục gốc repo |
| Không thấy file mật khẩu admin | Lệnh tạo secret chạy trong `automation/`, nên file nằm ở `automation/terraform/.artifacts/…`, **chỗ này không được Git bỏ qua** | Chuyển về `terraform/.artifacts/moodle-secrets/`, so mã băm với bản trên server để xác nhận trùng |
| `Required command not found: ansible-playbook` | Chưa cài Ansible | `brew install ansible` |

### 5.12. Ansible unreachable: Host key verification failed

| | |
|---|---|
| **Triệu chứng** | `Connection closed by UNKNOWN port 65535`, cả 2 node `UNREACHABLE` |
| **Nguyên nhân** | SSH config do Terraform sinh ra bật `StrictHostKeyChecking yes`, còn `known_hosts` chưa có host key của EC2 mới, nên jump host `monitor-ai-01` bị từ chối |
| **Cách sửa** | **Không tắt** strict checking. Lấy fingerprint ED25519 từ `aws ec2 get-console-output` (kênh tin cậy), quét host key qua mạng, và **chỉ thêm vào `known_hosts` khi khớp**. Cả 3 host đều khớp |
| **Lưu ý** | Mỗi lần EC2 được tạo lại, host key đổi, phải làm lại bước này |

### 5.13. Gateway Timeout (504) trên trang Moodle

| | |
|---|---|
| **Triệu chứng** | Người dùng gặp 504 một lần |
| **Điều tra** | ALB và cả 2 target healthy; `/healthz.php` và `/login` trả 200; tải thấp; không có lỗi PHP. Request bị 504 **không có trong access log** |
| **Kết luận** | Không tái hiện được. Khả năng cao là xảy ra trong lúc Moodle đang cài đặt hoặc khởi động. **Chưa đóng**: nếu lặp lại, ghi lại URL và thời điểm để đối chiếu với metric của ALB |

### 5.14. Hai phát hiện không phải lỗi

- Access log có request tới `wp-login.php` và `wp-links-opml.php`: đó là **bot quét lỗ hổng WordPress** trên Internet, vô hại với Moodle.
- `m365x22866088` hiện thành `m365×22866088` trên giao diện Okta: do **font** hiển thị chữ "x" giữa các con số. Ký tự thật vẫn là "x".

---

## 6. Vì sao triển khai như vậy

### 6.1. Ba hệ thống, ba trách nhiệm tách biệt

Mỗi hệ thống chỉ làm một việc ([mục 2.1](#21-phân-chia-trách-nhiệm)). Khi có sự cố, xác định *"câu hỏi nào đang trả lời sai"* là biết ngay lỗi nằm ở đâu. Group trong Okta chỉ quyết định **được vào Moodle hay không**. Role Teacher/Student vẫn do Moodle quản lý, để IdP không vô tình cấp quyền sửa course.

### 6.2. OIDC Authorization Code thay vì SAML hay đồng bộ mật khẩu

- Moodle 5.2.3 có sẵn client OAuth 2/OIDC, không cần plugin bên thứ ba.
- Authorization Code giữ token **ở phía server**. Trình duyệt chỉ thấy `code`, mà `code` không dùng được nếu thiếu client secret.
- Moodle **không bao giờ thấy mật khẩu**. Đây là điểm chính cần chứng minh trong demo.

### 6.3. Liên kết account theo `issuer + sub`, không theo email

`sub` là định danh **bất biến** do Okta cấp. Email có thể đổi hoặc trùng giữa các hệ thống. Mapping `sub → username`, kết hợp *Login domains*, ngăn việc chiếm account bằng cách tạo một user khác có cùng email.

### 6.4. Không dùng WS-Federation cho luồng Moodle

WS-Federation chỉ phục vụ việc **đăng nhập Microsoft 365 qua Okta**, không liên quan tới Moodle. Bật *Automatic* sẽ federate **cả domain**: mọi user của domain phải đăng nhập Microsoft qua Okta, và khi trial Okta hết hạn thì cả domain mất đăng nhập. Domain `.onmicrosoft.com` của tenant demo cũng **không thể federate**.

### 6.5. Mật khẩu và MFA đặt ở Okta

Import qua Graph không chép mật khẩu. Đây là hành vi đúng: mật khẩu không nên rời khỏi IdP. Người dùng có mật khẩu Okta riêng, và MFA được ép bởi **authentication policy của Okta**. Catch-all Rule là **Denied**, tạo **hai lớp chặn**: assignment của app, và policy.

### 6.6. Tài khoản AWS theo nguyên tắc quyền tối thiểu, bắt buộc MFA

- Không dùng root hay `AdministratorAccess`. Access key lâu dài của IAM user **không tự làm gì được** nếu thiếu MFA.
- Policy chỉ chứa đúng các dịch vụ Terraform và script vận hành cần, chỉ ở `ap-southeast-1`, và không cho tạo IAM user hay role. Điều này khớp với nguyên tắc *"IAM là HUMAN_ONLY"* trong kế hoạch dự án.
- Terraform quản lý tài nguyên cloud, Ansible quản lý cấu hình host (theo AGENTS.md).

### 6.7. Cấu hình Okta bằng tay thay vì Terraform

Lúc đầu có viết module Terraform `terraform/okta` (provider `okta/okta ~> 7.0`). Nó **bị xóa** vì cả API token lẫn OAuth service app đều thất bại. **Giờ đã biết** nguyên nhân thật là sai domain ([5.1](#51-lỗi-gốc-dùng-nhầm-domain-của-org-okta)), không phải Okta. Với `OKTA_ORG_NAME="trial-3489054"`, phương án Terraform **nhiều khả năng dùng lại được**. Các resource đã tạo bằng tay có thể đưa vào Terraform bằng `terraform import`.

### 6.8. Endpoint nhập thủ công

Quyết định để trống *Service base URL* và nhập endpoint thủ công được đưa ra khi discovery trả 403. Lỗi 403 đó cũng do **sai domain**. Trên domain đúng, discovery hoạt động, nên có thể chuyển sang dùng *Service base URL* `https://trial-3489054.okta.com`. Endpoint thủ công vẫn đúng và đang hoạt động, nên **không bắt buộc** phải đổi.

### 6.9. Dùng org authorization server, không dùng `/oauth2/default`

Org authorization server (`/oauth2/v1/...`) có ở mọi org, **không cần** add-on API Access Management và không cần access policy riêng. Đủ cho đăng nhập OIDC vào ứng dụng.

### 6.10. Chạy HTTP tạm thời

Mục đích là kiểm chứng luồng OIDC sớm. Đánh đổi là `code` và cookie session đi qua kết nối **không mã hóa**, nên **chỉ dùng để test chức năng**, không dùng cho demo chính thức. Khi chuyển sang HTTPS, chỉ cần sửa redirect URI trong Okta và wwwroot của Moodle. Account đã liên kết theo `issuer + sub` vẫn giữ nguyên.

### 6.11. Giữ nguyên các cơ chế an toàn sẵn có

Không tắt `StrictHostKeyChecking`, không tắt TLS `verify-full` tới RDS, không chạy cron trên cả hai node. Mọi lỗi đều được sửa bằng cách **thỏa mãn** cơ chế an toàn, không bỏ qua nó.

---

## 7. Trạng thái hiện tại và việc còn lại

| # | Việc | Ưu tiên |
|---|---|---|
| 1 | **Nghiệm thu đầu-cuối** (Bước 11): đăng nhập `student.test`, `teacher.test`; chặn user `moodle-denied`; kiểm tra liên kết bằng `sub` | Cao |
| 2 | **HTTPS**: domain, certificate ACM ở `ap-southeast-1`, `moodle_hostname` và `moodle_certificate_arn`, stage lại Moodle, đổi redirect URI trong Okta | Cao, trước demo chính thức |
| 3 | Revoke API token và xóa OAuth service app `terraform-moodle` cũ trong Okta (đã lộ trong ảnh chụp); xóa `~/.okta/terraform-moodle.pem` | Cao |
| 4 | Phân quyền Moodle: course demo, ghi danh Teacher/Student | Trung bình |
| 5 | Kiểm thử vòng đời: disable user ở Entra → Full import → đăng nhập bị chặn | Trung bình |
| 6 | Runbook §9: monitoring và AI Agent trên `monitor-ai-01` (chưa cài Docker) | Trung bình |
| 7 | Commit các bản sửa script macOS (`update-infrastructure.sh`, `stage-moodle-release.sh`) và tài liệu này | Trung bình |
| 8 | Cân nhắc khôi phục Terraform cho Okta với domain đúng, rồi `terraform import` | Thấp |
| 9 | Hạ tầng đang tính tiền (NAT, RDS, ALB, EC2). Destroy theo runbook §14 khi không dùng | Theo lịch demo |

---

## 8. Bài học rút ra

1. **Xác minh định danh của hệ thống từ nguồn kỹ thuật** (thanh địa chỉ, output API), không từ nhãn hiển thị trên giao diện.
2. **Đối chứng trước khi kết luận "hệ thống bên ngoài bị hỏng".** Thử ngay một request mà ta biết chắc phải thành công hoặc thất bại theo cách cụ thể, ví dụ redirect sai phải trả lỗi có mô tả.
3. **Tìm root cause trước khi làm lại.** Tạo org mới sẽ không sửa được lỗi, vì lỗi nằm ở cách ta gọi tới org.
4. **Secret không bao giờ đi qua dòng lệnh, ảnh chụp hay editor.** Mọi file chứa bí mật phải qua `git check-ignore`.
5. **Chạy lệnh từ đúng thư mục.** Đường dẫn tương đối trong runbook tính từ thư mục gốc repo.
6. **Script vận hành phải chạy được trên máy của người vận hành** (macOS, bash 3.2), hoặc ghi rõ yêu cầu môi trường.

---

## 9. Phụ lục

### A. IAM policy `InfraEngineerMoodle`

Thay `ACCOUNT_ID` trước khi dùng.

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
      "Resource": "arn:aws:secretsmanager:ap-southeast-1:ACCOUNT_ID:secret:rds!*", "Action": [
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
      "Resource": "arn:aws:logs:ap-southeast-1:ACCOUNT_ID:log-group:/aws/rds/*", "Action": [
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

### B. Trust policy của role `InfraEngineer`

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::ACCOUNT_ID:user/<iam-user>" },
    "Action": "sts:AssumeRole",
    "Condition": { "Bool": { "aws:MultiFactorAuthPresent": "true" } }
  }]
}
```

### C. Lệnh kiểm tra nhanh

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
