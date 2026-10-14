# Báo cáo và runbook quay demo Moodle staging

> **Kiểm tra bằng chứng ngày 2026-10-07:** Đây là tài liệu lịch sử, không phải xác nhận readiness hiện tại. Các artifact raw được trích dẫn cho CON-01 (`CON-01-20261001T105413Z.json` và `live-agent-shadow.json`) không có trong checkout này. Vì vậy các kết quả staging được mô tả bên dưới chưa được xác minh lại và không được trình bày như bằng chứng live cho đến khi artifact gốc được khôi phục, kiểm tra provenance và đối chiếu.

Ngày kiểm tra: 2026-10-02 (Asia/Bangkok)
Scenario đề xuất: **CON-01 — dừng Moodle web container trên một node**

## 1. Kết luận sẵn sàng

Hệ thống **đủ điều kiện quay demo luồng Moodle staging → ALB → Prometheus /
Alertmanager → AI Agent shadow → reset → verifier**. Đợt kiểm tra trạng thái
ngày 2026-10-02 chỉ dùng lệnh đọc trạng thái, không tạo fault mới.

| Thành phần | Kết quả kiểm tra |
|---|---|
| EC2 | `monitor-ai-01`, `moodle-app-a`, `moodle-app-b` đều `running` |
| RDS PostgreSQL | `available`, lớp `db.t4g.micro` |
| EFS | `available`, mã hóa bật; hai Moodle node đang mount kiểu `nfs4` |
| ALB | `active`; cả hai target Moodle `healthy` |
| Moodle | `/healthz.php` trả HTTP 200; hai web container `running/healthy` |
| Synthetic transaction | Prometheus có lần chạy thành công trong 180 giây gần nhất |
| Cron | Container `release-moodle-cron-1` đang chạy trên `moodle-app-a`; lần kiểm tra không thấy cron container trên `moodle-app-b` (baseline gate kiểm tra runner ở app-a) |
| Prometheus | Tất cả scrape target đang `up`; không có alert đang firing |
| Alertmanager | Đang chạy và route được cấu hình tới Agent webhook |
| AI Agent | Container healthy; `AIOPS_UNIFIED_CORE_MODE=shadow`; Celery queue `0` |
| Grafana | API health trả database `ok` |
| Fault marker | Không còn marker scenario trên hai app node |

URL Moodle hiện tại được Terraform cấp qua ALB:

```text
http://moodle-staging-alb-305664669.ap-southeast-1.elb.amazonaws.com
```

Lưu ý: `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` chưa được cấu hình trong Agent.
Alertmanager → Agent và xử lý shadow hoạt động, nhưng **không demo được gửi tin
Telegram**. Không đưa token vào terminal đang ghi hình hoặc commit vào repo.

## 2. Kết quả CON-01 được ghi nhận lịch sử, chưa xác minh lại

Tài liệu cũ ghi rằng ngày 2026-10-01 đã chạy drill trên staging: chỉ dừng
`release-moodle-web-1` ở `moodle-app-b`, để Moodle node còn lại tiếp tục phục
vụ. Các kết quả dưới đây là nội dung được báo cáo khi đó, chưa được xác minh lại:

- Prometheus phát hiện `MoodleWebContainerMissing`; Alertmanager gửi alert tới
  AI Agent và Celery queue sau xử lý trở về 0.
- Sau khi bổ sung scenario marker vào injector, Agent ghi nhận
  `scenario_id=CON-01`, trạng thái `shadow_complete`, đủ sáu stage và bốn
  evidence reference.
- Agent không tự thực thi hành động và không tự kết luận resolved:
  `execution_permitted=false`, `resolution_eligible=false` — đúng với shadow
  mode.
- Reset thành công. Moodle container healthy, hai ALB target healthy, synthetic
  transaction thành công, alert được resolve và scenario verifier pass.

Lần chạy đầu cho thấy alert thiếu `scenario_id`; nguyên nhân là helper tạo
marker đã có nhưng injector CON-01 chưa gọi helper. Injector local đã được cập
nhật để publish marker trước khi dừng container. Trong lần chạy sau, một alert
liên quan `MoodleSyntheticTransactionFailed` vẫn bị escalate vì tín hiệu này
không khớp contract CON-01; đây là hành vi fail-closed, không tính là scenario
match thành công.

Evidence đã lưu:

- Drill/reset/verifier JSON:
  `terraform/.artifacts/moodle-faults/CON-01-20261001T105413Z.json`
- Kết quả Agent shadow đã lọc log nhạy cảm:
  `terraform/.artifacts/sprint7-demo/live-agent-shadow.json`
