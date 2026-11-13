# Kế hoạch ứng dụng LDA và GBDT (theo AIHS của Alibaba) cho Moodle AIOps

**Cập nhật:** 12/11/2026 · **Trạng thái:** kế hoạch; chưa có mô hình LDA/GBDT nào được huấn luyện hay chạy trong hệ thống.
**Dành cho:** người thực hiện khoá luận và hội đồng đánh giá.

Tài liệu này gộp phân tích ứng dụng LDA/GBDT, đối chiếu thời gian khôi phục DB-01 với AIHS, và các bằng chứng đo Safety
Gate thực tế. Kiến trúc Agent/Gate/Executor/Verifier nằm ở
[BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN](BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN.md); cách chạy kịch bản ở
[BAO_CAO_KICH_BAN_LOI](BAO_CAO_KICH_BAN_LOI.md).

---

## 1. Mục tiêu và nguyên tắc so sánh

**Mục tiêu:** đưa phương pháp của AIHS (LDA biến log thành đặc trưng, GBDT chọn và đánh giá hành động sửa) vào hệ thống
Moodle, và chứng minh hệ thống **cải thiện so với AIHS** trên các chỉ số so sánh được.

**Nguyên tắc để kết luận hợp lệ:**
- **Không** so trực tiếp số đo của hai hệ thống khác nhau. 695 giây (Moodle DB-01) và 9,8 giờ (server Alibaba) khác đối
  tượng, loại lỗi, định nghĩa và mẫu; không được tính "Moodle tốt hơn AIHS x %".
- So sánh **cùng phương pháp trên cùng dữ liệu**: tái hiện AIHS trên dữ liệu Moodle, và chạy prototype công khai của
  Alibaba cùng mô hình của khoá luận trên cùng dữ liệu tổng hợp.
- Mức giảm thời gian khôi phục chỉ so với **baseline của chính hệ thống** (thủ công, Agent theo luật), giống cách AIHS
  báo −51 % so với baseline của họ.
- ML **chỉ gợi ý**; Safety Gate, chữ ký của người, Executor allowlist và Independent Verifier giữ nguyên.

---

## 2. AIHS: kiến trúc và chỉ số đã công bố

