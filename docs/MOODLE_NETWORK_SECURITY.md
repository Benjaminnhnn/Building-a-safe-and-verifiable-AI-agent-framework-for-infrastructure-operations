# Moodle — Terraform Network và Security Group

Cập nhật: 2026-09-16.

Trạng thái: đã triển khai code, kiểm tra offline và tạo plan review với AWS.
Chưa apply. Tài nguyên AWS chưa thay đổi.

## Phần đã triển khai

| File | Thay đổi |
|---|---|
| `terraform/network.tf` | Bổ sung 6 subnet, tổng cộng 7; 2 NAT Gateway/EIP; route và association theo AZ |
| `terraform/security.tf` | Thu hẹp ingress của các EC2 cũ, giữ nguyên resource address và tên SG |
| `terraform/moodle-security.tf` | SG riêng cho ALB, Moodle, RDS PostgreSQL, EFS và các rule ingress/egress |
| `terraform/variables.tf` | Prefix/environment của Moodle; validation CIDR quản trị/CI |
| `terraform/moodle-outputs.tf` | Output subnet/AZ/NAT/SG để kết nối RDS PostgreSQL, EFS và EC2 |
| `terraform/tests/network_security.tftest.hcl` | Kiểm tra topology, routing, SG và input bị cấm bằng mock provider |
| `.gitignore` | Bỏ qua plan/log cục bộ trong `terraform/.artifacts/` |

## Network sau khi apply

| Subnet | CIDR | Routing |
|---|---|---|
| Public A, có sẵn trong code | `10.10.1.0/24` | Internet Gateway |
| Public B | `10.10.2.0/24` | Internet Gateway |
| App A | `10.10.11.0/24` | NAT A trong Public A |
| App B | `10.10.12.0/24` | NAT B trong Public B |
| Data A | `10.10.21.0/24` | Chỉ VPC local route |
| Data B | `10.10.22.0/24` | Chỉ VPC local route |
| Management A | `10.10.31.0/24` | Internet Gateway; không tự gán public IP |

Data route table khai báo `route = []` để Terraform quản lý việc không có
route tùy chỉnh. VPC local route do AWS tạo vẫn tồn tại.

Management subnet dùng cho monitoring trong phương án tạo mới. Default chuyển
tiếp giữ monitoring ở Public A và giữ EC2 demo. Các tùy chọn tạo mới/chuyển tiếp
và moved blocks hiện nằm trong
[deployment guide](MOODLE_DEPLOYMENT_GUIDE.md). Đổi subnet EC2 sẽ buộc thay máy.

Tên tài nguyên mới dùng prefix `moodle-staging`, tag `Project=moodle-aiops`.
Các tài nguyên cũ giữ tên và project/environment hiện có. Tất cả nhận thêm
`Owner=Infrastructure` và `ExperimentScope=thesis`.

EFS mount target sẽ nằm tại Data A và Data B, cùng AZ với app client.
Vị trí này thống nhất với [thiết kế hạ tầng Moodle](MOODLE_INFRASTRUCTURE_DESIGN.md) và diagram.

## Security contract

| Nguồn | Đích | TCP port |
|---|---|---|
| Internet | ALB SG | 80, 443 |
| ALB SG | Moodle SG | 8080 |
| Monitor SG | Moodle SG | 22, 9100, 8088 |
| Moodle SG | RDS SG | 5432 |
| Monitor SG | RDS SG | 5432 |
| Moodle SG | EFS SG | 2049 |
| Moodle SG | HTTPS repositories/GHCR qua NAT | 443 |
| Admin CIDR và CIDR CI hợp lệ | Monitor SSH | 22 |
| Admin CIDR | Grafana | 3000 |

RDS PostgreSQL/EFS SG không có rule chủ động kết nối outbound; SG stateful cho phép traffic
trả lời. Quyền đọc DB của exporter vẫn phải được cấu hình bằng PostgreSQL role
khi triển khai database; SG chỉ kiểm soát đường mạng.