- Báo cáo Sprint 7 và phần giới hạn ERPNext:
  `docs/SPRINT7_DEMO_REPORT.md`

Tài liệu cũ kết luận rằng demo chứng minh luồng staging Moodle. Do artifact gốc
đang thiếu, kết luận đó chưa được xác minh và không được dùng làm bằng chứng
hiện tại. Nó cũng không đóng Sprint 7 theo acceptance ERPNext và không phải
kết quả benchmark 50+50 ablation.

## 3. Bố trí màn hình khi quay

Chuẩn bị ba terminal:

- **Terminal 1:** SSH tunnel tới Prometheus, Alertmanager và Grafana; để lệnh
  tunnel chạy trong suốt buổi quay.
- **Terminal 2:** baseline, inject và reset fault.
- **Terminal 3:** xem trạng thái ALB và log Agent trong lúc fault diễn ra.

Mở các địa chỉ sau sau khi tunnel đã kết nối:

- Moodle: URL ALB ở phần 1.
- Prometheus: `http://localhost:9090/graph`.
- Alertmanager: `http://localhost:9093`.
- Grafana: `http://localhost:3000`.

Trong Prometheus, chuẩn bị biểu thức:

```promql
ALERTS{alertname="MoodleWebContainerMissing",alertstate="firing"}
```

Ghi chú: kết quả drill CON-01 ngày 2026-10-01 bên dưới là bằng chứng lịch sử
và đã ghi nhận scenario marker trong Agent shadow. Mã nguồn hiện tại không còn
đưa marker vào Prometheus; truy vấn ở trên chỉ kiểm tra triệu chứng. Chưa chạy
lại drill staging sau thay đổi này.

## 4. Các bước và lệnh quay demo

### Bước 1 — Mở tunnel monitoring

Chạy ở Terminal 1:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config -N \
  -L 9090:127.0.0.1:9090 \
  -L 9093:127.0.0.1:9093 \
  -L 3000:127.0.0.1:3000 \
  monitor-ai-01
```

`-L` chuyển tiếp cổng từ máy local tới cổng chỉ bind trên monitor node; `-N`
không mở shell từ xa. Terminal này cần để nguyên mở. Mở các trang monitoring
trên trình duyệt trước khi bắt đầu quay.

### Bước 2 — Kiểm tra URL và baseline

Chạy ở Terminal 2, từ thư mục gốc repo:

```bash
export AWS_PROFILE=target-account

MOODLE_URL=$(terraform -chdir=terraform output -json moodle_application | jq -r .url)
printf 'Moodle URL: %s\n' "$MOODLE_URL"
curl -sS -o /dev/null -w 'health HTTP %{http_code}\n' "$MOODLE_URL/healthz.php"

AWS_PROFILE=target-account bash automation/moodle-environment-baseline.sh verify
```

Ý nghĩa:

- `AWS_PROFILE=target-account` buộc AWS CLI dùng profile đã dành cho staging,
  tránh vô tình dùng default profile.
- `terraform output ... .url` lấy URL ALB từ Terraform state thay vì hard-code
  vào script.
- `curl ... /healthz.php` kiểm tra endpoint health công khai; kỳ vọng HTTP 200.
- `moodle-environment-baseline.sh verify` kiểm tra runtime, EFS, ALB targets,
  Prometheus, cron, alert và chạy authenticated synthetic CRUD transaction.
  Synthetic transaction tạo fixture tạm rồi xóa; vì vậy đây là baseline gate có
  ghi/đọc dữ liệu thử, không phải lệnh thuần chỉ đọc.

Chỉ tiếp tục nếu baseline báo `passed`. Nếu baseline lỗi, **không inject fault**.

### Bước 3 — Inject fault CON-01

Khi đang quay, giải thích rằng chỉ web container trên `moodle-app-b` sẽ bị dừng.
Sau đó chạy ở Terminal 2:

```bash
MOODLE_FAULT_CONFIRM=staging AWS_PROFILE=target-account \
  bash automation/moodle-fault-inject.sh CON-01
```

Ý nghĩa:

- `MOODLE_FAULT_CONFIRM=staging` là xác nhận tường minh rằng đây là fault trên
  staging.
- Injector tạo marker `CON-01` để alert giữ được `scenario_id`, rồi dừng đúng
  container `release-moodle-web-1` trên `moodle-app-b`.
- Lệnh không dừng EC2, không thay đổi RDS/EFS và không thay đổi Security Group.
- Đợi khoảng 30–90 giây để Prometheus phát hiện trạng thái container và ALB
  cập nhật target health.

### Bước 4 — Quan sát ALB, alert và Agent

Ở Terminal 3, kiểm tra target health:

```bash
TG_ARN=$(terraform -chdir=terraform output -json moodle_application | jq -r .target_group_arn)

