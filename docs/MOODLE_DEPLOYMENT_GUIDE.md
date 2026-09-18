# Moodle — Infrastructure Deployment Guide

Cập nhật: 2026-09-18. Terraform infrastructure đã apply: RDS PostgreSQL là
`available`, nhưng Moodle runtime chưa được deploy nên hai ALB target vẫn
`unhealthy`. Application Moodle, Docker và monitoring stack sẽ được cấu hình
bằng Ansible và release workflow sau khi hạ tầng được provision.

## Thành phần được tạo

- Một VPC, 7 subnet, hai NAT Gateway và route tables.
- `monitor-ai-01`: public management subnet, EIP, SSH giới hạn CIDR.
- `moodle-app-a` và `moodle-app-b`: hai EC2 cố định, private app subnet khác AZ.
- ALB ở hai public subnet, target group HTTP 8080, hai EC2 targets.
- RDS PostgreSQL Single-AZ `db.t4g.micro` ở private data subnet và EFS Regional
  đã mô tả trong [database/storage guide](MOODLE_DATABASE_STORAGE.md).

Không có ASG hoặc Systems Manager execution. Terraform không cài Docker,
mount EFS hoặc deploy ứng dụng qua user-data; các việc đó thuộc Ansible/release.
UID/GID, shared sessions và cron sẽ được cấu hình lúc triển khai Moodle.

## Lựa chọn tạo mới hoặc chuyển tiếp

| Input | Default chuyển tiếp | Mẫu tạo mới |
|---|---|---|
| `enable_legacy_demo` | `true`: giữ web/core EC2 và EIP cũ | `false`: chỉ monitor + hai Moodle EC2 |
| `monitor_use_management_subnet` | `false`: giữ Public A | `true`: Management A |
| `monitor_root_volume_size` | `null`: giữ biến disk cũ | `30` GiB |
| `moodle_root_volume_size` | `30` GiB | `30` GiB |
| `moodle_allow_http` | `false`: yêu cầu certificate | `true`: HTTP smoke test |

`legacy-moves.tf` ánh xạ web/core EC2 và EIP từ địa chỉ cũ sang phần tử `[0]`.
Các moved block giúp Terraform nhận diện tài nguyên trong state khi thêm
`count`; chúng không tự khôi phục một state bị thiếu.

Nếu có state đang quản lý hệ thống cũ, đặt `enable_legacy_demo=false` sẽ đề xuất
xóa hai EC2 và EIP đó. Chuyển monitoring sang management subnet sẽ đề xuất thay
EC2 monitoring. Không dùng mẫu tạo mới cho migration đang chạy mà chưa review.
Hai legacy SG vẫn tồn tại để giữ cấu hình chuyển tiếp; không có EC2 payment
được tạo khi dùng mẫu tạo mới.

## ALB và health check

- Hai target cố định tại port 8080; health check `/healthz`, chỉ chấp nhận 200.
- Interval 15 giây, timeout 5 giây, healthy/unhealthy threshold 2.
- Deregistration delay 30 giây; không bật sticky session.
- HTTP-only phải được bật tường minh qua `moodle_allow_http=true`.
- Khi có `moodle_certificate_arn`, listener 443 dùng TLS 1.2/1.3 và port 80
  redirect sang HTTPS. Certificate phải ISSUED và ở cùng region.
- Cần khai báo `moodle_hostname` đúng SAN của certificate và tạo DNS
  ALIAS/CNAME đến ALB. Terraform hiện chưa quản lý hosted zone hoặc DNS record.

Health endpoint phải trả 200 với Host header là private-IP:8080 từ ALB,
không redirect về trang login/canonical hostname. Nguồn:
[AWS ALB troubleshooting](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-troubleshooting.html).

Ngay sau infrastructure apply, target group có hai target nhưng dự kiến
`unhealthy` vì chưa có server trên 8080. Đây không phải acceptance Moodle.
Không tạo một web server giả để làm xanh target rồi kết luận Moodle đã hoạt động.
Moodle login/data tests chỉ chạy sau khi cấu hình HTTPS và deploy ứng dụng.

## Chuẩn bị cấu hình trên máy của bạn

Chạy các lệnh dưới đây từ root repo. Cần Terraform >= 1.7 để chạy mock tests,
AWS CLI, jq, OpenSSH và Ansible cho bước kiểm tra inventory.

```bash
aws sts get-caller-identity --profile target-account
terraform -chdir=terraform init -input=false
terraform -chdir=terraform workspace show
terraform -chdir=terraform state list
```

