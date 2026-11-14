# Báo cáo AIHS — A1 đến A4, B1 và đánh giá B2

**Ngày thực hiện:** 14/11/2026. **Trạng thái:** A1 đã hoàn thành ở mức prototype; A2 đã kiểm kê và xuất bộ audit đã lọc, nhưng dữ liệu hiện có chưa đủ điều kiện huấn luyện/chia tập. A3 đã chốt hợp đồng đề xuất và kiểm thử ranh giới ánh xạ; chưa có action AIHS nào được duyệt để thực thi trên Moodle. A4 đã chạy nghiệm thu gói bàn giao, chấp nhận với các khoảng trống nghiên cứu còn mở. B1.1 đã nghiệm thu đầu vào có điều kiện; B1.2 đã tích hợp shadow offline; B1.3 đã có nhánh chuẩn bị staging fail-closed nhưng **chưa chạy live**. B2.1 đã có công cụ freeze và biên bản chặn đúng vì thiếu dữ liệu. B2.2 đã chạy kiểm toán điều kiện đo; chưa có mẫu số hợp lệ để tính metric model hoặc Gate. Chưa tái lập chỉ số production của Alibaba.

## 1. Kiểm kê nguồn và điều kiện chạy

### 1.1. Nguồn và phiên bản cố định


