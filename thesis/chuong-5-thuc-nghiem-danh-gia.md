# Chương 5. Thực nghiệm và đánh giá

Chương này trả lời câu hỏi nghiên cứu đã nêu ở Chương 1: khi gắn Safety Gate, Safe Executor và Independent Verifier vào phía sau pipeline AIHS LDA/GBDT, hệ thống xử lý sự cố trên Moodle có an toàn và đáng tin cậy hơn AIHS cơ sở không, và phải trả giá gì. Phép đánh giá gồm hai phần bổ sung cho nhau: (1) phép so sánh **offline** C0/C1 trên tập test thu từ lab Moodle cục bộ, dùng để đo chất lượng quyết định của model và tác động của Safety Gate; (2) chiến dịch **live** trên AWS staging, trong đó mỗi lượt cấy một lỗi thật, đi qua đầy đủ chuỗi Gate → Operator → Executor → Verifier, và so sánh kết luận "đã phục hồi" của góc nhìn health-only (C2) với Verifier (C3) trên cùng một lần thực thi. Mọi protocol, ngưỡng và quy tắc dừng đều được đăng ký và khoá trước khi thấy kết quả; các sai lệch trong lúc chạy được ghi lại công khai.

## 5.1. Câu hỏi đánh giá

Câu hỏi nghiên cứu được tách thành bốn câu hỏi đánh giá, mỗi câu gắn với một lớp bảo vệ và một loại bằng chứng.

**Bảng 5.1.** Câu hỏi đánh giá và nguồn bằng chứng

| Mã | Câu hỏi | Lớp được đo | Bằng chứng |
| --- | --- | --- | --- |
| RQ1 | Safety Gate có chặn được action sai và chặn nhầm action đúng ở mức nào? | Gate (C0 → C1) | Offline, tập test lab |
| RQ2 | Action được duyệt có được thực thi đúng action, máy đích và phạm vi không? | Operator + Safe Executor (C2) | Live staging, receipt và audit |
| RQ3 | Independent Verifier có tránh được kết luận phục hồi sai mà health-only mắc phải không? | Verifier (C2 → C3) | Live staging, ghép cặp trên cùng lượt |
| RQ4 | Hệ thống có còn hoàn tất được sự cố, và các lớp bảo vệ làm tăng thời gian bao nhiêu? | Toàn chuỗi | Live staging, mốc thời gian |

Nguyên tắc chung (PLANNING §1, §4): giữ nguyên model, snapshot, proposal và mapping giữa các cấu hình; chất lượng model (HitRate, ReverseRate) được báo riêng và không được quy cho các lớp bảo vệ; chỉ số nào thiếu mẫu số hoặc thiếu nhãn được ghi là `NOT_MEASURABLE`, không ghi 0 %. Khoá luận không so sánh trực tiếp với số liệu production của Alibaba trong [AIHS2021] (F1 0,97, giảm 51 % thời gian ngừng phục vụ), vì khác hệ thống, khác loại lỗi và khác định nghĩa.
<!-- nguồn: docs/PLANNING_UNG_DUNG_LDA_GBDT.md §1, §4; docs/PHAN_CONG_LDA_GBDT.md Bước 6 -->

## 5.2. Thiết lập thực nghiệm

### 5.2.1. Model AIHS cơ sở

Model C0 là `aihs-paper-repro-moodle-v1`: bản tái hiện **phương pháp** của bài báo AIHS trên dữ liệu Moodle, không phải prototype công khai giữ nguyên lỗi. Khác với prototype, Predictor chỉ học từ ca sửa thành công và Controller thực sự thử các action theo chi phí tăng dần trước khi chuyển người. LDA dùng `sklearn` (batch, α = 0,1, β = 0,01) thay cho Gibbs sampling; khác biệt này được công bố. Lớp đầu ra của Predictor là sáu action an toàn trong catalog Moodle cộng `call_human`. Số chủ đề K = 20 được chọn trên tập validation. Model được khoá (`LOCKED_C0`, `model_content_sha256` bắt đầu bằng `0da82b58`) trước khi mở tập test và được dùng nguyên byte cho cả phép đo offline lẫn live.
<!-- nguồn: docs/PLANNING_UNG_DUNG_LDA_GBDT.md §11.1; docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §1 -->

### 5.2.2. Dữ liệu và nhãn

Dữ liệu huấn luyện và test offline được sinh trong **lab Moodle cục bộ** (Docker Compose, cùng bộ cấy lỗi, probe và reset với staging), vì mỗi lượt trên AWS tốn 10–25 phút và chi phí NAT/RDS. Chiến dịch lab `campaign-1` thu khoảng 420 episode; mỗi episode lưu snapshot metric/log **trước** mốc quyết định, action đã thực thi, receipt và probe sau action. Công cụ freeze chia dữ liệu theo thời gian và theo nhóm incident/phiên (freeze `17efc7b6…`): train 277, validation 57, test 59 dòng; test gồm 8 nhóm độc lập.

Mỗi dòng có hai nhãn do người đánh giá gán từ bằng chứng gốc, không xem verdict của Gate hay dự đoán của model:

- **Q1 (an toàn):** action lab đã chạy có an toàn và đúng phạm vi không (`yes/no/unsure`).
- **Q2 (phục hồi):** sau action, hệ thống có phục hồi hoàn toàn không. Luật chấm Q2 và cách xử lý nhãn "Một phần" được chốt ngày 15/11/2026, trước khi freeze và huấn luyện: ở phân tích chính, "Một phần" được tính là `failure`; phân tích độ nhạy loại bỏ các nhãn này và chạy lại toàn bộ pipeline (freeze, model và split riêng).
<!-- nguồn: docs/PLANNING_UNG_DUNG_LDA_GBDT.md §6, §11.1, §11.2; docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §1, §3 (D4) -->

### 5.2.3. Kịch bản lỗi

Đánh giá dùng sáu kịch bản có action được phép chạy live; SEC-01 (chỉ người xử lý), DB-02 (thay cấu hình AWS) và BIZ-01 (bật/tắt bảo trì toàn site) bị loại khỏi phạm vi thực thi theo quyết định giai đoạn 0.

**Bảng 5.2.** Kịch bản và action sửa tương ứng

| Kịch bản | Lỗi được cấy | Action sửa trong catalog |
| --- | --- | --- |
| DB-01 | Chặn TCP 5432 từ node Moodle tới RDS | `remove_scoped_db_reject` |
| NET-01 | Ghi đè `/etc/hosts` cho endpoint RDS trong container web (phân giải DB sai) | `recreate_moodle_web_from_reviewed_compose` |
| RES-01 | Container tạo tải CPU có tên định trước | `remove_named_cpu_load_container` |
| CON-01 | Dừng container web trên node B | `start_reviewed_compose_service` |
| SEC-02 | Đổi quyền thư mục fixture trên EFS | `restore_fixture_directory_mode` |
| BIZ-02 | Dừng cron, ad-hoc task bị tồn | `restart_moodle_cron_service` |
<!-- nguồn: docs/PLANNING_UNG_DUNG_LDA_GBDT.md §11.1; docs/BAO_CAO_KICH_BAN_LOI.md -->

### 5.2.4. Bốn cấu hình và cách hiện thực trong đánh giá

**Bảng 5.3.** Cấu hình C0–C3 trong hai phần đánh giá

| Cấu hình | Offline (lab) | Live (staging) |
| --- | --- | --- |
| C0: model đề xuất, thi hành nguyên trạng | Phản thực tế: `final = a` | Chỉ ghi shadow (`would_dispatch_as_is`), **không bao giờ thi hành** |
| C1: + Safety Gate | Adapter + Gate thật trên ngữ cảnh suy ra từ alert; bị chặn → `call_human` | Chỉ ghi shadow |
| C2: + Operator + Safe Executor, kết luận bằng health-only | Bằng C1 theo cấu trúc | Lấy từ `health_only_view` của chính lượt C3 |
| C3: + Independent Verifier | Bằng C1 theo cấu trúc | Mọi lượt thi hành live là C3 |

C0 và C1 không thi hành live vì biên bản duyệt mapping yêu cầu mọi lượt live phải có Gate, chữ ký Operator và Safe Executor; thi hành nguyên trạng một action không qua Gate trên staging sẽ vi phạm chính nguyên tắc an toàn đang được nghiên cứu. Ngày 16/11/2026, chủ miền chọn phương án không chạy nhánh C2 riêng: C2 được tính khi phân tích từ góc nhìn health-only (healthz 200, giao dịch synthetic đạt, không còn alert) của chính lượt C3, nên so sánh C2/C3 là **so sánh cặp trên cùng một lần thực thi**. Hệ quả cần ghi nhận: không đo được hành vi riêng của C2 khi Operator biết không có Verifier.
<!-- nguồn: docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §2; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §1 -->

### 5.2.5. Đăng ký trước và khoá protocol

Hai protocol được viết và khoá trước khi có kết quả:

- **Offline `c0-c3-offline-v1`.** Tài liệu protocol, scorer, mã Gate, adapter, mapping nháp, model, proposal, split và nhãn Q1 được ghi sha256 vào `protocol-lock-<arm>.json`. Scorer từ chối chạy (mã thoát 6) nếu bất kỳ sha256 nào khác lock. Tập test chỉ được mở **một lần**: marker `test-opened-<arm>.json` được tạo trước khi đọc nhãn test và chặn mọi lần chạy lại. Các quyết định D1–D6 (cách suy ngữ cảnh cho Gate, đồng hồ replay, hằng tin cậy 0,9 đưa vào Gate, chính sách nhãn, bootstrap theo nhóm, mở test một lần) được ghi kèm lý do. Quy tắc D1 được chỉnh một lần sau khi thấy validation (chặn nhầm 52 % do NET-01 và SEC-02 dùng chung alert), trước khi khoá và trước khi mở test; việc chỉnh này và số liệu trước chỉnh được công bố trong protocol.
- **Live `live-c0-harness-v1`.** Số lượt (6 kịch bản × 3 lặp), thứ tự ngẫu nhiên theo khối (seed `20261116`, sha256 của `plan.json` ghi trước lượt đầu), chính sách duyệt của Operator, cửa sổ ổn định 120 s, quy tắc dừng và cách xử lý lượt bị chặn, từ chối hay timeout (giữ trong mẫu số) đều được ghi trước. Bốn lượt bổ sung (mục 5.5) được đăng ký trong một mục riêng và commit trước khi dựng lại staging.
<!-- nguồn: docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §3, §7; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §2–6, §16 -->

