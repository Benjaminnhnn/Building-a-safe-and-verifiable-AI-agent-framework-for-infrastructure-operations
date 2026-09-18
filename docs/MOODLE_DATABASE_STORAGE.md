# Moodle — Database và Shared Storage

Cập nhật: 2026-09-18.

Hạ tầng Moodle dùng Amazon RDS for PostgreSQL và Amazon EFS. Mục tiêu là một
môi trường private, chi phí thấp để chạy smoke test, fault injection và reset
trong thời gian ngắn. Kiến trúc tổng thể nằm trong
[Moodle Infrastructure Design](MOODLE_INFRASTRUCTURE_DESIGN.md).

## RDS PostgreSQL

| Thuộc tính | Giá trị |
|---|---|
| Terraform resource | `aws_db_instance.moodle` |
| Identifier | `moodle-staging-postgres` theo prefix mặc định |
| Engine | RDS PostgreSQL 16.10 |
| Instance class | `db.t4g.micro` |
| Availability | Single-AZ, Data subnet A |
| Networking | Private; DB subnet group gồm Data subnet A và B; public access tắt |
| Security group | `aws_security_group.rds_sg` |
| Storage | 20 GiB gp3, encrypted |
| Backup | Automated backup 1 ngày |
| Transport | `rds.force_ssl=1`; Moodle dùng `sslmode=verify-full` và CA bundle |
| Credentials | RDS quản lý master secret trong Secrets Manager |
| Logs | PostgreSQL log ở CloudWatch, retention 14 ngày |

`db.t4g.micro` và Single-AZ được chọn cho đợt test ngắn. Đây không phải một
database HA: không có Aurora reader endpoint, replica hay automatic database
failover. RDS subnet group vẫn có hai subnet data vì đó là yêu cầu placement
trong VPC của RDS.

RDS PostgreSQL 16.10 với `db.t4g.micro` đã được kiểm tra là orderable tại
`ap-southeast-1`; không coi kiểm tra này là bằng chứng cluster đã được tạo.

## Bảo mật và secrets

Terraform bật `manage_master_user_password=true`. Terraform chỉ xuất secret
ARN, không đọc hoặc ghi password vào state/output. Automation bootstrap lấy
secret vào bộ nhớ qua quyền IAM tối thiểu và tạo user riêng `moodleuser` cho
ứng dụng; không dùng master user trong Moodle runtime.

Moodle chỉ kết nối private endpoint của RDS qua port TCP 5432. RDS SG chỉ nhận
traffic từ Moodle SG và monitor SG (exporter read-only). Không mở port database
từ Internet.

## EFS cho moodledata

EFS là Regional và có mount target ở mỗi Data subnet. Moodle A/B mount cùng
access point `/moodledata` với TLS và UID/GID runtime không phải root. EFS giữ
uploaded files, cache/session theo cấu hình release; PostgreSQL vẫn là source
of-truth cho dữ liệu nghiệp vụ.

## Output và bootstrap

Sau apply:

```bash
terraform -chdir=terraform output -json moodle_database
terraform -chdir=terraform output -json moodle_filesystem
```

`moodle_database.endpoint` là hostname duy nhất cho Moodle và exporter.
Không có `reader_endpoint`. Bootstrap phải dùng endpoint, database name, port,
secret ARN, CA bundle và `sslmode=verify-full`; không đưa password vào Git,
command arguments hoặc log.

## Kiểm tra acceptance

1. `aws_db_instance.moodle` có trạng thái `available`, private, Single-AZ và
   dùng `db.t4g.micro`.
2. `terraform output -json moodle_database` có endpoint, secret ARN và không
   chứa password.
3. Từ Moodle EC2, kết nối TLS xác minh CA thành công; cùng lệnh từ Internet
   thất bại.
4. Moodle login và synthetic read/write thành công qua ALB.
5. EFS mount A/B dùng TLS/access point đúng, với ownership `1000:1000` và
   `0770`.

## Dọn dẹp sau test

RDS test có `deletion_protection=false`, `skip_final_snapshot=true` và
`delete_automated_backups=true` để không chặn `terraform destroy` hoặc để lại
snapshot có phí. Đây là quyết định chỉ phù hợp môi trường test ngắn hạn. Trước
khi destroy, export evidence/log cần giữ. Sau destroy, kiểm tra RDS instance,
automated backups, manual snapshots, NAT Gateway, ALB, EFS và EIP không còn.
