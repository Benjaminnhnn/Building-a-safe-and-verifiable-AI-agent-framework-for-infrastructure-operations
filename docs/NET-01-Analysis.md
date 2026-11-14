# Kết quả NET-01, nhánh "Agent không có Safety Gate / Executor / Verifier": 5 lượt

Ngày đo: 14/11/2026, từ 08:24 đến 08:56 UTC, trên staging AWS (Moodle image `c1c0ceb9`, Agent image `b342f799`, chế độ `investigate`).

**Nhánh thử nghiệm:** Agent hoạt động nhưng **không có** Safety Gate, chữ ký Operator, Safe Executor và Independent Verifier. Đề xuất của Agent được thực thi trực tiếp, và hệ thống được coi là phục hồi khi có 3 synthetic transaction thành công liên tiếp.

**Lỗi gây ra:** làm sai phân giải tên máy chủ DB **bên trong container web** (`/etc/hosts`) trên cả hai node. Hệ quả là ứng dụng mất đường tới RDS, trong khi đường mạng của chính node vẫn bình thường.

**Kết quả chung:** cả 5/5 lượt thành công. Agent chẩn đoán đúng nguyên nhân và đề xuất đúng hành động ở mọi lượt, hệ thống phục hồi, **không mất bản ghi nào đã xác nhận (0/249)**, và baseline sau mỗi lượt đều sạch.

## 1. Tổng hợp 5 lượt

| Chỉ số | Trung bình | Trung vị | Min | Max | Độ lệch chuẩn |
|---|---|---|---|---|---|
| **MTTD** (gây lỗi → alert activeAt) | **53.4 s** | 49.2 s | 43.9 s | 67.1 s | 10.5 s |
| Alert → Agent đề xuất | 20.8 s | 20.5 s | 20.5 s | 22.2 s | 0.7 s |
| Đề xuất → bắt đầu thực thi | 2.0 s | 1.4 s | 0.4 s | 3.8 s | 1.5 s |
| Thời gian thực thi (tạo lại container web trên 2 node) | 57.7 s | 56.3 s | 52.2 s | 67.1 s | 5.6 s |
| **RTO người dùng** (gây lỗi → phục hồi) | **133.9 s** | 135.1 s | 122.4 s | 148.1 s | 10.8 s |
| **RTO vận hành** (phát hiện → phục hồi) | **80.5 s** | 80.1 s | 73.2 s | 90.7 s | 6.5 s |
| Trang login gián đoạn (probe 2 s) | 84.1 s | 80.7 s | 78.9 s | 91.1 s | 5.6 s |
| Thời gian không ghi được DB | 80.1 s | 76.3 s | 73.8 s | 89.4 s | 7.5 s |
| **RPO** | **0 s** | 0 s | 0 s | 0 s | 0 |
| Bản ghi đã xác nhận bị mất | **0 / 249** | | | | |

## 2. Chi tiết từng lượt

| Lượt | Run ID | MTTD | Alert → đề xuất | Thực thi | RTO người dùng | RTO vận hành | Login gián đoạn | Không ghi được | RPO (mất/ACK) | Baseline sau |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `ABL-NET-01-20261114T082351Z` | 61.9 | 20.5 | 52.2 | 135.1 | 73.2 | 89.2 | 87.1 | 0 s (0/48) | passed |
| 2 | `ABL-NET-01-20261114T083205Z` | 49.2 | 22.2 | 67.1 | 139.9 | 90.7 | 78.9 | 76.3 | 0 s (0/53) | passed |
| 3 | `ABL-NET-01-20261114T083841Z` | 43.9 | 20.5 | 56.3 | 124.1 | 80.1 | 80.5 | 73.9 | 0 s (0/49) | passed |
| 4 | `ABL-NET-01-20261114T084459Z` | 67.1 | 20.5 | 56.7 | 148.1 | 81.0 | 91.1 | 89.4 | 0 s (0/51) | passed |
| 5 | `ABL-NET-01-20261114T085137Z` | 44.9 | 20.5 | 56.1 | 122.4 | 77.4 | 80.7 | 73.8 | 0 s (0/48) | passed |

