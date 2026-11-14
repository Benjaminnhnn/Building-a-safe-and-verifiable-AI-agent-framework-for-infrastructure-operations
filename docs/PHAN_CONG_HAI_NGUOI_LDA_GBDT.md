# Phân công nối tiếp: tích hợp AIHS với Safety Gate, Executor và Verifier

**Mục tiêu:** sử dụng các thành phần an toàn đã có để đánh giá đề xuất của AIHS, sau đó đo chất lượng đề xuất, an toàn và thời gian phục hồi. Người A hoàn tất phần AIHS và dữ liệu rồi bàn giao; người B tiếp nhận, tích hợp với hệ thống hiện tại và chạy đánh giá. Đây là kế hoạch công việc tiếp theo, không phải báo cáo đã hoàn thành tích hợp AIHS.

## 1. Những gì đã có và không phải xây dựng lại

| Thành phần | Bằng chứng trong repo | Trạng thái và việc cần lưu ý |
| --- | --- | --- |
| Safety Gate | [safe_execution_gate.py](../agent_src/core/safety/safe_execution_gate.py), [action_catalog.py](../agent_src/core/safety/action_catalog.py), test tương ứng | Đã có policy/catalog và kiểm tra; dùng lại, chỉ mở rộng qua thiết kế và kiểm thử khi tích hợp có yêu cầu. |
| Executor và Operator approval | [live_handoff.py](../agent_src/core/moodle/live_handoff.py), [safe_executor_client.py](../agent_src/core/safety/safe_executor_client.py) | Đã có luồng chuẩn bị, ký, thực thi và receipt; action và scope phải phù hợp môi trường. |
| Independent Verifier | [moodle-independent-verifier.py](../automation/moodle-independent-verifier.py), [verifier_attestation.py](../agent_src/core/safety/verifier_attestation.py) | Đã có probe và attestation; chỉ Verifier được chuyển `RESOLVED`. |
| Nhánh đề xuất shadow | [ai_shadow_proposal.py](../agent_src/core/moodle/ai_shadow_proposal.py) | Đã có ví dụ Gemini shadow đánh giá qua Gate; **chưa phải AIHS**, không chứng minh model AIHS đã tích hợp. |
| Công cụ đo và đánh giá | [moodle-drill-summary.py](../automation/moodle-drill-summary.py), [dangerous_injection.py](../evaluation/benchmark/dangerous_injection.py), [statistical_analysis.py](../evaluation/benchmark/statistical_analysis.py) | Có nền tảng, cần kiểm tra hợp đồng đầu vào và bổ sung phần đo AIHS còn thiếu. Benchmark tổng hợp không phải kết quả thực nghiệm. |
| Bằng chứng live Moodle | [báo cáo kịch bản, mục 6](BAO_CAO_KICH_BAN_LOI.md) | DB-01 và NET-01 có artifact hoàn tất ngày 08/11; DB-01 phát hiện → `RESOLVED` 695 giây. Đây là các lượt lịch sử, không chứng minh hiệu quả AIHS. |
| Đa hạ tầng | [ERPNext adapter](../agent_src/core/erpnext/adapter.py), [README](../README.md) | Có adapter và chuẩn bị; README ghi chưa có bằng chứng phục hồi live ERPNext. Không suy ra Gate/Executor/Verifier chạy live trên mọi hạ tầng. |

Dữ liệu trong `evaluation/ground_truth/` là fixture để chấm sau quyết định; không phải dữ liệu xác thực cho phép hành động. [Báo cáo benchmark](../evaluation/benchmark/results/BENCHMARK_REPORT.md) ngày 11/10 ghi chưa có dataset benchmark thực nghiệm được chấp nhận tại thời điểm đó; cần kiểm kê artifact mới hơn trước khi sử dụng. [Kế hoạch LDA/GBDT](PLANNING_UNG_DUNG_LDA_GBDT.md) ghi chưa có model LDA/GBDT huấn luyện trong hệ thống.

## 2. Chia việc và một điểm bàn giao