AWS_PROFILE=target-account aws elbv2 describe-target-health \
  --region ap-southeast-1 \
  --target-group-arn "$TG_ARN" \
  --query 'TargetHealthDescriptions[].{State:TargetHealth.State,Reason:TargetHealth.Reason}' \
  --output table
```

Trong lúc fault, kỳ vọng một target không healthy và target còn lại healthy.
Website có thể vẫn truy cập được qua node còn lại.

Trên Prometheus, chạy query ở phần 3. Kỳ vọng alert trên instance
`moodle-app-b`; query không tiết lộ scenario ID.

Để hiện kết quả Agent shadow trong terminal:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  'sudo docker logs --since 5m moodle-agent-worker 2>&1 \
   | grep -F "Moodle shadow pipeline result:" \
   | grep -F "\"scenario_id\": \"CON-01\"" \
   | tail -5'
```

Tìm `status: shadow_complete`, `execution_permitted: false` và
`resolution_eligible: false`. Đây là bằng chứng Agent đã xử lý alert nhưng vẫn
không tự sửa staging ở shadow mode.

### Bước 5 — Reset fault và xác minh phục hồi

Sau khi đã quay alert và Agent result, chạy lần lượt ở Terminal 2:

```bash
AWS_PROFILE=target-account bash automation/moodle-fault-reset.sh CON-01

AWS_PROFILE=target-account bash automation/moodle-environment-baseline.sh verify

AWS_PROFILE=target-account bash automation/moodle-scenario-verify.sh CON-01
```

Ý nghĩa:

- `moodle-fault-reset.sh CON-01` khởi động lại service bằng Compose đã review,
  gỡ marker của scenario và chờ cả hai ALB target healthy.
- Chạy lại baseline để xác nhận synthetic transaction, DB/EFS, cron, Prometheus
  và alert đã trở về trạng thái ổn.
- `moodle-scenario-verify.sh CON-01` chạy các recovery probe chỉ đọc: synthetic
  thành công mới đây, alert CON-01 không còn firing, web container healthy và
  cả hai ALB target healthy. Kết quả mong đợi là `status: passed`.

## 5. Điều kiện dừng an toàn

- Không chạy `moodle-fault-trial.sh` song song với sequence manual; script đó tự
  inject và reset nên sẽ làm demo khó điều khiển.
- Nếu target health hoặc alert không đổi sau tối đa khoảng 90 giây, dừng quay
  phần lỗi và chạy reset ngay:

  ```bash
  AWS_PROFILE=target-account bash automation/moodle-fault-reset.sh CON-01
  ```

- Nếu lệnh reset lỗi, không tiếp tục fault khác; giữ nguyên màn hình lỗi và xử
  lý phục hồi trước.
- Không chạy Terraform apply/destroy trong lúc demo.
- Khi quay, không hiển thị password, PAT, SSH private key, secret file hoặc
  nội dung `.env`.

Một lần diễn tập tự động để tạo JSON tổng hợp (không dùng trong đoạn quay có
dừng để giải thích) là:

```bash
MOODLE_FAULT_CONFIRM=staging AWS_PROFILE=target-account \
  bash automation/moodle-fault-trial.sh CON-01
```

Script này tự kiểm tra baseline, inject, chờ alert, reset và lưu evidence trong
`terraform/.artifacts/moodle-faults/`.

## 6. Bổ sung vào cùng kịch bản: RAG/Gemini, Safe Gate và Independent Verifier

### Trạng thái đã kiểm tra ngày 2026-10-02

- Terraform apply cập nhật CIDR thành công: `0 added, 3 changed, 0 destroyed`;
  plan chạy lại báo `No changes`.
- ALB vẫn có 2/2 target `healthy`; `/healthz.php` trả HTTP 200; URL Moodle trả
  HTTP 303 (redirect bình thường).
- Agent và worker đang ở `AIOPS_UNIFIED_CORE_MODE=shadow`, giới hạn Gemini là
  `GEMINI_MAX_ATTEMPTS=1` và `GEMINI_MAX_REMOTE_CALLS=1`.
- Hai container không có `GEMINI_API_KEY`; log worker 24 giờ gần nhất không có
  dấu vết gọi Gemini hoặc xử lý alert qua RAG.
