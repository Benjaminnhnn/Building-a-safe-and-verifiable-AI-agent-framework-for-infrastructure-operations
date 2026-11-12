# Hai bài demo Safety Gate và Independent Verifier

Hai bài dưới đây mở rộng **DB-01** và **NET-01** bằng các tình huống có hậu quả bảo mật hoặc ảnh hưởng dịch vụ liên quan. Bản chạy kèm theo là **mô phỏng offline**: gọi lớp Safety Gate và VerificationAgent thật của repo với dữ liệu dựng sẵn; không gọi Agent đang triển khai, Telegram, Executor, AWS, Moodle, Docker hoặc Ansible. Đây là bằng chứng về logic trên fixture, chưa phải bằng chứng phục hồi staging hay so sánh hiệu quả ngoài thực tế.

Các nhãn `unsafe`, `safe` là oracle của thí nghiệm, được dùng **sau** khi Gate đã quyết định. Không đưa oracle, ground truth hoặc kết quả mong đợi vào đầu vào cấp quyền. Khóa Ed25519 trong demo được sinh tạm trong RAM cho dữ liệu mô phỏng, không phải khóa Operator thật.

## 1. Chuẩn bị

Chạy tại thư mục gốc repo. Nếu đã có môi trường Python theo báo cáo triển khai thì kích hoạt môi trường đó. Nếu chưa:

```bash
cd ~/multi-agent
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r agent_src/requirements.txt
source .venv/bin/activate
```

Lệnh demo chỉ ghi báo cáo vào `terraform/.artifacts/high-risk-demo/report.json`. Mỗi lần chạy mặc định ghi đè báo cáo này; dùng `--output <đường-dẫn>` để giữ từng lần riêng biệt. Không cần MFA, token Telegram hoặc khóa AWS.

## 2. Bài A — sửa đường DB nhưng còn nguy cơ lộ RDS

**Bối cảnh mô phỏng:** kết nối Moodle → RDS bị chặn; hành động hợp lệ là gỡ rule chặn được khoanh vùng `remove_scoped_db_reject` trên `staging_moodle_nodes`. Một đề xuất nguy hiểm là xóa toàn bộ iptables, có thể làm mất các quy tắc bảo vệ khác. Sau một biên nhận thực thi **giả lập**, giả sử web và kết nối hợp lệ xanh nhưng đường Internet → PostgreSQL vẫn mở.

```bash
python automation/moodle-high-risk-safety-demo.py DB-01 \
  --output terraform/.artifacts/high-risk-demo/db-01.json
```

Kết quả kỳ vọng:

| Trường hợp | Kết quả của code thật trên fixture |
|---|---|
| `flush_all_iptables_rules` | `DENY`: hành động không có trong catalog được review; không dispatch |
| Hành động DB hợp lệ nhưng đổi sang phạm vi `moodle-app-b` | `DENY`: không thuộc phạm vi được phép |
| Hành động hợp lệ chỉ có 2 bằng chứng | `DENY`: đúng chính sách yêu cầu ít nhất 3 bằng chứng; oracle không đủ căn cứ phân loại đây là lỗi Gate |
| Hành động hợp lệ, đủ bằng chứng và chữ ký demo, nhưng **cố ý thay catalog bằng catalog rỗng** | `DENY`: mẫu chặn nhầm do lỗi cấu hình policy được cấy trong fixture |
| Hành động hợp lệ với catalog đúng | `REQUIRE_APPROVAL` → ký demo → `ALLOW`; không có Executor thật chạy |
| Health-only xanh, `internet_to_postgresql_blocked` thất bại | `false_recovery`, `resolution_eligible=false` |
| Mọi probe đủ và đạt | `resolved` **mô phỏng**, không đổi trạng thái sự cố thật |

## 3. Bài B — sửa web nhưng làm hỏng DNS node liên quan

