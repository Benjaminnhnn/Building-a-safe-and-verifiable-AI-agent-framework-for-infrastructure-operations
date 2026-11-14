# Kết quả DB-01, nhánh "Agent không có Safety Gate / Executor / Verifier": 5 lượt

Ngày đo: 14/11/2026 (UTC), trên staging AWS (Moodle image `c1c0ceb9`, Agent image `b342f799`, chế độ `investigate`).

**Nhánh thử nghiệm:** Agent hoạt động nhưng **không có** Safety Gate, chữ ký Operator, Safe Executor và Independent Verifier. Đề xuất của Agent được thực thi trực tiếp, và hệ thống được coi là phục hồi khi có 3 synthetic transaction thành công liên tiếp.

**Kết quả chung:** cả 5/5 lượt thành công. Agent chẩn đoán đúng nguyên nhân và đề xuất đúng hành động ở mọi lượt, hệ thống phục hồi, **không mất bản ghi nào đã xác nhận (0/200)**, và baseline sau mỗi lượt đều sạch.

## 1. Tổng hợp 5 lượt

| Chỉ số | Trung bình | Trung vị | Min | Max | Độ lệch chuẩn |
|---|---|---|---|---|---|
| **MTTD** (gây lỗi → alert activeAt) | **45.5 s** | 47.6 s | 19.1 s | 63.8 s | 16.3 s |
| Alert → Agent đề xuất | 36.5 s | 35.4 s | 35.4 s | 38.7 s | 1.5 s |
| Đề xuất → bắt đầu thực thi | 2.4 s | 2.0 s | 1.0 s | 4.0 s | 1.2 s |
| Thời gian thực thi | 21.2 s | 20.2 s | 18.2 s | 25.5 s | 3.0 s |
| **RTO người dùng** (gây lỗi → phục hồi) | **105.6 s** | 111.0 s | 73.7 s | 120.5 s | 18.7 s |
| **RTO vận hành** (phát hiện → phục hồi) | **60.1 s** | 59.6 s | 54.6 s | 65.9 s | 4.7 s |
| Trang login gián đoạn (probe 2 s) | 78.8 s | 82.6 s | 54.5 s | 90.7 s | 14.0 s |
| Thời gian không ghi được DB | 80.9 s | 88.5 s | 55.9 s | 92.1 s | 14.9 s |
| **RPO** | **0 s** | 0 s | 0 s | 0 s | 0 |
| Bản ghi đã xác nhận bị mất | **0 / 200** | | | | |

## 2. Chi tiết từng lượt

| Lượt | Run ID | MTTD | Alert → đề xuất | Thực thi | RTO người dùng | RTO vận hành | Login gián đoạn | Không ghi được | RPO (mất/ACK) | Baseline sau |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `ABL-DB-01-20261114T072502Z` | 47.6 | 37.3 | 23.1 | 111.0 | 63.4 | 90.7 | 89.5 | 0 s (0/39) | passed |
| 2 | `ABL-DB-01-20261114T073452Z` | 46.4 | 35.4 | 20.2 | 106.0 | 59.6 | 82.2 | 78.5 | 0 s (0/41) | passed |
| 3 | `ABL-DB-01-20261114T074112Z` | 63.8 | 35.4 | 19.2 | 120.5 | 56.7 | 84.2 | 92.1 | 0 s (0/41) | passed |
| 4 | `ABL-DB-01-20261114T074732Z` | 19.1 | 35.4 | 18.2 | 73.7 | 54.6 | 54.5 | 55.9 | 0 s (0/38) | passed |
| 5 | `ABL-DB-01-20261114T075305Z` | 50.8 | 38.7 | 25.5 | 116.7 | 65.9 | 82.6 | 88.5 | 0 s (0/41) | passed |

Đơn vị: giây.

## 3. Chẩn đoán của Agent (giống nhau ở cả 5 lượt)

| Trường | Giá trị |
|---|---|
| Alert kích hoạt | `MoodleNodeRdsTcpFailed` |
| Kịch bản Agent nhận diện | DB-01 ✅ |
| Nguyên nhân (`cause_code`) | `node_rds_path_probe_failed` ✅ |
| Hành động đề xuất | `remove_scoped_db_reject` ✅ (đúng hành động mong đợi) |
| Phạm vi | `staging_moodle_nodes` |
| Độ tin cậy | 0.65 |
| Trạng thái | `candidate_proposed` |
| Số bằng chứng tham chiếu | 4 |
| Tỷ lệ chẩn đoán đúng | **5/5** |

## 4. Dòng thời gian mẫu (lượt 1, `ABL-DB-01-20261114T072502Z`)

