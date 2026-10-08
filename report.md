# Báo cáo trạng thái bằng chứng AI AIOps

**Cập nhật:** 2026-10-07
**Phạm vi:** AI Agent cho Moodle trên AWS. Báo cáo này chỉ giữ kết quả có nguồn kiểm tra được; fixture, replay, demo offline và dữ liệu sinh ngẫu nhiên không được tính là kết quả live hay benchmark.

## Kết quả đã kiểm tra

- Bộ kiểm thử local: **753 passed, 62 warnings** trên Windows/Python 3.14. Đây là bằng chứng kiểm thử mã nguồn, không phải xác nhận hành vi trên AWS.
- Critical Ruff (`E9,F63,F7,F82`): **All checks passed**.
- Sprint 6 acceptance audit chạy ngày 2026-10-07: **incomplete, 0/225 empirical runs**. Raw dataset cho benchmark hiện không có trong thư mục kết quả.
- AWS STS: `InvalidClientTokenId`. Docker Engine: không kết nối được `dockerDesktopLinuxEngine`.

## Chưa được xác nhận

Chưa xác minh được AWS account/runtime hiện tại, chưa build được image, chưa thử Alertmanager → Agent trên runtime hiện tại, và chưa có fault drill hay recovery live được nghiệm thu trong lần kiểm tra này. Vì vậy không có kết luận RQ1/RQ2, tỷ lệ recovery, độ chính xác RCA, hoặc trạng thái “đã triển khai/đã phục hồi”.

Checkout ở nhánh `develop`, `HEAD=5307411`, có nhiều thay đổi chưa commit. Kết quả test trên áp dụng cho worktree hiện tại; chưa chứng minh hosted CI, image phát hành hay deployment tương ứng.

## Báo cáo và artifact lịch sử

- Các bảng 100%/0% và kết quả `15/15 Resolved` từng có trong báo cáo cũ dựa trên harness fixture/replay; chúng đã bị loại khỏi báo cáo này và không phải kết quả quan sát Moodle.
- Replay ngày 2026-09-25 có `mode=offline_replay` và `live_infrastructure_claim=false`; không dùng nó để khẳng định fault recovery.
- Các thư mục raw trial `terraform/.artifacts/moodle-faults/` và `moodle-sprint2-live/` hiện rỗng; các báo cáo Sprint 3–6 dẫn JSON trial trong đó nên các kết quả live/shadow-harness lịch sử chưa được kiểm chứng lại từ checkout này. Báo cáo CON-01 cũng thiếu raw drill và Agent-shadow artifacts. Không dùng các tuyên bố này làm bằng chứng live hiện tại.
- Scenario/ground-truth JSON còn trong repo chỉ là hợp đồng kiểm thử. Chúng không được tính là incident, lần chạy, hay thành tích benchmark.
- Legacy benchmark `RunResult` và summary của recorder hiện bị khóa ở `synthetic_simulation_not_empirical`; chúng không thể biểu diễn hàng `empirical_live`.

## Hướng dẫn đọc kết quả

Chỉ xem các mục trên là bằng chứng local. Để báo cáo RQ1/RQ2 cần raw records từ các trial thật, commit và image digest cố định, provenance, hashes, event trace, independent verifier probes, ổn định tối thiểu 120 giây, reset và acceptance audit hoàn chỉnh. Cho đến khi có đủ các bằng chứng đó, benchmark empirical vẫn là **0/225**.