Đơn vị: giây. Lượt 1 do người vận hành chạy; lượt 2–5 chạy tự động bằng cùng một script, giống cách làm với DB-01.

## 3. Chẩn đoán của Agent (giống nhau ở cả 5 lượt)

| Trường | Giá trị |
|---|---|
| Alert kích hoạt | `MoodleSyntheticTransactionFailed` |
| Kịch bản Agent nhận diện | NET-01 ✅ |
| Nguyên nhân (`cause_code`) | `application_path_not_isolated` ✅ |
| Hành động đề xuất | `recreate_moodle_web_from_reviewed_compose` ✅ (đúng hành động mong đợi) |
| Phạm vi | `staging_moodle_nodes` |
| Độ tin cậy | 0.8 |
| Trạng thái | `candidate_proposed` |
| Số bằng chứng tham chiếu | 7 |
| Tỷ lệ chẩn đoán đúng | **5/5** |

## 4. Dòng thời gian mẫu (lượt 1, `ABL-NET-01-20261114T082351Z`)

| # | Mốc | Thời điểm (UTC) | Cách sau lúc gây lỗi |
|---|---|---|---|
| ① | Gây lỗi: sửa `/etc/hosts` trong container web trên 2 node | 08:24:57.412 | 0 s |
| ② | Phát hiện: alert `MoodleSyntheticTransactionFailed` bắt đầu (activeAt) | 08:25:59.338 | +61.9 s |
| ③ | Agent ra đề xuất khắc phục | 08:26:19.868 | +82.5 s |
| ④ | Bắt đầu thực thi: tạo lại container web từ compose đã review | 08:26:20.272 | +82.9 s |
| ⑤ | Thực thi xong (container healthy trên cả 2 node) | 08:27:12.513 | +135.1 s |
| ⑥ | Phục hồi: lượt đầu của 3 synthetic OK liên tiếp | 08:27:12.519 | +135.1 s |

## 5. So sánh NET-01 với DB-01 (cùng nhánh, mỗi kịch bản 5 lượt)

| Chỉ số (trung bình) | DB-01 | NET-01 | Chênh lệch |
|---|---|---|---|
| MTTD | 45.5 s | 53.4 s | +7.9 s |
| Alert → Agent đề xuất | 36.5 s | 20.8 s | −15.7 s |
| Thời gian thực thi | 21.2 s | 57.7 s | +36.5 s |
| **RTO người dùng** | **105.6 s** | **133.9 s** | +28.3 s |
| **RTO vận hành** | **60.1 s** | **80.5 s** | +20.4 s |
| Trang login gián đoạn | 78.8 s | 84.1 s | +5.3 s |
| Không ghi được DB | 80.9 s | 80.1 s | −0.8 s |
| RPO | 0 s (0/200) | 0 s (0/249) | 0 |
| Độ tin cậy chẩn đoán | 0.65 | 0.8 | |
| Chẩn đoán đúng | 5/5 | 5/5 | |

## 6. Nhận xét

- **Thực thi là phần chiếm nhiều thời gian nhất của NET-01** (khoảng 58 s, gần gấp 3 DB-01).
  - Hành động phải tạo lại container web **lần lượt** trên hai node và chờ healthcheck báo healthy ở mỗi node.
  - DB-01 chỉ cần gỡ một luật iptables.
  - Đây là nguyên nhân chính khiến RTO vận hành của NET-01 cao hơn DB-01 khoảng 20 s.
- **Thời gian từ alert đến đề xuất rất ổn định** (20.5 s ở 4/5 lượt).
  - Agent nhận alert và hoàn tất điều tra trong một khoảng gần như cố định.
  - Chênh lệch với DB-01 (khoảng 36 s) nhiều khả năng đến từ việc gom nhóm và gửi của Alertmanager cho từng alert, chứ không phải tốc độ suy luận của Agent. Điểm này chưa được tách đo riêng.