| # | Mốc | Thời điểm (UTC) | Cách sau lúc gây lỗi |
|---|---|---|---|
| ① | Gây lỗi: chặn TCP 5432 từ hai node tới RDS | 07:26:11.728 | 0 s |
| ② | Phát hiện: alert `MoodleNodeRdsTcpFailed` bắt đầu (activeAt) | 07:26:59.338 | +47.6 s |
| ③ | Agent ra đề xuất khắc phục | 07:27:36.630 | +84.9 s |
| ④ | Bắt đầu thực thi trực tiếp | 07:27:39.689 | +88.0 s |
| ⑤ | Thực thi xong | 07:28:02.740 | +111.0 s |
| ⑥ | Phục hồi: lượt đầu của 3 synthetic OK liên tiếp | 07:28:02.748 | +111.0 s |

## 5. Trạng thái nhánh thử nghiệm

| Thành phần | Trạng thái |
|---|---|
| Safety Gate | bỏ qua (bypassed) |
| Chữ ký Operator | không có |
| Safe Executor | không dùng |
| Independent Verifier | không dùng |
| Quyền thực thi | test harness trực tiếp, theo allowlist |

## 6. Nhận xét

- **MTTD dao động nhiều nhất** (19.1–63.8 s, độ lệch chuẩn 16.3 s). Nó phụ thuộc vào thời điểm lỗi rơi vào chu kỳ scrape của Prometheus và điều kiện `for:` của rule. Biến động này truyền thẳng sang RTO người dùng: lượt 4 có MTTD thấp nhất nên RTO người dùng cũng thấp nhất (73.7 s).
- **RTO vận hành ổn định** (54.6–65.9 s, độ lệch chuẩn 4.7 s), vì phần do Agent và bước thực thi quyết định gần như cố định:
  - Từ alert đến đề xuất mất khoảng 35–39 s. Phần lớn thời gian này là chu kỳ gom và gửi của Alertmanager cộng hàng đợi Celery.
  - Thực thi mất khoảng 18–25 s.
- **RPO = 0 ở mọi lượt.** DB-01 chặn đường mạng tới RDS chứ không làm hỏng dữ liệu. Trong lúc lỗi, mọi lần ghi đều bị **từ chối** (FAIL), nên ứng dụng biết là chưa lưu; không có bản ghi nào được xác nhận rồi mới mất.
- **Thời gian không ghi được DB** (trung bình 80.9 s) xấp xỉ thời gian trang login gián đoạn (78.8 s). Hai con số này khớp nhau, cho thấy ảnh hưởng tới người dùng chính là mất đường tới DB.
- Nhánh này cho biết **giới hạn dưới của RTO** khi bỏ hết lớp an toàn. Nó dùng để so sánh với luồng đầy đủ (Gate → chữ ký Operator → Executor → Verifier), từ đó thấy chi phí thời gian mà lớp an toàn thêm vào.

## 7. Ghi chú phương pháp

- **Phạm vi:** 5 lượt chính thức chạy liên tiếp lúc 07:25–07:58 UTC. Lượt 1 do người vận hành chạy, lượt 2–5 chạy tự động theo cùng một script. Trước mỗi lượt đều kiểm tra baseline sạch (không còn alert).
- **Lượt chạy thử bị loại:** `ABL-DB-01-20261114T041721Z` (04:17 UTC) chạy bằng phiên bản script trước khi sửa lỗi. Lượt đó chưa ghi được `cause_code` và `confidence`, nên không đưa vào tổng hợp. Số liệu thời gian của nó vẫn nằm trong khoảng trên: MTTD 37.2 s, RTO người dùng 127.6 s, RTO vận hành 90.4 s, RPO 0 (0/40).
- **Cách tính:**
  - Probe trang login và ghi heartbeat chạy 2 giây một lần.
  - Thời điểm phục hồi là synthetic transaction đầu tiên trong chuỗi 3 lần có đăng nhập thành công liên tiếp.
  - Mốc phát hiện lấy từ `activeAt` của alert trong Prometheus.
  - Mốc đề xuất lấy từ `collected_at` của bản ghi `remediation_candidate` trong evidence DB của Agent.
- **Script:** `automation/moodle-rto-rpo-ablation.sh DB-01`. Hướng dẫn: `docs/BAO_CAO_HUONG_DAN_DO_RTO_RPO_DB01_NET01.md`.

## 8. Dữ liệu gốc

Thư mục `terraform/.artifacts/moodle-rto-rpo/<Run ID>/`, mỗi lượt có:

- `result.json`: toàn bộ chỉ số, mốc thời gian và quyết định của Agent
- `login-probe.tsv`: probe trang login
- `heartbeat-writer.tsv`: ghi heartbeat
- `acked.tsv`, `lost.tsv`: bản ghi đã xác nhận và bản ghi bị mất
