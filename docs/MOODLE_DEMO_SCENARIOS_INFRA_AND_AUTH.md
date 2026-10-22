# Hai kịch bản trình diễn: lỗi hạ tầng và lỗi luồng xác thực

**Mục đích:** chuẩn bị và chạy hai kịch bản trên Moodle staging, mỗi kịch bản
thể hiện trọn chuỗi:

```text
Lỗi → Alert → AI phân tích + đề xuất sửa → Safety Gate → Sửa → Independent Verifier
```

**Người thực hiện:** Infrastructure Engineer có quyền `InfraEngineer` (MFA), làm
trên máy điều khiển đã dựng hạ tầng theo
[DevOps Deploy Workflow Guide](DEVOPS_DEPLOY_WORKFLOW_GUIDE.md).

**Tài liệu liên quan:** [Kịch bản sự cố SSO](MOODLE_SSO_INCIDENT_SCENARIOS.md) ·
[Safe Executor](SPRINT3_S3_3_SAFE_EXECUTOR.md) ·
[Moodle Operations Runbook](MOODLE_OPERATIONS_RUNBOOK.md)

**Cách đọc tài liệu này.** Mỗi bước được viết theo cùng một khuôn:

| Cột | Nghĩa |
|---|---|
| **Làm gì** | Hành động bạn (hoặc drill) thực hiện |
| **Check bằng** | Lệnh chạy ở terminal T3 (bộ lệnh ở mục 3.4), hoặc trang web cần mở |
| **Ở đâu** | Nơi có dữ liệu: Prometheus, Alertmanager, Grafana, DB evidence của Agent, file evidence, AWS |
| **Kỳ vọng** | Kết quả phải thấy để được đi tiếp |
| **Nếu sai** | Giả thuyết đầu tiên cần kiểm tra |

Chưa thấy đúng "Kỳ vọng" thì **dừng lại**, đừng làm bước sau. Bước sau hỏng
thường là hậu quả của bước trước.

---

## Mục lục