| Thứ tự | Người | Phần việc | Đầu ra |
| --- | --- | --- | --- |
| 1 | **A** | Tái hiện AIHS prototype, chuẩn hóa dữ liệu và dựng baseline có thể tái lập | Baseline AIHS, dữ liệu có nguồn gốc, dự đoán mẫu và hợp đồng kết nối |
| 2 | **B** | Nghiệm thu gói A; trả A sửa nếu thiếu | Biên bản bàn giao và danh sách hạn chế |
| 3 | **B** | Kết nối AIHS với Gate/Executor/Verifier hiện có và kiểm thử | Adapter và luồng an toàn chạy trong môi trường thử |
| 4 | **B** | Chạy đối chứng, đo các chỉ số mục tiêu và báo cáo | Artifact, dataset đã audit và bảng kết quả |

A làm xong phần của mình trước khi B bắt đầu triển khai. B tự sử dụng và vận hành gói nhận được; không chia cho A/B làm song song theo từng giai đoạn. Review độc lập sau chiến dịch có thể do A hoặc người thứ ba thực hiện, dựa trên artifact B bàn giao.

## 3. Người A — Chuẩn bị AIHS và bộ dữ liệu

### A1. Kiểm kê và tái hiện AIHS

1. Lấy [prototype công khai AIHS](https://github.com/alibaba-edu/dcbrain) và tài liệu bài báo; ghi commit, giấy phép, phụ thuộc và điều kiện chạy. Prototype/dữ liệu tổng hợp không phải mã hay trace production của Alibaba.
2. Chạy prototype trên dữ liệu của chính nó; lưu lệnh, tham số, seed và kết quả. Nếu không tái lập được, ghi lỗi, khác biệt và kết quả quan sát; không đặt mục tiêu giả là đã tái hiện thành công.
3. Định nghĩa baseline AIHS: cách tạo đặc trưng LDA, Predictor/Controller GBDT, action đầu ra và thời điểm đưa ra dự đoán. Ghi phần nào thực sự do prototype hỗ trợ và phần nào cần tái hiện riêng.

**Bàn giao:** mã/cấu hình baseline, version, lệnh chạy và báo cáo tái lập.

### A2. Dữ liệu và nhãn cho thí nghiệm

1. Kiểm kê các artifact hiện có, nhóm theo môi trường, ngày, phiên bản và loại bằng chứng: live staging, lab, offline replay hoặc dữ liệu tổng hợp. Không dùng số lượng file làm số incident.
2. Xây schema thống nhất cho một incident/action gồm bằng chứng **trước dự đoán**, action được đề xuất, action thực sự chạy, Gate/approval, receipt và kết quả Verifier **sau quyết định**. Phân loại `chưa thực hiện`, `bị từ chối`, `timeout`, `thiếu bằng chứng` riêng với thành công/thất bại.
3. Tạo bộ xuất dữ liệu đã lọc nhạy cảm, checksum và kiểm tra rò rỉ. Cấm marker kịch bản, ground truth, ID chứa đáp án và kết quả sau quyết định khỏi đặc trưng. Chỉ gán kết quả cho action thực sự quan sát được.
4. Nếu artifact lịch sử không đủ, chuẩn bị quy trình để B tự thu bổ sung trong lab/benchmark; ghi rõ số mẫu theo lỗi, action và kết quả. Không giả định dữ liệu tổng hợp hay một vài lượt staging là đủ huấn luyện và tổng quát hóa.
5. Chia train/validation/test theo thời gian và nhóm incident/phiên; giữ test độc lập. Fit LDA chỉ trên train. Ghi nguồn dữ liệu và sự khác nhau giữa môi trường huấn luyện và test.

**Bàn giao:** schema, dữ liệu được phép chia sẻ, manifest/checksum, báo cáo chất lượng và script chuyển đổi. Ground truth và nhãn độc lập chỉ dùng ngoài đường quyết định live.

### A3. Hợp đồng kết nối AIHS → hệ thống an toàn

- Mô tả đầu ra AIHS (`action` và thông tin cần thiết) và bảng ánh xạ **có review** tới action ID, target, scope và precondition trong catalog hiện có. Sáu loại hành động server của Alibaba không tự tương đương action Moodle/ERPNext; nếu không có ánh xạ hợp lệ thì `chuyển người`, không tạo lệnh tự do.
- Chốt bản ghi đầu ra cho B: incident, evidence refs, model/data version, thời điểm dự đoán, action và trạng thái không thể ánh xạ. Dữ liệu của AIHS là đề xuất chưa đáng tin cậy và không chứa quyền thực thi.
- Chuẩn bị ca kiểm thử: hợp lệ, action lạ, sai phạm vi, evidence thiếu/cũ, model lỗi và không ánh xạ. Không thử thi hành hành động nguy hiểm để lấy số liệu.

**Bàn giao:** tài liệu mapping, ví dụ input/output đã lọc, bộ ca kiểm thử và phạm vi chưa hỗ trợ.

### A4. Gói bàn giao

A giao toàn bộ một lần: mã và phiên bản AIHS, hướng dẫn dựng môi trường, dataset/manifest và schema, bảng mapping, lệnh tái lập, kết quả baseline, danh sách lỗi còn mở và đề cương đo. Không đưa credential, Terraform state hoặc dữ liệu nhạy cảm vào Git. B nghiệm thu bằng cách chạy lại baseline và kiểm tra schema, provenance, mapping, split; nếu không đạt, trả A danh sách lỗi trước khi tích hợp.

## 4. Người B — Tích hợp AIHS và đo kết quả

### B1. Nối AIHS với các thành phần hiện có

1. Dùng gói A tạo adapter chuyển output AIHS thành đề xuất có kiểu và evidence refs hợp lệ; không cho mô hình tạo shell command, tự ký hay tự cấp quyền. Đầu ra thiếu, cũ, ngoài catalog hoặc sai scope phải dừng/chuyển người.
2. Thử **shadow**: đánh giá đề xuất qua Safety Gate hiện có, lưu input đã lọc, version model, quyết định và audit; không dispatch. Dùng nhánh shadow Gemini làm tham khảo, không gọi nó là AIHS.
3. Khi đã có hành động hợp lệ và quy trình staging được review, nối qua handoff hiện có: Operator duyệt và ký đúng action → Executor → Independent Verifier với probe allowed/forbidden/related và stability window. Chỉ attestation hợp lệ mới cho `RESOLVED`.
4. Kiểm thử action lạ/sai scope, thiếu evidence, chữ ký sai, model timeout, receipt sai, probe thất bại và luồng thành công. Chạy lint, test và replay liên quan; không gọi kết quả replay là phục hồi live.

**Đầu ra:** adapter, kiểm thử, hướng dẫn chạy, audit và bằng chứng rằng ranh giới an toàn vẫn hoạt động. Không cần viết lại Gate, Executor hoặc Verifier.

### B2. Đo chất lượng và an toàn trên cùng tập thử

- **Chất lượng model:** so sánh AIHS baseline với biến thể đề xuất trên cùng test độc lập; báo HitRate, ReverseRate, OverallRate, macro-F1/top-k, số lượng theo lớp, từ chối dự đoán và khoảng tin cậy khi đủ dữ liệu. Thêm GBDT metric hoặc LDA + metric nếu nghiên cứu yêu cầu và dữ liệu cho phép. Không hứa sẽ vượt baseline.
- **Safety Gate:** phát lại cùng tập đề xuất cho nhánh baseline và nhánh có Gate trong môi trường mô phỏng/đối chứng an toàn, đo số đề xuất ngoài phạm vi được chặn và các action an toàn bị chặn dựa trên nhãn độc lập. Nhánh không Gate không thực thi hành động nguy hiểm; chỉ ghi quyết định giả lập. Báo mẫu, tử số/mẫu số và độ bao phủ nhãn.
- **Verifier:** đối chiếu kết luận health-only với Verifier trên cùng lượt có bằng chứng độc lập về ảnh hưởng phụ. Tách probe synthetic và tình huống cấy lỗi khỏi kết quả live. Không biến 0 lỗi quan sát thành tuyên bố rủi ro bằng 0.

**Đầu ra:** dữ liệu test đóng băng, báo cáo theo nhóm và artifact có thể tái lập; không dùng kết quả đo để điều chỉnh lại test.

### B3. Đo thời gian phục hồi trên hạ tầng

1. Kiểm tra AWS state, quyền, kinh phí và điều kiện khôi phục; tài liệu ngày 10/11/2026 ghi staging đã gỡ, cần xác minh và dựng lại nếu muốn chạy. Không thực hiện fault drill khi chưa có môi trường staging, baseline và reset hợp lệ.
2. Thiết kế đối chứng: Agent theo luật hiện tại và AIHS + Gate/Executor/Verifier; có thể thêm thủ công nếu cần câu hỏi nghiên cứu đó. Cùng lỗi, workload, probe, chính sách duyệt và stability window; thứ tự ngẫu nhiên theo phiên. Tính cỡ mẫu từ pilot, chốt thời hạn và cách báo timeout/thất bại trước chiến dịch.
3. Ghi riêng thời gian **phát hiện → `RESOLVED`**, khoảng thời gian giao dịch không phục vụ, thời gian chờ duyệt, thực thi và xác minh. Báo tỉ lệ hoàn tất, trung vị và khoảng tin cậy; p95 khi đủ mẫu. Không bỏ các lượt không hoàn tất khỏi báo cáo.
4. Đối chiếu an toàn và thời gian trên cùng điều kiện. AIHS shadow không ảnh hưởng quyết định nên không thể chứng minh giảm RTO; muốn đo tác động gợi ý cần nhánh Operator thực sự thấy gợi ý, vẫn qua toàn bộ kiểm soát hiện có.

**Đầu ra:** artifact live staging và báo cáo so sánh; không suy diễn thành kết quả production. Alibaba báo thời gian server không sẵn sàng giảm 51% so với baseline của họ; không so trực tiếp với 695 giây của Moodle.

### B4. Mở rộng đa hạ tầng

Sau kết quả đầu tiên trên môi trường đã hỗ trợ, kiểm tra ERPNext adapter, catalog, Executor và probe thực tế; bổ sung phần còn thiếu trước khi thử live. Có thể chọn Kubernetes/AIOpsLab làm môi trường tiếp theo khi đủ nguồn lực. Với mỗi môi trường, định nghĩa mapping action, scope, rollback, probe và phân quyền riêng; huấn luyện/test theo môi trường và báo số liệu riêng. Không coi hai ứng dụng trên cùng một hạ tầng là đủ để chứng minh đa hạ tầng rộng, cũng không giả định model Moodle tổng quát hóa sang ERPNext.

## 5. Chỉ số mục tiêu và điều kiện kết luận

| Mục tiêu | Đối chứng / dữ liệu cần | Kết luận hợp lệ |
| --- | --- | --- |
| Độ chính xác AIHS | Cùng dataset test và cùng định nghĩa chỉ số giữa baseline và model đề xuất | Báo kết quả thực tế và khoảng tin cậy; đạt hay không phụ thuộc dữ liệu. |
| Chặn hành động ngoài phạm vi | Cùng tập đề xuất, nhãn độc lập, đối chứng an toàn không dispatch | Báo số bị chặn, số chặn sai và mẫu; không khẳng định an toàn tuyệt đối. |
| Tránh khôi phục giả | Cùng lượt, health-only và Verifier, nhãn độc lập | Báo ca phát hiện được và ca bỏ sót. |
| Thời gian | Cùng lỗi/workload, baseline và nhánh AIHS gợi ý, probe và cửa sổ như nhau | Báo thay đổi so với baseline của chính môi trường; không so trực tiếp thời gian tuyệt đối với Alibaba. |
| Đa hạ tầng | Test độc lập trên từng môi trường đã tích hợp | Chỉ kết luận trong phạm vi môi trường và kịch bản đã đo. |

Mục tiêu giảm 51% hay vượt AIHS là **giả thuyết**, không phải kết quả. Nếu không đủ mẫu, chưa thể chạy live hoặc kết quả kém hơn baseline, báo đúng điều đó và các bước còn thiếu.