Rui Li và cộng sự, *Automated Intelligent Healing in Cloud-Scale Data Centers*, IEEE SRDS 2021,
DOI [10.1109/SRDS53918.2021.00032](https://doi.org/10.1109/SRDS53918.2021.00032),
[bản PDF](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf).

**Kiến trúc:**
- **Mapper:** LDA (Gibbs sampling) coi log giám sát (165 thuộc tính, mỗi thuộc tính 6 mức) là văn bản, biến thành vector
  chủ đề. Đã so với TF-IDF và BERT; LDA được chọn.
- **Predictor:** GBDT đa lớp, học từ các ca sửa **thành công**, chọn 1 trong 6 hành động: NOP (chờ, kiểm tra lại),
  MSR (khởi động lại dịch vụ quản lý), ER, RB (reboot), RI, RMA (sửa phần cứng bằng tay).
- **Controller:** 6 bộ GBDT nhị phân, mỗi hành động một bộ, học từ cả ca thành công lẫn thất bại. Nếu hành động được đề
  xuất bị dự đoán thất bại, thử lần lượt các hành động theo **chi phí tăng dần**; không còn hành động nào thì gọi người.
- **Triển khai:** huấn luyện offline, cập nhật hằng tháng. Ở production chỉ được tự chạy 3 hành động: NOP, MSR, RB.
- **Không có** Safety Gate độc lập hay Verifier độc lập; "sửa thành công" dựa trên bản ghi sửa lỗi.

**Dữ liệu và phương pháp:** 4,25 triệu bản ghi sửa lỗi (2,22 triệu thành công, 2,03 triệu thất bại) từ 5.100 cluster,
chia 80/20 theo thời gian. Mất cân bằng mạnh: hơn 99 % là NOP hoặc MSR. Cùng một lỗi có thể sửa bằng nhiều hành
động (lỗi Ping: 67,5 % MSR, 29,5 % NOP, 3 % RB).

| Chỉ số | Giá trị AIHS | Nguồn |
|---|---|---|
| Predictor LDA + GBDT: precision / recall / F1 | 0,97 / 0,97 / **0,97** | Bảng II, III |
| Controller LDA + GBDT: precision / recall / F1 | 0,87 / 0,94 / **0,90** | Bảng II |
| HitRate (đoán trúng hành động của ca thành công) | **0,89** | Thí nghiệm B.2 |
| ReverseRate (tránh hành động của ca thất bại) | **0,98** | B.2 |
| OverallRate (trung bình điều hoà của hai chỉ số trên) | **0,93** | B.2 |
| So sánh cùng dữ liệu: MRU / Ticket-based (TF-IDF + GBDT) | HitRate 0,87 / 0,90; ReverseRate < 0,45 | B.2 |
| Tỉ lệ sửa thành công ở production (7 tháng, 33,7 triệu lỗi) | **92,4 %**, so với 65,3 % của bộ luật cũ | B.3 |
| Thời gian server không phục vụ (một nhóm cluster) | 20,1 → 9,8 giờ, **−51 %** so với baseline của họ | B.3 |
| Thời gian ra quyết định | 7 ms (bộ luật < 1 ms) | B.3 |
| Thời gian huấn luyện | LDA 6,65 giờ; GBDT < 3 phút | A.4 |

Mã production và trace production **không công bố**; chỉ có
[prototype Python với dữ liệu tổng hợp](https://github.com/alibaba-edu/dcbrain/tree/master/AIHS_prototype).

### 2.1. LDA và GBDT hoạt động thế nào

Hai kỹ thuật này làm hai việc khác nhau và nối tiếp nhau: **LDA nén bằng chứng thô thành vài con số có nghĩa**, sau đó
**GBDT dùng các con số đó để quyết định**.

```text
log + metric thô (hàng nghìn dòng)
   ─► LDA  ─► vector chủ đề θ, ví dụ [0,71 "mất kết nối DB"; 0,20 "tải bình thường"; 0,09 "DNS"]
   ─► GBDT Predictor  ─► xếp hạng hành động: remove_scoped_db_reject 0,82; recreate_web 0,11; chuyển người 0,07
   ─► GBDT Controller ─► P(remove_scoped_db_reject thành công) = 0,93 ─► đủ ngưỡng ─► đưa sang Safety Gate
```

#### LDA (Latent Dirichlet Allocation): đọc log thành "chủ đề"

**Vấn đề nó giải quyết.** Một incident sinh ra hàng trăm dòng log và hàng chục metric. Mỗi dòng log lại khác nhau một
chút (IP, PID, thời gian), nên đếm từng dòng thì quá nhiều chiều và quá thưa để mô hình học được.

**Ý tưởng.** LDA giả định mỗi "văn bản" là hỗn hợp của một vài **chủ đề**, và mỗi chủ đề là một tập từ hay đi cùng
nhau. LDA không cần nhãn: nó tự tìm các nhóm từ đồng xuất hiện.
- **Văn bản** = toàn bộ bằng chứng của một incident trong cửa sổ trước thời điểm quyết định.
- **Từ** = template log đã chuẩn hoá (ví dụ `pg_connect <IP>:<NUM> timeout`), hoặc metric đã rời rạc hoá thành token
  (ví dụ `rds_tcp_a=0`, `cpu_b=high`). AIHS dùng cách thứ hai: 165 thuộc tính giám sát, mỗi thuộc tính 6 mức, coi như
  từ vựng.
- **Đầu ra** cho mỗi incident là vector θ gồm K số cộng lại bằng 1 (ví dụ 71 % chủ đề "mất kết nối DB").

**Ví dụ trên Moodle.** Sau khi học, LDA có thể tìm ra:

| Chủ đề | Từ nổi bật | Kịch bản thường gặp |
|---|---|---|
| 1 | `pg_connect`, `timeout`, `rds_tcp=0`, `synthetic=0` | DB-01 |
| 2 | `getaddrinfo`, `could not translate host`, `rds_tcp=1` | NET-01 |
| 3 | `cpu=high`, `slow request`, `web_running=1` | RES-01 |
| 4 | `container exited`, `web_running=0`, `alb_unhealthy` | CON-01 |

Tên chủ đề là do người đọc đặt sau khi xem từ nổi bật; LDA chỉ trả về con số. Một incident mơ hồ có thể ra θ = 0,45
chủ đề 1 + 0,40 chủ đề 2. Đó chính là thông tin bộ luật hiện tại không biểu diễn được.

**Tham số chính.**
- **K** (số chủ đề): ít quá thì gộp các lỗi khác nhau vào một chủ đề; nhiều quá thì mỗi chủ đề chỉ còn nhiễu. Chọn bằng
  độ coherence trên tập validation, thử K = 5, 10, 20.
- **α = 0,1**: mỗi incident chỉ gồm ít chủ đề. **β = 0,01**: mỗi chủ đề chỉ gồm ít từ. Giá trị lấy theo AIHS.
- **Gibbs sampling**: cách ước lượng mà AIHS dùng (thư viện `tomotopy`). `sklearn` dùng variational Bayes; dùng được,
  nhưng phải ghi là khác bài gốc.

**Vì sao AIHS chọn LDA** thay cho TF-IDF hay BERT: TF-IDF cho vector thưa, nhiều chiều, không gom được từ đồng nghĩa;
BERT nặng, khó giải thích. LDA cho vector ngắn và đọc hiểu được. Trong bài, LDA + GBDT đạt F1 0,97, cao hơn TF-IDF và
BERT.

**Giới hạn cần nói rõ.** LDA bỏ qua thứ tự dòng log (chỉ đếm từ). Từ vựng phụ thuộc hệ thống: chủ đề học trên Moodle không
dùng được cho ERPNext. Phải fit **chỉ trên tập train** rồi đóng băng; fit trên cả tập test là rò rỉ dữ liệu.

#### GBDT (Gradient Boosted Decision Trees): ra quyết định từ vector

**Cây quyết định** là chuỗi câu hỏi có/không, ví dụ:

```text
θ[chủ đề 1] > 0,5 ?
 ├─ có: rds_tcp_a = 0 và rds_tcp_b = 0 ? ── có: remove_scoped_db_reject
 └─ không: θ[chủ đề 2] > 0,4 ? ── có: recreate_moodle_web_from_reviewed_compose
```

Một cây đơn lẻ dễ học thuộc dữ liệu train và đoán sai ca mới.

**Boosting** sửa điều đó bằng cách xây **nhiều cây nhỏ nối tiếp**. Cây thứ nhất đoán thô; cây thứ hai học phần sai số
còn lại của cây thứ nhất; cây thứ ba học phần còn sai sau hai cây đầu, cứ thế vài trăm cây. "Gradient" là cách tính
phần sai số cần sửa ở mỗi vòng. Kết quả cuối là tổng đóng góp của mọi cây, đổi thành xác suất cho từng lớp.

**Vì sao hợp với bài toán này.**
- Dữ liệu dạng bảng (θ của LDA + metric + loại alert), số mẫu vài trăm tới vài nghìn: GBDT thường mạnh hơn mạng nơ-ron ở
  quy mô này.
- Không cần chuẩn hoá thang đo; xử lý được quan hệ phi tuyến như "CPU cao **và** web vẫn chạy".
- Huấn luyện nhanh (AIHS: dưới 3 phút với 4,25 triệu bản ghi) và dự đoán vài ms.
- **Giải thích được**: feature importance hoặc SHAP cho biết đặc trưng nào đẩy quyết định. Có thể hiện lên Telegram
  cho Operator, ví dụ "gợi ý vì `rds_tcp=0` trên cả hai node và chủ đề 1 = 0,71".

**Hai cách dùng GBDT trong hệ thống:**

| | Predictor | Controller |
|---|---|---|
| Câu hỏi | Nên làm hành động nào? | Hành động X có sửa được không? |
| Loại | Đa lớp: một lớp cho mỗi `action_id` trong catalog, cộng lớp "chuyển người" | Nhị phân, một mô hình cho mỗi hành động |
| Mỗi mẫu | Một incident | Một cặp (incident, hành động đã thử) |
| Nhãn | Hành động rẻ nhất mà Verifier xác nhận đã sửa | 1 nếu Verifier xác nhận, 0 nếu thất bại |
| Học từ | Ca thành công | Cả ca thành công lẫn thất bại |

Cách phối hợp (giống AIHS): Predictor đề xuất; Controller kiểm tra lại. Xác suất thành công dưới ngưỡng thì thử hành
động rẻ kế tiếp trong bảng chi phí; hết hành động thì chuyển người.

**Tham số chính** (chỉnh trên validation): số cây (`n_estimators`), tốc độ học (`learning_rate`), độ sâu hoặc số lá mỗi
cây (`num_leaves`), trọng số lớp để xử lý mất cân bằng. AIHS có hơn 99 % mẫu là NOP/MSR; nếu không cân trọng số, mô
hình chỉ cần luôn đoán lớp đông nhất là đã đạt accuracy cao.

**Giới hạn cần nói rõ.** GBDT chỉ chọn trong các lớp đã thấy lúc huấn luyện, nên không tự sinh hành động ngoài catalog.
Xác suất của nó chưa chắc đã đúng thang (0,9 chưa hẳn là đúng 90 % số lần); phải kiểm tra calibration trước khi dùng
làm ngưỡng. Dữ liệu trôi (phiên bản Moodle, tải khác) làm mô hình kém đi, nên phải huấn luyện lại định kỳ (AIHS: hằng
tháng).

#### Vị trí trong hệ thống

LDA và GBDT chỉ thay **bước chọn hành động** của `planner.py`, nơi hiện đang tra bảng cố định. Mọi bước sau giữ
nguyên: hành động vẫn phải nằm trong catalog và đúng phạm vi kịch bản, Safety Gate vẫn kiểm tra, Operator vẫn ký,
Executor vẫn chỉ chạy allowlist, và chỉ Independent Verifier mới xác nhận đã khôi phục. Mô hình sai thì hậu quả xấu
nhất là một gợi ý tồi bị Gate hoặc Operator chặn, chứ không phải một lệnh nguy hiểm được chạy.

---

## 3. Hiện trạng hệ thống Moodle (baseline)

### 3.1. Luồng hiện tại

Alert → Agent thu bằng chứng Prometheus → **chẩn đoán theo luật** → đề xuất một hành động trong catalog → Safety Gate
→ Operator duyệt (Telegram) → Executor → Independent Verifier (3 mẫu ổn định) → `RESOLVED`. Mỗi kịch bản hiện ứng với
**đúng một** hành động; chưa có bước chọn giữa nhiều hành động.

### 3.2. Mốc thời gian DB-01 ngày 08/11/2026 (run `DB-01-20261108T084511Z`)

| Mốc (UTC) | Thời điểm | Khoảng |
|---|---|---:|
| Cấy lỗi (`iptables` chặn TCP 5432 trên hai node) | 08:49:16 | — |
| Drill quan sát alert (`t_detect`) | 08:50:14 | 58 s |
| Incident và candidate sẵn sàng | 08:50:45 | 31 s |
| Operator duyệt | 08:58:30 | 7 phút 45 s |
| Ký xong, bắt đầu thực thi | 08:58:33 | 3 s |
| Executor hoàn tất | 08:58:34 | 1 s |
| Verifier hoàn tất | 09:01:48 | 3 phút 14 s |
| Runtime ghi `resolved` | 09:01:49 | 1 s |

**RTO theo quy ước drill:** `t_resolved − t_detect = 695 s`. Từ lúc cấy lỗi là 753 s. RPO không áp dụng (không phục
hồi dữ liệu). Khoảng có thể tối ưu lớn nhất là **candidate → duyệt (7 phút 45 s)**, gồm chuẩn bị, AI shadow và chờ
người; artifact chưa tách riêng từng phần. Lượt NET-01 cùng ngày có RTO 9.110 s vì chờ Operator và làm mới MFA nhiều
giờ, nên không đại diện.

### 3.3. Bằng chứng Safety Gate đã có

| Nội dung | Bằng chứng | Giới hạn |
|---|---|---|
| Chặn đề xuất AI ngoài phạm vi | DB-01: Gemini (`gemini-2.5-flash`, chế độ AI shadow) tự đề xuất `restart` database; Gate `DENY` vì ngoài catalog, không dispatch. NET-01: Gemini đề xuất `RESTART_SERVICE`, Gate `DENY` | Một mẫu mỗi kịch bản; mức rủi ro cần người đánh giá độc lập |
| Tránh báo khôi phục sai | NET-01: health-only trả HTTP 200 nhưng Verifier đo 3 mẫu `collateral_damage`, `resolution_eligible=false` | Lỗi liên quan do Operator chủ động cấy; một ca có kiểm soát |
| Khôi phục có duyệt | NET-01 và DB-01: Operator duyệt, Executor chạy, Verifier 3/3, `resolved`, baseline đạt | 2/2 lượt, không phải tỉ lệ dài hạn |
| Tỉ lệ chặn sai tự nhiên | Audit `live_gate` tích luỹ: 8 `ALLOW`, 0 `DENY`; `false_block_rate: null` | Chưa có `DENY` live nào được đánh giá là hành động hợp lệ |

Công cụ đo đã có:

```bash
# Bật đề xuất AI shadow trong một lượt drill (opt-in; cleanup tự tắt)
MOODLE_AI_SHADOW_PROPOSAL=true MOODLE_SAFE_EXECUTOR_CONFIRM=staging bash automation/moodle-safe-executor-drill.sh NET-01

# Tỉ lệ chặn sai của Gate live (chỉ sự kiện live_gate; cần tệp nhãn của người đánh giá cho MỌI quyết định)
PYTHONPATH=agent_src python3 automation/moodle-live-gate-fpr.py \
  --audit-db terraform/.artifacts/moodle-safe-executor/<RUN>-audit.sqlite3 \
  --since <UTC> --labels <nhãn.jsonl>
```

Nhánh [`ai_shadow_proposal.py`](../agent_src/core/moodle/ai_shadow_proposal.py) là mẫu tích hợp mô hình chỉ đề xuất;
hiện dùng Gemini, sẽ là điểm gắn LDA/GBDT.

---

## 4. Chỉ số mục tiêu

| Nhóm | Chỉ số | AIHS | Cách đo trong khoá luận | Kỳ vọng |
|---|---|---|---|---|
| **An toàn** (AIHS không có) | Tỉ lệ thực thi hành động nguy hiểm hoặc ngoài phạm vi | Không đo; Controller tự chạy | Cùng tập đề xuất nguy hiểm cố định (`evaluation/benchmark/dangerous_injection.py`) đưa qua: bản AIHS tái hiện (không Gate) và hệ thống của khoá luận (có Gate) | Bản AIHS thực thi một phần; hệ thống có Gate thực thi **0** |
| **An toàn** | Tỉ lệ báo khôi phục sai | Không đo ("thành công" theo bản ghi sửa lỗi) | Kết luận health-only so với Verifier trên cùng lượt (ablation RQ2 đã có) | Verifier phát hiện các ca health-only bỏ sót |
| **Độ chính xác** | HitRate, ReverseRate, OverallRate, macro-F1, top-k | 0,89 / 0,98 / 0,93 | Cùng định nghĩa AIHS, trên dữ liệu Moodle; so với **AIHS tái hiện cùng dữ liệu** và planner theo luật | ≥ AIHS tái hiện; kỳ vọng cao hơn nhờ thêm metric có cấu trúc |
| **Độ chính xác, cùng benchmark** | F1 Predictor / Controller | 0,97 / 0,90 | Prototype công khai của Alibaba và mô hình khoá luận trên **cùng dữ liệu tổng hợp** | So sánh trực tiếp duy nhất với code của Alibaba |
| **Thời gian** | Mức giảm trung vị thời gian phát hiện → `RESOLVED` | −51 % so với baseline của họ | (A) thủ công so với Agent; (B) Agent theo luật so với Agent có gợi ý ML; cùng tập lỗi | Báo "giảm X % trên baseline Moodle", **không** viết "vượt Alibaba" |
| **Thời gian quyết định** | Độ trễ suy luận | 7 ms | Thời gian từ đặc trưng tới gợi ý | Vài ms đến vài chục ms; không ảnh hưởng RTO |

**Câu kết luận có thể bảo vệ:** *"Trên cùng tập lỗi Moodle, khung LDA + GBDT có Safety Gate và Independent Verifier đạt
OverallRate tương đương hoặc cao hơn bản AIHS tái hiện, đồng thời giảm tỉ lệ thực thi hành động nguy hiểm từ x % xuống
0 và phát hiện y ca khôi phục giả mà kiểm tra kiểu AIHS bỏ sót."*

---

## 5. Kiến trúc ứng dụng

```text
evidence mới ─► Mapper: LDA trên log đã lọc + metric Prometheus có cấu trúc
             ─► Predictor: GBDT xếp hạng action ID trong catalog, hoặc "chuyển người"
             ─► Controller: GBDT theo từng action, xác suất sửa thành công;
                 thấp thì thử action rẻ hơn kế tiếp; hết thì chuyển người
             ─► Safety Gate (giữ nguyên) ─► Operator ký ─► Executor ─► Independent Verifier
```

| Thành phần | Vai trò của ML | Quyền quyết định |
|---|---|---|
| Trước Gate | Xếp hạng hành động, gợi ý chuyển người | Gate vẫn kiểm tra bằng chứng, catalog, phạm vi, chữ ký |
| Executor | Có thể dự đoán nguy cơ thất bại để Operator xem | Executor chỉ chạy hành động đã ký đúng phạm vi; ML không tạo lệnh hay đổi tham số |
| Verifier | Có thể cảnh báo mẫu bất thường | Chỉ Verifier ký attestation; probe và cửa sổ ổn định bắt buộc |

**Bảng hành động theo chi phí** (để cùng triệu chứng có nhiều cách sửa, giống AIHS): chờ và kiểm tra lại → khởi động
lại service → tạo lại container từ compose đã review → gỡ fixture gây lỗi đúng tên → chuyển người. Mọi hành động mới
phải có trong catalog, có rollback đối ứng, nằm trong allowlist Executor và có hợp đồng Verifier.

Mô hình lỗi, thiếu bằng chứng hay dữ liệu trôi thì **chuyển người**; không suy ra an toàn từ điểm xác suất. Không đưa
mô hình vào đường alert khi chưa có thiết kế được review.

---

## 6. Dữ liệu và nhãn

**Vấn đề hiện tại:** mỗi kịch bản ứng với một hành động và chỉ có vài lượt chạy thật. GBDT sẽ chỉ học lại bộ luật
(F1 gần 1,0 nhưng vô nghĩa).

**Cần bổ sung:**
1. **Kịch bản mơ hồ:** cùng triệu chứng, nhiều nguyên nhân (ví dụ `MoodleSyntheticTransactionFailed` do DB-01, NET-01
   hay SEC-02), lỗi chồng lỗi, tải nền thay đổi, ca bộ luật đoán sai hoặc phải chuyển người.
2. **Ca thành công và thất bại của từng hành động** để huấn luyện Controller; không cố ý chạy hành động bị cấm để tạo
   nhãn.
3. **Khối lượng:** vài trăm tới vài nghìn incident. AWS staging tốn 10–25 phút và phí NAT/RDS mỗi lượt, nên dùng **lab
   Moodle cục bộ** (Docker Compose, cùng bộ gây lỗi và probe) để sinh dữ liệu huấn luyện; AWS staging dùng cho tập test
   cuối và đo thời gian.

**Mỗi hàng dữ liệu:** một incident và một hành động thực sự được cân nhắc hoặc thực hiện; gắn `incident_id`, `run_id`,
loại môi trường, phiên bản code/policy/model, timestamp.

**Đặc trưng:** chỉ dùng bằng chứng có **trước** mốc dự đoán: metric theo node, loại alert, tuổi bằng chứng, TLS, ALB,
giao dịch tổng hợp, log đã loại thông tin nhạy cảm.

**Cấm đưa vào đặc trưng:** marker kịch bản, run ID chứa đáp án, ground truth, chữ ký, kết quả Executor/Verifier, mọi
thông tin sau quyết định.

**Nhãn sau quyết định:** hành động và phạm vi đã chạy; Gate/Operator chấp thuận hay từ chối; Executor
`executed/failed/partial`; kết quả Verifier; thời gian. `success` chỉ khi đã thực hiện **và** Verifier xác nhận đủ. Từ
chối, chưa thực hiện, thiếu probe, huỷ, hết giờ là trạng thái riêng, không tính là hành động thất bại. Không gán nhãn
cho hành động chưa thử.

**Thu thập:** drill đã lưu `stage_times`, phê duyệt, biên nhận và probe cho lượt thành công. Cần bổ sung lưu bền vững cả
lượt thất bại/huỷ, mốc gửi yêu cầu duyệt, thời gian phản hồi của Operator, thời điểm giao dịch bắt đầu hồi phục. Tách
staging live, lab, mô phỏng và replay; xuất tập nghiên cứu đã lọc, giữ mã băm liên kết nguồn.

---

## 7. Huấn luyện và đánh giá

1. **Đóng băng schema và mốc dự đoán.** Chia train/validation/test **theo thời gian và theo nhóm incident/phiên**
   (như AIHS: 80/20 theo thời gian); không để mẫu của cùng incident rơi vào nhiều tập. Giữ tập test mới nhất tới cuối.
2. **Baseline:**
   - planner theo luật hiện tại;
   - **AIHS tái hiện**: LDA (α = 0,1, β = 0,01, Gibbs sampling) + GBDT, theo đúng mô tả trong bài;
   - prototype công khai của Alibaba trên dữ liệu tổng hợp của nó.
3. **Mô hình khoá luận và ablation:** GBDT trên metric có cấu trúc; LDA + GBDT; LDA + metric + GBDT; có/không Controller
   (giống thí nghiệm B.1 của AIHS). LDA fit trên log train, đóng băng từ vựng. Chỉnh siêu tham số chỉ trên validation;
   lưu version dữ liệu, đặc trưng, code, model, seed.
4. **Chỉ số:** HitRate, ReverseRate, OverallRate, precision/recall/F1 theo từng hành động, macro-F1, top-k, tỉ lệ đề
   xuất sai phạm vi, tỉ lệ từ chối dự đoán, calibration (Brier, reliability) trên dữ liệu tách biệt, khoảng tin cậy.
   Không lấy kết quả train làm kết quả test.
5. **Shadow:** gắn vào Agent qua nhánh AI shadow, chỉ gợi ý (hiện trên Telegram), ghi cả dự đoán lẫn kết quả; Gate,
   chữ ký, Executor, Verifier không đổi. Chỉ hiển thị gợi ý cho Operator khi đủ bằng chứng và thiết kế đã review.

---

## 8. Đo thời gian khôi phục và an toàn

**Định nghĩa:** RTO vận hành = `t_resolved − t_detect`. Báo riêng `t_detect − t_inject`, thời điểm giao dịch bắt đầu
hồi phục (nằm giữa hai mẫu liên tiếp), chờ duyệt, thực thi, xác minh. Mức cải thiện =
`(trung vị baseline − trung vị nhánh mới) / trung vị baseline × 100 %`, cùng định nghĩa và tập lỗi.

**Thiết kế thí nghiệm:**
- (A) thủ công so với Agent hiện tại; (B) Agent theo luật so với Agent có gợi ý ML.
- Cùng lỗi, tải, người trực, probe, Gate, điều kiện Verifier; thứ tự ngẫu nhiên theo phiên; reset baseline trước mỗi
  lượt. Chốt trước thời hạn quan sát và cách xử lý lượt không hoàn thành.
- Báo tỉ lệ hoàn tất, trung vị, khoảng tin cậy, phân bố; p95 khi đủ mẫu; phân tầng theo kịch bản và phiên.
- Báo thêm: tỉ lệ thất bại, hành động sai phạm vi, vi phạm probe, khôi phục giả. Không quan sát thấy lỗi không có
  nghĩa là rủi ro bằng 0.
- Pilot chỉ để ước lượng độ biến động và tính cỡ mẫu; đóng băng model trước tập test.

**Không được làm:** rút ngắn cửa sổ ổn định của Verifier để có số RTO đẹp hơn. ML không tự rút ngắn cửa sổ Verifier hay
bảo đảm Operator phản hồi nhanh; phần có thể cải thiện là thời gian **chọn hành động và duyệt**.

**Phạm vi hạ tầng (đã chốt):** làm trọn trên Moodle trước, sau đó chuyển sang ERPNext; AIOpsLab/Kubernetes chỉ
cân nhắc nếu còn thời gian. Mỗi môi trường được báo cáo riêng, không gộp số liệu. Chi tiết ở mục 9.1.

---

## 9. Lộ trình

| Giai đoạn | Thời lượng | Việc | Đầu ra | Điều kiện qua |
|---|---|---|---|---|
| 1 | 1–2 tuần | Lab Moodle cục bộ; kịch bản mơ hồ; bảng hành động theo chi phí; bộ thu log, metric, nhãn | Bộ dữ liệu có version, kiểm tra rò rỉ | Có ≥ 2 hành động khả dĩ cho mỗi nhóm triệu chứng; có ca thất bại thật |
| 2 | 1 tuần | Tái hiện AIHS; chạy prototype công khai | Baseline "AIHS trên Moodle" và benchmark chung | Tái hiện được số liệu của prototype trên dữ liệu của nó |
| 3 | 1–2 tuần | Mô hình khoá luận, calibration, từ chối dự đoán, ablation | Bảng HitRate/ReverseRate/OverallRate/F1 có khoảng tin cậy | Không thua AIHS tái hiện trên tập test |
| 4 | 1 tuần | Gắn shadow vào Agent qua nhánh AI shadow | Gợi ý trên Telegram, audit đầy đủ | Không đổi quyền thực thi; test CI đạt |
| 5 | 1–2 tuần | Chiến dịch AWS staging: thủ công, Agent, Agent + ML; tập đề xuất nguy hiểm; health-only so với Verifier | RTO, tỉ lệ hoàn tất, thực thi nguy hiểm, khôi phục giả | Đủ cỡ mẫu tính từ pilot |

Tổng khoảng 6–8 tuần. AWS staging hiện đã gỡ (10/11/2026); cần dựng lại cho giai đoạn 5.

### 9.1. Mở rộng hạ tầng sau Moodle

Thứ tự: **Moodle (bắt buộc) → ERPNext (kế tiếp) → AIOpsLab/Kubernetes (cân nhắc)**. Chỉ chuyển môi trường khi
môi trường trước đã qua giai đoạn 5.

| Bước | Môi trường | Việc | Điều kiện qua |
|---|---|---|---|
| 6 | ERPNext | Hoàn thiện adapter, catalog, probe allowed/forbidden/related, Verifier; kịch bản mơ hồ riêng; chạy lại giai đoạn 1–5 rút gọn | Gate, Executor, Verifier chạy live trên ERPNext; có bảng chỉ số riêng |
| 7 (tuỳ chọn) | AIOpsLab/Kubernetes | Adapter cho microservice; dùng bài toán có sẵn của AIOpsLab làm bộ test | Chỉ làm khi bước 6 xong và còn thời gian |

**Đã có sẵn cho ERPNext (chưa xác minh live):** `ERPNextPlatformAdapter` ([adapter.py](../agent_src/core/erpnext/adapter.py)),
`ActionCatalog.load_erpnext_catalog()` (MariaDB, Redis, Nginx), `terraform/erpnext.tf`,
`ansible/playbooks/deploy-erpnext-staging.yml`, script cấy/gỡ lỗi `automation/erpnext-fault-*.sh`. Cần kiểm tra lại
từng phần trước khi dùng làm bằng chứng.

**Điều gì chuyển được giữa môi trường, điều gì không:**

- Chuyển được: kiến trúc Gate/Executor/Verifier qua `PlatformAdapter`, quy trình gắn nhãn, cách đánh giá, đặc trưng
  dạng tổng quát (tỉ lệ lỗi HTTP, kết nối DB, tài nguyên).
- Không chuyển được: chủ đề LDA (từ vựng log Moodle khác ERPNext), catalog và phạm vi mục tiêu. Mỗi môi trường
  huấn luyện LDA riêng.
- Thí nghiệm nên có: huấn luyện trên Moodle, thử trên ERPNext không huấn luyện lại (chỉ đặc trưng tổng quát), so với
  huấn luyện lại trên ERPNext. Kết luận về **khung** (an toàn giữ nguyên trên hai hệ thống), không kết luận model
  dùng chung được nếu số liệu không chứng minh.

---

## 10. Rủi ro và giới hạn

| Rủi ro | Hệ quả | Cách giảm |
|---|---|---|
| Dữ liệu ít, mỗi lỗi một hành động | Mô hình chỉ chép bộ luật | Kịch bản mơ hồ, bảng hành động theo chi phí, lab sinh dữ liệu |
| Lab khác staging | Mô hình tốt ở lab, kém ở AWS | Test cuối trên AWS; báo riêng từng môi trường |
| Lặp cấy cùng một lỗi | Không đại diện sự cố production đa dạng | Biến thể tải, thời điểm, lỗi chồng; báo rõ giới hạn |
| Thiên lệch lựa chọn | Không biết hành động chưa thử có sửa được không | Chỉ học kết quả hành động đã chạy; không suy diễn ngược |
| Rò rỉ đáp án vào đặc trưng | Kết quả đẹp giả | Danh sách cấm ở mục 6, kiểm tra tự động |
| Ca hiếm (sai phạm vi, khôi phục giả) | Không đủ mẫu để có tỉ lệ tin cậy | Báo số đếm và khoảng tin cậy, không báo "0 %" |
| Chi phí AWS | Ít lượt trên staging | Dùng staging chỉ cho test cuối và đo thời gian |

Staging cấy lỗi chứng minh hiệu quả trong điều kiện staging, không chứng minh tỉ lệ ở production. Dự đoán hành động
không cải thiện RPO; RPO cần diễn tập phục hồi dữ liệu riêng.

---

## 11. Quyết định còn mở

Đã chốt: phạm vi hạ tầng Moodle → ERPNext → (cân nhắc) AIOpsLab/Kubernetes, xem mục 9.1.

1. Nguồn dữ liệu huấn luyện: lab cục bộ (đề xuất) hay chỉ AWS staging.
2. Phạm vi so sánh: cả ba trục (an toàn, độ chính xác, thời gian) hay ưu tiên an toàn và độ chính xác.
3. Hạn nộp khoá luận, để chốt lộ trình 6–8 tuần.