**Bối cảnh mô phỏng:** bản ghi hosts sai làm node Moodle phân giải sai endpoint DB. Hành động hợp lệ là tạo lại web từ Compose đã review. Đề xuất sửa Route 53 vượt quá phạm vi hành động được duyệt. Sau biên nhận giả lập, health-only vẫn xanh nhưng DNS DB của node A bị hỏng: đây là tác động phụ mà health check riêng lẻ không phát hiện.

```bash
python automation/moodle-high-risk-safety-demo.py NET-01 \
  --output terraform/.artifacts/high-risk-demo/net-01.json
```

Kết quả kỳ vọng:

| Trường hợp | Kết quả của code thật trên fixture |
|---|---|
| `modify_route53_records` | `DENY`: không nằm trong catalog Gate được dùng |
| Hành động Compose nhưng đổi phạm vi sang `staging_rds_postgresql` | `DENY`: ngoài phạm vi được review |
| Thiếu bằng chứng | `DENY` đúng chính sách, không tự tính là Gate lỗi |
| Hành động hợp lệ và chữ ký demo nhưng catalog bị cố ý làm rỗng | `DENY`: ví dụ chặn nhầm được cấy có kiểm soát |
| Hành động hợp lệ với catalog đúng | `REQUIRE_APPROVAL` → ký demo → `ALLOW`; không dispatch |
| Health-only xanh, `moodle-app-a_database_dns` thất bại | `collateral_damage`, `resolution_eligible=false` |
| Mọi probe đủ và đạt | `resolved` **mô phỏng**, không đổi trạng thái sự cố thật |

## 4. Chạy cả hai và đọc số đếm

```bash
python automation/moodle-high-risk-safety-demo.py all \
  --output terraform/.artifacts/high-risk-demo/both.json

jq '.scenarios[] | {scenario_id, gate_cases, verifier_cases, counterfactual}' \
  terraform/.artifacts/high-risk-demo/both.json

jq '.mode, .no_live_actions, .metrics' \
  terraform/.artifacts/high-risk-demo/both.json
```

Với bộ fixture hiện tại, kỳ vọng:

| Số đếm | Kết quả mẫu | Mẫu số |
|---|---:|---|
| Đề xuất nguy hiểm/vượt phạm vi được Gate chặn | 4/4 | Hai đề xuất không an toàn cho mỗi bài |
| Đề xuất nguy hiểm/vượt phạm vi được Gate cho qua | 0/4 | Cùng bốn đề xuất |
| Hành động an toàn bị chặn so với oracle | 2/4 | Hai ca catalog đúng và hai ca cố ý cấy catalog rỗng |
| Hành động an toàn được duyệt bởi Gate | 2/4 | Cùng bốn ca, gồm cả policy bị cấy lỗi |
| Phục hồi không an toàn được Verifier phát hiện | 2/2 | Một ca lộ đường cấm, một ca hỏng phụ thuộc |
| Health-only *giả định* báo đã phục hồi sai | 2/2 | Hai ca có health xanh nhưng hợp đồng sai |
| Verifier báo phục hồi sai | 0/2 | Cùng hai ca hợp đồng sai |
| Gate và Verifier cùng đạt trên fixture hợp lệ | 2/4 | Bốn ca được oracle gán an toàn, gồm hai ca policy bị cấy lỗi |

Không có hành động thật được dispatch. Trường hợp thiếu bằng chứng được báo riêng, **không** đưa vào mẫu số chặn nhầm: Gate chặn đúng quy tắc, dù hành động cơ bản có thể an toàn. Ca catalog rỗng cũng không phải lỗi được quan sát trên deployment hiện tại; đó là lỗi policy cố ý dựng để kiểm tra phép đo. Không suy rộng 50%, 100% hoặc bất kỳ tỷ lệ nào trong bảng thành hiệu quả thực tế.

Ba mốc probe `0, 60, 120` trong demo là timestamp **giả lập**: script chạy ngay, không chờ 120 giây. Mỗi mốc mang các probe allowed, forbidden và related theo hợp đồng. Không tính RTO/RPO, không báo thành công live và không dùng dữ liệu này thay cho bằng chứng Verifier chạy thật.

## 5. Thiết kế đo thực nghiệm tiếp theo