Provider đang dùng profile `target-account`. Xác nhận account và region đúng.
Nếu có hạ tầng cũ nhưng state không có tài nguyên, kết nối đúng backend/state
trước khi plan. Nếu tạo mới, state trống là dự kiến.

Tạo file input cục bộ, giữ nguyên `terraform.tfvars` hiện có:

```bash
cp -n terraform/deployment.tfvars.example terraform/deployment.tfvars
bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars
```

Mở `terraform/deployment.tfvars` bằng editor để điền:

- `my_ip_cidr` đã được script đặt thành public IPv4 hiện tại với `/32`.
- `public_key_path`, `private_key_path`: dùng `~/.ssh/<ten-key>.pub` và
  `~/.ssh/<ten-key>`. Terraform tự mở rộng `~` theo user chạy Terraform.
- Các lựa chọn tạo mới/chuyển tiếp trong bảng trên.
- Certificate ARN/hostname nếu đã có; HTTP chỉ dùng cho smoke test ban đầu.

File này được Git ignore. Không dùng key fixture trong `terraform/tests/`.
`-var-file=deployment.tfvars` sẽ override giá trị trùng tên trong tfvars tự nạp,
bao gồm danh sách CI SSH quá rộng trước đó.

Script cập nhật IP chỉ sửa file tfvars đã chọn; nó không chạy
`terraform plan/apply`, không sửa inventory và không kết nối EC2.

Lấy AMI Amazon Linux 2023 x86_64 cho region đã chọn, sau đó điền ID vào
`ec2_ami_id` trong deployment.tfvars để pin:

```bash
aws ssm get-parameter --profile target-account --region ap-southeast-1 --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text
```

Đây là lookup AMI public, không bật Systems Manager trên EC2. Khi đổi AMI pin
trên hệ thống đang chạy, phải review replacement EC2.

## Tạo plan để kiểm tra

```bash
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform test
umask 077
mkdir -p terraform/.artifacts
terraform -chdir=terraform plan -input=false -var-file=deployment.tfvars -out=.artifacts/deployment.tfplan
terraform -chdir=terraform show -no-color .artifacts/deployment.tfplan
```

Plan file chứa giá trị input và thông tin hạ tầng; không commit hoặc đăng công
khai. Xem danh sách thao tác mà không in password:

```bash
terraform -chdir=terraform show -json .artifacts/deployment.tfplan | jq -r '.resource_changes[] | select(.mode == "managed") | [.address, (.change.actions | join(","))] | @tsv'
```

Kiểm tra số EC2 và public-IP configuration:

```bash
terraform -chdir=terraform show -json .artifacts/deployment.tfplan | jq '[.resource_changes[] | select(.type == "aws_instance" and .change.after != null) | {resource: .address, public_ip: .change.after.associate_public_ip_address, type: .change.after.instance_type}]'
```

Với môi trường tạo mới, phải có đúng monitor public và hai Moodle EC2 private.
Plan phải không có `delete` hoặc replacement. Với migration, phân tích mọi thay
đổi theo state thực tế; không suy diễn từ plan tạo mới.

Plan review đã tạo trong workspace: `terraform/.artifacts/application-review.tfplan`.
Nó dùng tfvars cục bộ cùng override tạo mới, chưa phải file deployment bạn tự chốt.
Kết quả: **68 add, 0 change, 0 destroy**, đúng 3 EC2, 3 EIP (monitor + hai NAT),
hai ALB targets, HTTP smoke-test listener, không ASG. Chọn HTTPS sẽ thêm một
listener. Không apply file review thay cho plan được tạo từ deployment.tfvars.

## Deploy sau khi bạn duyệt plan

Lệnh sau bắt đầu tạo tài nguyên AWS. Saved plan được thực thi trực tiếp, không
có bước hỏi yes/no như apply không có plan file:

```bash
terraform -chdir=terraform apply .artifacts/deployment.tfplan
```

Nếu thay input/code sau khi review, tạo và review lại plan trước khi apply.
Không dùng `-target` để né các thay đổi ngoài dự kiến.
Chờ provisioning hoàn tất; RDS PostgreSQL instance có thể mất nhiều phút để
chuyển sang `available`.

## Kiểm tra và xuất cấu hình truy cập

