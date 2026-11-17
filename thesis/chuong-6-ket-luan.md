# Chương 6. Kết luận và hướng phát triển

Chương này tổng kết những gì khoá luận đã làm được, trả lời câu hỏi nghiên cứu trong đúng phạm vi bằng chứng đã thu, nêu các hạn chế còn lại và đề xuất hướng phát triển.

## 6.1. Kết quả đạt được

Khoá luận đặt ra câu hỏi: khi thêm Safety Gate, Safe Executor và Independent Verifier vào pipeline tự chữa lành AIHS (LDA → GBDT Predictor → GBDT Controller), hệ thống xử lý sự cố trên Moodle có an toàn và đáng tin cậy hơn AIHS cơ sở không. Để trả lời, khoá luận đã thực hiện các phần việc sau.

**Về hạ tầng và môi trường thực nghiệm.** Xây dựng môi trường Moodle trên AWS gồm ALB, hai node ứng dụng, RDS PostgreSQL, EFS và máy giám sát, được mô tả hoàn toàn bằng mã (Terraform cho tài nguyên cloud, Ansible cho cấu hình máy) và có thể dựng lại, huỷ đi nhiều lần. Hệ thống giám sát Prometheus/Alertmanager kèm probe theo node và giao dịch synthetic cung cấp bằng chứng trước quyết định cho Agent. Một lab Moodle cục bộ dùng cùng bộ cấy lỗi, probe và reset giúp sinh dữ liệu huấn luyện với chi phí thấp. Image Moodle và Agent chỉ được build trong CI và kéo về từ registry; không build trên máy chủ.

**Về khung an toàn.** Thiết kế và hiện thực một chuỗi kiểm soát phía sau mọi đề xuất của AI, trong đó đầu ra của model luôn được coi là đề xuất không đáng tin:

- **Safety Gate** kiểm tra catalog action, phạm vi, ngữ cảnh tin cậy và độ tươi của bằng chứng; action lạ hoặc ngữ cảnh mơ hồ bị fail-closed và chuyển người.
- **Phê duyệt tách khỏi thi hành:** Operator ký một chữ ký gắn đúng digest của action đã chuẩn bị; **Safe Executor** kiểm lại chữ ký, chỉ chạy action trong allowlist và ghi receipt.
- **Independent Verifier** đo lại probe allowed/forbidden/related trong cửa sổ ổn định 120 s và ký attestation; chỉ attestation này, khi được Agent chấp nhận, mới chuyển sự cố sang `RESOLVED`.
- Mọi bước được ghi vào audit có chuỗi băm, và ground truth của kịch bản không bao giờ là đầu vào cấp quyền.

**Về tái hiện AIHS và phương pháp đánh giá.** Tái hiện phương pháp AIHS trên dữ liệu Moodle (`aihs-paper-repro-moodle-v1`), sửa hai sai khác của prototype công khai so với bài báo, huấn luyện trên dữ liệu lab và khoá model bằng sha256. Xây dựng quy trình đánh giá có đăng ký trước: protocol offline được khoá trước khi mở tập test một lần duy nhất, protocol live ghi trước số lượt, thứ tự ngẫu nhiên, quy tắc dừng và chính sách duyệt; mọi sai lệch trong lúc chạy được ghi công khai.

**Về kết quả.** Chương 5 cho thấy:

- Offline, Gate làm HitRate giảm từ 0,905 xuống 0,810, ReverseRate tăng từ 0,867 lên 0,967, chặn nhầm khoảng 10 % action đúng và tăng tỷ lệ chuyển người từ 8,5 % lên 30,5 %; mẫu action không an toàn quá nhỏ để khẳng định Gate giảm hành động nguy hiểm.
- Live, trên 18 lượt chính và 4 lượt bổ sung: không có kết luận `RESOLVED` sai (0/11), không có thực thi ngoài phạm vi hay khi chưa có chữ ký (13/13 action được duyệt chạy đúng), mọi bất biến an toàn được giữ và mọi lượt được reset sạch.
- Verifier chặn được cả 4 ca có ảnh hưởng phụ mà kiểm tra health-only đã báo "đã khoẻ", trong đó có ca health-only vẫn xanh hoàn toàn còn một node ứng dụng đã ngừng hoạt động.
- Cái giá là một nửa số sự cố dừng ở các điểm kiểm soát của con người (Operator từ chối vì lý do bảo mật, model tự chuyển người, Gate fail-closed), và mốc xác nhận phục hồi chậm hơn mốc phục hồi thật khoảng 2 phút.

## 6.2. Trả lời câu hỏi nghiên cứu

Trong phạm vi môi trường Moodle staging và lab, với cùng một model AIHS LDA/GBDT đã khoá, khung Gate–Executor–Verifier **có** làm hệ thống an toàn và đáng tin cậy hơn ở hai khía cạnh đo được: không thực thi ngoài phạm vi đã duyệt, và không kết luận phục hồi sai, kể cả trong những ca mà kiểm tra health-only kết luận sai. Khung **không** làm model dự đoán chính xác hơn và có chi phí rõ ràng về tỷ lệ chuyển người và độ trễ. Hai khẳng định mạnh hơn — Gate giảm hành động nguy hiểm, và hệ thống phục hồi nhanh hơn hay chậm hơn AIHS cơ sở khi chạy thật — chưa đủ bằng chứng và được giữ là `NOT_MEASURABLE`.

