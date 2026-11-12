# Báo cáo đối chiếu thời gian khôi phục: DB-01 và nghiên cứu AIHS

**Ngày lập:** 11/11/2026. **Phạm vi:** một lượt diễn tập thật trên Moodle staging và một nghiên cứu đã công bố. Các số đo thuộc hai hệ thống khác nhau, không tạo thành phép thử đối chứng trực tiếp.

## 1. Nghiên cứu thực nghiệm để tham chiếu

Rui Li và cộng sự, *Automated Intelligent Healing in Cloud-Scale Data Centers*, IEEE SRDS 2021, DOI [10.1109/SRDS53918.2021.00032](https://doi.org/10.1109/SRDS53918.2021.00032), [bản PDF của nhóm tác giả](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf), đánh giá AIHS trên trung tâm dữ liệu Alibaba đang vận hành. Bài báo ghi 33,7 triệu lỗi production trong bảy tháng, 92,4% được AIHS sửa thành công. Với phép so sánh thời gian trên một nhóm cluster, thời gian mỗi server hỏng không phục vụ được dịch vụ giảm từ trung bình **20,1 giờ** (1.853 server hỏng trong sáu tháng trước AIHS) xuống **9,8 giờ** (1.547 server hỏng trong sáu tháng sau AIHS): giảm **51%**. Đây là *thời gian server không sẵn sàng*, gần với thời gian phục hồi thành phần, nhưng bài báo không gọi nó là RTO của ứng dụng và không đo RPO. Nguồn: mục IV-B, Experiment B.3, trang 8 của [bài báo](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf).

Đây là bằng chứng rằng cơ chế chọn hành động sửa chính xác hơn *có thể* rút ngắn thời gian không sẵn sàng trong một hệ thống thực tế. Không áp dụng mức giảm 51% làm kết quả hay mục tiêu đã đạt của Moodle: lỗi phần cứng/server ở quy mô Alibaba, đơn vị đo, thời gian quan sát và cơ chế phê duyệt khác với DB-01.

**Công nghệ AIHS sử dụng:** hệ thống giám sát tập trung thu log và KPI của server dưới dạng 165 thuộc tính. `Mapper` dùng **LDA (Latent Dirichlet Allocation)** để chuyển log thành vector đặc trưng; `Predictor` dùng **GBDT (Gradient Boosted Decision Trees)** để dự đoán một trong sáu hành động sửa; `Controller` dùng các bộ phân loại **GBDT** riêng cho từng hành động, học từ cả ca thành công lẫn thất bại để kiểm tra khả năng sửa được lỗi, rồi chọn hành động có chi phí phù hợp hoặc chuyển người xử lý. Hạ tầng hỗ trợ năm hành động tự động và một hành động sửa phần cứng cần người; trong phép đo production được công bố, **AIHS chỉ được phép tự thực hiện ba loại**: chờ và kiểm tra lại, khởi động lại dịch vụ quản lý, reboot. Các mô hình được huấn luyện offline và xem xét cập nhật hàng tháng. Bài báo có so sánh TF-IDF và BERT, nhưng **LDA + GBDT** là cấu hình được chọn cho triển khai. Bản prototype công khai viết bằng Python với dữ liệu tổng hợp; mã và trace production không công bố. Nguồn: mục II–IV của [bài báo AIHS](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf).

## 2. Kịch bản Moodle đã thực hiện: DB-01

**Run ID:** `DB-01-20261108T084511Z`, ngày 08/11/2026 trên AWS staging. Operator cấy quy tắc `iptables` chặn TCP/5432 từ **cả hai** Moodle node tới RDS. Alert `MoodleNodeRdsTcpFailed` xuất hiện; Agent thu bằng chứng và đề xuất hành động giới hạn `remove_scoped_db_reject`. Operator duyệt và ký hành động này; Safe Executor gỡ đúng quy tắc. Independent Verifier ghi ba mẫu probe đạt trên cả hai node, ALB, giao dịch Moodle, kết nối TLS tới DB, trạng thái alert và việc DB không mở ra Internet. Agent chấp nhận attestation và chuyển incident sang `resolved`. Kết quả artifact: `status=passed`, `baseline_after_reset=passed`, live switch đã tắt.

Nguồn gốc số liệu: [artifact cục bộ của lượt DB-01](../terraform/.artifacts/moodle-safe-executor/DB-01-20261108T084511Z.json) (Git bỏ qua, chỉ có trên máy Operator), [báo cáo kịch bản](BAO_CAO_KICH_BAN_LOI.md) mục 4.4 và 6, [báo cáo Gate thực tế](BAO_CAO_DO_LUONG_GATE_THUC_TE.md) mục DB-01. Các mốc UTC dưới đây lấy từ `stage_times` trong artifact; mốc duyệt lấy từ tệp `*-human-approval.json` cùng run ID.

| Mốc | UTC 08/11/2026 | Khoảng từ mốc trước |
| --- | --- | ---: |
| Cấy lỗi | 08:49:16 | — |
| Alert được drill quan sát (`t_detect`) | 08:50:14 | 58 giây |
| Incident/candidate sẵn sàng | 08:50:45 | 31 giây |
| Operator duyệt | 08:58:30 | 7 phút 45 giây |
| Ký xong, bắt đầu thực thi | 08:58:33 | 3 giây |
| Executor hoàn tất | 08:58:34 | 1 giây |
| Verifier hoàn tất | 09:01:48 | 3 phút 14 giây |
| Runtime ghi `resolved` | 09:01:49 | 1 giây |

**RTO đo theo quy ước của drill:** `t_resolved - t_detect = 695 giây = 11 phút 35 giây`. Nếu lấy từ lúc cấy lỗi, khoảng quan sát tới `resolved` là **753 giây = 12 phút 33 giây**; không gọi đây là RTO đã đo theo quy ước chính thức vì cấy lỗi không xác định chính xác thời điểm người dùng bắt đầu mất dịch vụ. `t_resolved` là mốc xác minh và ghi nhận khôi phục, không phải thời điểm đầu tiên ứng dụng có thể đã phục vụ lại. Thời gian từ incident/candidate đến duyệt chứa cả chuẩn bị, AI shadow và chờ Operator; artifact không tách riêng từng phần, nên không quy toàn bộ 7 phút 45 giây cho người duyệt.

**RPO:** không áp dụng cho DB-01. Không có mất dữ liệu hoặc phục hồi từ backup trong kịch bản chặn kết nối DB này; vì vậy không được ghi `RPO = 0` hay tuyên bố hệ thống đạt RPO nào từ lượt chạy. Một kịch bản xóa/hỏng dữ liệu và restore RDS/EFS riêng mới có thể đo RPO bằng chênh lệch giữa bản ghi được xác nhận cuối cùng trước lỗi và bản ghi mới nhất có mặt sau restore.

## 3. So sánh và kết luận về khả năng cải thiện

| Chỉ số | Nghiên cứu AIHS | Moodle DB-01 | Kết luận hợp lệ |
| --- | --- | --- | --- |
| Thời gian khôi phục | 20,1 → 9,8 giờ/server hỏng, giảm 51% so với chính baseline policy của AIHS | 695 giây từ alert đến `resolved`, **một** lượt staging | Hai số đo khác đối tượng và định nghĩa; không tính phần trăm “Moodle tốt hơn AIHS”. |
| RPO | Không báo cáo | Không áp dụng | Chưa có cặp số RPO để so sánh. |
| Mức tự động hóa | AIHS tự chọn và thực hiện sửa ở production | Agent đề xuất, Gate kiểm tra, Operator ký, Executor thực thi, Verifier xác nhận | Moodle giữ kiểm soát an toàn; thời gian chờ phê duyệt có thể làm RTO dài hơn hệ thống hoàn toàn tự động. |

**Hệ thống hiện tại có khả năng giảm thời gian khôi phục**, nhờ alert tự động, chẩn đoán và hành động giới hạn đã chuẩn bị sẵn, Executor chạy trong khoảng một giây ở lần DB-01 và Verifier có tiêu chí xác nhận rõ. Khoảng có tiềm năng tối ưu lớn nhất trong lượt này là từ incident/candidate đến duyệt (**7 phút 45 giây**), kế đến là phát hiện (**58 giây**). Có thể cải thiện bằng thông báo Operator rõ hơn, người trực sẵn, quy trình ký gọn hơn, và điều chỉnh alert sau khi đo false alarm; vẫn giữ duyệt người và thời gian ổn định bắt buộc của Verifier. Đây là **suy luận từ các mốc**, chưa phải mức giảm RTO đã được chứng minh. Việc giảm 120 giây ổn định chỉ để có số RTO đẹp hơn sẽ làm yếu điều kiện xác minh.

Để khẳng định “cải thiện RTO”, cần dựng lại staging (báo cáo kịch bản ghi hạ tầng AWS đã bị gỡ ngày 10/11/2026), chạy lặp lại **cùng lỗi DB-01** với hai quy trình: xử lý thủ công theo runbook và Agent/Gate/Executor/Verifier. Hai nhóm phải dùng cùng điểm bắt đầu/kết thúc, cùng mức tải, lịch trực và tiêu chí probe; báo trung vị, p95, số lần thất bại, thời gian chờ người và chất lượng khôi phục. Chỉ so sánh khi đủ lượt và không có khác biệt điều kiện quan trọng. Để đánh giá RPO, cần một diễn tập restore có dữ liệu đánh dấu và mốc ghi thành công, không suy ra từ backup retention hoặc cấu hình Single-AZ.