- **MTTD dao động nhiều nhất** (43.9–67.1 s). `MoodleSyntheticTransactionFailed` phụ thuộc vào chu kỳ chạy synthetic transaction cộng chu kỳ đánh giá rule. Biến động này truyền thẳng sang RTO người dùng: lượt 4 có MTTD cao nhất nên RTO người dùng cũng cao nhất (148.1 s).
- **RTO vận hành ổn định** (độ lệch chuẩn 6.5 s), vì phần do Agent và bước thực thi gần như cố định.
- **RPO = 0 ở mọi lượt.**
  - Lỗi làm ứng dụng không tìm được DB, nên mọi lần ghi trong lúc lỗi đều bị **từ chối** (FAIL) chứ không bị mất âm thầm.
  - Tạo lại container web không động tới dữ liệu, vì dữ liệu nằm trên RDS và EFS.
- Nhánh này cho biết **giới hạn dưới của RTO** khi bỏ hết lớp an toàn. Nó dùng để so sánh với luồng đầy đủ (Gate → chữ ký Operator → Executor → Verifier), từ đó thấy chi phí thời gian mà lớp an toàn thêm vào.

## 7. Ghi chú phương pháp

- **Cách chọn 5 lượt:** dùng cùng quy tắc với DB-01, tức là lượt cuối cùng do người vận hành chạy cộng 4 lượt tự động liền sau, tất cả chạy liên tiếp và trước mỗi lượt đều kiểm tra baseline sạch.
- **Hai lượt hợp lệ không đưa vào tổng hợp:** hai lượt người vận hành chạy trước đó cùng ngày, với cùng phiên bản script. Cả hai đều thành công: chẩn đoán đúng, RPO 0, baseline `passed`. Số liệu được ghi lại để minh bạch:

  | Run ID | MTTD | Alert → đề xuất | Thực thi | RTO người dùng | RTO vận hành | RPO |
  |---|---|---|---|---|---|---|
  | `ABL-NET-01-20261114T080606Z` | 45.2 | 20.5 | 58.8 | 126.4 | 81.2 | 0 s (0/50) |
  | `ABL-NET-01-20261114T081520Z` | 83.5 | 20.5 | 54.8 | 162.9 | 79.4 | 0 s (0/49) |

  Nếu tính cả 7 lượt, trung bình là: MTTD 56.5 s, RTO người dùng 137.0 s, RTO vận hành 80.4 s, RPO 0 (0/348). Kết luận không thay đổi.
- **Lượt bị loại:** thư mục `ABL-NET-01-20261113T155950Z` (13/11) rỗng. Đó là lượt chạy thử bằng phiên bản script cũ, không có kết quả.
- **Cách tính:**
  - Probe trang login và ghi heartbeat chạy 2 giây một lần.
  - Heartbeat được ghi vào bảng `config_plugins` qua container web của node A, tức là cùng đường DB mà Moodle dùng.
  - Thời điểm phục hồi là synthetic transaction đầu tiên trong chuỗi 3 lần có đăng nhập thành công liên tiếp.
  - Mốc phát hiện lấy từ `activeAt` của alert trong Prometheus.
  - Mốc đề xuất lấy từ `collected_at` của bản ghi `remediation_candidate` trong evidence DB của Agent.
- **Script:** `automation/moodle-rto-rpo-ablation.sh NET-01`. Hướng dẫn: `docs/BAO_CAO_HUONG_DAN_DO_RTO_RPO_DB01_NET01.md`. Kết quả DB-01 nằm trong `DB-01-Analysis.md`.

## 8. Dữ liệu gốc

Thư mục `terraform/.artifacts/moodle-rto-rpo/<Run ID>/`, mỗi lượt có:

- `result.json`: toàn bộ chỉ số, mốc thời gian và quyết định của Agent
- `login-probe.tsv`: probe trang login
- `heartbeat-writer.tsv`: ghi heartbeat
- `acked.tsv`, `lost.tsv`: bản ghi đã xác nhận và bản ghi bị mất
