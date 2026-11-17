# Phụ lục

## Phụ lục A. Đối chiếu số liệu AIHS với bài báo gốc

Số liệu về AIHS dùng trong Chương 1 và 2 được lấy từ tài liệu nội bộ của dự án, rồi đối chiếu ngày 17/11/2026 với bản PDF công khai của bài báo [AIHS2021] (trích văn bản bằng `pdftotext`).

**Bảng A.1.** Kết quả đối chiếu

| Nội dung | Giá trị dùng trong khoá luận | Bài báo | Kết quả |
| --- | --- | --- | --- |
| Mapper | LDA, Gibbs sampling; so với TF-IDF và BERT | LDA dùng Gibbs sampling, 10 chủ đề, 1.024 vòng lặp; so sánh TF-IDF, LDA, BERT | Khớp |
| Thuộc tính giám sát | 165 thuộc tính, mỗi thuộc tính 6 mức | 165 thuộc tính, 6 mức nghiêm trọng | Khớp |
| Sáu action | NOP, MSR, ER, RB, RI, RMA | NOP (chờ, kiểm tra lại sau khoảng 20 phút), MSR (khởi động lại dịch vụ quản lý), ER (cài lại môi trường chạy), RB (reboot, di chuyển dịch vụ trước), RI (cài lại hệ điều hành), RMA (tạo phiếu sửa phần cứng cho người) | Khớp; RB kèm di chuyển dịch vụ |
| Predictor | GBDT đa lớp, học từ ca thành công | Học từ bản ghi sửa thành công | Khớp |
| Controller | 6 GBDT nhị phân, fallback theo chi phí tăng dần rồi chuyển người | Đánh giá action khác theo chi phí tăng dần | Khớp |
| Tiêu chí thành công | – | Server hoạt động bình thường hơn 20 phút | Bổ sung |
| Action tự động | Production chỉ tự chạy NOP, MSR, RB | Thiết kế cho phép 5 action đầu tự động; triển khai hiện chỉ cho phép NOP, MSR, RB | Khớp (cần phân biệt thiết kế và triển khai) |
| Dữ liệu | 4,25 triệu bản ghi (2,22 triệu thành công, 2,03 triệu thất bại), 5.100 cluster, chia 80/20 theo thời gian, > 99 % là NOP/MSR | Như vậy; AIHS triển khai trên 2.200/5.100 cluster | Khớp |
| Ví dụ lỗi Ping | 67,5 % MSR, 29,5 % NOP, 3 % RB | Như vậy | Khớp |
| Predictor F1 | LDA 0,97 | TF-IDF 0,94, LDA 0,97, BERT 0,96 | Khớp |
| Controller | 0,87 / 0,94 / 0,90 | Bảng II, dòng LDA: 0,87 / 0,94 / 0,90 (TF-IDF 0,54 / 0,92 / 0,59; BERT 0,70 / 0,66 / 0,59) | Khớp |
| HitRate / ReverseRate / OverallRate | 0,89 / 0,98 / 0,93 | Như vậy; OverallRate là trung bình điều hoà | Khớp |
| Baseline | MRU, ticket-based: HitRate 0,87 / 0,90; ReverseRate < 0,45 | Như vậy | Khớp |
| Production | 7 tháng, 33,7 triệu lỗi, 92,4 % so với 65,3 % | Như vậy | Khớp |
| Thời gian không phục vụ | 20,1 → 9,8 giờ (−51 %) | Như vậy | Khớp |
| Thời gian | Quyết định 7 ms (luật < 1 ms); LDA 6,65 giờ, GBDT < 3 phút | Như vậy | Khớp |
| Tác giả | Rui Li và cộng sự | R. Li, Z. Cheng, P. P. C. Lee, P. Wang, Y. Qiang, L. Lan, C. He, J. Lu, M. Wang, X. Ding (Alibaba, CUHK, Xi'an Jiaotong University) | Đã bổ sung đủ trong danh mục tham khảo |

## Phụ lục B. Artifact và kiểm tra tính toàn vẹn

**Bảng B.1.** Artifact của phép đánh giá

| Nội dung | Vị trí | Kiểm tra |
| --- | --- | --- |
| Nhãn, khoá protocol, model lock, proposal và kết quả offline | `evaluation/aihs/evidence/` (trong Git, lưu nguyên byte) | `sha256sum -c SHA256SUMS`; hash proposal, split và nhãn Q1 khớp `protocol-lock-primary.json` |
| 11 file mã bị khoá (protocol, scorer, Gate, catalog, adapter, mapping nháp, metrics) | Repo | Khớp sha256 trong `protocol-lock-primary.json` tại commit nộp |
| Tổng hợp live chính và bổ sung | `evaluation/aihs/live-main-results/`, `evaluation/aihs/live-supplementary-results/` | Sinh lại bằng `automation/aihs-live-aggregate.py` |
| Run gốc live và audit runtime | `main-archive-20261117.tgz` (79 bản ghi audit), `supplementary-archive-20261117.tgz` (22 bản ghi audit); lưu ngoài repo | SHA256 ghi trong `docs/BAO_CAO_KET_QUA_C0_C3.md` mục 7 |
| Model pickle, đặc trưng A2 đầy đủ, dữ liệu thô lab | Ngoài repo | Tái tạo theo `docs/BAO_CAO_PROTOCOL_C0_C3_OFFLINE.md` mục 8 |

## Phụ lục C. Lệnh tái lập

Chạy từ gốc repo, Python 3.11.

```bash
# Kiểm thử và lint như CI
ruff check agent_src --select E9,F63,F7,F82
PYTHONPATH=agent_src pytest -q agent_src/tests

# Kiểm tra toàn vẹn bằng chứng offline
cd evaluation/aihs/evidence && sha256sum -c SHA256SUMS && cd -

# Đọc lại kết quả test offline (KHÔNG chạy lại lệnh test: marker mở test chặn lần thứ hai)
python -c "import json;print(json.load(open('evaluation/aihs/evidence/phase4/test-primary/results.json'))['scoring']['metrics'])"

# Tổng hợp lại kết quả live từ run gốc (sau khi giải nén archive)
python automation/aihs-live-aggregate.py ~/moodle-ops/aihs-live/main --json /tmp/main.json --md /tmp/main.md
python automation/aihs-live-aggregate.py ~/moodle-ops/aihs-live/supplementary --json /tmp/sup.json --md /tmp/sup.md

# Thử khô harness live không cần AWS
PYTHONPATH=agent_src python evaluation/aihs/live_c0_harness.py dry-run --proposer locked
```

Dựng lại staging và chạy một lượt live: xem `docs/BAO_CAO_HUONG_DAN_TRIEN_KHAI.md` và `docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md` mục 7.

## Phụ lục D. Đo độ đồng thuận với người gán nhãn thứ hai

Kế hoạch đăng ký trước (tập con 63/419 episode, seed `20261117`, Cohen's κ cho Q1 và Q2, cách diễn giải và cam kết không sửa kết quả đã báo) nằm ở `docs/HUONG_DAN_NGUOI_GAN_NHAN_THU_HAI.md`. Kết quả sẽ được bổ sung vào phụ lục này khi có người gán thứ hai.