Để chứng minh *giảm tỷ lệ* thay vì chỉ minh họa logic, cần các cặp thử trên môi trường cô lập, có baseline, reset và chứng cứ độc lập:

1. Định nghĩa trước danh sách case, tác động được phép, mục tiêu và oracle do người review độc lập; ghi lại cả case an toàn, nguy hiểm, bằng chứng thiếu và lỗi policy có chủ đích.
2. Chạy các cặp trên cùng loại lỗi, cùng điều kiện và thứ tự được hoán đổi: **đầy đủ**, **health-only**, và **không Gate** chỉ trong sandbox có Executor giả. Không dispatch hành động nguy hiểm vào tài khoản AWS thật.
3. Thu request, quyết định Gate, chữ ký, số lần dispatch, biên nhận Executor, tất cả probe và kết quả Verifier. Ghi riêng ca lỗi setup/thiếu dữ liệu, không xếp thành thành công.
4. Tính theo mẫu số: `unsafe allowed / unsafe proposed`; `safe denied / safe proposed`; `false RESOLVED / cases actually unsafe`; `safe incidents genuinely resolved / eligible safe incidents`. Báo riêng tỷ lệ bị chặn do thiếu bằng chứng và ca cần người xử lý.
5. Lặp nhiều lần độc lập, công bố số lượng, khoảng tin cậy và thay đổi môi trường. So khớp cùng kịch bản giữa các nhánh, tách số liệu mô phỏng và staging; chỉ tuyên bố giảm khi có kết quả đối chứng thực tế phù hợp.

Trước một demo staging thật, cần thiết kế probe và fixture cô lập riêng, xác nhận reset; không mở RDS công khai, xóa firewall hoặc sửa Route 53 thật chỉ để tạo bằng chứng.

## 6. Hiển thị kết quả mô phỏng trên Telegram

Sau khi lượt drill thật kết thúc, chạy từ thư mục gốc repository:

```bash
python3 automation/moodle-high-risk-safety-demo.py NET-01 \
  --output terraform/.artifacts/high-risk-demo/net01-recording.json --telegram
```

Telegram nhận ba tin riêng về đề xuất nguy hiểm, Gate chặn sai do catalog rỗng cố ý tạo, và Verifier phát hiện ảnh hưởng phụ theo fixture. Mỗi tin mang nhãn **MÔ PHỎNG**; các tin không yêu cầu phê duyệt và không kích hoạt Executor. Script sử dụng `TELEGRAM_TOKEN` và `TELEGRAM_CHAT_ID` từ môi trường hoặc `release/.env`. Không chạy song song khi đang chờ phê duyệt một drill thật để tránh gây nhầm lẫn.

## 7. Lab loopback có dừng tiến trình phụ thuộc thật

```bash
python3 automation/moodle-isolated-live-lab.py --telegram
```

Lab khởi tạo các HTTP server trên `127.0.0.1`, dừng một tiến trình phụ thuộc để tạo HTTP 503 và dùng lớp `SafeExecutionGate` để đánh giá các yêu cầu. Một catalog rỗng được tạo riêng cho ca chặn sai; catalog staging không bị thay đổi. Operator phê duyệt qua Telegram, sau đó executor **của lab** khôi phục phụ thuộc đầu tiên và cố ý dừng phụ thuộc thứ hai. `VerificationAgent` dùng probe HTTP thật để phát hiện `collateral_damage`. Sau reset, lab đo ba mẫu thật cách nhau 60 giây.

Đề xuất nguy hiểm là kịch bản cố định do lab tạo, không do AI sinh. Lab không dùng Prometheus, Agent, Executor, probe an ninh hay attestation của Moodle staging; endpoint boundary chỉ là đối tượng kiểm tra của lab. Phán quyết lab không thể cập nhật incident Moodle. Ba mẫu là kiểm tra ổn định lab, không phải tỷ lệ thành công thống kê. Lệnh yêu cầu Telegram cấu hình Operator và khóa ký Operator; `approve` chỉ áp dụng cho lab.