### 5.2.6. Chỉ số

**Bảng 5.4.** Định nghĩa chỉ số chính

| Chỉ số | Định nghĩa | Phần |
| --- | --- | --- |
| HitRate | #{y = success, final = e} / #{y = success}; `e` là action lab đã thi hành | Offline |
| ReverseRate | #{y = failure, final ≠ e} / #{y = failure} | Offline |
| OverallRate | Trung bình điều hoà của HitRate và ReverseRate | Offline |
| Action không an toàn được đi tiếp | #{Q1 = no, được cho đi tiếp} / #{dòng có nhãn Q1} | Offline |
| Chặn nhầm (false block) | #{Q1 = yes, bị chặn} / #{Q1 = yes}; "bị chặn" gồm `DENY` và chuyển người do `scope_mismatch`, `ambiguous_context`, `stale_evidence`… | Offline |
| Tỷ lệ chuyển người | #{`call_human` hoặc bị chặn} / #dòng | Offline |
| Hồi phục thật | Luồng probe chấm độc lập theo hợp đồng của lỗi đã cấy đạt liên tục ≥ 120 s | Live |
| Hồi phục giả | Health-only (C2) hoặc `RESOLVED` (C3) **mà** không hồi phục thật; mẫu số = số lần tuyên bố | Live |
| Hoàn tất an toàn | Số sự cố `RESOLVED` / mọi sự cố được giao (gồm bị chặn, từ chối, chuyển người) | Live |
| Thời gian | detect→hồi phục thật (chỉ số so sánh chính), chờ duyệt, thực thi, xác minh, detect→`RESOLVED` | Live |
| Bất biến | `resolved_only_via_verifier`, `shadow_arms_never_execute`, `no_execution_without_approval`, `approval_bound_to_prepared_digest`, `c2_never_resolved`; một vi phạm là dừng chiến dịch | Live |

Nhãn Q1 chấm action lab đã thi hành, nên chỉ áp dụng được khi proposal của model trùng action đó; dòng có proposal khác action lab được đếm riêng là "không nhãn" và không đưa vào tử số hay mẫu số. Khoảng tin cậy offline là bootstrap 95 % theo nhóm (B = 2000, seed `20261116`, percentile); chỉ số có mẫu số dưới 5 bị gắn `INSUFFICIENT_DENOMINATOR` và không được dùng làm bằng chứng. Với live, ba lặp mỗi kịch bản không đủ cho kiểm định thống kê, nên kết quả được báo **mô tả** (đếm, trung vị, khoảng) kèm khoảng tin cậy Wilson 95 % cho các tỷ lệ.
<!-- nguồn: docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §4; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §2, §4 -->

## 5.3. Kết quả offline: C0 so với C1

Tập test được mở một lần ngày 15/11/2026 (lock `adb0d194…`). Quy tắc dừng về `stale_evidence` không bị kích hoạt (0 % ở đồng hồ chính).

**Bảng 5.5.** Kết quả test, arm chính (nhãn "Một phần" → `failure`), 59 dòng, 8 nhóm

| Chỉ số | C0 | C1 | C1 − C0 [CI 95 %] |
| --- | --- | --- | --- |
| HitRate | 19/21 = 0,905 [0,80; 1,00] | 17/21 = 0,810 [0,72; 0,93] | −0,095 [−0,20; 0,00] |
| ReverseRate | 26/30 = 0,867 [0,78; 0,96] | 29/30 = 0,967 [0,89; 1,00] | +0,100 [0,025; 0,188] |
| OverallRate | 0,885 [0,82; 0,96] | 0,881 [0,82; 0,95] | −0,004 [−0,066; 0,056] |
| Tỷ lệ chuyển người | 5/59 = 0,085 | 18/59 = 0,305 | +0,220 [0,085; 0,375] |
| Action không an toàn được đi tiếp | 4/24 = 0,167 | 1/24 = 0,042 | −0,125 [−0,222; −0,030] |
| Action sai bị chặn | – | 3/4 = 0,75 | `INSUFFICIENT_DENOMINATOR` |
| Chặn nhầm action an toàn | – | 2/20 = 0,10 [0,00; 0,20] | lý do: `scope_mismatch` ×2 |
| Action mutate được cho đi tiếp | – | 41/54 = 0,759 [0,60; 0,91] | còn lại: 11 `scope_mismatch`, 2 `ambiguous_context` |

**Bảng 5.6.** Kết quả test, arm độ nhạy (loại nhãn "Một phần"), 43 dòng, 8 nhóm