```bash
terraform -chdir=terraform output -json moodle_application
terraform -chdir=terraform output -json moodle_database
terraform -chdir=terraform output -json moodle_filesystem
umask 077
terraform -chdir=terraform output -raw moodle_ssh_config > terraform/.artifacts/moodle-ssh.config
terraform -chdir=terraform output -raw moodle_ansible_inventory > terraform/.artifacts/moodle-inventory.yml
ansible-inventory -i terraform/.artifacts/moodle-inventory.yml --graph
```

Terraform không ghi đè `ansible/inventory.ini`. Inventory mới gồm monitor và
moodle; không dùng playbook monitoring cũ ngay vì nó còn phụ thuộc groups web/core.
SSH config và inventory chứa IP/path của môi trường, nằm trong thư mục Git ignore.
Nếu đổi nơi checkout/control workstation, xuất lại hai file để cập nhật path.

Kiểm tra host-key fingerprint qua kênh AWS đáng tin cậy trước lần SSH đầu.
Các lệnh sau hỏi chấp nhận host key khi chưa có, thay vì bỏ kiểm tra host key:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config -o StrictHostKeyChecking=ask monitor-ai-01 true
ssh -F terraform/.artifacts/moodle-ssh.config -o StrictHostKeyChecking=ask moodle-app-a true
ssh -F terraform/.artifacts/moodle-ssh.config -o StrictHostKeyChecking=ask moodle-app-b true
ansible -i terraform/.artifacts/moodle-inventory.yml all -m ping
```

Cả hai node dùng ProxyJump qua monitor; private key nằm trên workstation,
ForwardAgent bị tắt. Ansible ping chỉ xác minh SSH/Python, không deploy phần mềm.

Xem target health sau khi apply:

```bash
MOODLE_TARGET_GROUP_ARN="$(terraform -chdir=terraform output -json moodle_application | jq -r .target_group_arn)"
aws elbv2 describe-target-health --profile target-account --region ap-southeast-1 --target-group-arn "$MOODLE_TARGET_GROUP_ARN"
```

Sau khi deploy Moodle mới yêu cầu 2/2 target healthy và kiểm tra login/DB/EFS.
Chỉ thay đổi tags Cron/inventory chưa thực sự cài cron; Ansible cần bật cron
ở A và tắt ở B trong bước cấu hình ứng dụng.

## Chuẩn bị runtime Ngày 6

Playbook chuyên biệt dưới đây chỉ cài Docker, Docker Compose plugin `v2.29.7`
(pin SHA-256) và `amazon-efs-utils`, cấu hình log rotation Docker, rồi mount EFS
bằng **TLS và access point** do Terraform xuất.
Nó tạo các thư mục `moodledata` dùng chung nhưng **không** pull image, không đọc
RDS secret, không tạo database role và không chạy Moodle. Vì vậy ALB vẫn
`unhealthy` là đúng sau bước này.

Xác minh cú pháp trước:

```bash
terraform -chdir=terraform output -json moodle_filesystem > /tmp/moodle-filesystem.vars.json
ansible-playbook \
  -i terraform/.artifacts/moodle-inventory.yml \
  -e @/tmp/moodle-filesystem.vars.json \
  ansible/playbooks/prepare-moodle-runtime.yml \
  --syntax-check
rm /tmp/moodle-filesystem.vars.json
```

Sau khi bạn duyệt bước thay đổi host, lệnh sau tự xuất lại SSH config, inventory
và EFS values theo **Terraform state hiện tại**, rồi thực thi trên cả hai Moodle
node. Script tự xác định thư mục repository từ vị trí của chính nó, không hardcode
đường dẫn home/key của bạn.

```bash
bash automation/prepare-moodle-runtime.sh
```

Không dùng `--check` để xác nhận mount thành công: check mode không thể chứng minh
EFS policy, security group và NFS/TLS thực sự cho phép mount. Khi lệnh thành công,
chạy kiểm tra read/write trên từng node:

```bash
ansible -i terraform/.artifacts/moodle-inventory.yml moodle -b -m shell -a '
  set -euo pipefail
  findmnt -no FSTYPE,TARGET /mnt/efs/moodledata
  touch /mnt/efs/moodledata/.runtime-check-{{ inventory_hostname }}
  test -f /mnt/efs/moodledata/.runtime-check-{{ inventory_hostname }}
