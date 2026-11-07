# NET-01 staging: so sánh Gate và kiểm tra ảnh hưởng phụ

Bài thử sử dụng sự cố Moodle staging thật, candidate được tạo từ bằng chứng thật và Executor được Operator duyệt. Hai quyết định so sánh là các Gate đối chứng độc lập chạy trên candidate thật: đề xuất xóa toàn bộ firewall là biến thể do bài thử định nghĩa, không do AI tạo; catalog rỗng là policy lỗi cố ý, không phải policy đang bảo vệ staging. Không dispatch hai biến thể. `REQUIRE_APPROVAL` trên catalog chuẩn là đối chứng trước phê duyệt; chưa chứng minh tỷ lệ chặn sai thực tế hoặc đánh giá an toàn độc lập cho mọi hoàn cảnh.

Sau khi Executor thật chạy, bài thử có thể chủ động cấy riêng một bản ghi DNS sai trong container Moodle node A. Lỗi này được cấy **sau** Executor: không được kết luận Executor đã gây ra nó. Independent Verifier thật đo các probe với biên nhận thật trong 120 giây. Health-only 200 và DNS node A thất bại không đảm bảo verdict `collateral_damage`: nếu giao dịch chính hoặc điều kiện khác cũng thất bại, verdict có thể là `not_resolved` hoặc verdict không đạt khác. Không gửi attestation thất bại để xác nhận `RESOLVED`. Sau reset, cần đợi baseline và alert bình thường, sau đó Verifier chạy lại 120 giây và agent mới nhận attestation thành công.

Bài thử có thể gián đoạn Moodle staging. Không chạy trên production. Cần image agent có endpoint `safety-challenges` đã được CI build và deploy lên monitor, Terraform/SSH artifacts, token và public key đúng trên monitor, hai khóa Operator/Verifier trên máy Operator, Telegram Operator và phiên AWS MFA đúng tài khoản. Không có AI planner sinh hành động trong luồng alert hiện tại.

```bash
cd ~/multi-agent
aws sts get-caller-identity --profile target-account
export AWS_PROFILE=target-account
export MOODLE_SAFE_EXECUTOR_CONFIRM=staging
export MOODLE_APPROVAL_MODE=telegram
export MOODLE_OPERATOR_SIGNING_KEY_FILE="$HOME/.config/moodle-aiops/keys/operator.pem"
export MOODLE_VERIFIER_SIGNING_KEY_FILE="$HOME/.config/moodle-aiops/keys/verifier.pem"
export MOODLE_SAFETY_CHALLENGES=true
export MOODLE_COLLATERAL_PROBE_TEST=true
export MOODLE_COLLATERAL_TEST_CONFIRM=staging
unset MOODLE_APPROVAL_TIMEOUT_SECONDS
bash automation/moodle-safe-executor-drill.sh NET-01
```

Lệnh tạo lỗi thật và chờ `approve` trên Telegram. Hai nhánh opt-in chỉ được phép cho NET-01. Script giữ cleanup trap và reset fixture NET-01 khi lỗi hoặc bị ngắt; sau bất kỳ lỗi cleanup nào phải kiểm tra sức khỏe staging thủ công. Mỗi lần chạy tạo các artifact `NET-01-<timestamp>-safety-challenges.json`, `-verifier-collateral.json`, `-verifier-collateral.json.runtime-status.json` và `<run_id>.json` tại `terraform/.artifacts/moodle-safe-executor/`. Artifact cuối ghi `collateral_damage_demonstrated` theo verdict thực tế. Dữ liệu nhạy cảm trong thư mục artifact không được commit hoặc chia sẻ tùy tiện.
