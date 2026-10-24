# Hướng dẫn demo 3 lỗi đại diện

**Cập nhật:** 23/10/2026<br>
**Dành cho:** người trực tiếp chạy demo trên staging.<br>
**Danh mục đầy đủ 15 tình huống:** [MOODLE_FAULT_SCENARIO_CATALOG.md](MOODLE_FAULT_SCENARIO_CATALOG.md).

Ba lỗi được chọn để cho người xem thấy **ba kết cục khác nhau** của cùng một hệ thống an toàn:

| # | Lỗi | Lớp | Kết cục Safety Gate | Thông điệp chính | Thời lượng |
|---|---|---|---|---|---|
| 1 | **CON-01** web container node B dừng | Hạ tầng | Người **duyệt** trên Telegram → Executor sửa → Verifier `RESOLVED` | AI đề xuất, **người** quyết, chỉ **Verifier** được tuyên bố đã khắc phục | khoảng 13 phút |
| 2 | **SSO-13** issuer Okta bị tắt | Xác thực | **Chỉ người được sửa**: Agent chẩn đoán đúng nhưng không có quyền sửa | Có những lỗi AI biết sửa nhưng **không được phép** sửa | khoảng 6 phút |
| 3 | **RES-01** quá tải CPU node B | Tài nguyên | Người **từ chối** trên Telegram → không có gì được thực thi | Phê duyệt là quyết định thật: bấm *Từ chối* thì hệ thống không đổi | khoảng 6 phút |

> **Trạng thái đã kiểm chứng:** lỗi 1 đã chạy thật trọn chuỗi kèm duyệt trên Telegram (run 8, 23/10/2026). Lỗi 2 đã
> chạy thật (23/10/2026). Lỗi 3 đã chạy thử đạt các bước ①–⑤ (23/10/2026, mục 4.5). Lỗi 3 không đi tới thực thi:
> drill tích hợp chưa hỗ trợ RES-01.

---

## Mục lục

