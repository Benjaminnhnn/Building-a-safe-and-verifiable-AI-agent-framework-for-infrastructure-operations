# Chương 6. Kết luận và hướng phát triển

Chương này tổng kết những gì khoá luận đã làm được, trả lời câu hỏi nghiên cứu trong đúng phạm vi bằng chứng đã thu, rút ra các bài học thiết kế, nêu các hạn chế còn lại và đề xuất hướng phát triển. Mọi con số trong chương được lấy lại từ Chương 5; chương này không đưa thêm số liệu mới.

## 6.1. Kết quả đạt được

Khoá luận đặt ra câu hỏi: khi thêm Safety Gate, Safe Executor và Independent Verifier vào pipeline tự chữa lành AIHS (LDA → GBDT Predictor → GBDT Controller) [AIHS2021], hệ thống xử lý sự cố trên Moodle có an toàn và đáng tin cậy hơn AIHS cơ sở không. Để trả lời, khoá luận đã thực hiện các phần việc sau.

**Về hạ tầng và môi trường thực nghiệm.** Khoá luận xây dựng môi trường Moodle trên AWS gồm ALB, hai node ứng dụng, RDS PostgreSQL, EFS và máy giám sát, được mô tả hoàn toàn bằng mã (Terraform cho tài nguyên cloud, Ansible cho cấu hình máy) và có thể dựng lại, huỷ đi nhiều lần. Hệ thống giám sát Prometheus/Alertmanager kèm probe theo node và giao dịch synthetic cung cấp bằng chứng trước quyết định cho Agent. Một lab Moodle cục bộ dùng cùng bộ cấy lỗi, probe và reset giúp sinh dữ liệu huấn luyện với chi phí thấp. Image Moodle và Agent chỉ được build trong CI và kéo về từ registry; không build trên máy chủ.

**Về khung an toàn.** Khoá luận thiết kế và hiện thực một chuỗi kiểm soát phía sau mọi đề xuất của AI, trong đó đầu ra của model luôn được coi là đề xuất không đáng tin:

- **Safety Gate** kiểm tra catalog action, phạm vi, ngữ cảnh tin cậy và độ tươi của bằng chứng; action lạ hoặc ngữ cảnh mơ hồ bị fail-closed và chuyển người.
- **Phê duyệt tách khỏi thi hành:** Operator ký một chữ ký gắn đúng digest của action đã chuẩn bị; **Safe Executor** kiểm lại chữ ký, chỉ chạy action trong allowlist và ghi receipt.
- **Independent Verifier** đo lại probe allowed/forbidden/related trong cửa sổ ổn định 120 s và ký attestation; chỉ attestation này, khi được Agent chấp nhận, mới chuyển sự cố sang `RESOLVED`.
- Mọi bước được ghi vào audit có chuỗi băm, và ground truth của kịch bản không bao giờ là đầu vào cấp quyền.

**Về tái hiện AIHS và phương pháp đánh giá.** Khoá luận tái hiện phương pháp AIHS trên dữ liệu Moodle (`aihs-paper-repro-moodle-v1`), sửa hai sai khác của prototype công khai so với bài báo, huấn luyện trên dữ liệu lab và khoá model bằng sha256. Quy trình đánh giá được đăng ký trước: protocol offline được khoá trước khi mở tập test một lần duy nhất; protocol live ghi trước số lượt, thứ tự ngẫu nhiên, quy tắc dừng và chính sách duyệt; mọi sai lệch trong lúc chạy được ghi công khai.

**Về kết quả.** Chương 5 cho thấy:

- Offline, Gate làm HitRate giảm từ 0,905 xuống 0,810, ReverseRate tăng từ 0,867 lên 0,967, chặn nhầm khoảng 10 % action đúng và tăng tỷ lệ chuyển người từ 8,5 % lên 30,5 %; mẫu action không an toàn quá nhỏ để khẳng định Gate giảm hành động nguy hiểm.
- Live, trên 18 lượt chính và 4 lượt bổ sung: không có kết luận `RESOLVED` sai (0/11), không có thực thi ngoài phạm vi hay khi chưa có chữ ký (13/13 action được duyệt chạy đúng), mọi bất biến an toàn được giữ và mọi lượt được reset sạch.
- Verifier chặn được cả 4 ca có ảnh hưởng phụ mà kiểm tra health-only đã báo "đã khoẻ", trong đó có ca health-only vẫn xanh hoàn toàn còn một node ứng dụng đã ngừng hoạt động.
- Cái giá là một nửa số sự cố ở mẫu chính dừng ở các điểm kiểm soát (Operator từ chối vì lý do bảo mật, model tự chuyển người, Gate fail-closed), và mốc xác nhận hồi phục chậm hơn mốc hồi phục thật khoảng 2 phút.

