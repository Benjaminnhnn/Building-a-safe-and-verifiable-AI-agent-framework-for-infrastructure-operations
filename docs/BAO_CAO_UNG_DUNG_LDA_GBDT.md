# Phân tích ứng dụng LDA và GBDT cho Moodle AIOps

Ngày 11/11/2026. Phạm vi: thiết kế nghiên cứu và hướng dẫn huấn luyện; chưa triển khai mô hình, chưa huấn luyện và chưa chứng minh cải thiện RTO.

## Mục tiêu và giới hạn

Nghiên cứu AIHS của Alibaba dùng LDA chuyển log thành đặc trưng và GBDT dự đoán, đánh giá hành động sửa. Trong phép đo trên một nhóm cluster, thời gian server không phục vụ giảm từ trung bình 20,1 xuống 9,8 giờ so với giai đoạn trước triển khai; đây là số đo của họ, không phải RTO Moodle. [Bài báo gốc](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf).

Mục tiêu nghiên cứu đề xuất: đo liệu gợi ý ML giúp Operator chọn hành động nhanh hơn và giảm thời gian từ phát hiện đến xác nhận khôi phục so với pipeline Moodle hiện tại, đồng thời giữ chất lượng khôi phục. Không thể tuyên bố vượt Alibaba từ việc so sánh 695 giây với 9,8 giờ: hệ thống, lỗi, định nghĩa và mẫu khác nhau. Có thể đặt mục tiêu giảm tương đối trên baseline Moodle; nếu mức giảm ước lượng vượt 51%, báo đó là kết quả Moodle trong điều kiện thí nghiệm, không chứng minh Moodle vượt Alibaba trên cùng benchmark.

## Vị trí ứng dụng

| Thành phần | Ứng dụng ML đề xuất | Quyền quyết định |
| --- | --- | --- |
| Trước Safety Gate | LDA tạo đặc trưng log đã lọc; GBDT xếp hạng action ID trong catalog hoặc đề nghị chuyển người. | Gợi ý độc lập; Gate vẫn kiểm tra evidence, catalog, phạm vi và chữ ký. |
| Executor | GBDT có thể dự đoán nguy cơ thất bại để Operator xem hoặc tăng giám sát. | Executor chỉ thực hiện action đã ký đúng phạm vi; dự đoán không tạo lệnh, đổi tham số hoặc cấp quyền. |
| Verifier | GBDT có thể cảnh báo mẫu bất thường, gợi ý điều tra thêm. | Chỉ Independent Verifier quyết định attestation; probe allowed/forbidden/related và cửa sổ ổn định vẫn bắt buộc. |

Luồng đề xuất: evidence mới → đặc trưng theo thời điểm → ML shadow → so sánh với planner → Safety Gate → Operator ký → Executor → Independent Verifier. Nhánh [ai_shadow_proposal.py](../agent_src/core/moodle/ai_shadow_proposal.py) là ví dụ opt-in chỉ đề xuất; hiện nó dùng Gemini, không phải LDA/GBDT. Không đưa model vào alert path nếu chưa có thiết kế được review. Unknown, thiếu evidence hoặc model lỗi phải chuyển người; không suy ra an toàn từ điểm xác suất.

## Dữ liệu và nhãn

Mỗi hàng là một incident và một hành động thực sự được cân nhắc/tiến hành, gắn `incident_id`, `run_id`, loại môi trường, phiên bản code/policy/model và timestamp. Đặc trưng chỉ dùng evidence có trước mốc dự đoán: metric theo node, loại alert, tuổi evidence, trạng thái TLS, ALB, giao dịch và log đã loại thông tin nhạy cảm. Không dùng scenario marker cấy lỗi, ID run chứa đáp án, ground truth, chữ ký, kết quả Executor/Verifier hoặc thông tin sau quyết định làm đầu vào.

Nhãn sau quyết định gồm action và scope thực hiện, Gate/Operator chấp thuận hay từ chối, Executor executed/failed/partial, kết quả Verifier và thời gian. Nhãn `success` chỉ dùng khi action đã thực hiện và xác minh đầy đủ; từ chối, chưa thực hiện, thiếu probe, bị hủy và hết giờ có trạng thái riêng, không gán thành thất bại của action. Đánh giá độc lập action hợp lệ, sai phạm vi, hoặc chưa xác định; lưu người đánh giá và bằng chứng. Không gán nhãn kết quả của action chưa thử: dữ liệu quan sát có thiên lệch lựa chọn và không chứng minh phương án khác sẽ thành công.