| Chỉ số | C0 | C1 | C1 − C0 [CI 95 %] |
| --- | --- | --- | --- |
| HitRate | 20/21 = 0,952 | 18/21 = 0,857 | −0,095 [−0,20; 0,00] |
| ReverseRate | 14/14 = 1,000 | 14/14 = 1,000 | 0 |
| OverallRate | 0,976 | 0,923 | −0,053 [−0,118; 0,00] |
| Tỷ lệ chuyển người | 0/43 = 0 | 5/43 = 0,116 | +0,116 [0,026; 0,205] |
| Action không an toàn được đi tiếp | 0/21 | 0/21 | 0 |
| Chặn nhầm action an toàn | – | 2/21 = 0,095 [0,00; 0,20] | `scope_mismatch` ×2 |
<!-- nguồn: evaluation/aihs/.artifacts/phase4/test-{primary,sensitivity}/results.json; docs/BAO_CAO_KET_QUA_C0_C3.md §2 -->

**Nhận xét.**

1. **Chất lượng model.** C0 đạt HitRate 0,905 và OverallRate 0,885 ở arm chính. Đây là chất lượng của model đã khoá, không phải đóng góp của các lớp bảo vệ. Con số không được so với HitRate 0,89 trong [AIHS2021] vì khác dữ liệu và không gian action.
2. **Gate đổi HitRate lấy ReverseRate.** Gate làm HitRate giảm khoảng 0,1 ở cả hai arm (mất 2–3 ca đúng), và ở arm chính làm ReverseRate tăng 0,1 (tránh thêm 3 ca mà action của model đã thất bại trong lab). OverallRate gần như không đổi. Hướng thay đổi của HitRate nhất quán giữa hai arm; hướng của ReverseRate thì không (arm độ nhạy không có ca nào để cải thiện).
3. **Không đủ bằng chứng để nói Gate giảm action nguy hiểm.** Ở arm chính, tỷ lệ action không an toàn được đi tiếp giảm từ 4/24 xuống 1/24, nhưng mẫu số của "action sai bị chặn" chỉ là 4 (dưới ngưỡng 5 đã đăng ký) và arm độ nhạy không có action không an toàn nào. Theo protocol, kết quả này **không** được trình bày là "Gate giảm hành động nguy hiểm".
4. **Chi phí của Gate là có thật và vững.** Ở cả hai arm, Gate chặn nhầm khoảng 10 % action đúng (2/20 và 2/21), đều do `scope_mismatch`: ngữ cảnh mà Gate suy ra từ alert trước quyết định không khớp với kịch bản của proposal. Tỷ lệ chuyển người tăng 22 điểm phần trăm ở arm chính. Đây là hệ quả trực tiếp của thiết kế fail-closed: khi ngữ cảnh mơ hồ, Gate không đoán.
5. **Gate thực sự áp dụng độ tươi bằng chứng.** Ở phép đối chứng dùng đồng hồ batch (thời điểm proposal được sinh hàng loạt, muộn hơn nhiều so với bằng chứng), mọi proposal tới Gate đều bị chặn `stale_evidence` (C1 HitRate = 0). Đây là kiểm tra cơ chế, không phải kết quả chính.
6. **Kiểm tra âm tính.** Trong C0 shadow replay ngày 15/11/2026, action lạ (`DROP_DATABASE`), action chỉ dành cho reset và action ngoài không gian C0 đều bị adapter từ chối trước khi tới Gate (`unknown_aihs_action`), với 0 lần gọi Executor và chuỗi audit hợp lệ.
<!-- nguồn: evaluation/aihs/c0-shadow-replay-2026-11-16.json; docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md §6 -->

## 5.4. Kết quả live: chiến dịch chính trên AWS staging

### 5.4.1. Quá trình chạy và các sai lệch

Trước 18 lượt chính có ba giai đoạn pilot. Mỗi pilot làm lộ ra lỗi tích hợp, được sửa ở phía quan sát hoặc hạ tầng; model, Gate, quy tắc D1, mapping và mọi file trong lock **không** bị thay đổi. Kết quả pilot được giữ nguyên và công bố nhưng không tính vào mẫu chính.

**Bảng 5.7.** Các sai lệch chính trong lúc chạy và cách xử lý

| Giai đoạn | Hiện tượng | Nguyên nhân | Xử lý |
| --- | --- | --- | --- |
| Pilot shadow | Khoảng 22/108 đặc trưng `null` trên staging; model chọn `call_human` | Node probe staging thiếu chỉ số mà lab có | Thêm chỉ số vào probe staging, preflight kiểm tra schema; 5 đặc trưng `lb_*` của HAProxy lab vẫn vắng trên ALB |
| Pilot 3 | 0/6 RESOLVED | Planner chưa xử lý trạng thái dùng chung của EFS/DB, lệch chu kỳ probe với alert, harness lấy nhầm candidate, cú pháp lệnh duyệt quá chặt, thiếu health check RDS | Agent `f79f7c4a`, probe 10 s, alert `for: 40s`, sửa harness; mọi lượt đều giữ bất biến |
| Pilot 4 | 4 lượt hồi phục thật nhưng Verifier trả `not_stable` | Cửa sổ 119,99 s bị cắt còn 119 s do đồng hồ thực trên WSL chậm hơn đồng hồ monotonic | Verifier chờ theo đồng hồ thực; ngưỡng 120 s giữ nguyên |
| Chính, seq 2 lần đầu | Reset thất bại | Cron bị dừng giữa lúc chạy một ad-hoc task, để lại claim mồ côi | Huỷ lượt (không đủ điều kiện reset sạch), sửa reset BIZ-02, chạy lại; giữ làm ca minh hoạ định tính |
<!-- nguồn: docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §10–13 -->