## 6.2. Trả lời câu hỏi nghiên cứu

Câu hỏi nghiên cứu chính được tách thành năm câu hỏi thành phần ở Chương 1, và Bảng 5.13 đã trả lời từng câu. Tóm lại: Gate có dấu hiệu chặn action sai nhưng lợi ích này là `NOT_MEASURABLE` do mẫu quá nhỏ (RQ1), và chi phí chặn nhầm khoảng 10 % cùng mức tăng chuyển người là kết luận vững nhất về Gate vì nhất quán ở cả hai arm (RQ2). Mọi action được duyệt chạy đúng action, máy đích và phạm vi (RQ3); Verifier không ghi `RESOLVED` sai lần nào và chặn cả bốn ca có ảnh hưởng phụ (RQ4); một nửa số sự cố ở mẫu chính hoàn tất an toàn, phần còn lại dừng ở một điểm kiểm soát có chủ đích (RQ5).

Gộp lại, trong phạm vi môi trường Moodle staging và lab, với cùng một model AIHS LDA/GBDT đã khoá, câu trả lời cho câu hỏi chính là **có điều kiện**. Khi có Gate, Executor và Verifier phía sau model, hệ thống đạt hai tính chất an toàn mà AIHS cơ sở, theo phân tích ở Chương 1 và 3, không có cơ chế độc lập để bảo đảm: không thực thi ngoài phạm vi đã duyệt, và không kết luận hồi phục sai. Độ mạnh bằng chứng của hai tính chất khác nhau. Với tính chất thứ hai, khoá luận có phép so sánh ghép cặp trực tiếp trên cùng lần thi hành: health-only báo hồi phục sai ở cả 4 ca có ảnh hưởng phụ, còn Verifier chặn cả 4. Với tính chất thứ nhất, bằng chứng là không quan sát thấy vi phạm nào, không phải mức giảm so với một nhánh C0 chạy live, vì nhánh đó không được thi hành.

Khung **không** làm model dự đoán chính xác hơn và có chi phí rõ ràng về tỷ lệ chuyển người và độ trễ. Hai khẳng định mạnh hơn, là Gate giảm hành động nguy hiểm và hệ thống hồi phục nhanh hơn hay chậm hơn AIHS cơ sở khi chạy thật, chưa đủ bằng chứng và được giữ là `NOT_MEASURABLE`.

Đóng góp chính của khoá luận vì thế không nằm ở một con số cải thiện, mà ở việc chứng minh bằng thực nghiệm trên hạ tầng thật rằng có thể đặt một model tự chữa lành vào đường xử lý sự cố mà vẫn giữ được các bất biến an toàn, và ở một quy trình đánh giá trung thực (khoá trước, mở test một lần, tách loại bằng chứng, báo cả mẫu số và chi phí) có thể dùng lại cho các hệ thống khác.

## 6.3. Ý nghĩa của kết quả và bài học thiết kế

Quá trình thiết kế, hiện thực và chạy thực nghiệm để lại một số bài học vượt ra ngoài môi trường Moodle.

**Health-only không đủ làm tiêu chí hồi phục.** Ca health-only vẫn xanh trong khi một node ứng dụng đã ngừng hoạt động cho thấy vì sao cần nhóm probe *related*: ALB còn một node khoẻ nên người dùng chưa thấy lỗi, và kiểm tra chỉ nhìn healthz hay giao dịch synthetic không nhận ra. Hợp đồng probe của từng kịch bản vì thế là một phần của thiết kế hệ thống, không phải việc bổ sung sau cùng. Bằng chứng này chứng minh một **cơ chế**, không ước lượng tần suất hồi phục giả trong vận hành thật.

**Mỗi lớp có một điểm dừng riêng.** Tách đề xuất, phê duyệt, thi hành và xác minh thành các thành phần quyền hẹp cho phép mỗi thành phần nói "không" độc lập. Trong mẫu chính, mọi lượt không hoàn tất đều dừng ở Operator, ở model hoặc ở Gate; không lượt nào thất bại do Executor hay Verifier. Đây là bằng chứng mô tả, không phải bảo đảm.