[Drill hiện tại](../automation/moodle-safe-executor-drill.sh) đã lưu stage_times, phê duyệt, receipt và probe trong artifact thành công; cần bổ sung thu thập bền vững cho các lượt thất bại/hủy và mốc gửi yêu cầu duyệt, thời gian phản hồi, thời điểm giao dịch bắt đầu hồi phục. Bảo quản artifact nhạy cảm theo chính sách repo; xuất tập nghiên cứu đã lọc thông tin và giữ mã băm/liên kết nguồn. Tách staging live, production, mô phỏng và replay. Ground truth fixture chỉ chấm sau quyết định, không đi vào runtime.

## Huấn luyện và đánh giá

1. Dựng lại staging: tài liệu kịch bản ghi hạ tầng đã gỡ ngày 10/11/2026. Thu incident đa dạng qua nhiều phiên, điều kiện tải, người vận hành, loại lỗi; thử nghiệm lỗi có kiểm soát, reset và xác nhận baseline từng lượt. Có dữ liệu từ chối và hành động thất bại thực tế, không cố ý chạy hành động bị cấm để tạo nhãn. Một probe mẫu không phải một incident độc lập.
2. Đóng băng schema và thời điểm dự đoán. Chia train/validation/test theo thời gian và nhóm incident/phiên, không chia ngẫu nhiên các mẫu từ cùng incident sang nhiều tập. Bảo lưu test mới nhất đến cuối; đo sai lệch phân bố và số mẫu mỗi action. Dùng replay riêng để kiểm tra kỹ thuật, không gọi đó là kết quả live.
3. Baseline là planner theo luật hiện tại. Với metric có cấu trúc, thử GBDT trước; thêm LDA chỉ khi có log đủ đa dạng và chứng minh bổ sung thông tin. LDA fit từ log train, đóng băng từ vựng và biến đổi validation/test; so sánh bộ luật, GBDT trên metric và LDA + GBDT. Điều chỉnh hyperparameter chỉ trên validation theo thời gian; lưu version dữ liệu, đặc trưng, code, model và seed. Dữ liệu ít, lệch nhãn hoặc một action duy nhất cho từng lỗi có thể khiến bài toán chưa học được.
4. Predictor dự đoán action theo nhãn được đánh giá; mô hình kết quả chỉ học từ action đã thực hiện và quan sát được. Báo accuracy theo từng loại lỗi/action, macro F1, top-k, tỷ lệ đề xuất sai phạm vi, tỷ lệ không dự đoán, chất lượng xác suất và khoảng tin cậy. Calibrate trên dữ liệu tách biệt nếu dùng điểm như xác suất; xem [tài liệu calibration](https://scikit-learn.org/stable/modules/calibration.html). Không lấy kết quả train làm kết quả test.
5. Chạy shadow trên các incident mới, ghi cả dự đoán lẫn kết quả, không thực thi theo ML. Khi đủ bằng chứng và thiết kế được review, thử hiển thị gợi ý trong staging với Operator duyệt; giữ catalog, Gate, chữ ký và Verifier. Huấn luyện lại offline có version, đánh giá trên test mới trước khi thay model; nếu dữ liệu trôi, model lỗi hoặc thiếu evidence thì fallback và chuyển người.

Không có số lượng mẫu chung bảo đảm thành công. Bắt đầu bằng pilot nhiều phiên độc lập cho từng điều kiện và lỗi; dựa trên độ biến động và mức giảm RTO tối thiểu có ý nghĩa để tính cỡ mẫu trước lượt đánh giá chính thức. Đường học và số ca mỗi lớp phải cho thấy kết quả ổn định; lặp cấy cùng một lỗi không đại diện cho sự cố production đa dạng.

## Đo mức cải thiện

### Mở rộng sang nhiều hạ tầng

Có thể tái hiện phương pháp đo trên hạ tầng do nhóm nghiên cứu quản lý. Alibaba không công bố mã production và trace production; [prototype AIHS](https://github.com/alibaba-edu/dcbrain/tree/master/AIHS_prototype) dùng dữ liệu tổng hợp nên không cho phép chạy lại phép đo trên trung tâm dữ liệu Alibaba.

Đề xuất hai lớp thí nghiệm: (1) Moodle trên AWS EC2, RDS và EFS; (2) microservice trên Kubernetes trong phòng lab bằng [Microsoft AIOpsLab](https://github.com/microsoft/AIOpsLab), với workload, fault injection và telemetry. ERPNext là một ứng dụng bổ sung: repo có adapter và cấu hình chuẩn bị, nhưng [README](../README.md) ghi chưa triển khai AWS hay có bằng chứng recovery thực nghiệm. Hai ứng dụng cùng Docker/EC2 chỉ chứng minh đa ứng dụng trong cùng loại môi trường; cần môi trường Kubernetes hoặc hạ tầng khác để đánh giá đa hạ tầng rộng hơn. AIOpsLab chưa được tích hợp với Safety Gate/Executor/Verifier của repo; cần adapter và action catalog, probe, phạm vi và quy trình xác minh riêng trước khi so sánh.

Chọn các loại lỗi có thể áp dụng tương đương (dịch vụ dừng, mất kết nối database, cấu hình sai), xác định giao dịch tổng hợp đại diện trên từng ứng dụng và lấy mẫu với cùng chu kỳ. Ghi thời gian lần cuối giao dịch đạt trước khi lỗi và lần đầu đạt sau khắc phục; thời điểm bắt đầu/kết thúc thực tế chỉ nằm trong khoảng giữa hai mẫu liên tiếp. Ghi thêm mốc Verifier xác nhận ổn định. Đơn vị báo cáo là một lượt cấy lỗi và phục hồi, không phải số probe. Không giả định chuỗi lỗi có tác động tương đương trên các hạ tầng.

Chạy hai nhánh độc lập trên cùng tập lỗi và workload: baseline theo policy hiện tại và policy có gợi ý ML; cùng Gate, duyệt người, Verifier, thời hạn, thứ tự ngẫu nhiên và reset baseline trước từng lượt. Báo thời gian dịch vụ không sẵn sàng, thời gian phát hiện đến xác minh, tỷ lệ hoàn tất, lỗi an toàn và chi phí; công bố riêng theo hạ tầng/kịch bản, số lượt và khoảng tin cậy. Pilot chỉ để ước tính biến động và cỡ mẫu; đóng băng model trước tập test và giữ kịch bản/hạ tầng kiểm tra độc lập. Muốn đo khả năng khái quát, dành hẳn một hạ tầng hoặc cấu hình ra khỏi train, nhưng kết quả chỉ áp dụng cho các môi trường đã thử.

Tách hai câu hỏi: (A) thủ công so với Agent hiện tại; (B) Agent hiện tại so với Agent có gợi ý ML. Với B, hai nhóm cùng lỗi, tải, người trực, probe, Gate và điều kiện Verifier; phân thứ tự ngẫu nhiên theo phiên, lưu cả thất bại, timeout và trường hợp không khôi phục. Chốt thời hạn quan sát và cách xử lý các lượt không hoàn thành trước khi chạy; báo tỷ lệ hoàn thành và thời gian đến hồi phục theo thời hạn, không chỉ thống kê các lượt thành công. Báo trung vị, khoảng tin cậy và phân bố thời gian, p95 chỉ khi mẫu đủ; tính chênh lệch theo nhóm, phân tầng theo kịch bản/phiên.

RTO vận hành theo drill: `t_resolved - t_detect`. Báo riêng `t_detect - t_inject`, thời điểm giao dịch bắt đầu phục hồi, chờ duyệt, thực thi và xác minh. Tỷ lệ cải thiện = `(trung vị baseline - trung vị ML) / trung vị baseline × 100%`, cùng định nghĩa và tập lỗi. Báo thêm failure rate, hành động sai phạm vi, vi phạm probe và false recovery; các chỉ số hiếm cần số mẫu lớn và khoảng tin cậy, 0 lỗi quan sát không chứng minh rủi ro bằng 0.

## Đánh giá khả thi hiện tại

- **Khả thi kỹ thuật:** có evidence, catalog, audit, nhánh shadow và probe; cần bổ sung bộ dữ liệu, nhãn, kiểm tra rò rỉ, huấn luyện và đo độc lập. Chưa có model LDA/GBDT đã huấn luyện trong luồng này.
- **Khả thi giảm RTO:** có tiềm năng nhưng chưa xác định. DB-01 có 695 giây từ phát hiện đến resolved; 7 phút 45 giây từ candidate đến duyệt gồm nhiều bước, không chỉ người chờ. ML không tự rút ngắn cửa sổ Verifier hoặc đảm bảo Operator phản hồi nhanh.
- **Khả năng vượt Alibaba:** chưa thể ước lượng hay hứa hẹn khi chưa có tập dữ liệu, baseline và phép thử tương đương. Staging cấy lỗi chứng minh hiệu quả trong điều kiện staging, không chứng minh tỷ lệ production. RPO cần diễn tập phục hồi dữ liệu riêng; dự đoán action không trực tiếp cải thiện backup hay RPO.