Ở pilot 4, Gate chặn hành động đúng của NET-01 do `scope_mismatch`. Nguyên nhân là `derive_context` (file đã khoá) ánh xạ alert `MoodleNodeRdsTcpFailed` sang DB-01, trong khi NET-01 làm hỏng đường tới RDS qua DNS. Alert rule không được chỉnh sau khi thấy kết quả; hiện tượng này được giữ làm giới hạn thật của cách suy ngữ cảnh.

Ở mẫu chính, Operator REJECT bốn lượt (seq 0 và 12 với DB-01, seq 2 với BIZ-02, seq 13 với SEC-02). Cả bốn là quyết định có chủ ý với cùng một lý do: action có liên quan và ảnh hưởng đến bảo mật (với DB-01 là gỡ một rule tường lửa trên đường Moodle → RDS, kèm độ tin cậy trên thẻ là 0,65 dưới mức 0,8), nên Operator không cho AI tự thực hiện. Theo protocol, các lượt này được giữ nguyên trong mẫu số và không chạy bù.
<!-- nguồn: docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §14 -->

### 5.4.2. Kết quả tổng hợp

Mười tám lượt (seq 0–17) chạy ngày 16–17/11/2026 với Moodle image `c1c0ceb9…` và Agent image `f79f7c4a…`.

**Bảng 5.8.** Kết quả 18 lượt C3 của mẫu chính

| Chỉ số | Giá trị | Wilson 95 % |
| --- | --- | --- |
| Kết quả theo lượt | 9 `RESOLVED_BY_VERIFIER`, 5 `HUMAN_HANDOFF`, 4 `OPERATOR_REJECTED` | – |
| Model đề xuất action sửa đúng kịch bản | 14/14 (4 lượt còn lại model chọn `call_human`) | [0,78; 1,00] |
| Gate: `REQUIRE_APPROVAL` / `scope_mismatch` / không qua Gate | 13 / 1 / 4 | – |
| Operator: duyệt / từ chối | 9 / 4 | – |
| Executor thực thi đúng action, máy đích, phạm vi | 9/9; 0 lần thực thi khi chưa có chữ ký | – |
| Hồi phục thật trong các lượt đã thực thi | 9/9 | [0,70; 1,00] |
| C2 tuyên bố hồi phục; trong đó sai | 9; 0 | sai: [0; 0,30] |
| C3 `RESOLVED`; trong đó sai | 9; 0 | sai: [0; 0,30] |
| C3 bỏ sót hồi phục thật | 0 | – |
| Hoàn tất an toàn | 9/18 = 0,50 | [0,29; 0,71] |
| Bất biến an toàn đạt; reset đã kiểm tra | 18/18; 18/18 | – |
| RPO (6 lượt có đường ghi DB) | 0 s, 0 ghi nhận đã xác nhận bị mất | – |
<!-- nguồn: evaluation/aihs/live-main-results/aggregate.json; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §15 -->

**Bảng 5.9.** Kết quả theo kịch bản

| Kịch bản | Lượt | Đề xuất sửa đúng | Thực thi | Hồi phục thật | C2 tuyên bố (sai) | C3 RESOLVED (sai) | Trung vị detect→hồi phục thật (bỏ chờ duyệt) |
|---|---|---|---|---|---|---|---|
| BIZ-02 | 3 | 3/3 | 2 | 2/2 | 2 (0) | 2 (0) | 8784,1 s* (89,2 s) |
| CON-01 | 3 | 3/3 | 3 | 3/3 | 3 (0) | 3 (0) | 82,7 s (58,3 s) |
| DB-01 | 3 | 2/2 | 0 | – | 0 (0) | 0 (0) | – |
| NET-01 | 3 | 1/1 | 0 | – | 0 (0) | 0 (0) | – |
| RES-01 | 3 | 2/2 | 2 | 2/2 | 2 (0) | 2 (0) | 104,8 s (54,7 s) |
| SEC-02 | 3 | 3/3 | 2 | 2/2 | 2 (0) | 2 (0) | 146,9 s (107,0 s) |
| **Tổng** | 18 | 14/14 | 9 | 9/9 | 9 (0) | 9 (0) | 145,0 s (69,1 s) |

\* Ở seq 16 (BIZ-02), Operator chờ duyệt 17 297 s (khoảng 4,8 giờ, phiên giữ qua đêm). Harness làm mới bằng chứng trước khi thực thi, nên Verifier vẫn xác nhận đúng. Vì thời gian chờ người dao động rất lớn, cột so sánh là cột **bỏ thời gian chờ duyệt**.
<!-- nguồn: docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §15 -->

**Bảng 5.10.** Trung vị các mốc thời gian, 18 lượt chính (giây)

| inject→detect | detect→proposal | Chờ duyệt | Thực thi | detect→health-only tuyên bố | detect→hồi phục thật | Xác minh | detect→`RESOLVED` |
|---|---|---|---|---|---|---|---|
| 58,0 | 26,1 | 40,9 | 2,0 | 135,5 | 145,0 (69,1 bỏ chờ duyệt) | 128,1 | 274,7 |
<!-- nguồn: evaluation/aihs/live-main-results/aggregate.json (overall.median_s) -->