'
```

Xóa hai file `.runtime-check-*` sau khi xác minh. Chỉ sau đó mới thêm manifest
release có image Moodle được ghim digest/tag, RDS app credential và health endpoint
`/healthz`; không dùng web server giả để qua ALB health check.

## Release package Ngày 7

Moodle source không được build trên EC2. CI build image từ Moodle `5.2.3`, commit
upstream `344232c15336c71b80f9aca8359ce0e0a9f3d116`, trên PHP 8.4 Apache base
được pin digest. Moodle 5.2 yêu cầu tối thiểu PostgreSQL 16 và PHP 8.3, nên khớp
RDS PostgreSQL 16.10 hiện tại. Image tạo `config.php` lúc container khởi động;
password chỉ được đọc từ file runtime, không nằm trong image hoặc Git.

`release/moodle/docker-compose.yml` có ba service:

- `moodle-web`: chạy ở cả A và B, port 8080, ALB health endpoint `/healthz`.
- `moodle-cron`: chỉ bật bằng Compose profile `cron` tại A.
- `moodle-install`: one-off profile `installer`, chỉ chạy sau khi database role,
  CA bundle và admin secret đã được review.

Moodle dùng PostgreSQL TLS `verify-full` và RDS CA bundle. Với EFS access point
UID/GID 1000, container cũng chạy UID/GID 1000; dữ liệu dùng chung được giới hạn
trong `/mnt/efs/moodledata`. `config.php` sử dụng database locking mặc định của
PostgreSQL, không phụ thuộc file locking NFS.

Chỉ kiểm tra build/Compose cục bộ, không kết nối AWS hoặc deploy:

```bash
bash -n moodle/docker-entrypoint.sh moodle/cron-runner.sh
docker build -t local/moodle:5.2.3 moodle
MOODLE_RUNTIME_ENV_FILE=./moodle-runtime.env.example \
docker compose --env-file release/moodle/moodle-runtime.env.example \
  -f release/moodle/docker-compose.yml config
```

Workflow `Build Moodle image` sẽ push image **theo commit SHA** vào GHCR khi thay
đổi `moodle/` được push. Trước khi EC2 pull image, package GHCR phải được đặt
public hoặc có credential pull read-only trong release procedure. Chưa push/deploy
image từ bước này và chưa dùng image tag mutable như `latest`.

## Pre-deploy Ngày 8

Chạy preflight chỉ đọc trước khi tạo runtime secret hoặc khởi động container:

```bash
bash automation/moodle-preflight.sh \
  ghcr.io/benjaminnhnn/moodle:<commit-sha-da-build-thanh-cong>
```

Script kiểm tra AWS identity, trạng thái RDS/EFS, Docker Compose và EFS mount ở
cả hai Moodle node, rồi xác minh workstation và hai EC2 có thể đọc **cùng một
image reference bất biến** từ GHCR. Nó không login GHCR, không tạo database,
không ghi secret và không deploy Moodle. Nếu image package chưa public, cần
chọn một trong hai cách trước khi chạy deployment: đặt package public hoặc
đăng nhập GHCR bằng credential chỉ có quyền `read:packages` trên cả hai node.

Runtime file phải chứa `MOODLE_DATA_ROOT=/var/moodledata`; đây là đường dẫn
trong container, khác với `MOODLE_DATA_DIR=/mnt/efs/moodledata` trên host.

### GHCR private package access

Tạo **PAT classic** với duy nhất scope `read:packages` và thời hạn ngắn. Không
đặt token vào `.env`, Ansible inventory, shell history hoặc Git. Từ workstation
đã có SSH config được Terraform xuất, chạy helper sau; token được nhập ẩn và
chỉ gửi qua stdin đến `docker login` chạy bằng root trên hai Moodle node:

```bash
bash automation/configure-moodle-ghcr-access.sh \
  ghcr.io/benjaminnhnn/moodle:<commit-sha-da-build-thanh-cong>
```

Helper kiểm tra image reference bất biến có pull được trên cả hai node. Docker
lưu credential trong `/root/.docker/config.json` với mode `0600`; không có
token trong release Compose hoặc runtime environment. Revoke PAT sau demo hoặc
khi không còn dùng môi trường này.

## Kết quả kiểm tra và phần còn lại

- Terraform fmt/validate pass; 20/20 mock tests pass, kiểm tra network, storage, compute,
  HTTP/HTTPS, AMI pin và inventory.
- OpenSSH `-G`/Ansible inventory parser kiểm tra cấu hình sinh ra bằng IP mock;
  chưa có kết nối SSH thực tế.
- Không chạy AWS apply, Ansible bootstrap hoặc application release.
- Không sửa scenario trong planning theo yêu cầu của bạn.

Bước tiếp theo sau provisioning: Ansible bootstrap Docker/EFS, chuẩn bị
Moodle image/role và mở rộng release gate hiện có để deploy lên hai node.