Đóng góp chính của khoá luận vì thế không nằm ở một con số cải thiện, mà ở việc chứng minh bằng thực nghiệm trên hạ tầng thật rằng có thể đặt một model tự chữa lành vào đường xử lý sự cố mà vẫn giữ được các bất biến an toàn, và ở một quy trình đánh giá trung thực (khoá trước, mở test một lần, tách loại bằng chứng, báo cả mẫu số và chi phí) có thể dùng lại cho các hệ thống khác.

## 6.3. Hạn chế

1. **Nhãn chưa độc lập.** Nhãn an toàn và nhãn phục hồi do một người gán; chưa có người gán thứ hai để đo độ đồng thuận. Người thiết kế, người gán nhãn và Operator là cùng một người.
2. **Mẫu nhỏ.** 59 dòng test trong 8 nhóm; 18 lượt live chính, 3 lặp mỗi kịch bản. Kết quả live chỉ mang tính mô tả.
3. **Một môi trường, lỗi cấy có kiểm soát.** Mỗi kịch bản ứng với một action; chưa có sự cố tự nhiên, lỗi chồng lỗi hay kịch bản mơ hồ đủ nhiều.
4. **Lab khác staging.** Một số đặc trưng chỉ có ở lab (HAProxy) không có trên ALB staging; chu kỳ probe khác nhau.
5. **Giới hạn suy ngữ cảnh của Gate.** Quy tắc suy ngữ cảnh từ alert nhầm NET-01 với DB-01 khi đường tới RDS hỏng do DNS, nên NET-01 chưa được thực thi trong chiến dịch AIHS. Quy tắc này đã khoá trong protocol nên không được sửa sau khi thấy kết quả.
6. **C2 không chạy riêng.** Góc nhìn health-only được suy ra từ cùng lượt C3; chưa đo hành vi của Operator khi không có Verifier.
7. **Không có RTO live của C0.** Vì nguyên tắc an toàn, đề xuất của model không bao giờ được thi hành mà không qua Gate, nên không có đối chứng thời gian cùng điều kiện.
8. **Phụ thuộc vào người duyệt.** Thời gian chờ duyệt chiếm phần lớn RTO và dao động từ vài chục giây tới nhiều giờ.

## 6.4. Hướng phát triển

1. **Mở rộng sang ERPNext.** Đây là bước kế tiếp đã được lên kế hoạch. Mã nguồn đã có sẵn một phần (adapter nền tảng, catalog action cho MariaDB/Redis/Nginx, Terraform và playbook triển khai, script cấy/gỡ lỗi) nhưng chưa được kiểm chứng live. Việc cần làm là hoàn thiện probe allowed/forbidden/related và hợp đồng Verifier cho ERPNext, thu dữ liệu riêng và huấn luyện LDA riêng (từ vựng log của ERPNext khác Moodle), rồi lặp lại cùng quy trình đánh giá. Thí nghiệm đáng giá là so sánh model huấn luyện trên Moodle áp dụng thẳng sang ERPNext với model huấn luyện lại trên ERPNext, để tách kết luận về **khung** (các bất biến an toàn có giữ được trên hệ thống thứ hai không) khỏi kết luận về **model**.
2. **AIOpsLab và Kubernetes.** Khi ERPNext hoàn tất, có thể viết adapter cho microservice và dùng các bài toán có sẵn của AIOpsLab làm bộ test chuẩn.
3. **Nhãn độc lập và mẫu lớn hơn.** Mời người gán nhãn thứ hai, đo độ đồng thuận, mở rộng số lượt live (kế hoạch đã dự trù thêm lặp 4 và 5) để thu hẹp khoảng tin cậy.
4. **Cải thiện suy ngữ cảnh của Gate.** Thiết kế lại quy tắc ngữ cảnh để phân biệt NET-01 với DB-01 bằng tín hiệu DNS trước quyết định, đăng ký như một phiên bản protocol mới và đánh giá trên tập test mới, để giảm tỷ lệ chặn nhầm khoảng 10 %.
5. **Giảm chi phí chờ duyệt mà không bỏ phê duyệt.** Phân tầng rủi ro để các action rủi ro thấp, có rollback và có Verifier, được duyệt theo chính sách định trước; các action đụng ranh giới bảo mật (như gỡ rule tường lửa) vẫn bắt buộc người duyệt, đúng như lựa chọn của Operator trong chiến dịch.
6. **Đối chứng thời gian có kiểm soát.** Xây một lab cô lập để chạy cả C0 và C3 với cùng lỗi, tải và phép chấm phục hồi thật, nhằm đo chính xác độ trễ tăng thêm của các lớp bảo vệ.
7. **Hồi phục giả tự nhiên.** Thu thập sự cố có ảnh hưởng phụ phát sinh tự nhiên (như ad-hoc task mồ côi ở BIZ-02) để ước lượng tần suất hồi phục giả ngoài các ca cấy có chủ đích.

## 6.5. Lời kết

AI có thể đề xuất cách xử lý sự cố nhanh và thường đúng, nhưng trong vận hành hạ tầng, một lần thực thi sai phạm vi hay một lần báo "đã khỏi" sai có thể gây hậu quả lớn hơn nhiều so với vài phút chậm trễ. Khoá luận cho thấy một cách tiếp cận khả thi: không trao quyền cho model, mà đặt model vào giữa các lớp kiểm soát có thể kiểm chứng (chính sách, chữ ký người duyệt, bộ thực thi giới hạn và bộ xác minh độc lập) và đo trung thực cả lợi ích lẫn cái giá của chúng.