1. [Chuẩn bị (trước demo 30 phút)](#1-chuẩn-bị-trước-demo-30-phút)
2. [Lỗi 1: CON-01, duyệt và tự khôi phục](#2-lỗi-1-con-01)
3. [Lỗi 2: SSO-13, chỉ người được sửa](#3-lỗi-2-sso-13)
4. [Lỗi 3: RES-01, người từ chối ở Safety Gate](#4-lỗi-3-res-01)
5. [Dọn dẹp sau demo](#5-dọn-dẹp-sau-demo)
6. [Khi có bước hỏng: khoanh vùng thế nào](#6-khi-có-bước-hỏng)

---

## 1. Chuẩn bị (trước demo 30 phút)

### 1.1. Bố trí màn hình

| Cửa sổ | Dùng để | Mở bằng |
|---|---|---|
| **T1** | Chạy lệnh gây lỗi và drill | Terminal ở thư mục gốc repo, đã kích hoạt venv (1.3) |
| **T2** | Bảng theo dõi ①→⑥ | `python3 automation/moodle-demo-watch.py CON-01`, hoặc `SSO-13`. Bảng chưa hỗ trợ RES-01; lỗi 3 theo dõi qua Telegram và T3 |
| **T3** | Lệnh kiểm tra nhanh | Bộ hàm ở [MOODLE_DEMO_SCENARIOS_INFRA_AND_AUTH.md mục 3.4](MOODLE_DEMO_SCENARIOS_INFRA_AND_AUTH.md) |
| **Telegram** | Tin của Agent và nút phê duyệt | Group **NT114** (bot `@assistantnt114_bot`) |
| **Trình duyệt** | Trang login Moodle (lỗi 2), Grafana (lỗi 3) | Cửa sổ ẩn danh cho Moodle |

### 1.2. Danh sách kiểm tra

Làm lần lượt. Bước nào sai thì **dừng**, sửa xong mới đi tiếp.

| # | Làm gì | Lệnh | Kỳ vọng | Nếu sai |
|---|---|---|---|---|
| 1 | Credential AWS (nhập MFA) | `aws sts get-caller-identity --profile target-account` | `assumed-role/InfraEngineer/...` | Credential còn khoảng 1 giờ; nếu demo dài, nhập lại ngay trước lỗi 1 |
| 2 | IP laptop khớp security group | `curl -s https://checkip.amazonaws.com` so với `my_ip_cidr` trong `terraform/deployment.tfvars` | Trùng | Chạy `bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars`, rồi `terraform plan`/`apply` (chỉ đổi rule SSH/Grafana) |
| 3 | Baseline sạch | `bash automation/moodle-environment-baseline.sh verify` | `...verification passed.` | Không demo. Khôi phục baseline trước |
| 4 | Công tắc live đang tắt | Bảng T2 dòng `0` | `Agent=tắt · Executor=tắt` | Xem guide mục 7 về cách tắt |
| 5 | Telegram đã cấu hình | `grep -cE '^TELEGRAM_(TOKEN\|CHAT_ID\|ADMIN_USER_IDS)=.' release/.env` | `3` | Điền lại; lấy user ID bằng `python3 automation/moodle-telegram-approval.py whoami` |
| 6 | Bot không có webhook | `python3 -c 'import sys; sys.path.insert(0,"automation/lib"); from telegram_client import TelegramClient as C; print(repr(C().call("getWebhookInfo")["url"]))'` | `''` | Webhook chặn việc đọc nút bấm (lỗi 409). Kiểm tra nơi nhận còn dùng không rồi `deleteWebhook` |
| 7 | Ghi số executions ban đầu | Bảng T2 dòng ④/⑤, hoặc `executor_log` ở T3 | Ghi lại, ví dụ `7` | Lỗi 2 và lỗi 3 phải **giữ nguyên** số này |
| 8 | Bật Route 53 health check (chỉ cho lỗi 1) | Đặt `moodle_external_rds_probe_enabled = true`, rồi `terraform plan -out` + `apply` | `1 to add` | Không có check này thì probe *phải bị chặn* luôn hỏng và Verifier không bao giờ cho `RESOLVED` |

Lệnh Terraform cho bước 8 (và bước tắt ở mục 5). Không chạy `terraform apply` khi chưa có file plan:

```bash
cd terraform
eval "$(aws configure export-credentials --profile target-account --format env)"; unset AWS_PROFILE
terraform plan -var-file=deployment.tfvars -out=tfplan.r53     # phải đúng 1 to add (hoặc 1 to destroy khi tắt)
terraform apply tfplan.r53 && rm -f tfplan.r53
cd ..
```

### 1.3. Môi trường Python cho T1

Script ký và Verifier cần `cryptography` cùng các module `core.*` của Agent:

```bash
/opt/homebrew/bin/python3.12 -m venv ~/.venvs/moodle-ops
~/.venvs/moodle-ops/bin/pip install --require-hashes -r agent_src/requirements.txt
source ~/.venvs/moodle-ops/bin/activate
eval "$(aws configure export-credentials --profile target-account --format env)"
export AWS_PROFILE=target-account
```

**Không bao giờ chạy drill với `bash -x`:** chế độ đó in token API ra màn hình.

---

## 2. Lỗi 1: CON-01

**Câu chuyện kể cho người xem:** một node web chết giữa giờ học. AI phát hiện và đề xuất cách sửa, nhưng phải chờ
con người duyệt. Sau khi sửa, một bộ kiểm tra độc lập theo dõi 2 phút rồi mới được tuyên bố "đã khắc phục".

### 2.1. Chạy

T2: `python3 automation/moodle-demo-watch.py CON-01`

T1:

```bash
date -u +%FT%TZ
MOODLE_APPROVAL_MODE=telegram MOODLE_SAFE_EXECUTOR_CONFIRM=staging \
  bash automation/moodle-safe-executor-drill.sh CON-01
```

### 2.2. Diễn biến và điểm cần chỉ cho người xem

Mốc thời gian lấy từ run 8. Drill tự làm mọi bước, **trừ** bước 5 là bước bạn bấm.

| Phút | Bước | Nhìn ở đâu | Thấy gì | Nói gì |
|---|---|---|---|---|
| 0–3 | Chuẩn bị | T2 dòng `0` | Executor rồi Agent chuyển `BẬT` | "Cửa sổ live chỉ mở trong lúc diễn tập, tự đóng khi xong" |
| 3:40 | ① Lỗi | T2 dòng ① | `web_running B=0.0 A=1.0` | "Node B chết, node A vẫn phục vụ" |
| 4:05 | ② Alert | T2 ②, Telegram | `MoodleWebContainerMissing` firing; tin **🚨 ② ALERT … [CON-01]** | "Phát hiện trong khoảng 25 giây" |
| 4:35 | ③ AI | Telegram | Tin **🧠 ③ AI CHẨN ĐOÁN**: nguyên nhân, độ tin cậy 0.65, 8 bằng chứng, đề xuất kèm rollback, *AI KHÔNG tự thực thi* | "AI đề xuất đúng hành động có trong catalog, kèm cách hoàn tác" |
| 4:40 | ④ Gate | Telegram | Tin **🛡 ④ SAFETY GATE**: 8 dòng ✅, *CHƯA ĐƯỢC THỰC THI* vì *confidence 0.65 < 0.80* | "Gate đã kiểm tra 8 điều kiện, nhưng vẫn phải chờ người" |
| 4:45 | ⑤ **Bạn duyệt** | Telegram | Tin **✍️ YÊU CẦU PHÊ DUYỆT** với 2 nút | Bấm **✅ Duyệt (Safety officer)**. Tin đổi nút → bấm **✅ Duyệt (Operator)** |
| ~5:30 | Ký và thực thi | Telegram, T2 ④b ⑤ | *Đủ 2 phê duyệt*, rồi **⚙️ ⑤ EXECUTOR ĐÃ THỰC THI**, node B về 1 | "Khoá ký nằm trên máy operator, Agent không cầm khoá" |
| 6–10 | ⑥ Verifier | Telegram | **🔎 Verifier bắt đầu** với hợp đồng 3 nhóm, rồi 5 tin *Mẫu i/5* | "Probe *phải bị chặn*: Internet vẫn không vào được PostgreSQL" |
| ~10 | `RESOLVED` | Telegram, T2 ⑥b | **🏁 ⑥ INDEPENDENT VERIFIER: RESOLVED** | "Chỉ Verifier mới được chuyển trạng thái này" |
| 10–13 | Đóng cửa sổ live | T2 dòng `0` | Cả hai công tắc về `tắt` | — |

T1 kết thúc bằng dòng `CON-01 Agent handoff drill passed; evidence: ...CON-01-<thời điểm>.json`.

**Lưu ý:** khi container khởi động lại, Telegram có thể hiện thêm tin `MoodleNodeMemoryPressure`. Hãy nói trước với
người xem: đây là luật cảnh báo nhạy quá mức (đo độ giảm bộ nhớ), tự hết sau khoảng 1 phút.

### 2.3. Tiêu chí đạt

- [ ] Executor **chỉ** chạy sau khi bạn bấm đủ 2 vai. Trong lúc chờ, alert vẫn firing.
- [ ] Tin ⑤ ghi *ĐÃ THỰC THI*, `mutated=True`, node `['b']`.
- [ ] Verifier 5/5 mẫu, `RESOLVED`; số executions tăng đúng **1**.
- [ ] Công tắc về `tắt`; `moodle-environment-baseline.sh verify` passed.

---

## 3. Lỗi 2: SSO-13

**Câu chuyện:** sáng thứ Hai, sinh viên báo không thấy nút đăng nhập Okta. Mọi bảng giám sát hạ tầng đều xanh.
AI khoanh đúng lớp lỗi là xác thực, biết cách sửa, nhưng **không được phép** sửa cấu hình định danh.

### 3.1. Chuẩn bị hai script PHP (một lần)

Lưu đoạn sau thành `issuer-disable.php`. Sao chép thành `issuer-enable.php` và đổi `disable_issuer` thành `enable_issuer`:

```php
<?php
define('CLI_SCRIPT', true);
require '/var/www/html/config.php';
\core\session\manager::set_user(get_admin());
$id = $DB->get_field('oauth2_issuer', 'id', ['name' => 'Okta'], MUST_EXIST);
\core\oauth2\api::disable_issuer($id);
echo gmdate('Y-m-d\TH:i:s\Z') . " issuer Okta enabled=" . \core\oauth2\api::get_issuer($id)->get('enabled') . "\n";
```

Đặt hai file ngoài repo, ví dụ `~/moodle-demo/`. PHP **phải** chạy với `-u 1000:1999`, là định danh của web worker.
`-u moodle` sẽ báo `Database connection failed`.

### 3.2. Chạy

```bash
C=terraform/.artifacts/moodle-ssh.config
U=$(terraform -chdir=terraform output -json moodle_application | jq -r .url)
python3 automation/moodle-demo-watch.py SSO-13          # T2

# T1 — kiểm tra trạng thái sạch
curl -s "$U/login/index.php" | grep -c 'auth/oauth2/login.php'   # kỳ vọng 1

# ① Tạo lỗi
ssh -F $C moodle-app-a 'sudo docker exec -i -u 1000:1999 release-moodle-web-1 php' < ~/moodle-demo/issuer-disable.php
curl -s "$U/login/index.php" | grep -c 'auth/oauth2/login.php'   # kỳ vọng 0
curl -s -o /dev/null -w '%{http_code}\n' "$U/healthz.php"        # kỳ vọng 200
```

Có thể tạo lỗi bằng giao diện thay cho CLI: *Site administration → Server → OAuth 2 services* → bấm biểu tượng con
mắt ở dòng **Okta**.

### 3.3. Diễn biến

| Phút | Bước | Nhìn ở đâu | Thấy gì | Nói gì |
|---|---|---|---|---|
| 0 | ① | Trình duyệt ẩn danh | Không còn nút Okta; form thường vẫn còn | "Hạ tầng xanh, nhưng người dùng không đăng nhập được bằng SSO" |
| 0:10 | ① | T2 | `probe SSO=0.0 · trang login=1.0` | "Phép thử phân biệt: trang login lên, chỉ thiếu lựa chọn SSO" |
| ~1:10 | ② | T2, Telegram | `MoodleSsoLoginOptionMissing` firing; **không** có alert hạ tầng nào | "Đây là điểm khó: Grafana hạ tầng hoàn toàn xanh" |
| ~1:15 | ③ | Telegram | Tin **🚨 ② ALERT [SSO-13]** + **🙅 CHỈ NGƯỜI ĐƯỢC SỬA**: `restore_moodle_oauth2_issuer` | "AI biết cách sửa, nhưng biết mình không được sửa" |
| ~1:20 | ④ | T2 dòng ④ | `executions N → N (phải không đổi)` | "Không có yêu cầu phê duyệt nào được tạo" |
| tuỳ chọn | ④ | T3 | Lệnh thử forced-command bên dưới → `exit=64` | "Kể cả cố gọi, executor cũng từ chối ở mức SSH" |
| ~2 | ⑤ | T1 | Operator bật lại issuer | — |
| 2–4 | ⑥ | T1 | Probe OIDC 5/5 trong 120 s; Telegram báo alert hết firing | "Kiểm chứng bằng probe thật: Moodle chuyển hướng đúng tới Okta" |

Lệnh thử ranh giới (tuỳ chọn). Lệnh không thay đổi gì, chỉ chứng minh executor từ chối:

```bash
ssh -F $C monitor-ai-01 \
  'sudo -u aiops-executor ssh -i /var/lib/aiops-executor/.ssh/id_ed25519 -o BatchMode=yes \
     -o UserKnownHostsFile=/etc/aiops-safe-executor/known_hosts \
     aiops-executor@$(sudo grep -oP "^MOODLE_NODE_A_IP=\K.*" /etc/aiops-safe-executor/api.env) \
     restore_moodle_oauth2_issuer; echo "exit=$?"'
```

⑤ Khôi phục và ⑥ kiểm chứng:

```bash
ssh -F $C moodle-app-a 'sudo docker exec -i -u 1000:1999 release-moodle-web-1 php' < ~/moodle-demo/issuer-enable.php
# kỳ vọng: "... issuer Okta enabled=1"

oidc_probe() {
  local jar link location
  jar="$(mktemp)"
  link="$(curl -s -c "$jar" -b "$jar" "$U/login/index.php" \
    | grep -oE 'auth/oauth2/login\.php\?[^"]+' | head -1 | sed 's/&amp;/\&/g')"
  [ -n "$link" ] || { rm -f "$jar"; return 1; }
  location="$(curl -s -o /dev/null -c "$jar" -b "$jar" -w '%{redirect_url}' "$U/$link")"
  rm -f "$jar"
  printf '%s' "$location" | grep -qE '^https://trial-3489054\.okta\.com/oauth2/v1/authorize\?' \
    && printf '%s' "$location" | grep -q 'oauth2callback\.php'
}
pass=0
for i in 1 2 3 4 5; do
  if oidc_probe; then pass=$((pass+1)); echo "$(date -u +%T) sample $i OK"; else echo "$(date -u +%T) sample $i FAIL"; fi
  [ "$i" -lt 5 ] && sleep 30
done
echo "OIDC samples passed: $pass/5"
```

### 3.4. Tiêu chí đạt

- [ ] Trong lúc lỗi, **chỉ** `MoodleSsoLoginOptionMissing` firing; healthz 200.
- [ ] Telegram có tin *CHỈ NGƯỜI ĐƯỢC SỬA*, không có tin ④ SAFETY GATE.
- [ ] Số executions **không đổi**; nếu chạy lệnh thử thì `exit=64`.
- [ ] `OIDC samples passed: 5/5`; Telegram báo alert hết firing.

**Giới hạn cần nói rõ:** tình huống chỉ-người hiện **không** có attestation ký và không chuyển sang `RESOLVED`.
Kiểm chứng ở đây là probe do operator chạy.

---

## 4. Lỗi 3: RES-01

**Câu chuyện:** một tiến trình lạ ngốn CPU của node B. AI nhận ra đúng container gây tải và đề xuất xoá **đúng
container đó**. Người duyệt xem đề xuất, quyết định **không** cho chạy, và tự xử lý theo cách khác. Gate tôn trọng
quyết định đó: không có gì bị thay đổi.

Lỗi này không dùng drill: drill tích hợp hiện chỉ hỗ trợ CON-01. Các bước dưới đây gọi trực tiếp đúng những API mà
drill gọi, và **không** bật công tắc live. Vì vậy kể cả khi bấm nhầm *Duyệt*, Executor vẫn không thể chạy.

### 4.0. Chạy thử trước buổi demo (bắt buộc)

Chạy toàn bộ mục 4.2 một lần trước buổi demo, để xác nhận:
1. Alert `MoodleNodeCpuHigh` lên trong khoảng 1 phút.
2. Agent tạo được candidate `RES-01`. Bước tìm candidate ở dưới in ra một mã `ev-...`.
3. Tin ④ SAFETY GATE và nút phê duyệt hiện trên Telegram.

Nếu bước 2 không ra candidate sau 3 phút, **không** dùng lỗi này trong buổi demo. Thay bằng CON-01 và bấm *Từ chối*.

### 4.1. Chuẩn bị

```bash
C=terraform/.artifacts/moodle-ssh.config
TOKEN_FILE=terraform/.artifacts/safe-executor/safe-action-api-token
OUT=terraform/.artifacts/moodle-safe-executor; mkdir -p "$OUT"
RUN=RES-01-$(date -u +%Y%m%dT%H%M%SZ)
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Tunnel tới Agent API (chạy nền, nhớ PID để tắt sau)
ssh -F $C -N -L 127.0.0.1:18000:127.0.0.1:8000 monitor-ai-01 & TUNNEL=$!
curl -s http://127.0.0.1:18000/health | jq '{status, safe_action_live_enabled}'   # live phải false
```

### 4.2. Chạy

```bash
# ① Gây lỗi: container tải CPU có tên trên node B
MOODLE_FAULT_CONFIRM=staging bash automation/moodle-fault-inject.sh RES-01

# ② Chờ alert (khoảng 30–60 s)
ssh -F $C monitor-ai-01 "curl -s http://127.0.0.1:9090/api/v1/alerts | jq -r '.data.alerts[] | [.labels.alertname,.state,.labels.instance] | @tsv'"
# kỳ vọng: MoodleNodeCpuHigh  firing  moodle-app-b

# ③ Tìm candidate RES-01 mà Agent tạo sau mốc START
CANDIDATE=$(ssh -F $C monitor-ai-01 "sudo docker exec moodle-ai-agent python -c 'import sqlite3,sys; db=sqlite3.connect(\"file:/app/data/evidence.db?mode=ro\", uri=True); row=db.execute(\"select evidence_id from evidence where source=? and json_extract(metadata_json, ?) = ? and json_extract(json_extract(metadata_json, ?), ?) = ? and julianday(collected_at) >= julianday(?) order by collected_at desc limit 1\", (\"ai_decision\", \"$.decision_kind\", \"remediation_candidate\", \"$.decision_json\", \"$.scenario_id\", \"RES-01\", sys.argv[1])).fetchone(); print(row[0] if row else \"\")' '$START'")
echo "candidate=$CANDIDATE"            # kỳ vọng ev-...; rỗng thì chờ 15 s rồi chạy lại

# ④ Safety Gate chuẩn bị yêu cầu (Agent gửi tin ④ lên Telegram)
curl -s --fail -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  -X POST "http://127.0.0.1:18000/api/moodle/remediation/$CANDIDATE/prepare" > "$OUT/$RUN-prepared.json"
jq '{status, execution_permitted, gate: .gate_preview.decision, reasons: .gate_preview.approval_reasons}' "$OUT/$RUN-prepared.json"

# ⑤ Hỏi người duyệt trên Telegram. Trong demo: bấm ❌ Từ chối
python3 automation/moodle-telegram-approval.py approve --prepared "$OUT/$RUN-prepared.json" > "$OUT/$RUN-human-approval.json"
echo "exit=$?"; jq . "$OUT/$RUN-human-approval.json"     # kỳ vọng exit=10, "decision": "rejected"
```

### 4.3. Diễn biến

| Bước | Nhìn ở đâu | Thấy gì | Nói gì |
|---|---|---|---|
| ① | Grafana node B, hoặc T3 `pq 'moodle_node_cpu_busy_percent'` | CPU B > 70 %, A bình thường | "Một node bị quá tải, node kia không" |
| ② ③ | Telegram | **🚨 ② ALERT MoodleNodeCpuHigh [RES-01]**; đề xuất `remove_named_cpu_load_container` → `moodle-app-b`, kèm rollback | "AI chỉ đề xuất xoá **đúng container có tên**, không đề xuất restart cả node" |
| ④ | Telegram | **🛡 ④ SAFETY GATE**: 8 dòng ✅ và lý do vẫn cần người duyệt | "Mọi điều kiện kỹ thuật đã đạt, nhưng quyết định vẫn là của người" |
| ⑤ | Telegram | Bấm **❌ Từ chối** → tin đổi thành **⛔ BỊ TỪ CHỐI … Không thực thi** | "Từ chối là quyết định cuối: không có chữ ký, nên không có thực thi" |
| Kiểm chứng | T3 `executor_log` | Số executions **giữ nguyên** | "Bằng chứng: executor không nhận yêu cầu nào" |

### 4.4. Khôi phục (operator tự làm)

```bash
bash automation/moodle-fault-reset.sh RES-01           # docker rm -f moodle-fault-res-01 trên node B
kill $TUNNEL
bash automation/moodle-environment-baseline.sh verify  # passed
```

Telegram sẽ báo **✅ Alertmanager: MoodleNodeCpuHigh … đã hết firing**, kèm dòng *chưa phải RESOLVED*. Đúng như thiết
kế: không có thực thi nào thì không có Verifier, và không có `RESOLVED`.

### 4.5. Kết quả chạy thử 23/10/2026 (UTC)

| Bước | Thời điểm | Kết quả |
|---|---|---|
| ① Gây lỗi | 16:42:10 | Container `moodle-fault-res-01` chạy trên node B (lần đầu node phải kéo image `alpine:3.20`) |
| ② Alert | pending 16:42:49, firing 16:43:00 | `MoodleNodeCpuHigh` trên `moodle-app-b`; MTTD khoảng 50 s |
| ③ AI | 16:43:04 nhận, 16:43:06 gửi Telegram | Candidate `ev-bf12636a87f38e1c`, `remove_named_cpu_load_container` |
| ④ Gate | 16:43:2x | `REQUIRE_APPROVAL`, 8/8 kiểm tra đạt, độ tin cậy **0.80**; lý do duy nhất: catalog bắt buộc duyệt |
| ⑤ Người duyệt | 16:43:41, 16:43:46 | Lần này người duyệt bấm **Duyệt** cả hai vai. Không có lệnh `/execute`, công tắc live tắt, nên **không có gì được thực thi** |
| Khôi phục | 16:44:06 | `moodle-fault-reset.sh RES-01`; alert hết lúc 16:44:36; executions giữ nguyên **7**; baseline passed |

Lần chạy này cho thấy thêm một điều đáng trình bày: **phê duyệt khác với thực thi**. Có đủ hai phê duyệt nhưng không
có chữ ký và lệnh thực thi đi qua Gate với công tắc live đang bật, thì hệ thống vẫn không đổi.

### 4.6. Tiêu chí đạt

- [ ] Alert `MoodleNodeCpuHigh` trên `moodle-app-b`; candidate `RES-01` có `remove_named_cpu_load_container`.
- [ ] Tin ④ có 8 dòng ✅; `execution_permitted` = `false`.
- [ ] Bấm *Từ chối* → `exit=10`, `decision: rejected`; số executions không đổi.
- [ ] Sau khi reset: alert hết; baseline passed.

---

## 5. Dọn dẹp sau demo

| # | Việc | Lệnh | Kỳ vọng |
|---|---|---|---|
| 1 | Tắt Route 53 health check | `moodle_external_rds_probe_enabled = false`, rồi `plan -out` + `apply` (mục 1.2) | `1 to destroy` |
| 2 | Công tắc live | Bảng T2 dòng `0` | `tắt / tắt` |
| 3 | Không còn alert | T3 `alerts` | Không có dòng nào |
| 4 | Baseline | `bash automation/moodle-environment-baseline.sh verify` | passed |
| 5 | Lưu bằng chứng | File trong `terraform/.artifacts/moodle-safe-executor/` (Git bỏ qua) | Không commit; chép sang nơi lưu trữ an toàn nếu cần đưa vào luận văn |

---

## 6. Khi có bước hỏng

Bảng T2 (hoặc chuỗi tin Telegram) cho biết **bước đầu tiên** bị kẹt. Chỉ điều tra lớp nằm giữa bước cuối cùng
đã đạt và bước đang kẹt; đừng kiểm tra lại các bước đã đạt.

| Kẹt ở | Giả thuyết đầu tiên | Phép thử phân biệt | Xử lý |
|---|---|---|---|
| Không có tin Telegram nào | Biến Telegram chưa vào container | `sudo docker logs --since 10m moodle-agent-worker \| grep -i telegram` | Có dòng `not found in .env`: deploy lại Agent bằng `configure-moodle-monitoring.sh` |
| ① đạt, ② không firing | Alert rule hoặc Prometheus | Prometheus `/alerts`; lỗi 2: probe trang login có còn 1 không? | Trang login cũng 0 thì không phải SSO-13 mà trang login hỏng hẳn |
| ② đạt, ③ không có tin | Webhook từ Alertmanager tới Agent, worker, cooldown | `agent_logs 5m` ở T3: có `process_alerts_task received` không? | Có `received` mà không có kết quả: xem lỗi trong log worker |
| ③ có, nhưng không có candidate | Bằng chứng không khớp luật (fail closed) | Đọc lý do trong tin ③ (*Không kết luận được*) | Đây là hành vi đúng; ghi lại, khôi phục tay |
| Nút duyệt không phản hồi | Bot đang có webhook, hoặc user không nằm trong danh sách admin | Kết quả JSON của cầu nối: `409` → webhook; toast *không có quyền* → sai ID | `deleteWebhook`; sửa `TELEGRAM_ADMIN_USER_IDS` |
| Duyệt xong, ⑤ báo *bị từ chối* | Chữ ký hết hạn (> 5 phút) hoặc yêu cầu khác candidate | Thông báo HTTP 409 trong tin ⑤ | Đây là Gate hoạt động đúng. Chạy lại drill |
| ⑥ có mẫu hỏng | Probe nào hỏng (tin *Mẫu i/5* ghi tên) | `internet_to_postgresql_blocked` hỏng → Route 53 check chưa bật hoặc chưa có kết quả | Bật check, chờ 1–2 phút rồi chạy lại |
| SSH timeout | IP laptop đổi | `curl -s https://checkip.amazonaws.com` | Cập nhật CIDR (mục 1.2 bước 2) |
| Drill báo `Drill failed at stage '<tên>'` | Tên giai đoạn chỉ ra bước hỏng | Xem [bảng mục 7 của tài liệu demo cũ](MOODLE_DEMO_SCENARIOS_INFRA_AND_AUTH.md#7-xử-lý-khi-drill-thất-bại) | Drill tự dọn dẹp; kiểm tra baseline trước khi chạy lại |
