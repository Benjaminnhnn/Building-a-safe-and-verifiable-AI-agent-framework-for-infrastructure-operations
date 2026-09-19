# Báo cáo hoàn thiện Moodle operational baseline

## 1. Phạm vi và nguyên tắc lưu bằng chứng

Báo cáo này ghi lại các bước sửa Apache Moodle Router, điều chỉnh chu kỳ cron,
đưa Terraform về trạng thái hội tụ, diễn tập khôi phục RDS và cập nhật baseline
cho Prometheus, Grafana và RDS snapshot.

Các bằng chứng runtime được lưu tại
`terraform/.artifacts/moodle-baseline/` và bị loại khỏi Git. Thư mục này không
lưu mật khẩu database, tài khoản Moodle, GHCR hay Grafana. Terraform state cũng
không được đưa vào Git.

## 2. Sửa Apache Moodle Router và kiểm tra deep-link

### Hiện tượng ban đầu

Apache dùng `/var/www/html/public` làm `DocumentRoot`, nhưng chưa có fallback
cho các virtual route của Moodle. URL PHP trực tiếp hoạt động, trong khi một số
deep-link trả về trang 404 mặc định của Apache.

### Các bước thực hiện

1. Thêm `DirectoryIndex index.php` và `FallbackResource /r.php` cho thư mục
   `/var/www/html/public` trong `moodle/apache-moodle-router.conf`.
2. Copy file vào `/etc/apache2/conf-available/moodle-router.conf`, chạy
   `a2enconf moodle-router` và `apache2ctl -t` trong quá trình build image.
3. Chỉ ghi `$CFG->routerconfigured = true` trong image đã chứa cấu hình router.
4. Build image, chờ GitHub Actions hoàn tất, rồi rolling deploy node B trước,
   node A sau. Chỉ chuyển node tiếp theo khi target trước đã `healthy` trên ALB.
5. Kiểm tra health endpoint, router endpoint và deep-link qua ALB.

### Lỗi/nhầm lẫn khi kiểm tra và cách khắc phục

- Kiểm tra `/fakefile.php` vẫn trả 404. Đây là false negative vì Apache PHP
  handler bắt URL có đuôi `.php` không tồn tại trước `FallbackResource`. Đổi
  sang route Moodle thực tế không có đuôi PHP.
- Giá trị `$CFG->routerconfigured` sau khi nạp `config.php` được chuẩn hóa thành
  chuỗi `"1"`; phép so sánh nghiêm ngặt `=== true` vì vậy trả false. Đổi kiểm
  tra thành xác nhận biến tồn tại và có giá trị truthy.
- Chạy toàn bộ `admin/cli/checks.php` vượt quá timeout 120 giây. Các tiến trình
  chẩn đoán còn lại được dừng theo PID; việc nghiệm thu router chuyển sang các
  phép kiểm tra đích danh bên dưới, không dùng kết quả timeout để kết luận.

### Kết quả

- Docker build thành công; `apache2ctl -t` trả `Syntax OK`.
- Image đang chạy trên cả hai node:
  `ghcr.io/benjaminnhnn/moodle:1217aa080940cf714cbe13a8c3de87d98a399a63`.
- `/healthz.php` qua ALB trả HTTP 200.
- `/core/check/controller/test` qua ALB trả HTTP 200.
- `/course/1/manage` được thử bốn lần qua ALB; cả bốn lần trả HTTP 303 và
  chuyển tới `/login/index.php`. Điều này xác nhận route đã tới Moodle và áp
  dụng kiểm soát đăng nhập, thay vì rơi vào Apache 404.
- Cả hai target ALB ở trạng thái `healthy`.

## 3. Sửa Moodle cron runner để chu kỳ bắt đầu không quá 60 giây

### Hiện tượng ban đầu

Khoảng cách giữa hai lần cron bắt đầu xấp xỉ bốn phút. Cấu hình Moodle
`cron_keepalive` là 180 giây, sau đó wrapper trong container còn sleep cố định
thêm 60 giây.

### Các bước khắc phục

1. Gọi `cron.php --keep-alive=0` để mỗi vòng wrapper chỉ chạy một lượt cron.
2. Ghi nhận thời điểm bắt đầu/kết thúc và chỉ sleep phần thời gian còn lại của
   chu kỳ 60 giây.
3. Nếu một lượt chạy đã mất từ 60 giây trở lên thì bắt đầu lượt kế tiếp ngay,
   đồng thời ghi warning thay vì sleep thêm.
4. Deploy container cron trên node A cùng đúng image Moodle đã nghiệm thu.

### Kết quả

Sáu mốc bắt đầu liên tiếp trong log là `16:35:22`, `16:36:22`, `16:37:22`,
`16:38:22`, `16:39:22`, `16:40:22`; chênh lệch đúng 60 giây. Container cron
đang chạy và restart count bằng 0.

## 4. Đưa Terraform về trạng thái `No changes`