Các SG mới dùng standalone ingress/egress resources, các SG cũ giữ inline rules.
Không trộn hai cách quản lý trên cùng một SG, theo
[hướng dẫn AWS provider](https://registry.terraform.io/providers/hashicorp/aws/5.60.0/docs/resources/vpc_security_group_ingress_rule).

Moodle chỉ có egress TCP 443, RDS PostgreSQL 5432 và EFS 2049. Khi bootstrap, dùng repository
HTTPS và Amazon Time Sync mặc định; nếu chọn mirror HTTP hoặc NTP ngoài AWS thì
phải bổ sung rule tương ứng có chủ đích.

SG không đủ để chặn VPC DNS/link-local metadata. Bước bootstrap cần bổ sung và
kiểm chứng host/container controls cho communication contract metadata/admin.

## Ảnh hưởng lên hệ thống cũ khi apply

- Grafana chỉ nhận kết nối từ `my_ip_cidr`.
- Xóa ingress mạng vào Prometheus, Alertmanager, AI API và Redis exporter.
  Xem Prometheus/Alertmanager qua SSH tunnel hoặc trên monitoring host.
- HTTP/HTTPS/frontend staging của demo cũ chỉ còn truy cập từ IP quản trị.
- Xóa ingress Internet vào API staging; giữ luồng web/monitor đến core.
- Giữ SSH quản trị/CI hợp lệ và các luồng exporter nội bộ hiện có.
- Telegram webhook inbound trực tiếp vào AI API sẽ không còn đi qua SG; nếu đang
  dùng webhook này, cần đưa qua HTTPS ingress được thiết kế ở sprint tích hợp.

Lệnh ví dụ để mở Prometheus/Alertmanager qua SSH tunnel, với SSH alias do người
quản trị cấu hình:

```bash
ssh -N -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 monitor-ai-01
```

## Kiểm tra đã thực hiện

- Terraform CLI: 1.14.8; AWS provider: 5.100.0.
- `terraform init -backend=false -input=false`: pass.
- `terraform fmt -check -recursive`: pass.
- `terraform validate`: pass.
- `terraform test`: 5 run pass; dùng mock provider, không tạo tài nguyên AWS.
- CIDR subnet khớp thiết kế, AZ tách biệt, app route qua NAT cùng AZ.
- Data subnet không có default Internet route.
- SG cũ không còn ingress `0.0.0.0/0`; Moodle chỉ nhận SSH/exporter từ Monitor SG.
- Từ chối admin/CI IPv4 CIDR ngoài /24 đến /32, CIDR sai và Moodle production.

Mock test cần Terraform >= 1.7 theo
[tài liệu HashiCorp](https://developer.hashicorp.com/terraform/language/tests/mocking).
File `tests/fixtures/mock-public-key.txt` chỉ là dữ liệu test, không phải SSH key
hợp lệ và không được dùng cho deployment thật.

Chạy lại từ root repo:

```bash
terraform -chdir=terraform init -backend=false -input=false
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform test
```

## Kết quả plan và điểm cần giải quyết trước deployment

Profile `target-account` xác thực được bằng STS. Truy vấn read-only ở
`ap-southeast-1` không tìm thấy VPC có CIDR `10.10.0.0/16`.
Không tìm thấy file Terraform state trong workspace và code chưa khai báo remote
backend. Các thông tin này chưa đủ để kết luận hạ tầng cũ đã bị xóa; có thể nằm ở
account/region khác hoặc state nằm ngoài workspace.

Plan mặc định bị validation từ chối vì `ci_cd_ssh_cidr_blocks` trong tfvars cục bộ
chứa dải quá rộng. Không sửa tfvars của người dùng.

Để kiểm tra toàn bộ cấu hình, đã tạo plan review bằng:

```bash
mkdir -p terraform/.artifacts
terraform -chdir=terraform plan -input=false -var='ci_cd_ssh_cidr_blocks=[]' -var='moodle_allow_http=true' -out=.artifacts/review.tfplan
```

Override chỉ áp dụng cho plan review: CI SSH không được cho phép, SSH từ IP quản
trị vẫn được giữ. Kết quả tại thời điểm chỉ bổ sung network/security:
**55 add, 0 change, 0 destroy**. Khi thêm database/storage, tham khảo
[Moodle Database và Shared Storage](MOODLE_DATABASE_STORAGE.md) và lập plan mới.
Plan chứa cả VPC, 3 EC2 demo cũ, key pair và EIP cũ vì chưa có managed state;
đây không phải diff migration chỉ gồm các thay đổi network/security.

Plan binary và log nằm trong `terraform/.artifacts/`, bị Git ignore. Không dùng plan
này để deploy hạ tầng đang chạy. Trước deployment:

1. Xác định đúng account/region và state/backend của hạ tầng cần dùng.
2. Nếu là môi trường cũ, khôi phục kết nối đúng state rồi plan lại; không import
   hoặc tạo đè tài nguyên dựa trên phỏng đoán.
3. Nếu là môi trường mới, cần chốt có giữ ba EC2 demo trong cấu hình hay chuyển
   sang Moodle trước khi tạo toàn bộ stack.
4. Cập nhật CIDR CI thành các nguồn SSH cụ thể hoặc để trống; không mở lại /0.
5. Lập plan mới với input deployment thực tế và kiểm tra replacement/destroy.

## Tích hợp database, storage và application

- Dùng `aws_subnet.data_a/b` cho RDS DB subnet group và EFS mount targets.
- Gắn `aws_security_group.rds_sg` vào RDS PostgreSQL và `efs_sg` vào EFS mount targets.
- Dùng `aws_subnet.app_a/b` và `moodle_sg` khi tạo EC2 Moodle.
- Dùng Public A/B và `alb_sg` khi tạo ALB.
- Chốt state/account trước bất kỳ bước apply nào.