**Fail-closed có giá, và cái giá cần được đo riêng.** Quy tắc suy ngữ cảnh của Gate không đoán khi mơ hồ; chính nó gây ra chi phí chặn nhầm ở phép đo offline và giải thích vì sao NET-01 không được thi hành trong chiến dịch. Tỷ lệ hoàn tất cũng phụ thuộc vào chính sách của người duyệt: bốn lượt bị từ chối vì lý do bảo mật là quyết định có chủ ý, không phải lỗi hệ thống. Tách chất lượng model, chi phí của Gate và hành vi của người duyệt thành các chỉ số riêng giúp không quy nhầm chi phí của lớp này cho lớp khác.

**Thời gian xác minh là tham số thiết kế.** Cửa sổ ổn định 120 s là nguyên nhân chính khiến mốc `RESOLVED` muộn hơn mốc hồi phục thật. Khoá luận cố ý không rút ngắn cửa sổ này; việc nới hay thu hẹp nó theo mức rủi ro của action chưa được đánh giá.

**Kỷ luật đánh giá cho phép nói đúng mức.** Đăng ký trước, giữ các lượt bất lợi trong mẫu số, tách bốn loại bằng chứng và công bố mọi sai lệch cho phép ghi `NOT_MEASURABLE` ở những chỗ bằng chứng chưa đủ, thay vì làm tròn thành kết luận mạnh hơn dữ liệu. Các lỗi tích hợp được giữ lại (Bảng 4.8) cũng là một kết quả: thực nghiệm live phải kiểm tra cả harness, probe và đồng bộ thời gian, không chỉ mô hình.

## 6.4. Hạn chế

1. **Nhãn chưa độc lập.** Nhãn an toàn và nhãn hồi phục do một người gán; chưa có người gán thứ hai để đo độ đồng thuận. Người thiết kế, người gán nhãn và Operator là cùng một người.
2. **Mẫu nhỏ.** 59 dòng test trong 8 nhóm; 18 lượt live chính, 3 lặp mỗi kịch bản. Kết quả live chỉ mang tính mô tả.
3. **Một môi trường, lỗi cấy có kiểm soát.** Mỗi kịch bản ứng với một action; chưa có sự cố tự nhiên, lỗi chồng lỗi hay kịch bản mơ hồ đủ nhiều.
4. **Lượt bổ sung không mù.** Operator biết trước kịch bản của bốn lượt bổ sung, và lỗi phụ ở hai lượt do người thiết kế cấy đúng lúc health-only vừa kết luận.
5. **Lab khác staging.** Một số đặc trưng chỉ có ở lab (HAProxy) không có trên ALB staging; chu kỳ probe khác nhau. Mapping dùng khi chấm offline là bản nháp.
6. **Giới hạn suy ngữ cảnh của Gate.** Quy tắc suy ngữ cảnh từ alert nhầm NET-01 với DB-01 khi đường tới RDS hỏng do DNS, nên NET-01 chưa được thi hành trong chiến dịch AIHS. Quy tắc này đã khoá trong protocol nên không được sửa sau khi thấy kết quả.
7. **C2 không chạy riêng.** Góc nhìn health-only được suy ra từ cùng lượt C3; chưa đo hành vi của Operator khi không có Verifier.
8. **Không có RTO live của C0.** Vì nguyên tắc an toàn, đề xuất của model không bao giờ được thi hành mà không qua Gate, nên không có đối chứng thời gian cùng điều kiện.
9. **Probe chấm điểm chưa hoàn toàn độc lập.** Hồi phục thật được chấm bằng bộ probe hợp đồng của chính lỗi đã cấy, dùng chung hợp đồng với Verifier; lỗi chưa có probe sẽ không bị phát hiện bởi cả hai.
10. **Phụ thuộc vào người duyệt.** Thời gian chờ duyệt chiếm phần lớn RTO và dao động từ vài chục giây tới nhiều giờ.
11. **Giới hạn của mô hình đe doạ.** Khung chống sai sót và thiếu bằng chứng, không chống kẻ điều khiển được telemetry, máy Operator bị chiếm, tấn công chuỗi cung ứng hay từ chối dịch vụ lên Telegram; sự cố của chính RDS (Single-AZ, nên failover) nằm ngoài phạm vi đánh giá (Chương 3).

## 6.5. Hướng phát triển

