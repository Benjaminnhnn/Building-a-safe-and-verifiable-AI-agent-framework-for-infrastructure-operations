# Hướng dẫn phân quyền AWS IAM cho Infrastructure Engineer và AI Engineer

**Môi trường:** AWS account `<ACCOUNT_ID>`, region `ap-southeast-1` (Singapore), staging Moodle
**Người thực hiện:** quản trị viên AWS (admin). Các role trong tài liệu này **không** có quyền quản lý IAM
**Tài liệu liên quan:** [Triển khai Moodle–Entra–Okta](MOODLE_ENTRA_OKTA_OIDC_IMPLEMENTATION.md) · [Moodle Operations Runbook](MOODLE_OPERATIONS_RUNBOOK.md) · [AI Engineer test runbook](AI_ENGINEER_MOODLE_TEST_RUNBOOK.md)

Thay `<ACCOUNT_ID>`, `<infra-user>`, `<ai-user>` và `<mfa-device>` bằng giá trị thật khi thực hiện. **Không** commit giá trị thật của access key, mật khẩu hay link kích hoạt vào repo.

---

## Mục lục

1. [Mô hình phân quyền](#1-mô-hình-phân-quyền)
2. [Vì sao thiết kế như vậy](#2-vì-sao-thiết-kế-như-vậy)
3. [Phần chung: policy `InfraEngineerMoodle`](#3-phần-chung-policy-infraengineermoodle)
4. [Infrastructure Engineer: CLI và Terraform](#4-infrastructure-engineer-cli-và-terraform)
5. [AI Engineer: Console có username/mật khẩu](#5-ai-engineer-console-có-usernamemật-khẩu)
6. [Kiểm chứng quyền](#6-kiểm-chứng-quyền)
7. [Dùng với Terraform và các script automation](#7-dùng-với-terraform-và-các-script-automation)
8. [Vận hành: xoay vòng key, thu hồi quyền](#8-vận-hành-xoay-vòng-key-thu-hồi-quyền)
9. [Lỗi đã gặp và cách xử lý](#9-lỗi-đã-gặp-và-cách-xử-lý)
10. [Chẩn đoán khi bị "Access Denied"](#10-chẩn-đoán-khi-bị-access-denied)
11. [Checklist nhanh](#11-checklist-nhanh)

---

## 1. Mô hình phân quyền

```
                          ┌──────────────────────────────────────────────┐
IAM user <infra-user> ──► │ Role InfraEngineer  (MFA, phiên ≤ 4 giờ)       │──┐
  (access key + MFA)      └──────────────────────────────────────────────┘  │   Managed policy
                                                                             ├─► InfraEngineerMoodle
IAM user <ai-user>    ──► ┌──────────────────────────────────────────────┐  │   (chỉ ap-southeast-1,
  (mật khẩu Console + MFA)│ Role AIEngineer     (MFA, phiên ≤ 4 giờ)       │──┘    không quản lý IAM)
                          └──────────────────────────────────────────────┘
```

| Thành phần | Có quyền gì | Ghi chú |
|---|---|---|
| IAM **user** | Gần như không có quyền gì: chỉ assume **đúng một role**, và (với AI Engineer) tự đổi mật khẩu và tự quản lý MFA | Lộ mật khẩu hay key cũng không làm được gì nếu thiếu MFA |
| IAM **role** | Quyền thật, lấy từ policy `InfraEngineerMoodle` | Credential tạm, tự hết hạn |
| Policy **`InfraEngineerMoodle`** | EC2/VPC, ELB, RDS, EFS, log của RDS, đọc AMI, secret RDS do AWS quản lý | **Chặn mọi region khác Singapore**, không tạo được IAM user/role/policy |

---

## 2. Vì sao thiết kế như vậy

| Quyết định | Lý do |
|---|---|
| **Không dùng root hay `AdministratorAccess`** | Một lỗi thao tác hay một key bị lộ không được phép phá cả account |
| **Quyền nằm ở role, không nằm ở user** | Credential của role là **tạm thời** (≤ 4 giờ) và **bắt buộc MFA**. Credential lâu dài của user (mật khẩu, access key) một mình không đủ để làm gì |
| **Mỗi vai trò một role riêng** (`InfraEngineer`, `AIEngineer`), dùng chung một policy | Quyền giống nhau, nhưng CloudTrail ghi rõ **ai** làm (`assumed-role/AIEngineer/...`), và thu hồi quyền một người không ảnh hưởng người kia |
| **Chỉ cho phép `ap-southeast-1`** | Giới hạn phạm vi thiệt hại, và tránh tài nguyên "mồ côi" ở region khác, vốn khó phát hiện và vẫn tính tiền |
| **Không có quyền quản lý IAM** | Đúng nguyên tắc dự án: thay đổi IAM là **HUMAN_ONLY**. Kể cả AI agent hay người dùng role này cũng không tự nâng quyền được |
| **KMS chỉ dùng được "qua" RDS, Secrets Manager, EFS** (`kms:ViaService`) | Mã hóa vẫn hoạt động, nhưng không ai dùng được khóa KMS trực tiếp cho việc khác |
| **Secrets Manager chỉ với secret `rds!*`** | Chỉ đọc được secret master do RDS tự quản lý, không đọc được secret khác trong account |

⚠️ **Rủi ro cần chấp nhận khi cấp quyền giống Infra Engineer:** policy cho phép **terminate EC2, xóa RDS, xóa EFS** trong staging. Nếu AI Engineer chỉ cần **quan sát** để test agent, nên tạo một policy **chỉ đọc** và gắn vào role `AIEngineer` thay cho `InfraEngineerMoodle`. Các bước khác giữ nguyên.

---

## 3. Phần chung: policy `InfraEngineerMoodle`

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
        "elasticfilesystem.amazonaws.com", "backup.amazonaws.com" ] } } }
  ]
}
```

**Ghi chú:**
- ARN của SSM là `arn:aws:ssm:ap-southeast-1::parameter/...` (**không có Account ID**). Đúng như vậy, vì parameter AMI là tài nguyên công khai của AWS.
- Ô JSON chỉ nhận **nội dung policy**. Không dán câu lệnh shell vào đây. Policy này **không có** trường `Principal`.
- Policy được suy ra từ các resource Terraform đang dùng. Nếu Terraform báo `not authorized to perform: <action>`, chỉ bổ sung **đúng action đó** (xem mục 10).

---

## 4. Infrastructure Engineer: CLI và Terraform

Infra Engineer làm việc chủ yếu bằng **AWS CLI và Terraform**, nên dùng **access key + MFA**.

### 4.1. Tạo IAM user

**IAM → Users → Create user**
- **User name:** `<infra-user>`
- **Không** tick quyền truy cập Console (trừ khi cần)
- **Không** gán policy, **không** thêm vào group

### 4.2. Gán MFA

User → **Security credentials → Assign MFA device → Authenticator app**. Quét QR, nhập 2 mã liên tiếp, rồi **ghi lại MFA ARN**: `arn:aws:iam::<ACCOUNT_ID>:mfa/<mfa-device>`.

### 4.3. Tạo role `InfraEngineer`

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

### 4.4. Cho user quyền assume role

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

### 4.5. Access key và AWS CLI

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

## 5. AI Engineer: Console có username/mật khẩu

AI Engineer dùng **AWS Console** (đăng nhập bằng username và mật khẩu). Quyền **giống Infra Engineer**, qua một role **riêng**.

Thứ tự bắt buộc: **tạo user (5.1) trước, rồi mới tạo role (5.3)**. Trust policy tham chiếu tới user nào thì user đó phải tồn tại sẵn.

### 5.1. Tạo IAM user có quyền đăng nhập Console

**IAM → Users → Create user**
1. **User name:** `<ai-user>` (ví dụ `ai-engineer-01`).
2. Tick **Provide user access to the AWS Management Console** → **I want to create an IAM user**.
3. **Console password:** *Autogenerated password*.
4. Tick **Users must create a new password at next sign-in**. AWS tự gắn policy `IAMUserChangePassword`.
5. **Permissions:** không gán policy nào khác, không thêm vào group.
6. **Create user**. Lưu **Console sign-in URL**, **username** và **mật khẩu tạm**. Mật khẩu chỉ hiện một lần.

### 5.2. Policy tự phục vụ và quyền assume role

User `<ai-user>` → **Permissions → Add permissions → Create inline policy → JSON**, đặt tên `AIEngineerSelfServiceAndAssume`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ViewOwnIdentity", "Effect": "Allow",
      "Action": ["iam:GetUser", "iam:ListMFADevices", "iam:ListVirtualMFADevices", "iam:GetAccountPasswordPolicy"],
      "Resource": "*" },
    { "Sid": "ManageOwnPasswordAndMFA", "Effect": "Allow",
      "Action": ["iam:ChangePassword", "iam:CreateVirtualMFADevice", "iam:EnableMFADevice",
                 "iam:ResyncMFADevice", "iam:DeactivateMFADevice", "iam:DeleteVirtualMFADevice"],
      "Resource": ["arn:aws:iam::<ACCOUNT_ID>:user/${aws:username}",
                   "arn:aws:iam::<ACCOUNT_ID>:mfa/*"] },
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
| `ManageOwnPasswordAndMFA` | `${aws:username}` thay bằng **chính tên người đang đăng nhập**, nên user **chỉ quản lý được tài khoản của mình** |
| `ManageOwnAccessKeysWithMFA` | Tự tạo, xoay vòng, xóa **access key của chính mình** (cho CLI/Terraform), chỉ khi phiên có MFA |
| `AssumeAIEngineerWithMFA` | Chỉ được vào role `AIEngineer`, và chỉ khi **phiên đăng nhập có MFA** |

### 5.3. Tạo role `AIEngineer`

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

### 5.4. Bàn giao thông tin đăng nhập

Gửi **hai mảnh qua hai kênh khác nhau**, không bao giờ gửi chung, và không gửi qua chat công cụ AI:

| Kênh 1 (ví dụ email công việc) | Kênh 2 (ví dụ tin nhắn trực tiếp) |
|---|---|
| Link: `https://<ACCOUNT_ID>.signin.aws.amazon.com/console`, kèm username `<ai-user>` | Mật khẩu tạm |

Lý do: lộ một kênh thì kẻ xấu vẫn thiếu mảnh còn lại. Mật khẩu tạm cũng bị buộc đổi ở lần đăng nhập đầu.

### 5.5. AI Engineer đăng nhập lần đầu

1. Mở link, đăng nhập bằng username và mật khẩu tạm, rồi **đặt mật khẩu mới**.
2. Góc phải → **Security credentials → Assign MFA device → Authenticator app**, quét QR.
3. **Đăng xuất, đăng nhập lại có MFA.** Bắt buộc: điều kiện `aws:MultiFactorAuthPresent` chỉ đúng khi phiên đăng nhập đã qua MFA.
4. Góc phải → **Switch role**:
   - **Account:** `<ACCOUNT_ID>`
   - **IAM role name:** `AIEngineer`
   - **Display name:** `AIEngineer-Moodle`
5. Đổi region sang **Asia Pacific (Singapore) ap-southeast-1**.

### 5.6. (Tùy chọn) AI Engineer dùng AWS CLI hoặc Terraform

1. AI Engineer đăng nhập Console **bằng user, có MFA, chưa Switch role** (nếu đang ở role thì **Switch back**: role không có quyền IAM). Vào **Security credentials → Access keys → Create access key** (use case CLI) và nhập ngay vào `aws configure`; không lưu CSV, không chụp màn hình. Quyền này đến từ statement `ManageOwnAccessKeysWithMFA` ở mục 5.2.
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

## 6. Kiểm chứng quyền

**Đoán trước kết quả**, rồi mới thử. Lần thử nào có kết quả khác với dự đoán cho thấy policy, hoặc cách mình hiểu policy, đang sai.

### 6.1. Infra Engineer (CLI)

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

### 6.2. AI Engineer (Console, sau khi Switch role)

| Thử | Kỳ vọng | Vì sao |
|---|---|---|
| EC2 → Instances (Singapore) | Thấy `monitor-ai-01`, `moodle-app-a`, `moodle-app-b` | `ec2:Describe*` được cho phép |
| Đổi region sang N. Virginia | Báo không có quyền | `DenyOutsideSingapore` |
| IAM → Users → Create user | Access denied | Policy không cho quản lý IAM |
| S3 → Buckets | Access denied | S3 không có trong policy |
| **Trước khi Switch role**, mở EC2 | **Access denied** | Bản thân user không có quyền gì. Quyền chỉ nằm ở role, và vào role thì cần MFA |

Dòng cuối là bài kiểm tra quan trọng nhất của mô hình.

---

## 7. Dùng với Terraform và các script automation

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

## 8. Vận hành: xoay vòng key, thu hồi quyền

### 8.1. Xoay vòng access key (ví dụ khi key bị lộ)

Làm theo thứ tự này để không bị gián đoạn:

1. **Create access key** mới. Một user được có tối đa 2 key cùng lúc.
2. `aws configure --profile <base-profile>`, nhập key mới (gõ trực tiếp vào terminal).
3. `rm -rf ~/.aws/cli/cache && aws sts get-caller-identity --profile target-account` để chắc chắn đang dùng key mới.
4. Key cũ: **Deactivate**, rồi **Delete**.
5. Xóa mọi file CSV chứa key: `rm ~/Downloads/*accessKeys*.csv`.

### 8.2. Thu hồi quyền

| Mục tiêu | Cách làm |
|---|---|
| Tạm khóa một người | User → **Security credentials**: Disable console access, Deactivate access key |
| Thu hồi vĩnh viễn | Xóa user. Role có thể giữ lại cho người thay thế (sửa `Principal` trong trust policy) |
| Thu hẹp quyền AI Engineer | Gỡ `InfraEngineerMoodle` khỏi role `AIEngineer`, gắn policy chỉ đọc. Infra Engineer không bị ảnh hưởng |
| Thu hồi khẩn cấp các phiên đang mở của role | Role → **Revoke active sessions**. Phiên cũ mất hiệu lực, user phải assume lại |

---

## 9. Lỗi đã gặp và cách xử lý

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| Policy editor báo *"Fix all syntax errors"* | Dán **câu lệnh `sed`** vào ô JSON | Chạy lệnh trong terminal để lấy JSON, rồi dán JSON |
| *"Has prohibited field Principal"* | Dán **trust policy** vào màn hình tạo **permission policy** | Trust policy thuộc *Roles → Create role → Custom trust policy* |
| ARN `user/vietle>` | Còn sót dấu `>` của placeholder | Xóa ký tự thừa. Kiểm tra mọi ARN không còn `<` hay `>` |
| Tạo role không thấy chỗ tick policy | Đang ở trang trust policy | Ô tick nằm ở trang **Add permissions** ngay sau đó |
| Không tìm thấy policy khi tick | Đang lọc *AWS managed* | Lọc **Customer managed** |
| `AssumeRoleTokenProvider session option not set` | Terraform không hỏi được MFA | Dùng `aws configure export-credentials` (mục 7) |
| Secret access key xuất hiện trong chat/editor | Mở file CSV trong VS Code | Xoay vòng key theo mục 8.1 |

---

## 10. Chẩn đoán khi bị "Access Denied"

Áp dụng phương pháp chung (xem [MOODLE_SSO_INCIDENT_SCENARIOS.md, mục 1](MOODLE_SSO_INCIDENT_SCENARIOS.md#1-phương-pháp-chẩn-đoán-dùng-chung)):

1. **Ai đang gọi?** `aws sts get-caller-identity`. Nếu kết quả là `user/...` thay vì `assumed-role/...`, bạn **chưa vào role**: thiếu MFA hoặc chưa Switch role.
2. **Ở region nào?** Mọi region khác `ap-southeast-1` đều bị chặn **có chủ đích**.
3. **Thông báo lỗi nói gì?**
   - Có chữ *"explicit deny"* → bị chặn bởi statement `Deny`, thường là chặn region. **Đúng thiết kế.**
   - *"no identity-based policy allows the X action"* → action X **chưa có** trong policy.
4. **Action đó có nên được phép không?** Đối chiếu với thứ Terraform hoặc script thực sự cần. **Chỉ thêm đúng action đó**, không thêm wildcard rộng hơn, rồi cập nhật cả mục 3 của tài liệu này.
5. **Kiểm chứng lại** bằng đúng lệnh đã bị từ chối.

Muốn thử policy mà không cần chạy thật: **IAM → Policy simulator**, chọn role và action.

---

## 11. Checklist nhanh

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