- Tôi đã truy vấn RAG riêng trong worker: RAG khả dụng và trả ngữ cảnh runbook
  (3174 ký tự). Đây chỉ là kiểm tra RAG độc lập, không chứng minh alert Moodle
  được đưa qua RAG.

Vì vậy, câu trả lời chính xác khi quay là: **Agent hiện chưa gọi Gemini để đề
xuất giải pháp trong luồng Moodle staging**. Nhánh `shadow` của
`process_moodle_alert()` thu thập observation/evidence rồi return trước RAG và
`run_agent_workflow()`. Biến `GEMINI_MAX_REMOTE_CALLS=1` chỉ đặt giới hạn khi
đã có credential và code đi tới bước gọi; biến này không tự bật Gemini.

### Bổ sung các cảnh quay vào sequence ở trên

Giữ nguyên CON-01 live staging ở các bước 1–5 của kịch bản này. Sau khi reset
và scenario verifier đã pass, tiếp tục ngay trong cùng buổi quay:

#### Cảnh A — Nói rõ kết quả RAG/Gemini

Trình bày phần trạng thái ở trên. Có thể mở log Agent shadow đã có trong
`terraform/.artifacts/sprint7-demo/live-agent-shadow.json`. Không khẳng định
Gemini đã đề xuất; không đổi Agent sang `live` và không inject thêm fault chỉ
để tạo log Gemini.

#### Cảnh B — Safe Gate và human approval (offline, không sửa hạ tầng)

Chạy helper demo:

```bash
PYTHONPATH=agent_src python3 automation/moodle-safe-gate-verifier-demo.py
```

Kết quả mong đợi:

```text
Safe Gate, no approval: REQUIRE_APPROVAL
Safe Gate, exact human approval: ALLOW
Adapter dispatch: none (no mutation)
```

Giải thích trên màn hình: request là probe `READ_HEALTH` đã có trong catalog
DB-01 staging, trỏ tới `postgres-db` và có ba evidence tham chiếu fixture. Với
confidence 0.72, gate yêu cầu người duyệt. Không có approval thì bị giữ lại
(`REQUIRE_APPROVAL`); approval ký cho đúng hash của probe mới cho kết quả
`ALLOW`. Nếu action hoặc scope đổi thì approval không còn khớp. Đây là probe
đọc-only nên không cần rollback. Helper dừng trước adapter, do đó không có
hành động nào được chạy trên Moodle. Demo không xác nhận action sửa lỗi DB-01
đã có cặp rollback thực thi được.

#### Cảnh C — Independent Verifier không tin mỗi health xanh

Helper tiếp tục chạy hai bộ probe giả lập:

```text
Independent Verifier, health green but forbidden path open: false_recovery; resolution_eligible=False
Independent Verifier, fixture contract + 120s stability: resolved; simulated=True
```

Giải thích: trường hợp đầu, ứng dụng khỏe nhưng đường truy cập PostgreSQL bị
cấm vẫn mở, nên verifier phát hiện `false_recovery` và không cho resolve. Trường
hợp thứ hai có đủ allowed/forbidden/related contract probe và cửa sổ ổn định 120
giây, nên logic verifier cho verdict `resolved`; nhưng `simulated=True` nghĩa là
đây là fixture offline, không phải bằng chứng phục hồi live. Independent
Verifier chỉ xác nhận sau khi kiểm tra contract và stability; nó không có quyền
thực thi remediation.

JSON của helper nằm ở
`terraform/.artifacts/sprint7-demo/safe-gate-verifier.json` và bị Git ignore.
Helper dùng fixture cùng khóa ký giả lập, không dùng secret AWS, không gửi alert,
không gọi Gemini và không mutate staging.

### Ranh giới cần nói rõ khi kết thúc demo

Buổi demo này có hai phần liền mạch nhưng khác loại bằng chứng:

1. **CON-01** là fault observation/reset/verifier trên Moodle staging thật; Agent
   xử lý shadow và không tự sửa.
2. **Safe Gate/Independent Verifier** là functional demo offline; chứng minh
   quyết định approval và quy tắc xác minh, không chứng minh adapter đã thực thi
   hoặc incident live đã resolve.

Hiện chưa có bằng chứng cho một pipeline end-to-end
`Alert → RAG → Gemini → đề xuất → Safe Gate → human approval → adapter →
Independent Verifier`. Muốn demo đúng pipeline đó cần nối nhánh Moodle với RAG/LLM
và adapter/verifier, cấu hình credential Gemini an toàn, rồi mới thiết kế một
test có kiểm soát. Không đưa API key lên màn hình quay.