| Mục | Kết quả kiểm kê |
| --- | --- |
| Prototype | [alibaba-edu/dcbrain/AIHS_prototype](https://github.com/alibaba-edu/dcbrain/tree/07314287ca0858b9a7d8dba951c96394487e2be2/AIHS_prototype) |
| Commit | `07314287ca0858b9a7d8dba951c96394487e2be2` (`master`, thời điểm commit 21/06/2022 05:12:17 UTC) |
| Giấy phép repository | Apache License 2.0, file `LICENSE` ở gốc; SHA-256 `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4`. Giấy phép của các thư viện phụ thuộc cần kiểm tra riêng nếu phân phối lại. |
| Bài báo | Rui Li và cộng sự, *Automated Intelligent Healing in Cloud-Scale Data Centers*, SRDS 2021; [PDF từ trang tác giả](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf) |
| Manifest máy đọc được | [`evaluation/aihs/source-manifest.json`](../evaluation/aihs/source-manifest.json), gồm kích thước và SHA-256 từng file của prototype, giấy phép, archive và PDF |

Archive nguồn và PDF đã được tải vào `evaluation/aihs/.artifacts/` trên máy làm việc, được Git bỏ qua. SHA-256 của archive là `6a90744d617b81484dc977e05fc612de00b8375a05070c3dfa18fbb9e4edc6ff`; PDF là `3021c7b0dd3505abee96a68f73b728dca1e1c21578c3142bc5fcdaf5972dceae`. Archive chứa cả các thư mục khác của repository; phạm vi kiểm kê và tái hiện AIHS là **`AIHS_prototype/`**, không tự coi các thư mục dữ liệu khác là trace AIHS.

Lệnh tải lại từ nguồn cố định, chạy tại gốc repository:

```bash
mkdir -p evaluation/aihs/.artifacts
curl -fsSL --retry 3 \
  https://codeload.github.com/alibaba-edu/dcbrain/tar.gz/07314287ca0858b9a7d8dba951c96394487e2be2 \
  -o evaluation/aihs/.artifacts/dcbrain-07314287.tar.gz
curl -fsSL --retry 3 \
  https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf \
  -o evaluation/aihs/.artifacts/srds21aihs.pdf
sha256sum evaluation/aihs/.artifacts/dcbrain-07314287.tar.gz \
  evaluation/aihs/.artifacts/srds21aihs.pdf
tar -xzf evaluation/aihs/.artifacts/dcbrain-07314287.tar.gz \
  -C evaluation/aihs/.artifacts
```

### 1.2. Nội dung prototype

Prototype có `DataGenerator.py`, `AIHS_Training.py`, `AIHS_api.py`, `README.md`, `dataset/train.csv` và ba file `model/{mapper,predictor,controller}.pkl`. CSV đi kèm có **500 dòng**, các cột `attributes`, `action`, `final_state` và cột chỉ số không tên. Sáu giá trị `action` là `NOP`, `MSR`, `ER`, `RB`, `RI`, `RMA`; nhãn `final_state` là `successful`/`unsuccessful`. `DataGenerator.py` tạo dữ liệu ngẫu nhiên tổng hợp, gồm 165 thuộc tính với sáu mức; cả hành động và trạng thái cuối được lấy ngẫu nhiên. Không được dùng nhãn này làm bằng chứng hiệu quả khôi phục thực tế.

README yêu cầu **Python 3**, `numpy`, `pandas`, `sklearn` và `lightgbm`, không ghi phiên bản hay cung cấp lockfile. Lệnh `pip3 install sklearn` trong README dùng tên cài đặt cũ; lượt chạy này dùng package `scikit-learn` như ghi ở mục 2. Các script dùng đường dẫn tương đối `./dataset` và `./model`, vì vậy phải chạy trong thư mục `AIHS_prototype/`. README có chỗ viết `./Dataset` (chữ hoa), nhưng đường dẫn thực trong mã và archive là `./dataset`.

Trình tự README nêu: `python3 DataGenerator.py`, `python3 AIHS_Training.py`, `python3 AIHS_api.py`. `DataGenerator.py` ghi đè `dataset/train.csv`; `AIHS_Training.py` ghi đè ba file model. Các lượt tái hiện bên dưới chạy trên bản sao và giữ checksum bản gốc. `AIHS_api.py` giải tuần tự hóa pickle; chỉ chạy với file đã xác minh nguồn/checksum trong môi trường cô lập. Seed `random.seed(2020)` có trong hai script đầu; phiên bản thư viện và tính tái lập đầu ra được kiểm tra trong mục 2.

Kiểm tra tĩnh cho thấy Mapper dùng `CountVectorizer` và LDA của scikit-learn; Predictor/Controller mặc định dùng `lightgbm.LGBMClassifier`. Điều này mô tả **mã prototype**, không khẳng định trùng mọi chi tiết của hệ thống production trong bài báo. Kiểm tra tĩnh cũng thấy nhánh fallback của `Controller._control_process` so sánh đối tượng classifier với số `1`; kết quả chạy và giới hạn của nhánh này được ghi ở mục 2.3.

### 1.3. Giới hạn nguồn

Bài báo nói rõ mã hệ thống và trace production của Alibaba là tài sản riêng; repository phát hành **prototype Python với dữ liệu tổng hợp** để minh họa workflow. Vì vậy số liệu từ CSV/model đi kèm không được trình bày như số liệu vận hành Alibaba hay Moodle. Chưa có bằng chứng prototype tái lập các chỉ số công bố trong bài báo.

### 1.4. Kiểm tra môi trường trước khi chạy

Máy hiện có Python 3.12.3, khoảng 895 GiB dung lượng trống và 5,2 GiB RAM khả dụng tại thời điểm kiểm tra. Đã tạo `evaluation/aihs/.artifacts/venv/` và cài các phiên bản trong [`evaluation/aihs/requirements-a1.txt`](../evaluation/aihs/requirements-a1.txt). Kiểm tra import bốn thư viện trực tiếp, `pip check`, kiểm tra cú pháp ba script, checksum 9 file nguồn và số dòng CSV đều **đạt**. Vùng `venv` và file nguồn tải về được Git bỏ qua.

## 2. Chạy và kiểm tra khả năng tái lập

**Run ID:** `20261114T095639Z`. Mọi thao tác ghi dữ liệu/model đều diễn ra trong bản sao. Các lượt chạy dưới đây kiểm tra prototype trên máy này, không phải số liệu production trong bài báo.

### 2.1. Môi trường và cách chạy

- Linux x86_64; Python 3.12.3. Môi trường `venv` dùng các phiên bản trong [`evaluation/aihs/requirements-a1.txt`](../evaluation/aihs/requirements-a1.txt): numpy 1.26.4, pandas 2.2.3, scikit-learn 1.5.2, lightgbm 4.5.0, cùng phụ thuộc bắc cầu đã ghi trong file.
- Đầu vào là `dataset/train.csv` 500 dòng tổng hợp, SHA-256 `5b4c69b35de32a1890b2eb5ac9bca039c2f6bd60d0cf27683c6cd9f91a1eccb1`. Script tạo dữ liệu gọi `random.seed(2020)`; LightGBM dùng `random_state=2020`; LDA để `random_state=None`.
- Archive nguồn gốc và ba bản sao làm việc nằm trong `evaluation/aihs/.artifacts/`, bị Git bỏ qua. Không sửa file nguồn upstream. Log stdout, stderr, thời gian và model của từng lượt ở `evaluation/aihs/.artifacts/runs/20261114T095639Z/`; SHA-256 của log và model trong [`reproduction-results-2026-11-14.json`](../evaluation/aihs/reproduction-results-2026-11-14.json).

Các lệnh chính, chạy từ gốc repository (mỗi lệnh Python chạy trong thư mục bản sao tương ứng):

```bash
AIHS_SOURCE="$PWD/evaluation/aihs/.artifacts/dcbrain-07314287ca0858b9a7d8dba951c96394487e2be2/AIHS_prototype"
AIHS_RUN_ROOT="$PWD/evaluation/aihs/.artifacts/runs/20261114T095639Z"
AIHS_PYTHON="$PWD/evaluation/aihs/.artifacts/venv/bin/python"
mkdir -p "$AIHS_RUN_ROOT"
cp -a "$AIHS_SOURCE" "$AIHS_RUN_ROOT/pretrained"
cp -a "$AIHS_SOURCE" "$AIHS_RUN_ROOT/generated"
cp -a "$AIHS_SOURCE" "$AIHS_RUN_ROOT/original"

(cd "$AIHS_RUN_ROOT/pretrained" && "$AIHS_PYTHON" AIHS_api.py)
(cd "$AIHS_RUN_ROOT/generated" && "$AIHS_PYTHON" DataGenerator.py)
sha256sum "$AIHS_RUN_ROOT/generated/dataset/train.csv" \
  "$AIHS_RUN_ROOT/original/dataset/train.csv"
(cd "$AIHS_RUN_ROOT/generated" && "$AIHS_PYTHON" AIHS_Training.py)
(cd "$AIHS_RUN_ROOT/generated" && "$AIHS_PYTHON" AIHS_api.py)
(cd "$AIHS_RUN_ROOT/original" && "$AIHS_PYTHON" AIHS_Training.py)
(cd "$AIHS_RUN_ROOT/original" && "$AIHS_PYTHON" AIHS_api.py)
"$AIHS_PYTHON" evaluation/aihs/compare_retrained.py \
  "$AIHS_RUN_ROOT/generated" "$AIHS_RUN_ROOT/original"
```

Lượt thực hiện thực tế bọc từng lệnh bằng `/usr/bin/time -f 'elapsed_s=%e max_rss_kb=%M exit=%x'` và `/usr/bin/timeout` (90 giây cho API/generator, 900 giây cho huấn luyện), ghi stdout/stderr riêng. Các giới hạn timeout này là biện pháp đo và kiểm soát trên máy, không phải tham số của prototype.

### 2.2. Kết quả quan sát

| Bước | Exit | Thời gian | RAM đỉnh | Kết quả |
| --- | ---: | ---: | ---: | --- |
| API với model dựng sẵn | 0 | 4,53 s | ~194 MiB | `{'hostname': '12x8dj02', 'timestamp': 12345, 'action': ['RI']}`; khớp ví dụ README |
| Tạo lại CSV | 0 | 0,44 s | ~70 MiB | SHA-256 trùng chính xác CSV đi kèm |
| Huấn luyện lượt 1 | 0 | 38,06 s | ~1,15 GiB | Tạo đủ ba model, in `training completed` |
| API với model lượt 1 | 0 | 8,30 s | ~191 MiB | Cùng kết quả ví dụ `['RI']` |
| Huấn luyện lượt 2 từ CSV gốc | 0 | 41,67 s | ~1,15 GiB | Tạo đủ ba model, in `training completed` |
| API với model lượt 2 | 0 | 4,76 s | ~191 MiB | Cùng kết quả ví dụ `['RI']` |

So sánh hai bộ model mới trên **cùng 500 dòng dùng để huấn luyện**: Predictor trùng `500/500`, Controller trùng `500/500`. Phân bố action cuối của cả hai lượt: `RI=51`, `MSR=37`, `RMA=47`, `RB=50`, `ER=45`, `NOP=30`, `call human=240`. Đây là kiểm tra tính lặp lại của đầu ra trên dữ liệu in-sample, **không phải accuracy, HitRate, tỷ lệ sửa thành công hay khả năng tổng quát hóa**. Dữ liệu được tạo ngẫu nhiên, không có ground truth sự cố thực.

### 2.3. Sai khác và giới hạn đã ghi nhận

1. **Model không tái lập từng byte.** Checksum của cả ba `.pkl` khác giữa hai lượt. Tham số `LDA.components_` cũng khác; mã đặt `random_state=None` cho LDA. Vì vậy chỉ kết luận workflow và hành vi trên bộ 500 dòng này lặp lại được, không kết luận model tái lập chính xác.
2. **Model dựng sẵn có cảnh báo phiên bản.** Khi mở pickle upstream, scikit-learn báo model được lưu bởi bản 0.24.1 nhưng đang đọc bằng 1.5.2. Ví dụ API vẫn chạy và khớp README; điều đó chưa chứng minh mọi dự đoán của model cũ đáng tin cậy trên phiên bản mới.
3. **Huấn luyện có 24.000 cảnh báo mỗi lượt** `No further splits with positive gain`. Script vẫn exit 0, nhưng cảnh báo cùng nhãn tổng hợp ngẫu nhiên khiến không thể xem model này là baseline khôi phục có chất lượng đã được chứng minh.
4. **Controller có nhánh fallback cần sửa/đánh giá riêng.** Mã so sánh đối tượng classifier với số `1`; khi action đề xuất bị từ chối, vòng lặp sẽ rơi về `call human` thay vì thực sự đánh giá từng action thay thế. Kết quả 240/500 `call human` là đầu ra prototype quan sát được, không phải minh chứng Gate chặn an toàn hay mô hình chọn đúng.
5. **Pickle phụ thuộc ngữ cảnh lớp.** Model huấn luyện khi chạy script chứa tham chiếu tới các lớp ở `__main__`; bộ so sánh phải import các lớp đó vào ngữ cảnh trước khi unpickle. Chỉ mở model từ commit đã xác minh checksum; không nhận pickle từ nguồn không tin cậy.

## 3. A1.3 — Định nghĩa baseline AIHS

### 3.1. Phạm vi baseline có thể chạy

Baseline có thể chạy hiện tại là **`aihs-public-prototype-as-is-v1`**, được đặc tả trong [`evaluation/aihs/baseline-prototype.json`](../evaluation/aihs/baseline-prototype.json). Nó dùng đúng ba script của commit cố định, dữ liệu tổng hợp đi kèm và các phiên bản thư viện ở mục 2. Đây là baseline về *hành vi của prototype công khai*, chưa phải bản sao tương đương thuật toán hoặc kết quả của hệ thống production. Model huấn luyện sau A1.2 nằm trong artifact cục bộ; SHA-256 ghi ở file kết quả, không đưa pickle vào Git.

| Thành phần | Bài báo AIHS | Mã prototype tại commit đã cố định | Cần bổ sung nếu muốn baseline theo phương pháp bài báo |
| --- | --- | --- | --- |
| Kích hoạt và đầu vào | Monitoring phát hiện lỗi, đẩy log thuộc tính của server sang Mapper | `AIHS_api.healing_process` chỉ nhận `attribute_str`, `hostname`, `timestamp` do caller đưa vào; CSV tổng hợp không có cột thời gian | Định nghĩa thời điểm phát hiện và snapshot bằng chứng **trước quyết định**; kết nối nguồn dữ liệu riêng, không lấy kết quả sau sửa làm đặc trưng |
| Biểu diễn log | 165 thuộc tính, sáu mức; mỗi thuộc tính là một từ; Mapper xuất phân bố topic | `DataGenerator.py` tạo 165 thuộc tính; `Mapper._attributes_deal` lặp tên thuộc tính theo mức `info=0`, `good=1`, `warning=2`, `error=3`, `critical=4`, `fatal=5`, rồi `CountVectorizer` và LDA xuất vector 10 chiều | Chốt schema thuộc tính Moodle và cách xử lý thiếu/sai mức; không coi `attributes_0…164` tổng hợp là telemetry thật |
| LDA | 10 topics, α=0,1, β=0,01; Gibbs sampling 1.024 vòng trong thí nghiệm bài báo | scikit-learn `LatentDirichletAllocation`, `learning_method='batch'`, `max_iter=100`, α=0,1, β=0,01, `random_state=None`; thư viện dùng variational Bayes | Cài và kiểm tra riêng Gibbs sampling 1.024 vòng nếu cần đối chiếu đúng thuật toán bài báo; cố định seed, fit từ vựng/LDA chỉ trên tập train |
| Predictor GBDT | Huấn luyện classifier đa lớp bằng **repair record thành công** | LightGBM GBDT đa lớp 2.000 cây, `num_leaves=1024`, `learning_rate=0.2`, `class_weight='balanced'`, seed 2020; `train_predictor(df)` dùng **toàn bộ dòng**, gồm cả `unsuccessful` | Lọc train theo `final_state='successful'`, khóa hyperparameter và đánh giá trên dữ liệu ngoài train |
| Controller GBDT | Sáu classifier nhị phân, mỗi action học cả ca thành công/thất bại; thử action được đề xuất, nếu thất bại thử từ chi phí thấp đến cao, rồi chuyển người | Có sáu LightGBM GBDT nhị phân 2.000 cây; nhận nhãn `successful=1`; nhánh fallback so sánh **đối tượng classifier** với `1` nên không gọi classifier của action thay thế | Sửa trong **biến thể riêng có version**, kiểm thử thứ tự chi phí, classifier thiếu, khả năng abstain; không sửa lặng lẽ baseline nguyên trạng |
| Đầu ra và hành động | NOP → MSR → ER → RB → RI → RMA theo chi phí tăng; RMA cần người | API trả `{'hostname', 'timestamp', 'action': [một mã]}`; có thể trả `['call human']`; không thi hành action | Viết mapping có review từ mã server AIHS sang catalog Moodle nếu có; không suy diễn RB/RI là lệnh EC2 hoặc RDS; action chưa ánh xạ phải chuyển người |
| Thời điểm và nhãn | Dự đoán ngay **sau khi phát hiện lỗi, trước sửa**; repair result chỉ được coi thành công khi server ổn định hơn 20 phút sau sửa | `timestamp` chỉ được copy từ input sang output; không có detection, executor hoặc verifier; generator bốc ngẫu nhiên `action` và `final_state` | Tạo timeline incident/action; khóa toàn bộ đặc trưng ở thời điểm quyết định; chỉ đánh giá nhãn sau cửa sổ quan sát và bằng chứng độc lập |
| Chia dữ liệu | Thí nghiệm bài báo chia 80% đầu và 20% cuối theo thời gian | `AIHS_Training.py` fit cả 500 dòng, không có split; CSV không có timestamp | Dữ liệu thực cần train/validation/test theo thời gian và nhóm incident; chỉ fit vectorizer/LDA trên train |

Nguồn đối chiếu: [bài báo SRDS 2021, mục II–IV](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf), [mã prototype tại commit cố định](https://github.com/alibaba-edu/dcbrain/tree/07314287ca0858b9a7d8dba951c96394487e2be2/AIHS_prototype), [tài liệu LDA scikit-learn 1.5](https://scikit-learn.org/1.5/modules/generated/sklearn.decomposition.LatentDirichletAllocation.html). Các khác biệt trong bảng là kết quả **đối chiếu** giữa nguồn và mã, không phải kết quả benchmark mới.

### 3.2. Quy tắc ra quyết định và quyền hạn

Đường dự đoán của prototype: `attribute_str` → document theo mức → CountVectorizer → vector LDA 10 topic → Predictor chọn một trong sáu action → Controller kiểm tra classifier của action đó → `action` cuối. Việc `timestamp` xuất hiện trong output **không** chứng minh dự đoán được thực hiện tại thời điểm đó. Thời điểm dự đoán phải được ghi bởi hệ thống gọi model, sau khi chụp bằng chứng đầu vào và trước bất kỳ hành động sửa nào.

Trong luận văn, output của baseline chỉ là **đề xuất**. Nó không chứa target/scope Moodle, bằng chứng hợp lệ, chữ ký hay quyền thực thi. Mã `NOP/MSR/ER/RB/RI/RMA` chưa được ánh xạ sang action catalog. Không nối trực tiếp output này tới Safe Executor; mọi ca không ánh xạ hoặc thiếu bằng chứng đều chuyển người. Safety Gate, approval, Executor và Independent Verifier vẫn là các thành phần riêng của hệ thống Moodle.

### 3.3. Kiểm chứng nhánh fallback và tiêu chí nghiệm thu

Chạy kiểm chứng không tải pickle, không huấn luyện, không thực thi action:

```bash
evaluation/aihs/.artifacts/venv/bin/python evaluation/aihs/check_controller_fallback.py \
  evaluation/aihs/.artifacts/dcbrain-07314287ca0858b9a7d8dba951c96394487e2be2/AIHS_prototype
```

Với đề xuất `RB` bị classifier từ chối và `NOP` được cấu hình là action rẻ nhất có thể thành công, mã upstream thực tế trả `call human`, **không gọi** classifier `NOP` (số lần gọi = 0). Điều này xác nhận nhánh fallback của prototype không thực hiện bước thử action theo chi phí như bài báo. Bản baseline nguyên trạng phải giữ hành vi đó trong kết quả; biến thể sửa lỗi chỉ được so sánh khi có mã, version và test riêng.

**A1 hoàn thành ở mức:** nguồn/phiên bản/giấy phép đã kiểm kê, workflow công khai đã chạy, baseline prototype nguyên trạng đã đặc tả. Chưa đạt điều kiện để công bố accuracy, HitRate, false recovery, RTO hay hiệu quả sửa của AIHS trên Moodle: bộ tổng hợp không có ground truth thực, không có test độc lập và không có hành động đã thực thi/kiểm chứng. Mục 4 tiếp tục kiểm kê dữ liệu và nhãn A2.

## 4. A2 — Dữ liệu và nhãn cho thí nghiệm

### 4.1. Bước 1 — Kiểm kê artifact và nguồn gốc

Kiểm kê trên máy Operator tại thời điểm 14/11/2026. Đây là artifact lịch sử; commit hiện tại của repository (`b342f7995ee0`) **không chứng minh** phiên bản mã đã chạy từng lượt cũ. Các file staging được chọn có `evidence_class=operator_approved_staging_drill`; phiên bản mã trong artifact không có nên ghi `unknown_not_in_artifact`.

| Nhóm | Kết quả | Phân loại sử dụng |
| --- | --- | --- |
| `moodle-safe-executor/` | 189 file, 39 tiền tố run theo tên, 25 JSON tổng kết run; trong đó 8 JSON có lớp staging rõ ràng, 17 JSON chưa đủ xác nhận lớp, 14 tiền tố không có JSON tổng kết | Chỉ 8 hồ sơ staging đầy đủ được xuất thành bản ghi audit; **không** lấy 189 file làm 189 incident |
| 8 run staging được chọn | 5 run ngày 07/11, 3 run ngày 08/11; 2 CON-01, 3 DB-01, 3 NET-01 | 8 incident riêng theo ID gốc, được thay bằng group ID mờ danh tính |
| `aiops-unified-replay/` | 22 file trong 11 thư mục run | Offline replay, không phải live recovery hay mẫu độc lập |
| `isolated-live-lab/` | 1 file | Lab riêng; chưa đủ hồ sơ để chuyển thành mẫu huấn luyện |
| `moodle-sprint2-live/` | 196 file trong 22 thư mục con | Bằng chứng lịch sử hỗn hợp; chưa nối provenance/schema A2, chưa gộp vào 8 run trên |
| `evaluation/ground_truth/` | 15 fixture Moodle, 3 ERPNext, 15 fixture legacy ở gốc | Ground truth/hậu nghiệm cho đánh giá offline; **cấm** đưa vào đặc trưng hoặc đếm như incident live |

Manifest kiểm kê và SHA-256 của 8 JSON nguồn đã chọn nằm trong [`evaluation/aihs/a2-export/manifest.json`](../evaluation/aihs/a2-export/manifest.json). Các nhóm chưa phân loại được giữ ngoài bộ xuất để tránh biến tên file hoặc trạng thái `passed` thành bằng chứng đã khôi phục live.

### 4.2. Bước 2 — Schema incident/action

Đã tạo [JSON Schema A2](../evaluation/aihs/a2-record.schema.json). **Một dòng = một ứng viên action của một incident/phiên**, không phải một file. Mỗi dòng có group ID mờ danh tính, provenance, bốn mốc thời gian, phần `pre_decision` (snapshot, hash tham chiếu bằng chứng, đặc trưng số), proposal và phần `post_decision` (Gate, duyệt, action thực thi, receipt, Verifier, runtime). `scenario_code` chỉ là metadata audit và bị cấm vào model input. ID incident/request/user gốc, nội dung tự do, chữ ký và token không có trong bản ghi xuất.

Outcome tách riêng `success`, `failure`, `not_executed`, `denied`, `timeout`, `missing_evidence`. Chỉ action **thực sự chạy** mới có thể nhận nhãn thành công/thất bại; Gate từ chối không bị gán nhãn thất bại khôi phục. Với hồ sơ staging, `success` đòi receipt `executed`/`mutated`, Verifier `ready_for_runtime_acceptance` và runtime `resolved`. Đây là nhãn **lượt drill**, không chứng minh thành công trên sự cố tự nhiên.

### 4.3. Bước 3 — Xuất dữ liệu đã lọc và kiểm tra rò rỉ

Đã tạo [bộ xuất A2](../evaluation/aihs/a2_export.py) và chạy trên 8 JSON staging có lớp bằng chứng rõ ràng. Kết quả là [10 bản ghi audit JSONL](../evaluation/aihs/a2-export/audit-records.jsonl): 8 action catalog được Operator duyệt và đã chạy, cùng 2 đề xuất Gemini shadow bị Gate `DENY` và không chạy. SHA-256 của JSONL: `808d06b6d0f9511062f1f6f0c56c5552acc6978d725603349b7e52716a23ef55`.

Exporter dùng allowlist trường và giá trị trạng thái, chỉ chép action ID dạng mã, thay ID gốc bằng group ID, không sao chép raw alert, prompt, rationale, IP, ARN, email hoặc chữ ký. Kiểm tra không thấy ID gốc hoặc mẫu dữ liệu nhạy cảm trong JSONL; cả 10 dòng hợp lệ với schema. **Toàn bộ `pre_decision.features` đang rỗng** vì các hồ sơ này chưa có snapshot telemetry đã được xác minh là tồn tại trước `t_plan`; không dùng tên kịch bản, root cause fixture, gate/verifier hay kết quả sau quyết định để lấp khoảng trống. `training_eligible=0/10`.

Lệnh chạy lại tại gốc repo:

```bash
python3 evaluation/aihs/a2_export.py \
  --artifact-root terraform/.artifacts \
  --ground-truth-root evaluation/ground_truth \
  --output-dir evaluation/aihs/a2-export
python3 evaluation/aihs/a2_split.py \
  --records evaluation/aihs/a2-export/audit-records.jsonl \
  --output-dir evaluation/aihs/a2-export
```

### 4.4. Bước 4 — Độ đủ dữ liệu và quy trình thu bổ sung

| Nhóm lỗi | Action ứng viên | Outcome trong audit | Số dòng |
| --- | --- | --- | ---: |
| CON-01 | `start_reviewed_compose_service` | `success` (staging drill) | 2 |
| DB-01 | `remove_scoped_db_reject` | `success` (staging drill) | 3 |
| DB-01 | Gemini `restart` | `denied`, không thực thi | 1 |
| NET-01 | `recreate_moodle_web_from_reviewed_compose` | `success` (staging drill) | 3 |
| NET-01 | Gemini `RESTART_SERVICE` | `denied`, không thực thi | 1 |

Không có proposal AIHS trong 10 dòng, không có ca thực thi thất bại có nhãn độc lập, và không có đặc trưng trước quyết định. Do đó không thể huấn luyện/đánh giá Predictor hoặc Controller từ bộ này. Tám thành công đều thuộc ba kịch bản drill lặp lại; không được suy ra tỷ lệ thành công tổng quát. Các fixture offline và dữ liệu tổng hợp A1 giữ riêng khỏi tập live.

Quy trình thu bổ sung cho người vận hành: (1) tạo run/incident ID và ghi phiên bản mã/mô hình trước khi bắt đầu; (2) chụp các metric/log đã cho phép **trước** dự đoán, lưu timestamp/hash bằng chứng và không đưa marker kịch bản hay đáp án vào feature; (3) ghi proposal, Gate, duyệt, action **thực thi thực tế** và receipt theo từng candidate; (4) ghi probe allowed/forbidden/related cùng kết quả Verifier sau action và cửa sổ ổn định; (5) giữ raw artifact trong vùng hạn chế, chạy exporter đã review để tạo bản ghi lọc; (6) kiểm kê số mẫu theo nhóm lỗi/action/kết quả và lập kế hoạch thu thêm các nhóm độc lập cùng cả kết quả thành công/thất bại, không tạo hành động nguy hiểm chỉ để lấy nhãn. Mỗi lần mở rộng exporter phải được kiểm tra provenance và rò rỉ trước khi đưa dữ liệu vào model.

### 4.5. Bước 5 — Chia tập theo thời gian và nhóm

Đã tạo [công cụ chia tập A2](../evaluation/aihs/a2_split.py). Nó chỉ nhận dòng có snapshot trước `decision_at`, hash bằng chứng, feature số, action đã chạy khớp ứng viên và outcome được quan sát; nối các dòng cùng incident **hoặc** phiên thành một nhóm, sắp nhóm theo thời gian rồi dự kiến chia train/validation/test 70/15/15. Từ vựng và LDA chỉ được fit trên train; validation/test giữ độc lập. Ngưỡng kỹ thuật `min-groups=10` chỉ bảo đảm có thể tạo ba tập, **không** phải ngưỡng đủ mẫu về thống kê hay chất lượng model.

Với artifact thật: [split manifest](../evaluation/aihs/a2-export/split-manifest.json) báo `blocked_insufficient_data`, **0 dòng đủ điều kiện, 0 nhóm**, nên **không tạo** file train/validation/test. Kiểm tra riêng công cụ bằng 12 dòng tổng hợp tạm thời trong vùng Git bỏ qua cho kết quả 8/2/2 nhóm đúng thứ tự thời gian, không trùng ID giữa ba tập; các dòng kiểm tra này không được đưa vào bộ dữ liệu A2.

**Kết luận A2:** đã có schema, inventory, exporter, manifest/checksum, báo cáo chất lượng và logic split có chặn khi thiếu dữ liệu. Dữ liệu hiện tại **chưa đủ để huấn luyện hoặc tuyên bố baseline AIHS trên Moodle**. Điều kiện mở bước đánh giá là thu được snapshot trước quyết định, nhãn hậu nghiệm gắn với action đã chạy, provenance phiên bản và đủ nhóm incident độc lập cho từng action/kết quả cần đo.

## 5. A3 — Hợp đồng kết nối AIHS với hệ thống an toàn

### 5.1. Bước 1 — Đối chiếu sáu action với catalog Moodle

[Bảng mapping có phiên bản](../evaluation/aihs/a3-action-mapping.json) ghi `review_status=no_approved_action_mapping`. Kết quả review hiện tại:

| Output AIHS | Ý nghĩa phía server AIHS | Kết quả với Moodle staging | Lý do |
| --- | --- | --- | --- |
| `NOP` | Chờ/không can thiệp | Chưa ánh xạ | Cần hợp đồng quan sát riêng; không mặc nhiên tương đương hành động sửa Moodle. |
| `MSR` | Khởi động lại dịch vụ quản lý server | Chưa ánh xạ | Không xác định dịch vụ, node và điều kiện tiên quyết Moodle. |
| `ER` | Cài lại môi trường runtime | Chưa ánh xạ | Chưa có mục catalog, rollback và phạm vi đã duyệt. |
| `RB` | Khởi động lại server kèm di chuyển dịch vụ | Chưa ánh xạ | Catalog Moodle không mô tả hành động tổng hợp này. |
| `RI` | Tạo lại image server kèm di chuyển dịch vụ | Chưa ánh xạ | Thiếu target, scope, rollback và hợp đồng Verifier. |
| `RMA` | Yêu cầu sửa phần cứng | Chuyển người | Không có hành động Executor tương ứng. |

Không suy ra `catalog_action_id`, target hoặc scope từ hostname do mô hình trả về. Với một mapping được review trong tương lai, hợp đồng yêu cầu action ID, scenario, environment, target scope, target type và precondition khớp catalog hiện hành. Kiểm thử chỉ tiêm tạm ánh xạ `NOP` sang probe đọc `wait_for_alb_target_health` của `CON-01` để chứng minh phép kiểm tra catalog; cấu hình bàn giao **không** phê duyệt ánh xạ đó.

### 5.2. Bước 2 — Bản ghi đề xuất cho B

[Adapter A3](../agent_src/core/moodle/aihs_contract.py) nhận output AIHS có kiểu (`hostname`, `timestamp`, một mã `action`) và một ngữ cảnh đáng tin cậy riêng: `incident_id`, `scenario_id`, `environment`, target scope/resource/type, `evidence_refs`, `predicted_at`, `model_version`, `data_version`. Kết quả có `mapping_version`, trạng thái `HUMAN_REVIEW` hoặc `SHADOW_CANDIDATE`, `reason_code`, action AIHS, action catalog nếu hợp lệ, cùng các cờ `execution_permitted=false` và `dispatched=false`. Không có lệnh shell, chữ ký hay quyền phê duyệt trong hợp đồng.

`timestamp` từ prototype chỉ là trường output không tin cậy, không dùng để chứng minh độ mới. `predicted_at` phải do tầng gọi đáng tin cậy gắn vào. Adapter kiểm tra đủ ba evidence ref độc lập, tính toàn vẹn bản ghi, cùng incident, nguồn trước quyết định, tuổi bằng chứng và tuổi dự đoán theo profile staging. Ground truth không được vào đường quyết định. Thiếu/sai/cũ/ngoài phạm vi đều chuyển người; một candidate hợp lệ cũng chỉ được chuyển sang bước shadow sau này, chưa phải kết luận `ALLOW` của Safety Gate.

[Hai ví dụ input/output đã lọc](../evaluation/aihs/a3-examples.json) dùng ID và telemetry tổng hợp: action prototype `RI` trả `HUMAN_REVIEW/unmapped_action`; action lạ trả `HUMAN_REVIEW/unknown_aihs_action`. Cả hai không được dispatch.

### 5.3. Bước 3 — Kiểm thử và giới hạn

Chạy `PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_contract.py -v`: **7/7 test đạt**. [Bộ test](../agent_src/tests/test_aihs_contract.py) bao gồm mapping mặc định và candidate shadow chỉ trong test; action lạ/định dạng sai/model không có/RMA; evidence thiếu, trùng, sai incident, cũ hoặc từ ground truth; prediction cũ; sai environment, scope, target và catalog. `python3 -m py_compile agent_src/core/moodle/aihs_contract.py` cũng đạt.

**Phạm vi chưa hỗ trợ:** A2 hiện có 0 dòng đủ điều kiện train, nên chưa có model AIHS huấn luyện trên Moodle hoặc chỉ số baseline Moodle. Không có mapping action được phê duyệt; A3 chưa gọi Safety Gate thật, chưa xin Operator ký, chưa chạy Executor hay Independent Verifier, và chưa tạo bằng chứng recovery live. B1 chỉ được chạy shadow sau khi có model/output hợp lệ và mapping được người phụ trách domain duyệt; không được gọi nhánh Gemini shadow hiện có là AIHS.

## 6. A4 — Gói bàn giao và nghiệm thu độc lập

### 6.1. Bước 1 — Chốt gói và lệnh chạy

Gói gồm [manifest nguồn và giấy phép](../evaluation/aihs/source-manifest.json), [pin môi trường prototype](../evaluation/aihs/requirements-a1.txt), [định nghĩa baseline](../evaluation/aihs/baseline-prototype.json), [kết quả tái lập A1](../evaluation/aihs/reproduction-results-2026-11-14.json), schema, exporter, audit JSONL và split manifest ở mục 4, cùng mapping, ví dụ và adapter ở mục 5. [Biên bản nghiệm thu A4](../evaluation/aihs/a4-acceptance-2026-11-14.json) lưu SHA-256 của từng file bàn giao, commit nguồn và kết quả chạy lại. Prototype và model pickle được giữ trong `.artifacts/` bị Git bỏ qua; người nhận phải lấy đúng commit và xác minh checksum trong manifest trước khi chạy. Không đưa credential, state Terraform hoặc dữ liệu runtime thô vào gói.

Trên máy mới, dùng Python 3.12 và tải nguồn/PDF theo URL, hash đã cố định trong manifest. Chỉ giải nén sau khi hash archive khớp:

```bash
mkdir -p evaluation/aihs/.artifacts
curl --fail --location --output evaluation/aihs/.artifacts/dcbrain-07314287.tar.gz \
  https://codeload.github.com/alibaba-edu/dcbrain/tar.gz/07314287ca0858b9a7d8dba951c96394487e2be2
printf '%s  %s\n' 6a90744d617b81484dc977e05fc612de00b8375a05070c3dfa18fbb9e4edc6ff \
  evaluation/aihs/.artifacts/dcbrain-07314287.tar.gz | sha256sum -c -
tar -xzf evaluation/aihs/.artifacts/dcbrain-07314287.tar.gz -C evaluation/aihs/.artifacts
curl --fail --location --output evaluation/aihs/.artifacts/srds21aihs.pdf \
  https://www.cse.cuhk.edu.hk/~pclee/www/pubs/srds21aihs.pdf
printf '%s  %s\n' 3021c7b0dd3505abee96a68f73b728dca1e1c21578c3142bc5fcdaf5972dceae \
  evaluation/aihs/.artifacts/srds21aihs.pdf | sha256sum -c -
python3 -m venv evaluation/aihs/.artifacts/venv
python3 -m venv evaluation/aihs/.artifacts/a4-venv
evaluation/aihs/.artifacts/a4-venv/bin/python -m pip install -r evaluation/aihs/requirements-a4.txt
evaluation/aihs/.artifacts/venv/bin/python -m pip install -r evaluation/aihs/requirements-a1.txt
evaluation/aihs/.artifacts/a4-venv/bin/python evaluation/aihs/a4_acceptance.py
```

Từ gốc repository, trên máy đã có archive/prototype và môi trường A1 ở đúng đường dẫn đã ghi trong manifest:

```bash
python3 -m pip install -r evaluation/aihs/requirements-a4.txt
evaluation/aihs/.artifacts/venv/bin/python -m pip install -r evaluation/aihs/requirements-a1.txt
python3 evaluation/aihs/a4_acceptance.py
```

Script A4 chạy API model dựng sẵn, tạo lại CSV, huấn luyện trong thư mục tạm, gọi API model mới và kiểm tra nhánh fallback. Nó cũng xác minh SHA-256 archive, PDF và chín file nguồn, schema của từng dòng A2, checksum audit, kết quả split, cấu hình mapping và 7 test A3. Chỉ chạy pickle từ bản nguồn đã xác minh; không nhận model pickle bên ngoài.

### 6.2. Bước 2 — Kết quả chạy lại ngày 14/11/2026

| Kiểm tra | Kết quả quan sát |
| --- | --- |
| Commit và checksum nguồn | `07314287ca0858b9a7d8dba951c96394487e2be2`; archive, PDF và file nguồn khớp manifest |
| Baseline prototype | API dựng sẵn trả `RI`; CSV tạo lại khớp SHA-256 `5b4c69b35de32a1890b2eb5ac9bca039c2f6bd60d0cf27683c6cd9f91a1eccb1`; huấn luyện exit 0; API model mới trả `RI` |
| Fallback upstream | Đề xuất `RB` bị từ chối trả `call human`, dù stub `NOP` cho kết quả tốt; xác nhận lỗi đã biết |
| A2 | 10/10 bản ghi audit hợp schema, checksum khớp; 0 dòng đủ train; split tái tạo đúng `blocked_insufficient_data` |
| A3 | 6/6 action có trạng thái, 0 mapping được duyệt; 7/7 test hợp đồng đạt, ví dụ đều không có quyền dispatch |

Ba file model mới có checksum trong biên bản A4; checksum **khác** hai lượt A1 vì LDA upstream không cố định seed. Đầu ra ví dụ lặp lại không phải độ chính xác hay hiệu quả phục hồi trên dữ liệu Moodle. Trạng thái gói: `accepted_with_research_gaps` — chấp nhận tính đầy đủ và khả năng chạy lại trên dữ liệu tổng hợp, **không** chấp nhận claim baseline Moodle hoặc recovery live.

### 6.3. Bước 3 — Lỗi còn mở và đề cương đo tiếp

1. A2 thiếu snapshot/feature trước quyết định và nhãn hậu nghiệm theo action; 0 dòng train đủ điều kiện. Thu thêm incident độc lập có provenance và phiên bản code trước khi chia train/validation/test, fit từ vựng/LDA chỉ trên train.
2. Sáu action server AIHS chưa có ánh xạ Moodle được domain review. Mọi output hiện chuyển người; B1 không được dispatch. Nhánh fallback upstream và LDA không cố định seed phải giữ trong baseline nguyên trạng, nếu sửa phải tạo biến thể riêng có version.
3. B1 cần đo **shadow** với cùng đề xuất và cùng evidence cho nhánh có/không có Gate, lưu action, phạm vi, lý do quyết định và trạng thái abstain. Chưa chạy Gate thật trong A4. Không gọi kết quả Gemini shadow là AIHS.
4. B2 chỉ tính tỷ lệ chặn đề xuất trái phép, tỷ lệ chặn sai, false recovery và tỷ lệ xử lý thành công khi có **nhãn độc lập**, mẫu số và độ bao phủ nhãn rõ ràng. Đánh giá model trên test độc lập theo nhóm incident/thời gian; báo số mẫu mỗi lớp và khoảng tin cậy khi đủ dữ liệu. Các chỉ số này hiện **chưa đo được** trên Moodle từ gói A4.

**Điều kiện chuyển B1:** người nhận chạy lại biên bản A4 thành công và chấp nhận các giới hạn trên; bước shadow AIHS cần đầu ra model hợp lệ và mapping được review trước khi chuyển candidate vào Safety Gate. Không nối Executor hoặc Verifier khi chưa có quy trình staging được duyệt.

## 7. B1.1 — Nghiệm thu đầu vào tích hợp

**Ngày kiểm tra:** 14/11/2026. [Biên bản B1.1](../evaluation/aihs/b1-input-acceptance-2026-11-14.json) tham chiếu checksum của biên bản A4 vừa chạy lại. Trạng thái tổng: `CONDITIONAL_INPUT_ACCEPTANCE` — đủ để kiểm thử hợp đồng và fail-closed offline, chưa đủ để đánh giá action AIHS qua Gate có ý nghĩa hoặc chạy sửa staging.

| Bước | Kết quả | Bằng chứng / giới hạn |
| --- | --- | --- |
| 1. Nguồn và gói A | `PASS_LOCAL` | 18/18 checksum file gói khớp; commit upstream `07314287ca0858b9a7d8dba951c96394487e2be2` khớp manifest. |
| 2. Chạy lại baseline | `PASS_SYNTHETIC_ONLY` | CSV tạo lại khớp hash, huấn luyện exit 0; API model dựng sẵn và mới cùng trả mẫu `RI`. Không phải kết quả Moodle. |
| 3. Schema và dữ liệu | `PASS_SCHEMA_BLOCKED_TRAINING` | 10/10 dòng audit hợp schema; 0 dòng đủ train, split `blocked_insufficient_data`. |
| 4. Hợp đồng evidence | `PASS_CONTRACT_ONLY` | 7/7 test adapter đạt, bao gồm bằng chứng thiếu/cũ/sai incident. Chưa có snapshot Moodle trước quyết định đủ điều kiện trong A2. |
| 5. Mapping action | `BLOCKED` | 0/6 action AIHS có mapping được duyệt; các mã server không mặc nhiên là action Moodle. |
| 6. Điều kiện shadow | `LIMITED` | Có thể kiểm thử ca từ chối/chuyển người offline; chưa có candidate AIHS được duyệt để đo quyết định Gate có ý nghĩa. Chưa gọi Gate thật. |
| 7. Bàn giao qua Git | `LOCAL_ONLY` | 0/18 file của gói A đang được Git theo dõi; chưa commit/push. Biên bản này xác nhận trên máy hiện tại, không xác nhận máy khác đã nhận gói. |

**Việc cần trước bước tiếp theo:** thu feature/evidence trước quyết định và nhãn độc lập trên Moodle; domain review mapping action cùng target/scope, rollback và precondition; đưa gói đã review vào version control nếu cần B tái lập trên máy khác. Chỉ sau đó mới đánh giá shadow qua Safety Gate, vẫn không dispatch.

## 8. B1.2 — Tích hợp AIHS shadow với Safety Gate

[Bridge shadow](../agent_src/core/moodle/aihs_shadow.py) gọi adapter A3 trước. Output không ánh xạ, action lạ, model không có, evidence thiếu/cũ hoặc sai phạm vi đều được ghi vào audit `aihs_shadow_evaluation` và chuyển người **trước khi gọi Gate**. Khi adapter có `SHADOW_CANDIDATE`, bridge chỉ nhận một `SafeActionRequest` `dry_run=true` đã được chuẩn bị từ nguồn đáng tin cậy; action ID, scenario, target, scope, environment và evidence refs phải khớp candidate. Confidence phải do caller đáng tin cậy cung cấp vì prototype AIHS không xuất confidence đã hiệu chuẩn. Request/confidence thiếu hoặc sai đều dừng.

Nếu các điều kiện trên đạt, bridge gọi `SafeExecutionGate.explain()` thật và ghi quyết định `ALLOW`, `DENY` hoặc `REQUIRE_APPROVAL` cùng reason vào `SafeActionAuditStore` có hash chain. Audit chỉ giữ mã action, phiên bản model/data/mapping, thời điểm, hash input/evidence refs và kết quả Gate; không ghi raw hostname, raw evidence, prompt hoặc khóa. `execution_permitted=false`, `dispatched=false`, `resolution_eligible=false` cho mọi kết quả. Không gọi approval, Executor hay Verifier.

Lệnh kiểm thử/replay từ gốc repo:

```bash
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_contract.py -v
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_shadow.py -v
python3 evaluation/aihs/b1_shadow_replay.py
```

Kết quả ngày 14/11/2026: **7/7 test adapter** và **7/7 test bridge shadow** đạt. [Replay và tóm tắt audit](../evaluation/aihs/b1-shadow-replay-2026-11-14.json) gồm bốn trường hợp:

| Nguồn input | Kết quả adapter | Gate thật | Ý nghĩa |
| --- | --- | --- | --- |
| Output `RI` từ prototype công khai trên dữ liệu tổng hợp | `HUMAN_REVIEW/unmapped_action` | Không gọi | Mapping repository vẫn có 0 action được duyệt. |
| Action lạ do fixture tạo | `HUMAN_REVIEW/unknown_aihs_action` | Không gọi | Fail closed. |
| Model không có, fixture giả lập | `HUMAN_REVIEW/model_unavailable` | Không gọi | Không suy đoán action thay thế. |
| `NOP` với mapping probe đọc **chỉ trong test** và request đã chuẩn bị | `SHADOW_CANDIDATE` | `ALLOW`, chỉ preview | Chứng minh đường gọi Gate; không phải action AIHS được domain review hoặc kết quả Moodle live. |

Hash chain của audit replay đã xác minh. SQLite audit và evidence tổng hợp nằm tại đường dẫn `.artifacts/` trong JSON, bị Git bỏ qua; file JSON chỉ giữ tóm tắt đã lọc. Test thêm Gate `DENY` khi confidence thấp, template sai, thiếu confidence/evidence và prediction cũ. **Giới hạn:** kết quả `ALLOW` thuộc fixture probe đọc; không phải tỷ lệ Gate chặn sai, không phải recovery, và không cho phép mở luồng live. Dữ liệu A2 vẫn có 0 dòng đủ train và mapping repository vẫn 0/6 action được duyệt.

## 9. B1.3 — Chuẩn bị handoff staging có kiểm soát

[Bridge staging](../agent_src/core/moodle/aihs_staging_handoff.py) chỉ chuẩn bị đề xuất cho Operator. Nó yêu cầu mapping `reviewed_for_live` với action `live_reviewed`, mã tham chiếu review, target/scope/role khớp catalog và action dạng sửa do Executor đảm nhiệm. Adapter A3 phải xác minh evidence trước quyết định. Sau đó `prepare_moodle_live_handoff()` hiện có phải độc lập tìm lại **cùng action** từ candidate của planner và evidence tươi; bridge so khớp candidate ID, incident, action, target, scope, evidence refs, Gate preview `REQUIRE_APPROVAL` và cả hai digest Gate/Executor. Bất kỳ sai khác nào đều trả `BLOCKED` và ghi audit. Bridge không ký thay Operator, không gọi Executor, không nhận attestation Verifier và không đánh dấu `RESOLVED`.

Sau khi người phụ trách domain thực sự review mapping và staging đã dựng, `operator_handoff` trả về từ bridge mới được chuyển vào luồng Operator ký action-bound → `execute_moodle_live_handoff()` → Independent Verifier hiện có. Chính luồng hiện có sẽ xác nhận lại candidate/evidence lúc thi hành và chỉ chấp nhận `RESOLVED` khi attestation Verifier hợp lệ. Không có đường tắt từ model tới Executor.

Lệnh kiểm thử nhánh chuẩn bị:

```bash
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_staging_handoff.py -v
```

[Bảy ca test staging](../agent_src/tests/test_aihs_staging_handoff.py) đạt: mapping repository chặn trước planner; shadow review không đủ quyền live; fixture **chỉ trong test** `MSR`→`start_reviewed_compose_service` tạo request chờ Operator; sai scope/digest, candidate thiếu và prediction cũ đều chặn. Một test còn chạy planner, Gate preview và `prepare_moodle_live_handoff()` thật trên probe tổng hợp, chỉ tới trạng thái chờ Operator. Fixture này không phải ánh xạ ngữ nghĩa được phê duyệt và không được đưa vào cấu hình repository. [Biên bản readiness](../evaluation/aihs/b1-staging-readiness-2026-11-14.json) ghi trạng thái kiểm tra hạ tầng và các điều kiện còn thiếu.

**Trạng thái thực tế:** `terraform/terraform.tfstate` cục bộ có 0 resource; profile `target-account` yêu cầu MFA nên chưa xác minh được AWS bằng CLI. Mapping chính thức có 0 action live được review; A2 vẫn có 0 dòng đủ train. Vì vậy lượt này **không** triển khai hạ tầng, không fault drill, không xin Operator approve, không gọi Executor hoặc Verifier. Chỉ sau khi đối chiếu AWS state, dựng lại staging nếu cần, review mapping hợp lệ và có evidence live mới kiểm thử end-to-end trên staging.

## 10. B2.1 — Đóng băng tập đánh giá

Đã tạo [công cụ freeze](../evaluation/aihs/b2_freeze_dataset.py). Nó xác minh checksum của audit JSONL và từng artifact nguồn theo manifest A2, kiểm tra toàn bộ dòng với JSON Schema, loại feature có tên chứa marker kịch bản/đáp án/hậu quyết định, và yêu cầu nhãn success/failure gắn với action đã thực thi cùng một file review hậu quyết định. Dòng đủ điều kiện còn phải có snapshot/feature trước quyết định, phiên bản mã lịch sử xác định và cùng lớp môi trường đã chọn. Review nhãn phải có `record_id`, `outcome`, `executed_action_id`, `reviewer_id`, `evidence_sha256`, `reviewed_at` sau thời điểm Verifier; file này là metadata kiểm duyệt, vẫn cần quy trình con người kiểm chứng tính độc lập của reviewer và bằng chứng gốc.

Nếu đủ dữ liệu, công cụ nối các dòng chung incident **hoặc** phiên, chia 70/15/15 theo thời gian, kiểm tra không chồng nhóm và không giao thoa thời gian qua ranh giới. Nó tạo file feature và label **riêng** cho train, validation, test trong `.artifacts/` bị Git bỏ qua; feature chỉ có `record_id` và `pre_decision.features`. Freeze ID dựa trên hash input, schema, code, policy và file review; cùng ID không được ghi đè. Với trạng thái `BLOCKED`, ID chỉ là fingerprint của lần kiểm tra, không phải ID của một tập test đã tạo. Từ vựng/LDA chỉ được fit trên train ở bước model sau này; B2.1 không fit model hay xem test để chỉnh tham số.

Chạy trên dữ liệu A2 hiện có:

```bash
python3 evaluation/aihs/b2_freeze_dataset.py
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_freeze.py -v
```

[Manifest B2.1](../evaluation/aihs/b2-freeze-manifest-2026-11-14.json) ghi **`BLOCKED`**, 10 bản ghi audit, **0 dòng được chọn, 0 nhóm độc lập, không có train/validation/test**. Các thiếu sót có thể chồng lên cùng dòng: 10/10 thiếu feature/nhãn đủ điều kiện trước quyết định, 10/10 thiếu phiên bản mã lịch sử, 8 dòng success thiếu review nhãn độc lập; hai dòng còn lại là proposal bị từ chối và không có nhãn phục hồi. Theo scenario có 2 bản ghi CON-01, 4 DB-01, 4 NET-01; theo outcome có 8 `success` của drill và 2 `denied`. Không có proposal AIHS trong tập này. Do đó không thể gọi đây là tập test độc lập cho AIHS hoặc công bố metric model/Gate/Verifier.

[Năm test freeze](../agent_src/tests/test_aihs_freeze.py) đạt: dữ liệu A2 thực bị chặn; 12 dòng **tổng hợp chỉ để thử công cụ** tạo 8/2/2 nhóm không trùng và không cho ghi đè; feature chứa `scenario_marker` bị từ chối; cohort chỉ có một outcome và nhãn được review trước Verifier cũng bị chặn. Dữ liệu test công cụ nằm trong thư mục tạm và không nhập vào cohort nghiên cứu.

**Điều kiện mở freeze thật:** thu artifact có snapshot trước quyết định và provenance phiên bản; gắn nhãn hậu nghiệm độc lập chỉ cho action đã chạy; bổ sung đủ nhóm incident/phiên cùng cả success/failure trong một môi trường; review rò rỉ và giữ test chưa dùng để điều chỉnh model. Báo riêng môi trường huấn luyện và test nếu khác nhau. Ngưỡng 10 nhóm trong code chỉ là kiểm tra kỹ thuật tối thiểu cho ba split, chưa bảo đảm đủ cỡ mẫu thống kê.

## 11. B2.2 — Lượt đo model và Safety Gate

Đã chạy [công cụ kiểm toán phép đo](../evaluation/aihs/b2_measure_attempt.py) trên manifest freeze B2.1, audit A2, mapping A3 và replay shadow B1.2. Nó kiểm tra checksum và buộc kết quả thiếu mẫu số/nhãn phải là `null`, không ghi tỷ lệ 0%. Lệnh:

```bash
python3 evaluation/aihs/b2_measure_attempt.py
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_measure_attempt.py -v
```

[Biên bản đo B2.2](../evaluation/aihs/b2-measurement-2026-11-14.json) ghi `NOT_MEASURABLE`. Kết quả theo bước:

| Bước | Kết quả quan sát | Có thể kết luận |
| --- | --- | --- |
| 1. Khóa input | Audit A2 khớp checksum manifest và input freeze; 10 dòng audit, 0 dòng test độc lập | Nguồn đếm nhất quán; chưa có cohort đánh giá. |
| 2. Đo model | 0 proposal AIHS trên Moodle, 0 mapping được review; không có nhãn độc lập về **action chấp nhận được** cho từng incident | Top-1/top-k, macro-F1, HitRate, ReverseRate, OverallRate và abstention trên Moodle đều `null`. 500 dòng synthetic A1 chỉ là chẩn đoán in-sample, không dùng làm test B2. |
| 3. Đo Gate lịch sử | 8 proposal `reviewed_catalog_plan` có `REQUIRE_APPROVAL` và outcome drill `success`; 2 proposal `gemini_shadow` có `DENY` và không thực thi | Đây là đếm theo **hai nguồn khác AIHS**, không phải tỷ lệ chặn đúng/chặn sai AIHS. `REQUIRE_APPROVAL` được giữ riêng khỏi `DENY`. |
| 4. Kiểm tra shadow | Một output prototype trên dữ liệu tổng hợp chưa ánh xạ; ba fixture synthetic, trong đó một Gate `ALLOW` chỉ để thử đường gọi; 0 Executor call | Fixture không tạo nhãn an toàn độc lập hoặc bằng chứng phục hồi live. |
| 5. Chốt chỉ số | `unsafe_labeled=0`, `safe_labeled=0`; unsafe block rate và false block rate đều `null` | **Không** được diễn giải `0/0` thành `0%`, cũng không suy ra Gate không chặn sai. |

Để đo model thật cần test B2.1 đóng băng, prediction của baseline và biến thể trên **cùng test**, nhãn độc lập về các action chấp nhận được được adjudicate mù với model, và định nghĩa metric chốt trước khi mở test. Outcome của action đã thực thi **không tự chứng minh** đó là action tối ưu cho incident. Để đo Gate thật cần phát lại cùng đề xuất qua Gate thực nhưng không dispatch; người review độc lập, mù với quyết định Gate/model, gán nhãn an toàn trong scope từ bằng chứng **trước quyết định**. Khi đó mới tính `unsafe DENY / unsafe có nhãn` và `safe DENY / safe có nhãn`, báo `REQUIRE_APPROVAL`, abstain và nhãn thiếu thành nhóm riêng. Nhánh không Gate chỉ là đối chứng quyết định offline, không thi hành action nguy hiểm. Hai test đo kiểm tra không biến thiếu nhãn thành `0%` và chặn audit sai checksum; đều đạt.

## 12. B2.3 — Đo Independent Verifier

Đã chạy [công cụ đo B2.3/B3](../evaluation/aihs/b2b3_measure.py) trên manifest A2 và artifact staging có checksum khớp. [Kết quả máy đọc được](../evaluation/aihs/b2b3-measurement-2026-11-14.json) ghi một ca đối chiếu trực tiếp cùng lượt NET-01 ngày 08/11/2026: sau khi Executor sửa lỗi chính, người vận hành cấy lỗi liên quan có kiểm soát. HTTP health trả **200**, nhưng 3/3 mẫu probe cho thấy DNS database trên `moodle-app-b` lỗi và hai ALB target không cùng khỏe; Verifier trả `collateral_damage`, `resolution_eligible=false`. Runtime sidecar của lần kiểm tra collateral vẫn là `verifying`, **không** chấp nhận `RESOLVED`. Trạng thái `resolved` của main run thuộc đường phục hồi bình thường; không được dùng thay cho sidecar của phép kiểm tra collateral. Probe được artifact ghi `simulated=false`; kết luận vẫn phụ thuộc vào artifact lịch sử và chưa kiểm chứng chữ ký attestation bằng public key tại lượt phân tích này.

Phép đối chiếu này chứng minh **1/1 ca cấy lỗi có kiểm soát được chọn** có health HTTP xanh mà Verifier chặn xác nhận phục hồi. Nó không cho biết tỷ lệ false recovery của quần thể, độ nhạy/độ đặc hiệu trên mọi loại collateral damage, hoặc hiệu quả Verifier trên AIHS; các chỉ số đó để `null`. Mẫu NET-01 ngày 09/11 có sidecar nhưng thiếu main run và giá trị health HTTP được phân loại, nên không ghép vào mẫu số.

Lệnh tái kiểm:

```bash
python3 evaluation/aihs/b2b3_measure.py
PYTHONPATH=agent_src python3 -m unittest discover -s agent_src/tests -p test_aihs_b2b3_measure.py -v
```

## 13. B3 — Đo phục hồi live staging

**Lượt live mới ngày 14/11/2026: `BLOCKED_NOT_RUN`, 0 fault drill mới, 0 thay đổi AWS.** Terraform state *cục bộ* có 0 resource và 0 output; đây không đủ để khẳng định AWS thực tế đã xóa. Lệnh kiểm tra `target-account` yêu cầu MFA và không hoàn tất ở phiên CLI không tương tác, nên chưa xác minh được ALB, EC2, RDS, baseline hoặc điều kiện reset. Ngoài ra mapping AIHS chính thức có 0 action `live_reviewed`, freeze B2.1 có 0 test record. Vì vậy không thể chạy hai nhánh current-rules và AIHS trên cùng staging hoặc công bố so sánh RTO/RPO. Không xin Operator approve hay kích hoạt fault khi chưa xác nhận staging.

Để có số mô tả lịch sử, công cụ chỉ lấy **8 main artifact được gắn rõ `operator_approved_staging_drill`** từ manifest A2, kiểm checksum, yêu cầu runtime `resolved` và tính `t_detect → t_resolved`. Từng lượt (giây): CON-01 **525, 263**; DB-01 **232, 257, 695**; NET-01 **518, 564, 9110**. Median **521,5 giây**, min **232 giây**, max **9110 giây**, trung bình **1520,5 giây**. Lượt 9110 giây chủ yếu do 8914 giây từ phát hiện đến bắt đầu Executor, vẫn được giữ nguyên. Các mốc gồm chờ duyệt, thi hành và Verifier; **không phải thời gian dịch vụ ngừng hoạt động**. Chỉ chọn lượt hoàn chỉnh từ 8 artifact phân loại rõ, còn 17 main artifact lịch sử chưa phân loại; 8/8 `resolved` vì vậy **không phải tỷ lệ phục hồi thành công của tất cả sự cố**. RPO và so sánh RTO AIHS để `null`.

Muốn đo B3 mới: xác minh AWS state bằng phiên MFA hợp lệ và baseline staging; dựng/cấu hình staging nếu đã xóa; có mapping AIHS được domain review và model có dữ liệu hợp lệ; khóa trước fault, workload, probe, thời gian ổn định, quy trình reset, nhánh đối chứng và thứ tự lượt; sau đó chạy các lượt có Operator approve với bằng chứng Verifier và kiểm tra baseline sau reset. Báo riêng detect→RESOLVED, downtime, chờ duyệt, thời gian thực thi, Verifier, noncompletion và RPO nếu có nguồn dữ liệu chứng minh. Không dùng các lượt lịch sử này làm đối chứng nhân quả cho AIHS.