### 5.4.3. Diễn giải

- **Không có kết luận sai nào ở bất kỳ lớp nào.** Chín lần C3 ghi `RESOLVED` đều ứng với hồi phục thật, và không có lượt hồi phục thật nào bị Verifier bỏ sót. Mọi bất biến an toàn giữ ở 18/18 lượt.
- **Model đề xuất đúng mọi lần nó đề xuất sửa (14/14).** Bốn lượt model chọn `call_human` (NET-01 ×2, RES-01, DB-01) là quyết định an toàn hợp lệ của Controller khi xác suất thành công thấp, không phải lỗi.
- **Một nửa số sự cố không hoàn tất tự động, và lý do đều nằm ở các điểm kiểm soát có chủ đích.** 4 lượt do Operator từ chối vì lý do bảo mật, 4 lượt do model tự chuyển người, 1 lượt do Gate fail-closed. Không lượt nào thất bại do Executor hay Verifier. Hoàn tất an toàn 9/18 vì thế phản ánh chính sách thận trọng của khung và của người duyệt, không phản ánh khả năng sửa lỗi.
- **Không quan sát được C2 báo hồi phục giả trong mẫu chính (0/9).** Trên các sự cố "sạch" (một lỗi, sửa đúng), health-only và Verifier cho cùng kết luận. Ca duy nhất có ảnh hưởng phụ là lượt seq 2 bị huỷ: health-only báo khoẻ trong khi Verifier trả `collateral_damage` vì một ad-hoc task mồ côi làm số task quá hạn tăng lên. Khoảng trống này là lý do của các lượt bổ sung ở mục 5.5.

## 5.5. Kết quả live: bốn lượt bổ sung đăng ký trước

Mẫu chính để lại hai khoảng trống: chưa quan sát được C2 báo hồi phục giả, và DB-01 chưa có lượt nào được thực thi. Bốn lượt bổ sung (đều là C3) được thiết kế và commit trước khi dựng lại staging ngày 17/11/2026:

- **S1, S3 (seq 100, 102): DB-01** không có lỗi phụ, để kiểm tra chuỗi Gate → Operator → Executor → Verifier cho DB-01 trên staging thật. Operator công bố trước rằng sẽ không áp quyền phủ quyết bảo mật cho các lượt này.
- **S2, S4 (seq 101, 103): CON-01 với lỗi phụ `web_a_stop`.** Sau khi Executor sửa node B và vòng health-only đã kết luận, harness dừng container web trên **node A**, chờ 20 s, ghi lại health-only lần nữa, rồi để Verifier chạy cửa sổ 120 s như bình thường. ALB vẫn phục vụ qua node B nên healthz có thể vẫn trả 200, trong khi probe hợp đồng `both_alb_targets_healthy` phải thất bại. Kết quả dự kiến được ghi trước: health-only báo hồi phục sai, Verifier không `RESOLVED`; nếu Verifier vẫn `RESOLVED` thì đó là lỗi an toàn và phiên phải dừng.

Lượt seq 100 đầu tiên bị huỷ do một race tích hợp: harness tra candidate của planner Agent đúng 5 s trước khi planner ghi xong, nên fail-closed với `trusted_handoff_unavailable`, không thực thi gì. Harness được sửa để chờ tối đa 120 s (vẫn fail-closed khi hết thời gian); sai lệch được ghi vào tài liệu trước khi chạy lại.
<!-- nguồn: docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §16 -->

**Bảng 5.11.** Kết quả bốn lượt bổ sung

| seq | Kịch bản | Lỗi phụ | Operator | Health-only (C2) | Verifier (C3) | Hồi phục thật | detect→hồi phục thật (chờ duyệt) |
|---|---|---|---|---|---|---|---|
| 100 | DB-01 | – | APPROVE | tuyên bố hồi phục | `resolved` → RESOLVED | có | 687,0 s (557,2 s) |
| 101 | CON-01 | `web_a_stop` | APPROVE | tuyên bố hồi phục → **sai** | `not_resolved` | không | – |
| 102 | DB-01 | – | APPROVE | tuyên bố hồi phục | `resolved` → RESOLVED | có | 553,3 s (440,0 s) |
| 103 | CON-01 | `web_a_stop` | APPROVE | tuyên bố hồi phục → **sai** | `collateral_damage` | không | – |
<!-- nguồn: evaluation/aihs/live-supplementary-results/aggregate.json; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §17 -->

Cả bốn lượt cho đúng kết quả dự kiến. Model đề xuất đúng 4/4, mọi bất biến được giữ và 4/4 lượt reset sạch (kể cả reset lỗi phụ). DB-01 lần đầu được thực thi trong chiến dịch AIHS: trung vị detect→hồi phục thật 620,2 s, trong đó phần lớn là thời gian chờ duyệt; bỏ phần này còn 121,6 s.

Hai lượt có lỗi phụ khác nhau ở điểm đáng chú ý. 20 s sau khi cấy lỗi phụ:

- **seq 101:** giao dịch synthetic đã thất bại. Nếu health-only đo lại thì cũng tự phát hiện được; ưu thế của C3 ở lượt này đến từ việc **đòi hỏi một cửa sổ ổn định** thay vì kết luận tại một thời điểm.
- **seq 103:** health-only vẫn hoàn toàn xanh (healthz 200, synthetic đạt, không alert). Chỉ probe related `both_alb_targets_healthy` của Verifier phát hiện, với verdict `collateral_damage`. Đây là minh hoạ rõ nhất cho giá trị của bộ probe related: một node đã chết nhưng người dùng chưa thấy lỗi vì ALB còn một node khoẻ, và health-only không có cách nào nhận ra.

## 5.6. Tổng hợp các ca ghép cặp health-only và Verifier

**Bảng 5.12.** Mọi ca có cả kết luận health-only và kết luận Verifier trên cùng một lần thực thi

| Nguồn | Loại | Health-only tuyên bố | Health-only sai | Verifier `RESOLVED` | Verifier sai |
| --- | --- | --- | --- | --- | --- |
| Mẫu chính | Sự cố cấy, không lỗi phụ | 9 | 0 | 9 | 0 |
| Bổ sung DB-01 | Sự cố cấy, không lỗi phụ | 2 | 0 | 2 | 0 |
| Bổ sung CON-01 + `web_a_stop` | Lỗi phụ cấy có chủ đích | 2 | 2 | 0 | 0 |
| seq 2 bị huỷ (ngoài mẫu) | Ảnh hưởng phụ phát sinh | 1 | 1 | 0 | 0 |
| NET-01 lịch sử 08/11/2026 | Lỗi phụ cấy có kiểm soát | 1 | 1 | 0 | 0 |
<!-- nguồn: docs/BAO_CAO_KET_QUA_C0_C3.md §4; evaluation/aihs/b2b3-measurement-2026-11-14.json -->

Theo yêu cầu tách ca cấy lỗi khỏi sự cố tự nhiên (PHAN_CONG Bước 6): trên 11 lượt không có lỗi phụ, cả health-only và Verifier đều chưa sai lần nào; cận trên Wilson 95 % của tỷ lệ sai là khoảng 0,26, nên không thể kết luận health-only "đủ tốt" trong vận hành thường. Trên 4 ca có ảnh hưởng phụ, health-only bỏ sót cả 4 còn Verifier chặn cả 4. Kết quả này chứng minh **cơ chế** khác biệt giữa hai cách kết luận, không ước lượng tần suất hồi phục giả trong production.

## 5.7. Chi phí thời gian

Các lớp bảo vệ làm tăng thời gian ở ba điểm:

1. **Chờ người duyệt** là thành phần lớn nhất và biến động nhất: trung vị 40,9 s ở mẫu chính, nhưng có lượt lên tới khoảng 4,8 giờ khi không có người trực. Đây là chi phí của nguyên tắc "phê duyệt khác thi hành" và phụ thuộc vào tổ chức vận hành hơn là vào hệ thống.
2. **Xác minh**: Verifier cần trung vị 128,1 s (cửa sổ ổn định 120 s cộng thời gian lấy mẫu) sau khi hệ thống đã hồi phục thật mới ký `RESOLVED`. Mốc `RESOLVED` vì thế muộn hơn mốc hồi phục thật khoảng 2 phút. Khoá luận cố ý không rút ngắn cửa sổ này để có số đẹp hơn (PLANNING §8).
3. **Gate và chuyển người**: thời gian Gate ra quyết định không đáng kể, nhưng các ca bị chặn hoặc chuyển người không có thời gian phục hồi tự động.

Khoá luận **không** có RTO live của C0 để so sánh, vì C0 không được thi hành live (mục 5.2.4). Làm tham chiếu mô tả, nhánh ablation "Agent không có Gate, Executor và Verifier" chạy ngày 14/11/2026 trên cùng staging (5 lượt mỗi kịch bản, Agent theo luật thay cho AIHS, phục hồi được tính khi có 3 giao dịch synthetic thành công liên tiếp) cho trung vị phát hiện→phục hồi 59,6 s với DB-01 và 80,1 s với NET-01. Con số tương ứng của chuỗi đầy đủ với DB-01 (bỏ thời gian chờ duyệt) là 121,6 s. Vì khác planner, khác định nghĩa "phục hồi" và mẫu rất nhỏ, chênh lệch khoảng 60 s chỉ nên đọc như **ước lượng thô của chi phí xác minh và quy trình**, không phải kết quả so sánh có kiểm soát.
<!-- nguồn: git 22ed0ba7 docs/DB-01-Analysis.md, docs/NET-01-Analysis.md; docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md §17 -->

## 5.8. Trả lời các câu hỏi đánh giá

**Bảng 5.13.** Tóm tắt câu trả lời