1. **Mở rộng sang ERPNext.** Đây là bước kế tiếp đã được lên kế hoạch. Mã nguồn đã có sẵn một phần (adapter nền tảng, catalog action cho MariaDB/Redis/Nginx, Terraform và playbook triển khai, script cấy/gỡ lỗi) nhưng chưa được kiểm chứng live. Việc cần làm là hoàn thiện probe allowed/forbidden/related và hợp đồng Verifier cho ERPNext, thu dữ liệu riêng và huấn luyện LDA riêng (từ vựng log của ERPNext khác Moodle), rồi lặp lại cùng quy trình đánh giá. Thí nghiệm đáng giá là so sánh model huấn luyện trên Moodle áp dụng thẳng sang ERPNext với model huấn luyện lại trên ERPNext, để tách kết luận về **khung** (các bất biến an toàn có giữ được trên hệ thống thứ hai không) khỏi kết luận về **model**.
2. **AIOpsLab và Kubernetes.** Khi ERPNext hoàn tất, có thể viết adapter cho microservice và dùng các bài toán có sẵn của AIOpsLab làm bộ test chuẩn [AIOpsLab2025]. Kubernetes là nền tảng điều phối phổ biến theo khảo sát của CNCF [CNCF2025], nên là môi trường đáng thử tiếp theo sau ERPNext.
3. **Nhãn độc lập và mẫu lớn hơn.** Mời người gán nhãn thứ hai, đo độ đồng thuận, mở rộng số lượt live (kế hoạch đã dự trù thêm lặp 4 và 5) để thu hẹp khoảng tin cậy.
4. **Cải thiện suy ngữ cảnh của Gate.** Thiết kế lại quy tắc ngữ cảnh để phân biệt NET-01 với DB-01 bằng tín hiệu DNS trước quyết định, đăng ký như một phiên bản protocol mới và đánh giá trên tập test mới, để giảm tỷ lệ chặn nhầm khoảng 10 %.
5. **Giảm chi phí chờ duyệt mà không bỏ phê duyệt.** Phân tầng rủi ro để các action rủi ro thấp, có rollback và có Verifier, được duyệt theo chính sách định trước; các action đụng ranh giới bảo mật (như gỡ rule tường lửa) vẫn bắt buộc người duyệt, đúng như lựa chọn của Operator trong chiến dịch.
6. **Đối chứng thời gian có kiểm soát.** Xây một lab cô lập để chạy cả C0 và C3 với cùng lỗi, tải và phép chấm hồi phục thật, nhằm đo chính xác độ trễ tăng thêm của các lớp bảo vệ.
7. **Hồi phục giả tự nhiên.** Thu thập sự cố có ảnh hưởng phụ phát sinh tự nhiên (như ad-hoc task mồ côi ở BIZ-02) để ước lượng tần suất hồi phục giả ngoài các ca cấy có chủ đích.
8. **Mở rộng độ phủ và độ độc lập của probe.** Bổ sung probe cho các loại lỗi chưa có hợp đồng và tách bộ probe chấm điểm khỏi hợp đồng của Verifier.

## 6.6. Lời kết

AI có thể đề xuất cách xử lý sự cố nhanh và thường đúng, nhưng trong vận hành hạ tầng, một lần thực thi sai phạm vi hay một lần báo "đã khỏi" sai có thể gây hậu quả lớn hơn nhiều so với vài phút chậm trễ. Khoá luận cho thấy một cách tiếp cận khả thi: không trao quyền cho model, mà đặt model vào giữa các lớp kiểm soát có thể kiểm chứng (chính sách, chữ ký người duyệt, bộ thực thi giới hạn và bộ xác minh độc lập) và đo trung thực cả lợi ích lẫn cái giá của chúng.

## Tài liệu tham khảo của chương

- [AIHS2021] R. Li và cộng sự, "Automated Intelligent Healing in Cloud-Scale Data Centers," trong *Proc. IEEE International Symposium on Reliable Distributed Systems (SRDS)*, 2021. DOI: 10.1109/SRDS53918.2021.00032.
- [AIOpsLab2025] Y. Chen, M. Shetty, G. Somashekar, M. Ma, Y. Simmhan, J. Mace, C. Bansal, R. Wang, S. Rajmohan, "AIOpsLab: A Holistic Framework to Evaluate AI Agents for Enabling Autonomous Clouds," arXiv:2501.06706v1 [cs.AI], 01/2025.
- [CNCF2025] A. Lawson, J. Sica, *CNCF Annual Cloud Native Survey: The infrastructure of AI's future*, The Linux Foundation / Cloud Native Computing Foundation, 01/2026.
