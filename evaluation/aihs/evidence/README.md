# Bằng chứng gốc của phép đánh giá C0–C3

Bản sao nguyên byte các file kết quả, nhãn và khoá dùng trong khoá luận (thư mục làm việc `evaluation/aihs/.artifacts/` bị Git bỏ qua). Mọi file được liệt kê trong `SHA256SUMS`; kiểm tra bằng `sha256sum -c SHA256SUMS` từ thư mục này.

| Thư mục / file | Nội dung |
| --- | --- |
| `labels/tien-labels.jsonl` | Nhãn gốc do người gán `tien` tạo trong `label_tool.py`, kèm lịch sử sửa (`history`) |
| `label-export-primary/` | Nhãn đã xuất cho arm chính (Một phần → `failure`): `independent-label-review.jsonl` (Q2), `proposal-safety-labels.jsonl` (Q1), danh sách loại trừ, tóm tắt |
| `label-export-sens-exclude/` | Như trên cho arm độ nhạy (loại nhãn Một phần) |
| `phase2-primary/`, `phase2-sensitivity/` | Freeze/split manifest, `model-lock.json`, báo cáo huấn luyện và proposal của C0 trên tập test, cho từng arm |
| `phase4/` | `protocol-lock-*.json`, marker `test-opened-*.json`, kết quả `results.json` và `per-row.jsonl` của validation và test, stdout của lần mở test duy nhất |
| `a2-export-manifest.json` | Manifest của bản export A2 (checksum bản ghi đặc trưng) |

**Kiểm tra tính toàn vẹn đã làm (17/11/2026):** sha256 của `proposals-test.jsonl` (`2c44b1a3…`), `split-manifest.json` (`8cd88920…`) và nhãn Q1 (`a0ae41f7…`) khớp `protocol-lock-primary.json`; cả 11 file mã được khoá trong lock vẫn khớp sha256 ở commit này.

**Không đưa vào Git:** `model.pkl` (pickle, tái tạo bằng `train_moodle_aihs.py lock`), bản ghi đặc trưng A2 đầy đủ (`audit-records.jsonl`), dữ liệu thô lab `campaign-1` và hai archive live (`main-archive-20261117.tgz`, `supplementary-archive-20261117.tgz`). Các file này được lưu ngoài repo, có checksum ghi trong `docs/BAO_CAO_KET_QUA_C0_C3.md` mục 7.

Nhãn trong thư mục này do **một** người gán; chưa có người gán thứ hai độc lập (xem `docs/BAO_CAO_KET_QUA_C0_C3.md` mục 6 và gói gán nhãn thứ hai).