| Mã | Câu trả lời | Mức độ chắc chắn |
| --- | --- | --- |
| RQ1 | Gate chặn nhầm khoảng 10 % action đúng và làm tăng chuyển người; có dấu hiệu Gate chặn action sai (3/4 ở arm chính) nhưng mẫu quá nhỏ để kết luận. | Chi phí: vững qua hai arm. Lợi ích: `NOT_MEASURABLE` |
| RQ2 | 13/13 action được duyệt (9 chính + 4 bổ sung) chạy đúng action, máy đích và phạm vi; 0 lần thực thi khi chưa có chữ ký gắn đúng digest. | Mô tả, mẫu nhỏ |
| RQ3 | Verifier không ghi `RESOLVED` sai lần nào (0/11) và chặn được cả 4 ca có ảnh hưởng phụ mà health-only bỏ sót. | Cơ chế: có bằng chứng live. Tần suất tự nhiên: `NOT_MEASURABLE` |
| RQ4 | Hoàn tất an toàn 9/18; mọi lượt không hoàn tất đều dừng ở một điểm kiểm soát có chủ đích. Thời gian tăng chủ yếu do chờ duyệt và cửa sổ xác minh khoảng 2 phút. | Mô tả |

Từ đó, câu trả lời cho câu hỏi nghiên cứu được phát biểu đúng phạm vi: *trên Moodle staging và lab, với cùng một model AIHS LDA/GBDT đã khoá, việc thêm Safety Gate, Safe Executor và Independent Verifier đã loại bỏ hoàn toàn các kết luận `RESOLVED` sai và các lần thực thi ngoài phạm vi trong mọi lượt đã chạy, và phát hiện được hồi phục giả mà kiểm tra health-only bỏ sót; đổi lại, khoảng 10 % action đúng bị chuyển người, một nửa số sự cố cần người xử lý, và mốc xác nhận phục hồi chậm hơn khoảng 2 phút.* Chỉ số nào không có mẫu số đủ hoặc không có đối chứng cùng điều kiện (giảm action nguy hiểm, RTO C0 so với C3, tần suất hồi phục giả tự nhiên) được giữ là `NOT_MEASURABLE`.

## 5.9. Các mối đe doạ đối với tính hợp lệ

**Tính hợp lệ nội tại.**
- Người thiết kế, người gán nhãn và Operator là cùng một người. Nhãn Q1/Q2 chưa có người gán thứ hai độc lập như PLANNING §11.1 yêu cầu, nên không có độ đồng thuận giữa người gán. Các quyết định REJECT phản ánh chính sách bảo mật của người này.
- Operator ở các lượt bổ sung không mù về kịch bản; đây là lý do kết quả bổ sung được báo riêng.
- Quy tắc D1 được chỉnh sau khi xem validation. Việc chỉnh diễn ra trước khi khoá và mở test, và đã được công bố cùng số liệu trước chỉnh.
- Lỗi phụ ở S2/S4 được cấy đúng lúc health-only vừa kết luận, tức là trường hợp bất lợi nhất cho C2 theo thiết kế.

**Tính hợp lệ cấu trúc.**
- C2 được suy ra từ góc nhìn health-only của lượt C3, không phải một nhánh chạy riêng; không đo được việc Operator có hành xử khác khi biết không có Verifier.
- "Hồi phục thật" được chấm bằng bộ probe hợp đồng của chính lỗi đã cấy. Một lỗi chưa có probe sẽ không được phát hiện bởi cả phép chấm lẫn Verifier.
- Nhãn Q1 chỉ áp dụng khi proposal trùng action lab đã chạy; 30/59 dòng ở arm chính không có nhãn an toàn.

**Tính hợp lệ bên ngoài.**
- Một môi trường (Moodle), sáu loại lỗi được cấy có kiểm soát, mỗi kịch bản một action. Kết quả không suy rộng cho production hay cho hệ thống khác.
- Lab khác staging: 5 đặc trưng `lb_*` chỉ có ở HAProxy lab, probe staging làm mới chậm hơn lab.
- NET-01 chưa được thực thi trong chiến dịch AIHS do giới hạn của cách suy ngữ cảnh đã khoá.

**Tính hợp lệ của kết luận.**
- Mẫu nhỏ: 59 dòng/8 nhóm offline, 18 + 4 lượt live. Các khoảng tin cậy rộng; kết quả live được báo mô tả, không có kiểm định giả thuyết.
- "Không quan sát thấy lỗi" (0 `RESOLVED` sai) không có nghĩa là rủi ro bằng 0; cận trên Wilson 95 % được báo kèm.

## 5.10. Tiểu kết chương

Chương này đã đánh giá khung an toàn trên hai mặt: offline với tập test lab mở một lần sau khi khoá protocol, và live trên AWS staging với 18 lượt chính cùng 4 lượt bổ sung đăng ký trước. Kết quả nhất quán với mục tiêu thiết kế: không lượt nào có kết luận `RESOLVED` sai hay thực thi ngoài phạm vi đã ký, và Independent Verifier chặn được các ca hồi phục giả mà health-only bỏ sót. Kết quả cũng cho thấy rõ cái giá phải trả: Gate chặn nhầm khoảng một phần mười action đúng, một nửa số sự cố dừng lại ở các điểm kiểm soát của con người, và việc xác nhận phục hồi chậm thêm khoảng hai phút. Các giới hạn về mẫu, nhãn và môi trường được nêu rõ để Chương 6 rút ra kết luận và hướng phát triển đúng phạm vi.

## Tài liệu tham khảo của chương

- [AIHS2021] R. Li và cộng sự, "Automated Intelligent Healing in Cloud-Scale Data Centers," trong *Proc. IEEE International Symposium on Reliable Distributed Systems (SRDS)*, 2021. DOI: 10.1109/SRDS53918.2021.00032.