### Vấn đề phát hiện

- `rds.force_ssl` là tham số tĩnh nhưng đang dùng `apply_method = "immediate"`.
- `deployment.tfvars` chưa đạt định dạng chuẩn của `terraform fmt`.
- Hai security group cũ, hiện không gắn ENI, còn chứa admin CIDR đã lỗi thời.

### Các bước thực hiện

1. Đổi `apply_method` của `rds.force_ssl` thành `pending-reboot`.
2. Chạy `terraform fmt terraform/deployment.tfvars` và
   `terraform fmt -check -recursive terraform`.
3. Chạy `terraform validate` và `terraform test`.
4. Tạo saved plan. Sau khi xác nhận hai security group không gắn ENI, apply
   hai thay đổi CIDR tại chỗ: `0 added, 2 changed, 0 destroyed`.
5. Chạy lại plan bằng cùng `deployment.tfvars`.

### Kết quả

- `terraform validate`: passed.
- `terraform test`: 19 passed, 0 failed.
- Plan cuối: `No changes. Your infrastructure matches the configuration.`

### Lỗi khi lưu log plan và cách khắc phục

Lệnh kiểm tra cuối ban đầu redirect output vào
`.artifacts/final-no-change-plan.txt`. Tùy chọn `-chdir=terraform` chỉ thay đổi
working directory của tiến trình Terraform, không thay đổi working directory
của shell đang xử lý dấu `>`, nên shell báo `No such file or directory`. Đích
redirect được sửa thành
`terraform/.artifacts/final-no-change-plan.txt`; lệnh chạy lại thành công với
exit code 0 và kết quả `No changes`.

## 5. RDS restore drill từ snapshot

### Các bước thực hiện

Script `automation/moodle-rds-restore-drill.sh` thực hiện tuần tự:

1. Chọn manual snapshot mới nhất nếu không truyền snapshot ID.
2. Restore một RDS `db.t4g.micro` tạm, private, dùng subnet group, parameter
   group và security group hiện có.
3. Chờ instance đạt `available`.
4. Từ Moodle node A, kết nối bằng role `moodle_app` với TLS `verify-full` và chỉ
   chạy `SELECT current_database(), current_user`.
5. Xóa instance tạm, chờ xóa hoàn tất rồi mới ghi JSON kết quả.

Lệnh chạy:

```bash
bash automation/moodle-rds-restore-drill.sh
```

### Lỗi lần đầu và cách khắc phục

Lần đầu, RDS tạm được restore thành công nhưng lệnh PHP lồng qua SSH/shell bị
`unexpected EOF while looking for matching quote`. Cleanup trap vẫn xóa RDS
tạm. Phần kiểm tra được sửa để gửi PHP source qua standard input tới
`docker exec -i`; cách này loại bỏ nhiều lớp quote và không đưa password vào
command arguments hoặc log.

### Kết quả lần chạy lại

- Snapshot nguồn: `moodle-staging-baseline-20260919-145355`.
- RDS tạm: `moodle-staging-restore-drill-20260919152150`.
- Database/user xác nhận: `moodle` / `moodle_app`.
- TLS: `verify-full`; kết quả kết nối: `passed`.
- Cleanup: `deleted`; truy vấn AWS sau drill không còn instance có tên chứa
  `restore-drill`.
- Bằng chứng: `terraform/.artifacts/moodle-baseline/rds-restore-drill.json`.

## 6. Cập nhật baseline Prometheus, Grafana và snapshot

Chạy:

```bash
bash automation/capture-moodle-baseline.sh
```

Script thu thập ALB target health, trạng thái RDS và manual snapshot, EFS và
backup policy, runtime của hai Moodle node, public endpoint, Prometheus targets
và alerts, cùng Grafana health/datasource/dashboard.

### Kết quả baseline lúc `2026-09-19T15:35:30Z`

- ALB: 2/2 target `healthy`.
- Prometheus: 8/8 target `up`, 0 target `down`, 0 active alert.
- Grafana 13.2.0: database `ok`; Prometheus datasource `OK`.
- Dashboard `Moodle Infrastructure Overview`: 13 panel, folder `Moodle`.
- Manual snapshot: `moodle-staging-baseline-20260919-145355`, `available`,
  encrypted.
- Public Moodle health và login endpoint đều trả HTTP 200.

## 7. Kết luận nghiệm thu

Tất cả nội dung yêu cầu đã hoàn thành:

- Apache Router xử lý deep-link thật qua ALB, không còn rơi vào Apache 404.
- Cron bắt đầu theo chu kỳ 60 giây.
- Terraform validate/test thành công và plan cuối là `No changes`.
- Restore drill xác nhận snapshot có thể phục hồi, kết nối TLS được và tài
  nguyên tạm đã bị xóa.
- Baseline Prometheus, Grafana, ALB, RDS snapshot và runtime đã được cập nhật
  dưới `.artifacts` mà không ghi secret vào Git.