1. [Tóm tắt và hiện trạng](#1-tóm-tắt-và-hiện-trạng)
2. [Workflow chung và thành phần thực hiện từng bước](#2-workflow-chung-và-thành-phần-thực-hiện-từng-bước)
3. [Chuẩn bị chung cho cả hai kịch bản](#3-chuẩn-bị-chung-cho-cả-hai-kịch-bản)
4. [Kịch bản 1: Lỗi hạ tầng CON-01](#4-kịch-bản-1-lỗi-hạ-tầng-con-01)
5. [Kịch bản 2: Lỗi luồng xác thực SSO-13](#5-kịch-bản-2-lỗi-luồng-xác-thực-sso-13)
6. [So sánh hai kịch bản](#6-so-sánh-hai-kịch-bản)
7. [Xử lý khi drill thất bại](#7-xử-lý-khi-drill-thất-bại)
8. [Checklist trước buổi trình diễn](#8-checklist-trước-buổi-trình-diễn)
9. [Kết quả chạy thật 21–22/10/2026](#9-kết-quả-chạy-thật-2122102026)

---

## 1. Tóm tắt và hiện trạng

| | Kịch bản 1: CON-01 | Kịch bản 2: SSO-13 |
|---|---|---|
| **Lỗi** | Container web Moodle trên node B bị dừng | Dịch vụ OAuth 2 "Okta" trong Moodle bị tắt; trang login mất nút đăng nhập Okta |
| **Lớp** | Hạ tầng / runtime | Luồng xác thực (Moodle → Okta OIDC) |
| **Alert** | `MoodleWebContainerMissing` | `MoodleSsoLoginOptionMissing` (thêm ngày 22/10/2026) |
| **AI phân tích** | Luật chẩn đoán đã duyệt → `remediation_candidate` | Luật `sso_login_option_missing` → disposition `human_only_escalation`, không có candidate |
| **Safety Gate** | **ALLOW sau 2 chữ ký độc lập** (Safety Officer + Operator) | **HUMAN_ONLY**: Agent không được tự sửa cấu hình định danh |
| **Ai sửa** | Safe Executor (forced-command, allowlist) | Operator làm tay theo runbook |
| **Verifier** | `moodle-independent-verifier.py`, ký attestation, đưa incident sang `RESOLVED` | Probe OIDC lặp 5 mẫu trong cửa sổ 120 s. **Chưa ký attestation, chưa đưa incident sang `RESOLVED`** (xem 5.1). Thêm `moodle-auth-verify.py` khi staging có HTTPS |
| **Trạng thái** | ✅ **Đạt đủ 11 bước** ngày 22/10/2026 (lần chạy thứ 5, sau 3 bản sửa; xem mục 9) | ✅ **Đạt đủ tiêu chí 5.5** ngày 22/10/2026, gồm đăng nhập đầu-cuối bằng Okta (xem mục 9) |

**Vì sao kịch bản 2 khác kịch bản 1?** Đây là hệ quả của thiết kế an toàn, không
phải thiếu sót của kịch bản:

- Agent chỉ được tự động sửa **5 hành động trong allowlist** (DB-01, RES-01,
  NET-01, CON-01, SEC-02). Các hành động này đều có rollback đối ứng và chỉ
  chạy được qua forced-command của executor.
- Thay đổi cấu hình **định danh và xác thực** (Okta app, OAuth 2 issuer) thuộc
  nhóm **HUMAN_ONLY** của dự án, giống thay đổi IAM. Safety Gate phải từ chối
  tự động hoá và chuyển cho con người.
- Với một alert chưa có luật chẩn đoán đã duyệt, Agent trả
  `insufficient_evidence` và **không đề xuất gì**. Đây là hành vi fail-closed
  có chủ đích.

Vì vậy hai kịch bản bổ sung cho nhau: kịch bản 1 chứng minh **đường tự động có
kiểm soát**, kịch bản 2 chứng minh **ranh giới mà Agent không được vượt qua**.

**Quy tắc an toàn chung**

1. Chỉ chạy trên **staging**. Mọi script tạo lỗi đều đòi `...CONFIRM=staging`.
2. Chụp baseline trước, verify baseline sau. Baseline không đạt thì **không**
   bắt đầu.
3. Mỗi lần chỉ chạy **một** kịch bản. Kịch bản trước phải về trạng thái sạch
   (alert đã hết firing, baseline đạt) rồi mới chạy kịch bản sau.
4. Ghi giờ bắt đầu và kết thúc để tính **MTTD** (từ lúc tạo lỗi tới lúc alert
   firing) và **MTTR** (từ lúc tạo lỗi tới lúc verifier xác nhận).
5. Không dán khoá, token, mật khẩu vào chat hay ảnh chụp màn hình.

---

## 2. Workflow chung và thành phần thực hiện từng bước

```text
 ① LỖI            moodle-fault-inject.sh / thao tác UI có kiểm soát
     │
 ② ALERT          Probe (node exporter, blackbox, synthetic) → Prometheus rule
     │            → Alertmanager → POST /webhook (bearer token) → Agent
     │
 ③ AI PHÂN TÍCH   Agent (chế độ investigate):
     │              thu bằng chứng Prometheus → luật chẩn đoán đã duyệt
     │              → hypothesis + remediation_candidate (lưu evidence, có hash)
     │
 ④ SAFETY GATE    prepare → request gắn với action hash, idempotency key
     │              • ALLOW: cần 2 chữ ký Ed25519 độc lập (Safety Officer + Operator), TTL 5 phút
     │              • HUMAN_ONLY / DENY: dừng, chuyển cho người vận hành
     │
 ⑤ SỬA            Safe Executor API (kill switch phải BẬT có chủ đích)
     │              → SSH forced-command → wrapper chỉ chạy action trong allowlist
     │            hoặc Operator sửa tay (HUMAN_ONLY)
     │
 ⑥ VERIFIER       Independent Verifier (tách khỏi Agent và Executor):
                    probe allowed / forbidden / related + cửa sổ ổn định
                    → ký attestation → chỉ verifier mới chuyển incident sang RESOLVED
```

| Bước | Thành phần trong repo | Dữ liệu kiểm chứng nằm ở |
|---|---|---|
| ① | `automation/moodle-fault-inject.sh`, `moodle-fault-reset.sh` | Trạng thái container trên node; marker kịch bản trên host (Prometheus không thấy scenario ID) |
| ② | `ansible/config/moodle_alert_rules.yml`, `ansible/files/moodle-monitoring/moodle-node-probes.sh` | Prometheus `/alerts`, Alertmanager `/api/v2/alerts` |
| ③ | `agent_src/core/shadow_pipeline.py`, `core/moodle/diagnosis.py`, `core/moodle/planner.py` | Bảng `evidence` (`source = 'ai_decision'`) trong `/app/data/evidence.db` của container `moodle-ai-agent` |
| ④ | API `/api/moodle/remediation/<candidate_id>/prepare` và `/execute`; `moodle-safe-gate-approval.py`, `moodle-safe-approval.py` | `terraform/.artifacts/moodle-safe-executor/<run>-prepared.json`, `-gate-approval.json`, `-executor-approval.json` |
| ⑤ | `automation/moodle-safe-executor-api.py` (dịch vụ `moodle-safe-executor-api` trên monitor), wrapper `/usr/local/sbin/moodle-safe-executor` trên node | `<run>-execute-result.json`; bảng `executions` trong `/var/lib/aiops-executor/api/executions.sqlite3` trên monitor; `journalctl -u moodle-safe-executor-api` |
| ⑥ | `automation/moodle-independent-verifier.py`, `core/verification/live_contracts.py`, `moodle-auth-verify.py` | `<run>-verifier.json`, `<run>-verifier-attestation.json`, `<run>-runtime-verification.json.status.json` |

**"AI phân tích" ở đây là gì?** Với Moodle, Agent chẩn đoán bằng **luật đã duyệt
dựa trên bằng chứng đo được**, không dùng LLM để quyết định hành động. Mỗi đề
xuất trỏ về các evidence ID cụ thể và có hash. Nhờ vậy quyết định kiểm chứng
được: có thể chỉ ra *vì sao* Agent đề xuất hành động đó.

---

## 3. Chuẩn bị chung cho cả hai kịch bản

Mở **ba terminal**, tất cả `cd` vào thư mục gốc repo:

| Terminal | Dùng để |
|---|---|
| **T1** | Chạy drill hoặc thao tác tạo lỗi |
| **T2** | Giữ SSH tunnel (mục 3.4) |
| **T3** | Chạy các lệnh check (bộ lệnh ở mục 3.4) |

### 3.1. Môi trường Python cho máy điều khiển

Các script ký phê duyệt và verifier cần `cryptography` và các module `core.*` của
Agent. `python3` hệ thống của macOS (3.9) không có các thư viện này. Tạo một venv
riêng **ngoài repo**, và kích hoạt nó ở **T1** trước khi chạy drill, vì drill gọi
`python3` theo `PATH`:

```bash
/opt/homebrew/bin/python3.12 -m venv ~/.venvs/moodle-ops
~/.venvs/moodle-ops/bin/pip install --require-hashes -r agent_src/requirements.txt
source ~/.venvs/moodle-ops/bin/activate
python3 -c 'import cryptography, pydantic; print("ops python OK")'
```

**Check:** dòng cuối in `ops python OK`, và `which python3` trỏ vào
`~/.venvs/moodle-ops/bin/python3`.

### 3.2. Quyền AWS, IP và trạng thái hệ thống

| # | Làm gì | Check bằng | Kỳ vọng | Nếu sai |
|---|---|---|---|---|
| 1 | Lấy credential mới (nhập MFA) | `aws sts get-caller-identity --profile target-account` | `assumed-role/InfraEngineer/...` | Xem bảng lỗi credential ở guide mục 5 |
| 2 | Kiểm tra IP public còn khớp security group | `bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars --check` | Báo không cần cập nhật | IP đã đổi: chạy lại không có `--check`, rồi `terraform plan` (chỉ đổi rule SSH/Grafana) và `apply` đúng file plan. Xem guide mục 6 |
| 3 | Kiểm tra SSH tới 3 host | `ansible -i terraform/.artifacts/moodle-inventory.yml all -m ping` | 3 × `pong` | `timed out` gần như luôn do IP đổi (bước 2) |
| 4 | Kiểm tra baseline | `bash automation/moodle-environment-baseline.sh verify` | `Moodle experiment baseline verification passed.` | Không chạy kịch bản. Sửa hệ thống về trạng thái chuẩn trước |

Credential của role sống tối đa 4 giờ. Lấy credential mới ngay trước buổi chạy;
drill CON-01 mất khoảng 15–20 phút, phần lớn do hai lần chạy playbook để bật
và tắt công tắc live.

### 3.3. Agent, Executor và các công tắc an toàn

| Kiểm tra | Lệnh | Kỳ vọng |
|---|---|---|
| Image Agent đã ghi nhận | `cat terraform/.artifacts/moodle-agent-image` | Image tag SHA 40 ký tự đã qua CI |
| Khoá của executor | `ls terraform/.artifacts/safe-executor/` | Có `agent-hmac.key`, `safe-action-api-token`, 3 cặp khoá Ed25519 |
| Chế độ Agent | `ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 'sudo docker exec moodle-ai-agent env' \| grep AIOPS_` | `AIOPS_UNIFIED_CORE_MODE=investigate`, `AIOPS_SAFE_ACTION_LIVE_ENABLED=false` |
| Công tắc live | `switches` (mục 3.4) | `agent_live:false`, `executor_live:false` |
| Alert đang firing | `alerts` (mục 3.4) | Không in dòng nào |

Nếu thiếu khoá executor, chạy theo thứ tự (kill switch vẫn tắt):

```bash
bash automation/configure-moodle-safe-executor.sh
bash automation/configure-moodle-safe-executor-api.sh
bash automation/configure-moodle-monitoring.sh --agent-image "$(cat terraform/.artifacts/moodle-agent-image)"
```

**Hai công tắc live chỉ bật trong lúc drill.** Drill CON-01 tự bật cả hai,
rồi **tự tắt lại** khi kết thúc, kể cả khi thất bại (nhờ `trap cleanup EXIT`).
Sau mỗi lần chạy, luôn chạy `switches` để chắc cả hai đã tắt.

### 3.4. Bộ công cụ quan sát

**T2: SSH tunnel.** Giữ terminal này mở trong suốt buổi trình diễn:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config -N \
  -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 \
  -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 monitor-ai-01
```

Drill tự mở tunnel riêng ở cổng `18000`, nên không xung đột với tunnel này.

**Trình duyệt:** mở sẵn các tab sau để khán giả thấy trạng thái thay đổi:

| Tab | URL | Dùng ở bước |
|---|---|---|
| Grafana, dashboard Moodle Infrastructure | `http://127.0.0.1:3000` | ① ⑤ |
| Prometheus Alerts | `http://127.0.0.1:9090/alerts` | ② ⑤ |
| Alertmanager | `http://127.0.0.1:9093/#/alerts` | ② |
| Moodle (qua ALB) | giá trị `$MOODLE_URL` ở dưới | ① ⑤ |

**T3: bộ lệnh check.** Dán khối này **một lần** vào T3. Các hàm chỉ tồn tại
trong terminal đó; mở terminal mới thì phải dán lại. Mọi lệnh đều **chỉ đọc**.

```bash
export AWS_PROFILE=target-account AWS_REGION=ap-southeast-1
SSH_CFG=terraform/.artifacts/moodle-ssh.config
EVID_DIR=terraform/.artifacts/moodle-safe-executor
MOODLE_URL="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"
TG_ARN="$(terraform -chdir=terraform output -json moodle_application | jq -r .target_group_arn)"

# Prometheus: giá trị hiện tại của một biểu thức
pq() { curl -s --get --data-urlencode "query=$1" http://127.0.0.1:9090/api/v1/query \
  | jq -r '.data.result[] | "\(.metric.instance // .metric.job // "-")\t\(.value[1])"'; }

# Prometheus: alert đang pending/firing (cột 4 = activeAt, dùng để tính MTTD)
alerts() { curl -s http://127.0.0.1:9090/api/v1/alerts \
  | jq -r '.data.alerts[] | "\(.state)\t\(.labels.alertname)\t\(.labels.instance // "")\t\(.activeAt)"'; }

# Alertmanager: alert đã nhận để gửi đi (tới webhook của Agent)
am() { curl -s http://127.0.0.1:9093/api/v2/alerts \
  | jq -r '.[] | "\(.status.state)\t\(.labels.alertname)\t\(.startsAt)"'; }

# AWS: sức khoẻ 2 target phía sau ALB
tg() { aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State,TargetHealth.Reason]' --output text; }

# Node: container Moodle (tham số: moodle-app-a | moodle-app-b)
ps_node() { ssh -F "$SSH_CFG" "$1" \
  'sudo docker ps -a --filter name=release-moodle --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'; }

# Công tắc live của Agent và Executor
switches() {
  curl -s http://127.0.0.1:8000/health | jq -c '{agent_status: .status, agent_live: .safe_action_live_enabled}'
  ssh -F "$SSH_CFG" monitor-ai-01 'curl -s http://172.17.0.1:8765/healthz' | jq -c '{executor_live: .execution_enabled}'
}

# Agent: 6 quyết định gần nhất trong DB evidence (mở chế độ read-only)
agent_decisions() { ssh -F "$SSH_CFG" monitor-ai-01 'sudo docker exec -i moodle-ai-agent python -' <<'PY'
import json, sqlite3
db = sqlite3.connect("file:/app/data/evidence.db?mode=ro", uri=True)
rows = db.execute(
    "select evidence_id, incident_id, collected_at,"
    " json_extract(metadata_json, '$.decision_kind'), json_extract(metadata_json, '$.decision_json')"
    " from evidence where source = 'ai_decision' order by collected_at desc limit 6").fetchall()
for eid, incident, at, kind, payload in rows:
    data = json.loads(payload or "{}")
    print(at[:19], kind, "evidence_id=" + eid, "incident=" + incident)
    for key in ("scenario_id", "status", "hypothesis", "catalog_action_id", "target_scope",
                "disposition", "confidence", "reason"):
        if data.get(key) is not None:
            print("    %s = %s" % (key, str(data[key])[:160]))
    if data.get("evidence_refs"):
        print("    evidence_refs = %d" % len(data["evidence_refs"]))
PY
}

# Agent: log của worker xử lý alert (tham số: khoảng thời gian, mặc định 10m)
agent_logs() { ssh -F "$SSH_CFG" monitor-ai-01 "sudo docker logs --since ${1:-10m} moodle-agent-worker 2>&1 | tail -40"; }

# Executor: các lần thực thi đã ghi nhận
executor_log() { ssh -F "$SSH_CFG" monitor-ai-01 'sudo python3 -' <<'PY'
import sqlite3
db = sqlite3.connect("file:/var/lib/aiops-executor/api/executions.sqlite3?mode=ro", uri=True)
print("executions:", db.execute("select count(*) from executions").fetchone()[0])
for created, status, key in db.execute(
        "select created_at, status, idempotency_key from executions order by created_at desc limit 5"):
    print("   ", created, status, key[:16] + "...")
PY
}

# File evidence mới nhất của drill (tham số: hậu tố, ví dụ prepared | execute-result | verifier)
latest() { ls -t "$EVID_DIR"/*-"$1".json 2>/dev/null | head -1; }
```

**Check bộ công cụ:** chạy `switches`, `alerts`, `tg`. Phải thấy hai công tắc
`false`, không có alert nào, và 2 dòng `healthy`. Nếu `curl` báo
`Connection refused`, tunnel ở T2 đã đóng: mở lại.

---

## 4. Kịch bản 1: Lỗi hạ tầng CON-01

### 4.1. Mô tả

| Mục | Nội dung |
|---|---|
| **Lỗi** | Container `release-moodle-web-1` trên **node B** bị dừng (`docker stop`). Node A vẫn chạy |
| **Ảnh hưởng mong đợi** | ALB còn **1/2 target healthy**; người dùng vẫn vào được Moodle qua node A. Đây là lỗi một phần, không phải sập toàn bộ |
| **Alert** | `MoodleWebContainerMissing` (`moodle_node_web_running == 0` trong 15 s) |
| **Chẩn đoán của Agent** | Luật trực tiếp theo node → hypothesis: web container trên node B không chạy |
| **Đề xuất sửa** | `start_reviewed_compose_service`, phạm vi `moodle-app-b` |
| **Safety Gate** | Hành động reversible, có rollback đối ứng, đúng phạm vi staging → **ALLOW sau 2 chữ ký** |
| **Verifier** | allowed: `common_synthetic_transaction`; forbidden: `internet_to_postgresql_blocked`; related: `scenario_alert_resolved`, `moodle_app_b_container_healthy`, `both_alb_targets_healthy`; cửa sổ 120 s, lấy mẫu mỗi 30 s |

**Vì sao chọn CON-01?** Đây là kịch bản duy nhất đã có **drill tích hợp đầy đủ**
qua API của Agent. Nó an toàn vì luôn giữ một node phục vụ, và hành động sửa có
rollback rõ ràng.

### 4.2. Cách drill vận hành

Drill `automation/moodle-safe-executor-drill.sh` **tự chạy cả chuỗi** ①→⑥ và
gần như không in gì ra màn hình. Việc của bạn trong lúc nó chạy là **quan sát
từng bước ở T3 và trên trình duyệt** theo bảng 4.4, để khán giả thấy từng mắt
xích của workflow.

> **Đã sửa ngày 21/10/2026:** bước tìm candidate của drill truy vấn cột `id`,
> nhưng bảng `evidence` dùng cột `evidence_id`. Câu truy vấn cũ luôn lỗi
> `no such column: id`, nên drill dừng ở giai đoạn `observe-fault`. Hãy chắc
> bạn đang dùng bản script đã sửa:
> `grep -c 'select evidence_id from evidence' automation/moodle-safe-executor-drill.sh`
> phải in `1`.

### 4.3. Trước khi chạy: kiểm tra trạng thái sạch (T3)

| # | Check bằng | Ở đâu | Kỳ vọng |
|---|---|---|---|
| 0.1 | `switches` | Agent `/health`, Executor `/healthz` | `agent_live:false`, `executor_live:false` |
| 0.2 | `alerts` | Prometheus | Không in dòng nào |
| 0.3 | `tg` | AWS ELB | 2 dòng `healthy` |
| 0.4 | `ps_node moodle-app-b`; `ps_node moodle-app-a` | Node B, node A | B: chỉ `release-moodle-web-1` `Up (healthy)`. A: `web` và `cron` đều `Up` |
| 0.5 | `pq 'moodle_node_web_running'` | Prometheus | `moodle-app-a 1`, `moodle-app-b 1` |
| 0.6 | `executor_log` | Executor DB trên monitor | Ghi lại số `executions` hiện tại, để so sánh sau |
| 0.7 | `curl -s -o /dev/null -w '%{http_code}\n' "$MOODLE_URL/healthz.php"` | ALB | `200` |

### 4.4. Chạy và quan sát từng bước

**T1** (venv đã kích hoạt):

```bash
date -u +%FT%TZ                          # ghi giờ bắt đầu
MOODLE_SAFE_EXECUTOR_CONFIRM=staging bash automation/moodle-safe-executor-drill.sh CON-01
date -u +%FT%TZ                          # ghi giờ kết thúc
```

Trong lúc T1 chạy, làm lần lượt các bước sau ở **T3** và trên trình duyệt.

#### Bước 1: Preflight và bật công tắc live (khoảng 3–8 phút đầu)

| | |
|---|---|
| **Drill làm gì** | Kiểm tra account AWS khớp ALB, baseline đạt, 2 target healthy, cả hai công tắc đang tắt. Sau đó **bật** công tắc của Executor rồi của Agent, bằng cách chạy lại hai playbook |
| **Check bằng** | `switches`, lặp lại mỗi 30–60 s |
| **Ở đâu** | Agent `/health`, Executor `/healthz` |
| **Kỳ vọng** | Lúc đầu `false/false`, sau đó `executor_live:true`, rồi `agent_live:true` |
| **Nếu sai** | T1 báo `Drill failed at stage 'preflight'`: credential hết hạn hoặc baseline lệch. Báo `stage 'preflight-kill-switches'`: công tắc còn bật từ lần trước (chạy lệnh tắt ở mục 7) |

Điểm thuyết minh: *"Mặc định hệ thống không thể tự sửa gì. Người vận hành
phải bật có chủ đích, chỉ cho một drill, trên staging."*

#### Bước 2 (①): Lỗi xảy ra

| | |
|---|---|
| **Drill làm gì** | `docker stop release-moodle-web-1` trên node B |
| **Check bằng** | `ps_node moodle-app-b` |
| **Ở đâu** | Node B |
| **Kỳ vọng** | `release-moodle-web-1 ... Exited (0) ...` |
| **Check thêm** | `pq 'moodle_node_web_running'` → `moodle-app-b 0` (sau tối đa ~30 s, chu kỳ probe trên node) |
| | `tg` → một target chuyển `unhealthy` (hoặc `draining`) sau khoảng 30–60 s |
| | `curl -s -o /dev/null -w '%{http_code}\n' "$MOODLE_URL/healthz.php"` → **vẫn `200`**, vì node A gánh tải |
| **Trên trình duyệt** | Grafana: panel trạng thái web của node B về 0. Tab Moodle vẫn dùng được bình thường |
| **Nếu sai** | `moodle_node_web_running` vẫn 1 sau 1 phút: timer probe trên node B không chạy (`ssh -F $SSH_CFG moodle-app-b 'systemctl status moodle-node-probes.timer'`) |

Ghi lại giờ dừng chính xác (dùng để tính MTTD/MTTR):

```bash
ssh -F "$SSH_CFG" moodle-app-b "sudo docker inspect -f '{{.State.FinishedAt}}' release-moodle-web-1"
```

#### Bước 3 (②): Alert phát ra

| | |
|---|---|
| **Check bằng** | `alerts` |
| **Ở đâu** | Prometheus |
| **Kỳ vọng** | `pending  MoodleWebContainerMissing  moodle-app-b  <activeAt>`, rồi sau 15 s chuyển sang `firing`. **Chỉ** alert này (có thể kèm `MoodlePublicProbeFailed` thoáng qua, nhưng không được có alert RDS/EFS) |
| **Check thêm** | `am` → `active  MoodleWebContainerMissing`: Alertmanager đã nhận và sẽ gửi tới Agent |
| | `agent_logs 5m` → có dòng xử lý alert `MoodleWebContainerMissing` |
| **Trên trình duyệt** | Prometheus `/alerts`: rule chuyển vàng (pending) rồi đỏ (firing). Alertmanager: alert xuất hiện |
| **Nếu sai** | Alert không firing sau 150 s: T1 báo `Expected one healthy ALB target` hoặc timeout ở `observe-fault`. Xem lại bước 2 |

**MTTD** = `activeAt` của alert − `FinishedAt` của container (bước 2).

#### Bước 4 (③): AI phân tích và đề xuất sửa

| | |
|---|---|
| **Check bằng** | `agent_decisions` |
| **Ở đâu** | Bảng `evidence` của Agent (`source = ai_decision`) |
| **Kỳ vọng** | Một bản ghi `remediation_candidate` mới với: `scenario_id = CON-01`, `catalog_action_id = start_reviewed_compose_service`, `target_scope = moodle-app-b`, `evidence_refs` **≥ 3**. Thường đi kèm các bản ghi chẩn đoán (hypothesis) cùng `incident` |
| **Ý nghĩa** | `evidence_id` của bản ghi này chính là **candidate_id** mà drill dùng ở bước sau. Mỗi đề xuất trỏ về evidence cụ thể, có hash |
| **Nếu sai** | Không có bản ghi mới sau 2–3 phút: (a) Alertmanager không gửi được webhook (`agent_logs` có `401`/`403` là sai bearer token); (b) Agent không ở chế độ `investigate` (mục 3.3). T1 sẽ báo `No evidence-linked CON-01 candidate was created; execution stopped.` |

Điểm thuyết minh: *"Agent đề xuất **một** hành động cụ thể, trong phạm vi **một**
node, kèm danh sách bằng chứng đã lưu. Đề xuất này chưa được phép chạy."*

#### Bước 5 (④): Safety Gate tạo yêu cầu phê duyệt

| | |
|---|---|
| **Drill làm gì** | Gọi `POST /api/moodle/remediation/<candidate_id>/prepare` |
| **Check bằng** | `jq '{status, execution_permitted, gate_action_sha256, request: (.request \| {request_id, scenario_id, catalog_action_id, target_scope, idempotency_key})}' "$(latest prepared)"` |
| **Ở đâu** | `terraform/.artifacts/moodle-safe-executor/<run>-prepared.json` |
| **Kỳ vọng** | `status = approval_required`, **`execution_permitted = false`**, `target_scope = moodle-app-b`, có `gate_action_sha256` |
| **Nếu sai** | T1 báo lỗi ở `stage 'prepare'`. HTTP 404 `fresh remediation candidate is unavailable` nghĩa là candidate đã quá hạn hoặc thiếu evidence (cần ≥ 3) |

#### Bước 6 (④): Hai chữ ký độc lập

| | |
|---|---|
| **Drill làm gì** | Safety Officer ký `gate_action_sha256` bằng khoá gate; Operator ký hành động + idempotency key bằng khoá operator. Mỗi chữ ký có TTL 5 phút |
| **Check bằng** | `jq '{actor_id, expires_at, action_sha256}' "$(latest gate-approval)"` |
| | `jq '.approval \| {actor_id, expires_at, action_sha256}' "$(latest executor-approval)"` |
| | `jq -r .gate_action_sha256 "$(latest prepared)"` |
| **Ở đâu** | Các file `-gate-approval.json` và `-executor-approval.json` |
| **Kỳ vọng** | `actor_id` khác nhau (`safety-officer` và `operator`); `expires_at` khoảng 5 phút sau thời điểm ký; `action_sha256` của chữ ký gate **trùng** `gate_action_sha256` trong file prepared |
| **Ý nghĩa** | Chữ ký gắn với **đúng** hành động này. Không thể dùng lại cho hành động khác, hay sau khi hết hạn |

Không mở các file `*-private.pem`. Chữ ký trong các file JSON không phải bí mật,
nhưng khoá riêng thì có.

#### Bước 7 (⑤): Thực thi qua Safe Executor

| | |
|---|---|
| **Drill làm gì** | Gửi request kèm hai chữ ký tới `POST /api/moodle/remediation/<candidate_id>/execute`. Agent chuyển cho Executor API; Executor SSH vào node B bằng key forced-command; wrapper chạy `docker compose up -d --wait moodle-web` |
| **Check bằng** | `jq '{status, result: (.result \| {status, scenario_id, target_scope, mutated, completed_nodes})}' "$(latest execute-result)"` |
| **Ở đâu** | File `-execute-result.json` |
| **Kỳ vọng** | `status = awaiting_verification`; `result.status = executed`, `mutated = true`, `target_scope = moodle-app-b` |
| **Check thêm** | `executor_log` → số `executions` tăng **đúng 1**, bản ghi mới có trạng thái hoàn tất |
| | `ps_node moodle-app-b` → `release-moodle-web-1 Up ... (healthy)`; **không có** `moodle-cron` trên B |
| | `ssh -F "$SSH_CFG" monitor-ai-01 'sudo journalctl -u moodle-safe-executor-api --since -10min --no-pager \| tail -20'` → log nhận và xử lý request |
| **Nếu sai** | `.result.status = disabled` (`423`): công tắc Executor chưa bật. `awaiting_approval` (`403`): chữ ký hết TTL hoặc sai khoá |

Điểm thuyết minh: *"Executor không có shell. Key của nó bị ép vào một wrapper
chỉ biết 5 hành động; gửi lệnh khác sẽ nhận `forced command rejected`."*
Muốn chứng minh tại chỗ, xem lệnh thử ở 5.4, bước 4.

#### Bước 8 (⑤ → ⑥): Hệ thống hồi phục

| | |
|---|---|
| **Check bằng** | `tg` → 2 × `healthy` |
| | `pq 'moodle_node_web_running'` → cả hai node bằng `1` |
| | `alerts` → `MoodleWebContainerMissing` **biến mất** (đã resolved) |
| **Trên trình duyệt** | Prometheus `/alerts`: rule về xanh. Grafana: node B trở lại |
| **Nếu sai** | T1 báo `Alert did not resolve after the executed action.`: xem `ps_node moodle-app-b` và log container (`ssh -F $SSH_CFG moodle-app-b 'sudo docker logs --tail 50 release-moodle-web-1'`) |

#### Bước 9 (⑥): Independent Verifier xác nhận

| | |
|---|---|
| **Drill làm gì** | Xuất audit của Agent; chạy `moodle-independent-verifier.py` trong cửa sổ **120 s**, lấy mẫu mỗi 30 s; ký attestation bằng khoá verifier |
| **Check bằng** | `jq '{verdict, resolution_eligible, samples: (.probe_samples \| length), signed: (.attestation.signature \| type == "string")}' "$(latest verifier)"` |
| **Ở đâu** | File `-verifier.json` |
| **Kỳ vọng** | `verdict = ready_for_runtime_acceptance`, `resolution_eligible = true`, `samples ≥ 5`, `signed = true` |
| **Check thêm** | `jq '.probe_samples[0]' "$(latest verifier)"` → mỗi mẫu gồm kết quả của probe allowed (`common_synthetic_transaction`), forbidden (`internet_to_postgresql_blocked`) và related (alert đã hết, container B healthy, 2 target healthy) |
| **Nếu sai** | `resolution_eligible = false`: xem mẫu nào thất bại trong `probe_samples`. Hệ thống chưa ổn định đủ 120 s là nguyên nhân hay gặp nhất |

#### Bước 10 (⑥): Incident chuyển sang RESOLVED

| | |
|---|---|
| **Drill làm gì** | Gửi attestation về Agent (`POST /api/moodle/verifications/<request_id>`), rồi đọc trạng thái request |
| **Check bằng** | `jq '{status, incident_id, request_id, verification_event_id, verification_event_hash}' "$(ls -t "$EVID_DIR"/*-runtime-verification.json.status.json \| head -1)"` |
| **Ở đâu** | File `-runtime-verification.json.status.json` |
| **Kỳ vọng** | `status = resolved`; `verification_event_hash` là chuỗi 64 ký tự hex |
| **Ý nghĩa** | Agent **không tự đặt** `resolved`. Chỉ attestation đã ký của Verifier mới làm được |

#### Bước 11: Tắt công tắc và kết thúc

| | |
|---|---|
| **Drill làm gì** | Tắt công tắc Executor rồi Agent (chạy lại hai playbook); in `CON-01 Agent handoff drill passed; evidence: <file>` |
| **Check bằng** | `switches` → `agent_live:false`, `executor_live:false` |
| | `ps_node moodle-app-b` → chỉ `web`; `ps_node moodle-app-a` → `web` và `cron` |
| | `bash automation/moodle-environment-baseline.sh verify` → passed |
| **Nếu sai** | Công tắc còn `true`: chạy hai lệnh tắt ở mục 7 **ngay** |

### 4.5. Đọc lại toàn bộ bằng chứng và tính MTTD/MTTR

File tổng hợp của drill gom mọi bước vào một chỗ:

```bash
EV="$(ls -t "$EVID_DIR"/CON-01-*.json | grep -vE -- '-(prepared|gate-approval|executor-approval|execute-body|execute-result|verifier|verifier-attestation|runtime-verification)' | head -1)"
jq '{status, observed_alert, candidate_id, incident_id,
     gate_status: .prepared_request.status,
     gate_actor: .gate_approval.actor_id, operator_actor: .executor_approval.actor_id,
     executed: .execution_receipt.status, mutated: .execution_receipt.mutated,
     verdict: .verifier.verdict, samples: (.probe_samples | length),
     runtime: .runtime_status.status, live_switch, completed_at}' "$EV"
```

Thời điểm cho MTTD/MTTR:

```bash
ssh -F "$SSH_CFG" moodle-app-b \
  "sudo docker inspect -f 'stopped={{.State.FinishedAt}} restarted={{.State.StartedAt}}' release-moodle-web-1"
jq -r '.completed_at' "$EV"
```

| Chỉ số | Cách tính |
|---|---|
| **MTTD** | `activeAt` của alert (ghi ở bước 3) − `stopped` |
| **Thời gian sửa** | `restarted` − `stopped` |
| **MTTR (đã được xác nhận)** | `completed_at` (đã qua verifier và cửa sổ ổn định) − `stopped` |

### 4.6. Tiêu chí đạt

- [ ] Drill in `passed`; file evidence có `status: "passed"`.
- [ ] `observed_alert = MoodleWebContainerMissing`.
- [ ] Candidate có `evidence_refs ≥ 3`; prepared có `execution_permitted = false`.
- [ ] Hai chữ ký có `actor_id` khác nhau; `action_sha256` của gate khớp file prepared.
- [ ] `execution_receipt.mutated = true`, `target_scope = moodle-app-b`; số `executions` tăng đúng 1.
- [ ] `verifier.verdict = ready_for_runtime_acceptance`, `resolution_eligible = true`, ≥ 5 mẫu, có chữ ký.
- [ ] `runtime_status.status = resolved`.
- [ ] Cả hai công tắc đã tắt; node B chỉ có `moodle-web`, node A có `moodle-web` + `moodle-cron`; baseline verify đạt.

---

## 5. Kịch bản 2: Lỗi luồng xác thực SSO-13

### 5.1. Mô tả

| Mục | Nội dung |
|---|---|
| **Lỗi** | Issuer OAuth 2 **Okta** trong Moodle bị tắt (*Site administration → Server → OAuth 2 services*, biểu tượng con mắt). Theo [SSO-13](MOODLE_SSO_INCIDENT_SCENARIOS.md#sso-13-plugin-oauth-2-hoặc-service-bị-tắt) |
| **Ảnh hưởng mong đợi** | Trang `/login/index.php` **không còn** nút đăng nhập Okta; người dùng SSO không vào được. Đăng nhập bằng tài khoản Moodle cục bộ (admin) vẫn hoạt động. Hạ tầng hoàn toàn khỏe |
| **Điểm khó của sự cố này** | Mọi probe hạ tầng (healthz, ALB, RDS, EFS, container) đều **xanh**. Chỉ một probe hiểu luồng xác thực mới phát hiện được |
| **Alert** | `MoodleSsoLoginOptionMissing` (bổ sung ở P2) |
| **Chẩn đoán của Agent** | Hypothesis: lựa chọn đăng nhập SSO biến mất trong khi hạ tầng khỏe, nên nguyên nhân nằm ở cấu hình xác thực của Moodle |
| **Safety Gate** | **HUMAN_ONLY**: thay đổi cấu hình định danh nằm ngoài allowlist của executor. Agent không được tự sửa |
| **Sửa** | Operator bật lại issuer Okta theo runbook |
| **Verifier** | (a) Probe OIDC lặp lại trong cửa sổ 120 s: trang login có link SSO, và link đó chuyển hướng tới endpoint authorize của Okta với `redirect_uri` của Moodle. (b) `moodle-auth-verify.py`: tài khoản Moodle cục bộ đăng nhập được, user không tồn tại bị từ chối, ổn định ≥ 120 s. **(b) bắt buộc HTTPS** (xem P6). **Giới hạn hiện tại:** luồng attestation ký Ed25519 và chuyển `RESOLVED` chỉ áp dụng cho hành động do executor thực hiện (cần biên nhận `executed` trong audit). SSO-13 do người sửa nên chưa có attestation ký; kết quả verifier là bằng chứng probe, sự cố vẫn ở trạng thái chờ xác minh |

**Vì sao chọn SSO-13 mà không chọn lỗi phía Okta?** SSO-13 tạo lỗi và khôi phục
**hoàn toàn trong Moodle**, chỉ bằng một công tắc, nên an toàn và lặp lại được.
Các lỗi phía Okta (SSO-11, SSO-12) phải sửa cấu hình app Okta. SSO-12 còn làm
mất client secret cũ, nên không phù hợp để trình diễn.

### 5.2. Chuẩn bị bắt buộc

| # | Việc | Ai làm | Bắt buộc? | Trạng thái 22/10/2026 |
|---|---|---|---|---|
| P1 | Khôi phục đăng nhập Okta trên staging | Infra Engineer | Có | ✅ Phía Moodle dựng lại bằng CLI (mục 5.6); phía Okta đã cập nhật redirect URI. Okta authorize trả **200** (trang Sign In) |
| P2 | Probe và alert xác thực | Infra Engineer | Có | ✅ Đã có trong repo (commit `c62240d`) và đã triển khai |
| P3 | Luật chẩn đoán và disposition `human_only` cho Agent | AI Engineer | Có | ✅ Commit `c62240d`, CI xanh, image `moodle-ai-agent:ded1536…` đang chạy |
| P4 | Tài khoản Moodle cục bộ cho probe | Infra Engineer | **Không** (chỉ cần cho verifier (b)) | Bỏ qua |
| P5 | CI, rollout, baseline | Cả hai | Có | ✅ |
| P6 | HTTPS cho ALB (chỉ cần cho verifier (b)) | Infra Engineer | **Không** | Bỏ qua; staging vẫn HTTP |

**P4 và P6 là tuỳ chọn.** `moodle-auth-verify.py` từ chối HTTP, nên khi staging chưa
có HTTPS thì P4 không dùng được. Verifier của kịch bản dùng probe OIDC (a), đủ để
chứng minh nhánh SSO đã hoạt động trở lại.

#### P1: Khôi phục đăng nhập Okta

**Làm:** theo [guide mục 13](DEVOPS_DEPLOY_WORKFLOW_GUIDE.md#13-giai-đoạn-9-khôi-phục-đăng-nhập-okta):
cập nhật redirect URI trong app Okta, tạo lại issuer trong Moodle (field mapping
có `sub` → `username`), bật plugin OAuth 2.

Phía Moodle có thể làm bằng CLI từ bản xuất
`terraform/.artifacts/preserve/moodle-oauth2-okta.json` (mục 5.6, "Dựng lại issuer").
Phía Okta luôn phải làm tay trong Okta admin: sau mỗi lần dựng lại hạ tầng, DNS
của ALB đổi. Nếu redirect URI chưa cập nhật, Okta trả **HTTP 400** ở bước
authorize, dù phía Moodle đã chuyển hướng đúng.

| Check bằng | Ở đâu | Kỳ vọng |
|---|---|---|
| `curl -s "$MOODLE_URL/login/index.php" \| grep -c 'auth/oauth2/login.php'` | Trang login qua ALB | `≥ 1` |
| Bấm nút Okta trên trình duyệt | Moodle → Okta | Chuyển tới trang đăng nhập `trial-3489054.okta.com`, không phải lỗi 400 |
| `curl` URL authorize mà Moodle tạo ra (xem bước 6 của 5.4) | Okta | HTTP **200**, tiêu đề trang chứa `Sign In` |
| Đăng nhập bằng `student.test` | Moodle | Vào được Dashboard |

#### P2: Probe và alert xác thực

**Làm:** sửa hai file, rồi triển khai lại monitoring.

1. `ansible/playbooks/configure-moodle-monitoring.yml`, phần `modules:` của
   `blackbox.yml`, thêm module chỉ đọc (không đăng nhập):

   ```yaml
   http_moodle_sso_option:
     prober: http
     timeout: 5s
     http:
       valid_status_codes: [200]
       fail_if_body_not_matches_regexp:
         - 'auth/oauth2/login\.php\?id=[0-9]+'
   ```

2. Cùng file, phần scrape job của Prometheus, thêm job cạnh
   `blackbox_moodle_public` (sao chép nguyên khối `relabel_configs` của job đó):

   ```yaml
   - job_name: blackbox_moodle_sso
     metrics_path: /probe
     params: {module: [http_moodle_sso_option]}
     static_configs:
       - targets: ['{{ moodle_monitor_public_url }}/login/index.php']
         labels: {service: moodle, probe: sso_login_option}
     relabel_configs:
       - source_labels: [__address__]
         target_label: __param_target
       - target_label: __address__
         replacement: blackbox-exporter:9115
       - source_labels: [__param_target]
         target_label: target
   ```

3. `ansible/config/moodle_alert_rules.yml`, thêm rule:

   ```yaml
   - alert: MoodleSsoLoginOptionMissing
     expr: probe_success{job="blackbox_moodle_sso"} == 0 and on() probe_success{job="blackbox_moodle_public",probe="login"} == 1
     for: 1m
     labels: {severity: critical, service: moodle}
     annotations:
       summary: Moodle login page is up but the Okta SSO option is missing
   ```

   Điều kiện `and on() ... probe="login" == 1` làm alert **chỉ** bắn khi trang
   login vẫn lên mà thiếu SSO. Nhờ vậy nó không trùng với alert hạ tầng khi
   Moodle sập hẳn, và giúp Agent khoanh đúng lớp lỗi. Job
   `blackbox_moodle_public` hiện đã có label `probe: login` cho target trang
   login.

4. Kiểm tra cú pháp, rồi triển khai (giữ nguyên image Agent đang chạy):

   ```bash
   bash automation/configure-moodle-monitoring.sh --syntax-check
   bash automation/configure-moodle-monitoring.sh --agent-image "$(cat terraform/.artifacts/moodle-agent-image)"
   ```

| Check bằng (T3) | Ở đâu | Kỳ vọng |
|---|---|---|
| `curl -s http://127.0.0.1:9090/api/v1/rules \| jq -r '.data.groups[].rules[] \| select(.name=="MoodleSsoLoginOptionMissing") \| .state'` | Prometheus rules | `inactive` (rule đã nạp, đang bình thường) |
| `pq 'probe_success{job="blackbox_moodle_sso"}'` | Prometheus | `blackbox_moodle_sso 1` khi nút Okta đang hiện |
| Prometheus `/targets`, nhóm `blackbox_moodle_sso` | Trình duyệt | `UP` |
| `alerts` | Prometheus | Không có `MoodleSsoLoginOptionMissing` |

Nếu target `blackbox_moodle_sso` ở trạng thái `down` với lỗi `HTTP 400`, và
`curl 'http://127.0.0.1:9115/probe?module=http_moodle_sso_option&target=...'` trên
monitor trả `Unknown module`, thì blackbox-exporter chưa nạp config mới. Playbook
đã được sửa để tạo lại container này mỗi lần triển khai; nếu dùng bản playbook cũ,
chạy `sudo docker compose -f /opt/moodle-observability/docker-compose.yml up -d
--force-recreate blackbox-exporter` trên monitor.

Nếu `probe_success` bằng `0` ngay cả khi nút Okta đang hiện, regex không khớp
HTML thật. Kiểm tra bằng
`curl -s "$MOODLE_URL/login/index.php" | grep -oE 'auth/oauth2/login\.php\?id=[0-9]+'`.

#### P3: Luật chẩn đoán cho Agent (đã làm: commit `c62240d`)

- `agent_src/core/moodle/diagnosis.py`: alert `MoodleSsoLoginOptionMissing`
  cùng bằng chứng `probe_success` của SSO = 0 **và** của trang login/healthz = 1
  → hypothesis `sso_login_option_missing`. Truy vấn Prometheus cho alert mới
  phải được khai báo trong `get_alert_queries` của adapter Moodle.
- `agent_src/core/moodle/planner.py`: disposition `human_only`,
  `catalog_action_id` trỏ tới một mục catalog **chỉ dành cho operator** (ví dụ
  `restore_moodle_oauth2_issuer`, `required_role = operator`),
  `execution_permitted = False`, `approval_required = True`. Làm theo mẫu
  `MoodleDatabasePortExposure` → SEC-01 đã có.
- **Không** thêm hành động này vào wrapper của executor hay vào
  `_S3_3_REMEDIATIONS`. Ranh giới HUMAN_ONLY phải được giữ ở cả hai nơi.
- Thêm unit test: alert mới với bằng chứng đúng → `human_only`; thiếu bằng
  chứng → `insufficient_evidence`.

| Check | Ở đâu | Kỳ vọng |
|---|---|---|
| `PYTHONPATH=agent_src pytest -q agent_src/tests -k sso` | Máy dev | Test mới đạt |
| CI của commit | GitHub Actions | Xanh (`ci_ok <sha>`, guide mục 10) |
| `grep -n restore_moodle_oauth2_issuer ansible/playbooks/configure-moodle-safe-executor.yml` | Repo | **Không** có kết quả |

**Nếu chưa làm P3:** chuỗi vẫn chạy, nhưng ở bước ③ Agent trả
`insufficient_evidence` ("no evidence-derived decision rule is approved for
this alert") và escalate. Đó vẫn là hành vi fail-closed đúng, nhưng sẽ **không
có đề xuất sửa** để trình diễn.

#### P4: Tài khoản Moodle cục bộ cho probe

**Làm:** đăng nhập Moodle bằng `admin` → *Site administration → Users →
Accounts → Add a new user*. Đặt username (ví dụ `aiops-probe`), chọn
*Authentication method* = **Manual accounts**, không gán vai trò quản trị.
Sau đó lưu thông tin vào file, gõ mật khẩu ẩn để nó không vào shell history:

```bash
umask 077
mkdir -p terraform/.artifacts/auth-probe
printf '%s\n' 'aiops-probe' > terraform/.artifacts/auth-probe/username
read -rs -p 'Password: ' probe_password; echo
printf '%s\n' "$probe_password" > terraform/.artifacts/auth-probe/password
unset probe_password
```

| Check bằng | Kỳ vọng |
|---|---|
| `ls -l terraform/.artifacts/auth-probe/` | Hai file mode `-rw-------` |
| `git check-ignore -q terraform/.artifacts/auth-probe/password && echo ignored` | `ignored` |
| Đăng nhập thử user này qua form Moodle (không qua Okta) | Vào được |

#### P5: CI, rollout và baseline

Sau P2 và P3: chạy CI → chọn image Agent bằng `pick_image` và kiểm tra bằng
`ci_ok` → rollout (guide mục 10 và 14) → `baseline capture`, rồi `verify`.

| Check | Kỳ vọng |
|---|---|
| `ssh -F "$SSH_CFG" monitor-ai-01 'sudo docker inspect -f "{{.Config.Image}}" moodle-ai-agent'` | Image có luật P3 |
| `bash automation/moodle-environment-baseline.sh verify` | passed |

#### P6 (tuỳ chọn): HTTPS cho verifier (b)

`moodle-auth-verify.py` **từ chối HTTP**, trừ khi địa chỉ là `127.0.0.1`. Staging
hiện chạy HTTP qua ALB, và Moodle chuyển hướng mọi request về `wwwroot`, nên
không thể vượt qua bằng SSH tunnel. Muốn chạy (b), cấu hình HTTPS cho ALB: đặt
`moodle_certificate_arn` (chứng chỉ ACM đã cấp ở `ap-southeast-1`) và
`moodle_hostname` trong `deployment.tfvars`, plan, review, apply, rồi cập
nhật lại redirect URI trên Okta. Khi chưa có HTTPS, verifier dùng (a): probe
OIDC lặp trong cửa sổ ổn định ở bước 6 của 5.4.

### 5.3. Trước khi chạy: kiểm tra trạng thái sạch (T3)

| # | Check bằng | Kỳ vọng |
|---|---|---|
| 0.1 | `switches` | `false` / `false`. Kịch bản này **không** bật công tắc nào |
| 0.2 | `alerts` | Không có dòng nào |
| 0.3 | `pq 'probe_success{job="blackbox_moodle_sso"}'` | `1` |
| 0.4 | `curl -s "$MOODLE_URL/login/index.php" \| grep -c 'auth/oauth2/login.php'` | `≥ 1` |
| 0.5 | `executor_log` | Ghi lại số `executions`; số này **không được đổi** suốt kịch bản |
| 0.6 | `bash automation/moodle-environment-baseline.sh verify` | passed |

### 5.4. Chạy và quan sát từng bước

#### Bước 1 (①): Tạo lỗi

| | |
|---|---|
| **Làm gì** | Ghi giờ (`date -u +%FT%TZ`). Trên trình duyệt, đăng nhập Moodle bằng `admin` (form cục bộ) → *Site administration → Server → OAuth 2 services* → bấm **biểu tượng con mắt** ở dòng issuer **Okta** để tắt |
| **Check bằng** | `curl -s "$MOODLE_URL/login/index.php" \| grep -c 'auth/oauth2/login.php'` |
| **Ở đâu** | Trang login qua ALB |
| **Kỳ vọng** | `0`. Mở trang login trên một cửa sổ ẩn danh: không còn nút Okta, form đăng nhập thường vẫn còn |
| **Check thêm** | `tg` → 2 × `healthy`; `curl -s -o /dev/null -w '%{http_code}\n' "$MOODLE_URL/healthz.php"` → `200`. Hạ tầng **không** bị ảnh hưởng |
| **Cách khác (CLI)** | Tắt issuer bằng script `disable` ở mục 5.6. Đây đúng là hàm mà biểu tượng con mắt gọi (`core\oauth2\api::disable_issuer`) |
| **Nếu sai** | Nút vẫn hiện: trang bị cache. *Site administration → Development → Purge caches*, rồi thử lại |

#### Bước 2 (②): Alert phát ra

| | |
|---|---|
| **Check bằng** | `pq 'probe_success{job="blackbox_moodle_sso"}'` → `0` (trong khoảng 15–30 s) |
| | `alerts` → `pending` rồi, **sau 1 phút**, `firing  MoodleSsoLoginOptionMissing` |
| | `am` → `active  MoodleSsoLoginOptionMissing` |
| **Ở đâu** | Prometheus, Alertmanager |
| **Kỳ vọng** | **Chỉ** alert này firing. Không có alert hạ tầng nào (`MoodlePublicProbeFailed`, `MoodleWebContainerMissing`, RDS, EFS) |
| **Trên trình duyệt** | Prometheus `/alerts`: chỉ `MoodleSsoLoginOptionMissing` đỏ. Grafana: mọi panel hạ tầng vẫn xanh. Đây chính là điểm khó của sự cố |
| **Nếu sai** | Không firing: xem `pq 'probe_success{job="blackbox_moodle_public",probe="login"}'`. Nếu cũng bằng 0 thì trang login hỏng hẳn, không phải lỗi SSO-13 |

**MTTD** = `activeAt` của alert − giờ ghi ở bước 1.

#### Bước 3 (③): AI phân tích

| | |
|---|---|
| **Check bằng** | `agent_decisions` (đợi 1–2 phút sau khi alert firing) |
| | `agent_logs 5m` → có dòng xử lý `MoodleSsoLoginOptionMissing` |
| **Ở đâu** | Bảng `evidence` của Agent, log worker |
| **Kỳ vọng (đã có P3)** | Bản ghi mới với hypothesis `sso_login_option_missing`, disposition **`human_only`**, `catalog_action_id = restore_moodle_oauth2_issuer`, `execution_permitted = false`, có `evidence_refs` |
| **Kỳ vọng (chưa có P3)** | Agent ghi `insufficient_evidence` và escalate. Không có đề xuất |
| **Nếu sai** | Không có bản ghi nào: webhook không tới Agent. Xem log `moodle-alertmanager` và `moodle-ai-agent` |

Điểm thuyết minh: *"Agent khoanh đúng lớp lỗi: hạ tầng khỏe, trang login lên,
nhưng thiếu lựa chọn SSO. Agent biết sửa thế nào, nhưng cũng biết mình không
được phép sửa."*

#### Bước 4 (④): Safety Gate chặn tự động hoá

| | |
|---|---|
| **Check bằng** | `executor_log` → số `executions` **không đổi** so với bước 0.5 |
| **Chứng minh ranh giới (tuỳ chọn)** | Thử gửi hành động này qua đúng danh tính executor. Wrapper phải từ chối |

```bash
ssh -F "$SSH_CFG" monitor-ai-01 \
  'sudo -u aiops-executor ssh -i /var/lib/aiops-executor/.ssh/id_ed25519 -o BatchMode=yes \
     -o UserKnownHostsFile=/etc/aiops-safe-executor/known_hosts \
     aiops-executor@$(sudo grep -oP "^MOODLE_NODE_A_IP=\K.*" /etc/aiops-safe-executor/api.env) \
     restore_moodle_oauth2_issuer; echo "exit=$?"'
```

| | |
|---|---|
| **Kỳ vọng** | `forced command rejected`, `exit=64`. Lệnh này không thay đổi gì |
| **Ý nghĩa** | Kể cả khi có người cố gọi, executor **không có** đường nào để sửa cấu hình định danh |

#### Bước 5 (⑤): Operator sửa tay

| | |
|---|---|
| **Làm gì** | Ghi giờ. Trên trình duyệt: *OAuth 2 services* → bấm lại biểu tượng con mắt để **bật** issuer Okta. Nếu nút chưa hiện lại thì *Purge caches*. Cách khác: script `enable` ở mục 5.6 |
| **Check bằng** | `curl -s "$MOODLE_URL/login/index.php" \| grep -c 'auth/oauth2/login.php'` → `≥ 1` |
| | `pq 'probe_success{job="blackbox_moodle_sso"}'` → `1` |
| **Ở đâu** | Trang login, Prometheus |

#### Bước 6 (⑥): Verifier

**(a) Probe OIDC trong cửa sổ ổn định 120 s.** Chạy ở T3. Script lấy 5 mẫu cách
nhau 30 s. Mỗi mẫu tải trang login với cookie (link SSO có `sesskey` gắn với
phiên), lấy link SSO, rồi kiểm tra Moodle chuyển hướng tới endpoint authorize
của Okta, với `redirect_uri` trỏ về `oauth2callback.php` của Moodle:

```bash
oidc_probe() {
  local jar link location
  jar="$(mktemp)"
  link="$(curl -s -c "$jar" -b "$jar" "$MOODLE_URL/login/index.php" \
    | grep -oE 'auth/oauth2/login\.php\?[^"]+' | head -1 | sed 's/&amp;/\&/g')"
  [ -n "$link" ] || { rm -f "$jar"; return 1; }
  location="$(curl -s -o /dev/null -c "$jar" -b "$jar" -w '%{redirect_url}' "$MOODLE_URL/$link")"
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

| | |
|---|---|
| **Kỳ vọng** | `OIDC samples passed: 5/5` |
| **Nếu sai** | Mẫu nào `FAIL`: chạy riêng `curl -s -c /tmp/j -b /tmp/j "$MOODLE_URL/login/index.php" \| grep -o 'auth/oauth2[^"]*'` để xem link. Nếu chuyển hướng tới trang lỗi của Moodle thay vì Okta, kiểm tra endpoint của issuer (bẫy SSO-10: domain `trial-3489054`, không phải tên hiển thị) |

**(b) Hợp đồng đăng nhập form (chỉ khi đã có P6 HTTPS):**

```bash
MOODLE_AUTH_PROBE_BASE_URL="$MOODLE_URL" \
MOODLE_AUTH_TEST_USER_FILE=terraform/.artifacts/auth-probe/username \
MOODLE_AUTH_TEST_PASSWORD_FILE=terraform/.artifacts/auth-probe/password \
PYTHONPATH=agent_src python3 automation/moodle-auth-verify.py
echo "exit=$?"
```

| | |
|---|---|
| **Kỳ vọng** | `exit=0`, JSON có `resolution_eligible: true`: user cục bộ đăng nhập được, `aiops-invalid-probe-user` bị từ chối, ổn định ≥ 120 s |
| **Ghi chú** | Script không in username, password hay chi tiết lỗi thô. Báo `exit=2` nghĩa là không hoàn tất được; với HTTP thì đó là do bị từ chối vì thiếu HTTPS |

**(c) Kiểm tra end-to-end bằng tay:** cửa sổ ẩn danh → nút Okta → đăng nhập
`student.test` → vào được Dashboard. Đăng nhập `moodle-denied` → Okta từ chối
(user không được assign app).

Lần đầu đăng nhập, Okta đòi đăng ký Okta Verify. Nút *Set up Okta Verify* trên
máy Mac chỉ hoạt động khi đã cài ứng dụng Okta Verify for macOS. Cách nhanh hơn:
*Return to authenticator list*, rồi đăng ký bằng điện thoại qua mã QR.

**Kiểm tra chéo (c) trong DB Moodle, chỉ đọc**, chạy với `-u 1000:1999` (mục 5.6).
Đoạn PHP chỉ dùng `SELECT`, đọc các bảng `user`, `auth_oauth2_linked_login` và
`logstore_standard_log`:

| Kiểm tra | Kỳ vọng |
|---|---|
| Số tài khoản có email `student.test@…` | Đúng **1**, `auth = oauth2` |
| `username` của tài khoản đó | Là `sub` của Okta (bắt đầu bằng `00u`), không phải email |
| `auth_oauth2_linked_login` của user | Đúng **1** dòng, issuer `Okta` |
| `firstaccess` / `currentlogin` và sự kiện `\core\event\user_loggedin` | Trùng thời điểm đăng nhập vừa làm |
| Số tài khoản `moodle-denied` | **0**: Okta chặn trước khi tới Moodle, nên Moodle không tạo account |

`lastlogin` sẽ bằng 0 ở lần đăng nhập đầu tiên, vì cột đó lưu **lần đăng nhập trước**.
Dùng `firstaccess`/`currentlogin` để xác nhận.

#### Bước 7: Kết thúc

| Check bằng | Kỳ vọng |
|---|---|
| `alerts` | `MoodleSsoLoginOptionMissing` biến mất |
| `executor_log` | Số `executions` **vẫn bằng** bước 0.5 |
| `switches` | `false` / `false` |
| `bash automation/moodle-environment-baseline.sh verify` | passed |

**MTTR** = thời điểm mẫu OIDC cuối cùng đạt (cửa sổ ổn định kết thúc) − giờ
ghi ở bước 1.

### 5.5. Tiêu chí đạt

- [ ] Trong lúc có lỗi: **chỉ** `MoodleSsoLoginOptionMissing` firing; healthz, ALB và RDS vẫn xanh.
- [ ] Agent ghi hypothesis đúng lớp xác thực, disposition `human_only`, có evidence ID.
- [ ] Số `executions` của executor không đổi; lệnh thử qua forced-command bị từ chối (`exit=64`).
- [ ] Sau khi operator sửa: probe OIDC đạt 5/5 trong 120 s; nếu có HTTPS thì `moodle-auth-verify.py` đạt.
- [ ] Đăng nhập bằng tay `student.test` qua Okta thành công; `moodle-denied` bị từ chối.
- [ ] Alert hết firing; baseline verify đạt.

Không bao giờ ghi username, password, cookie hay token của Okta vào evidence
hay ảnh chụp màn hình.

### 5.6. Thao tác Moodle bằng CLI

**Chạy với đúng định danh của web worker: `-u 1000:1999`.** Web worker của Moodle
chạy uid `1000` (`moodle`) với gid `1999`, nhóm được phép đọc file secret và CA
của RDS. `docker exec -u moodle` lấy gid mặc định `1000`, nên PHP báo
`Database connection failed`. `-u www-data` thì không đọc được `config.php`.
Chạy bằng root cũng được, nhưng sẽ tạo file cache thuộc root trong moodledata.

Kiểm tra định danh của worker: `sudo docker exec release-moodle-web-1 grep -E
'^(Uid|Gid)' /proc/1/status`.

**Tắt hoặc bật issuer Okta** (dùng cho bước ① và ⑤). Lưu đoạn sau thành
`issuer-disable.php`; đổi `disable_issuer` thành `enable_issuer` để có
`issuer-enable.php`:

```php
<?php
define('CLI_SCRIPT', true);
require '/var/www/html/config.php';
\core\session\manager::set_user(get_admin());
$id = $DB->get_field('oauth2_issuer', 'id', ['name' => 'Okta'], MUST_EXIST);
\core\oauth2\api::disable_issuer($id);
echo gmdate('Y-m-d\TH:i:s\Z') . " issuer Okta enabled=" . \core\oauth2\api::get_issuer($id)->get('enabled') . "\n";
```

```bash
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
  'sudo docker exec -i -u 1000:1999 release-moodle-web-1 php' < issuer-disable.php
```

Dòng in ra có thời điểm UTC, dùng làm mốc tính MTTD/MTTR. Hai node dùng chung
database nên chỉ cần chạy trên node A.

**Dựng lại issuer từ bản xuất** (P1 phía Moodle, sau khi dựng lại hạ tầng). Bản
xuất chứa client secret, nên không đưa nó lên dòng lệnh. Chép nó vào container
qua stdin với mode 600, chạy script, rồi xoá ngay:

```bash
C=terraform/.artifacts/moodle-ssh.config
ssh -F $C moodle-app-a 'sudo docker exec -i -u 1000:1999 release-moodle-web-1 sh -c "umask 077; cat > /tmp/okta-issuer.json"' \
  < terraform/.artifacts/preserve/moodle-oauth2-okta.json
ssh -F $C moodle-app-a 'sudo docker exec -i -u 1000:1999 release-moodle-web-1 php' < restore-okta.php
ssh -F $C moodle-app-a 'sudo docker exec -u 1000:1999 release-moodle-web-1 rm -f /tmp/okta-issuer.json'
```

`restore-okta.php` đọc `/tmp/okta-issuer.json`, xoá file ngay sau khi đọc, rồi gọi
`core\oauth2\api::create_issuer`, `create_endpoint` và `create_user_field_mapping`
theo dữ liệu xuất. Sau đó nó bật issuer, đặt `auth` (`email,oauth2`), khoá các
trường họ tên và email, rồi `purge_all_caches()`. Nếu đã có issuer cùng tên, script
dừng mà không thay đổi gì. Kết quả mong đợi:
`restored issuer=Okta enabled=1 showonloginpage=2 endpoints=3 mappings=4 auth=email,oauth2`.

Nếu lệnh `rm` báo `Operation not permitted`, hãy xoá bằng **đúng user đã tạo file**.
Container chạy không có quyền `CAP_FOWNER`, nên root cũng không xoá được file của
user khác trong `/tmp`.

---

## 6. So sánh hai kịch bản

| Câu hỏi nghiên cứu | CON-01 chứng minh | SSO-13 chứng minh |
|---|---|---|
| Agent có phát hiện đúng lớp lỗi không? | Lỗi runtime trên đúng một node | Lỗi xác thực khi mọi chỉ số hạ tầng đều xanh |
| Agent có đề xuất hành động có căn cứ không? | Có, kèm evidence ID và phạm vi `moodle-app-b` | Có hypothesis; hành động được đánh dấu chỉ dành cho người |
| Safety Gate có giới hạn được Agent không? | ALLOW chỉ khi có 2 chữ ký độc lập, TTL 5 phút, gắn action hash | HUMAN_ONLY: Agent không thể tự sửa cấu hình định danh; executor từ chối ở mức SSH |
| Ai được phép tuyên bố "đã khắc phục"? | Chỉ Verifier, bằng attestation đã ký | Verifier, bằng probe lặp trong cửa sổ ổn định (chưa có attestation ký) |

---

## 7. Xử lý khi drill thất bại

Áp dụng phương pháp ở [guide mục 18.1](DEVOPS_DEPLOY_WORKFLOW_GUIDE.md#181-phương-pháp-đừng-sửa-khi-chưa-biết-lỗi-nằm-ở-lớp-nào):
đọc **giai đoạn** drill báo hỏng (`Drill failed at stage '<tên>'`), vì tên giai
đoạn cho biết lỗi nằm ở bước nào của workflow.

| Giai đoạn hỏng | Bước | Giả thuyết đầu tiên | Phép thử |
|---|---|---|---|
| `preflight` | Chuẩn bị | Credential hết hạn; baseline lệch; IP đổi | `aws sts get-caller-identity --profile target-account`; `baseline verify`; `update-infrastructure.sh --check` |
| `preflight-kill-switches` | ④ | Công tắc còn bật từ lần chạy trước | `switches`; chạy hai lệnh tắt ở dưới |
| `enable-*-switch` | ④ | Playbook lỗi | Chạy lại playbook bằng tay để thấy lỗi đầy đủ |
| `observe-fault` | ② ③ | Probe trên node chưa chạy (timer); alert chưa tới hạn; **script cũ còn truy vấn cột `id`** (mục 4.2) | `systemctl status moodle-node-probes.timer` trên node B; `alerts`; `grep -c 'select evidence_id' automation/moodle-safe-executor-drill.sh` |
| `No evidence-linked CON-01 candidate` | ③ | Alertmanager không gửi được webhook (token); Agent không ở `investigate` | `agent_logs`; env `AIOPS_UNIFIED_CORE_MODE`; `agent_decisions` |
| `prepare` | ④ | Candidate hết hạn hoặc thiếu evidence | Nội dung `-prepared.json` hoặc HTTP 404 |
| `agent-execute` | ④ ⑤ | Chữ ký hết TTL 5 phút; khoá không khớp; executor tắt | `jq . "$(latest execute-result)"`; `switches` |
| `pre-verifier-recovery` | ⑤ | Web container trên B chưa healthy | `ps_node moodle-app-b`; log container |
| `export-audit-and-independent-verification` | ⑥ | (1) Probe forbidden `internet_to_postgresql_blocked = false` ở mọi mẫu; (2) verdict `not_stable` dù mọi probe đạt; (3) hệ thống chưa ổn định đủ 120 s | (1) `terraform output -json moodle_database \| jq .external_tcp_health_check_id` phải khác `null` (cần `moodle_external_rds_probe_enabled = true` và quyền IAM Route 53); bản `moodle-scenario-verify.sh` phải có `checked_epoch`. (2) Bản `moodle-independent-verifier.py` phải có `max_stability_gap_seconds=60`. (3) `.probe_samples` trong `-verifier.json` |
| `persist-runtime-verification` | ⑥ | Attestation sai khoá hoặc request ID | `-runtime-verification.json` |
| Bất kỳ, kèm `Network is unreachable` | — | **Máy điều khiển mất mạng.** Cleanup của drill cũng cần SSH nên không chạy được: công tắc có thể **vẫn bật**, node B có thể **vẫn dừng** | Khi có mạng lại: cập nhật IP nếu đổi, rồi **tắt công tắc trước**, sau đó reset CON-01, xoá marker, baseline verify (xem dưới) |

Khi drill thất bại, `trap` đã tự reset lỗi và tắt lại công tắc. Dù vậy, **luôn**
kiểm tra thủ công ba điều: (1) `switches` → cả hai `false`, (2) `ps_node
moodle-app-b` → `web` đang chạy, (3) baseline verify đạt. **Không chạy lại
ngay** khi chưa rõ nguyên nhân.

Nếu công tắc vẫn còn bật, tắt ngay:

```bash
SAFE_EXECUTOR_LIVE_ENABLED=false bash automation/configure-moodle-safe-executor-api.sh
MOODLE_SAFE_ACTION_LIVE_ENABLED=false bash automation/configure-moodle-monitoring.sh
```

Nếu container web trên node B vẫn dừng, reset rồi xoá marker kịch bản:

```bash
bash automation/moodle-fault-reset.sh CON-01
bash -c 'source automation/lib/moodle-fault-common.sh; load_moodle_environment; remove_scenario_marker CON-01'
```

**Các lỗi khác đã gặp khi chuẩn bị kịch bản 2:**

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| PHP CLI trong container báo `Database connection failed` | Chạy với `-u moodle` (gid 1000), không đọc được secret và CA | Chạy với `-u 1000:1999` (mục 5.6) |
| `Failed opening required '/var/www/html/config.php'` | Chạy với `-u www-data` | Như trên |
| Okta trả HTTP 400 ở `/oauth2/v1/authorize` | Redirect URI trên Okta là của ALB cũ | Cập nhật redirect URI theo P1; kiểm tra lại bằng `curl` URL authorize: phải ra 200 |
| Target `blackbox_moodle_sso` `down`, `Unknown module` | blackbox-exporter chưa nạp config mới | Dùng playbook đã sửa, hoặc tạo lại container (xem P2) |
| `route53:CreateHealthCheck ... explicit deny` khi apply | Policy `InfraEngineerMoodle` chặn mọi region ngoài Singapore; Route 53 là dịch vụ toàn cầu | Admin thêm các action health check của Route 53 vào `NotAction` của statement chặn region, và một statement Allow riêng (IAM guide) |
| Okta đòi *Set up Okta Verify*, bấm nút không có phản hồi | Luồng đăng ký Okta Verify **trên máy Mac**, cần ứng dụng Okta Verify for macOS | *Return to authenticator list* → đăng ký bằng điện thoại (quét QR), hoặc cài ứng dụng macOS và dùng cửa sổ thường |

---

## 8. Checklist trước buổi trình diễn

```text
Chung
[ ] T1: venv ~/.venvs/moodle-ops đã kích hoạt (python3 import được cryptography)
[ ] aws sts get-caller-identity --profile target-account → assumed-role/InfraEngineer (vừa lấy)
[ ] update-infrastructure.sh --check: IP không đổi; ansible ping 3 × pong
[ ] Baseline verify đạt
[ ] T2: tunnel mở (3000, 8000, 9090, 9093); T3: đã dán bộ lệnh 3.4
[ ] switches → false/false; alerts → trống; tg → 2 healthy
[ ] Tab trình duyệt: Grafana, Prometheus /alerts, Alertmanager, Moodle

Kịch bản 1 (CON-01)
[ ] grep -c 'select evidence_id from evidence' automation/moodle-safe-executor-drill.sh → 1
[ ] Route 53 health check đã bật: external_tcp_health_check_id khác null; ≥ 3 checker báo Failure
[ ] Mạng ổn định trong khoảng 15 phút (drill cần SSH liên tục)
[ ] Có đủ khoá trong terraform/.artifacts/safe-executor/
[ ] Ghi số executions (executor_log); ghi giờ bắt đầu; chạy drill; theo dõi bước 1–11
[ ] Sau drill: switches false/false; node B chỉ có web; baseline đạt; lưu đường dẫn file evidence

Kịch bản 2 (SSO-13)
[ ] P1 Okta đã khôi phục: có nút Okta; curl URL authorize → HTTP 200 (Sign In)
[ ] P2 alert MoodleSsoLoginOptionMissing đã nạp (inactive); target blackbox_moodle_sso up; probe SSO = 1
[ ] P3 Agent chạy image có luật human_only (đã qua CI)
[ ] Script CLI issuer-disable.php / issuer-enable.php sẵn sàng (mục 5.6), chạy với -u 1000:1999
[ ] (Tuỳ chọn) P4 + P6 nếu muốn chạy moodle-auth-verify.py
[ ] Ghi số executions; ghi giờ; tắt issuer → alert → quyết định Agent → thử forced-command
    → bật lại → probe OIDC 5/5 → executions không đổi
```

---

## 9. Kết quả chạy thật 21–22/10/2026

### 9.1. Kịch bản 1: CON-01

| Lần | Dừng ở | Nguyên nhân gốc | Xử lý |
|---|---|---|---|
| (trước) | `observe-fault` | Drill truy vấn cột `id`, bảng dùng `evidence_id` | Sửa drill (commit `2ef0f4c`) |
| 1 | ⑥ Verifier | Chưa có Route 53 health check, nên probe forbidden luôn `failed` | Admin cấp quyền IAM Route 53; bật `moodle_external_rds_probe_enabled = true` (1 to add) |
| 2 | ⑥ Verifier | jq `fromdateiso8601` không đọc được `CheckedTime` của Route 53 (`…56.998000+00:00`) | Chuẩn hoá thời gian, vẫn fail-closed (commit `2ef0f4c`) |
| 3 | ⑥ Verifier (`not_stable`) | Ngưỡng khoảng cách mẫu là 30 s, khoảng cách thực tế 30.005 s | Đặt 60 s theo hợp đồng attestation (commit `2ef0f4c`) |
| 4 | ② `observe-fault` | Máy điều khiển mất mạng; cleanup không chạy được, công tắc bật khoảng 10 giờ | Không phải lỗi code; dọn tay theo mục 7 |
| **5** | **Đạt cả 11 bước** | — | — |

Lần 5 (22/10, UTC): lỗi lúc 01:00:13; alert pending 01:01:03, firing khoảng
01:01:18; Agent tạo candidate với 8 evidence; hai chữ ký `safety-officer` +
`operator`; executor sửa lúc 01:01:24 (`mutated: true`, node `b`); Verifier 5/5
mẫu đạt cả 5 probe (01:03:22 → 01:05:22); incident **`resolved`**; công tắc tắt
lúc 01:08:32.

| Chỉ số | Giá trị |
|---|---|
| MTTD (lỗi → alert pending / firing) | khoảng 50 s / 65 s |
| Thời gian sửa (lỗi → container chạy lại) | 72 s |
| Tới khi kết thúc cửa sổ ổn định của Verifier | khoảng 5 phút 9 giây |

Evidence: `terraform/.artifacts/moodle-safe-executor/CON-01-20261022T005639Z.json`
(Git-ignored).

### 9.2. Kịch bản 2: SSO-13

| Bước | Thời điểm (22/10, UTC) | Kết quả |
|---|---|---|
| ① Tắt issuer (CLI) | 02:06:26 | Nút Okta biến mất; `healthz` 200; 2 target healthy |
| ② Alert | 02:06:44 pending → 02:07:44 firing | Chỉ `MoodleSsoLoginOptionMissing`; probe SSO = 0, trang login = 1 |
| ③ AI phân tích | 02:07:50 | Hypothesis đúng lớp xác thực, confidence 0.65; `human_only_escalation`, action `restore_moodle_oauth2_issuer`, `remediation_candidate = null` |
| ④ Safety Gate | 02:08 | `prepare` → HTTP 404; forced-command → `exit 64`; `executions` không đổi (4) |
| ⑤ Operator bật lại (CLI) | 02:08:55 | Nút Okta hiện lại; alert hết firing lúc 02:08:58 |
| ⑥ Verifier (probe OIDC) | 02:09:40 → 02:11:49 | 5/5 mẫu đạt |
| Sau khi cập nhật Okta | 08:45 | URL authorize mà Moodle tạo ra: Okta trả **200** (trang Sign In), 3/3 lần |
| ⑥c Đăng nhập đầu-cuối | 08:44:29 | `student.test` đăng ký Okta Verify bằng điện thoại rồi đăng nhập qua Okta, vào Dashboard. DB Moodle xác nhận: đúng **1** tài khoản `auth = oauth2`, username là `sub` của Okta, 1 liên kết OAuth 2 tới issuer `Okta`, sự kiện `user_loggedin` cùng thời điểm. `moodle-denied` bị Okta từ chối; Moodle có **0** tài khoản `moodle-denied` |

| Chỉ số | Giá trị |
|---|---|
| MTTD (lỗi → alert pending / firing) | 18 s / 78 s (rule có `for: 1m`) |
| Tới khi operator sửa | 2 phút 29 giây |
| Tới khi kết thúc cửa sổ ổn định của probe OIDC | 5 phút 23 giây |

**Giới hạn cần nêu khi trình bày:** Verifier của SSO-13 là probe OIDC chạy tay,
chưa ký attestation và chưa đưa incident sang `RESOLVED`. Xem mục 9.3.

**Bài học khi đăng nhập lần đầu:** Okta đòi đăng ký Okta Verify theo policy MFA.
Nút *Set up Okta Verify* trên máy Mac chỉ hoạt động khi đã cài ứng dụng Okta
Verify for macOS. Cách nhanh hơn là *Return to authenticator list*, rồi đăng ký
bằng điện thoại qua mã QR.

### 9.3. Giới hạn của kịch bản 2

| # | Giới hạn | Hệ quả | Hướng khắc phục |
|---|---|---|---|
| L1 | **Verifier không ký attestation và không đưa incident sang `RESOLVED`.** Luồng attestation chỉ chấp nhận biên nhận `executed` của executor; SSO-13 do người sửa nên không có biên nhận | Incident SSO-13 vẫn ở trạng thái chờ xác minh. Bằng chứng phục hồi là kết quả probe, không phải attestation có chữ ký | Thêm luồng "operator-remediated": operator ký biên nhận hành động thủ công, verifier dùng hợp đồng probe riêng cho SSO, rồi ký attestation |
| L2 | **Probe verifier là script chạy tay**, chưa nằm trong `moodle-independent-verifier.py` và `live_contracts.py` | Kết quả phụ thuộc người chạy; chưa có hợp đồng allowed/forbidden/related cho SSO | Đưa probe OIDC vào hợp đồng của verifier: allowed (link SSO chuyển tới Okta authorize, Okta trả 200), forbidden (user bị cấm không vào được), related (healthz, ALB) |
| L3 | **`moodle-auth-verify.py` không chạy được** vì staging chỉ có HTTP, mà script bắt buộc HTTPS | Thiếu kiểm tra đăng nhập tự động trong cửa sổ ổn định | P6: cấu hình HTTPS cho ALB (ACM + hostname), rồi chạy (b) |
| L4 | **Đăng nhập đầu-cuối qua Okta là thao tác tay**, cần Okta Verify trên điện thoại | Không lặp lại tự động được; MFA chặn mọi script | Dùng user Okta riêng cho probe với policy MFA phù hợp cho staging, hoặc giữ là bước kiểm tra tay có bằng chứng DB |
| L5 | **Alert chỉ phát hiện việc nút SSO biến mất.** Các lỗi phía Okta (sai redirect URI, client secret, policy) đều có nút nhưng hỏng ở bước sau | Lỗi SSO-10/11/12/15 không sinh alert này | Bổ sung probe gọi URL authorize và kiểm tra Okta trả 200 (như ⑥a mở rộng) |
| L6 | **Thao tác tạo lỗi và sửa chạy qua CLI**, tương đương biểu tượng con mắt nhưng không đi qua giao diện | Không có thao tác UI để quay video trình diễn | Khi trình diễn, dùng UI cho bước ① và ⑤; giữ CLI để lặp lại nhanh |
| L7 | **Redirect URI trên Okta phải cập nhật tay sau mỗi lần dựng lại hạ tầng**, vì DNS của ALB đổi | Okta trả 400 nếu quên | Dùng hostname cố định (Route 53 + HTTPS) hoặc đưa app Okta vào Terraform |
