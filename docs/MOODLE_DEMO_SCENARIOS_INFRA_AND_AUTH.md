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

---

## 1. Tóm tắt và hiện trạng

| | Kịch bản 1: CON-01 | Kịch bản 2: SSO-13 |
|---|---|---|
| **Lỗi** | Container web Moodle trên node B bị dừng | Dịch vụ OAuth 2 "Okta" trong Moodle bị tắt; trang login mất nút đăng nhập Okta |
| **Lớp** | Hạ tầng / runtime | Luồng xác thực (Moodle → Okta OIDC) |
| **Alert** | `MoodleWebContainerMissing` (đã có) | `MoodleSsoLoginOptionMissing` (**cần bổ sung**, mục 5.2) |
| **AI phân tích** | Luật chẩn đoán đã duyệt → `remediation_candidate` | **Cần bổ sung** luật chẩn đoán → disposition `human_only` |
| **Safety Gate** | **ALLOW sau 2 chữ ký độc lập** (Safety Officer + Operator) | **HUMAN_ONLY**: Agent không được tự sửa cấu hình định danh |
| **Ai sửa** | Safe Executor (forced-command, allowlist) | Operator làm tay theo runbook |
| **Verifier** | `moodle-independent-verifier.py`, ký attestation, đưa incident sang `RESOLVED` | Probe OIDC (chuyển hướng tới Okta) + `moodle-auth-verify.py` (đăng nhập form hợp lệ được phép, user không tồn tại bị từ chối, ổn định ≥ 120 s) |
| **Trạng thái** | ✅ **Chạy được ngay** bằng `moodle-safe-executor-drill.sh` | ⚠️ **Cần hoàn tất phần chuẩn bị** ở mục 5.2 trước khi trình diễn |

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

| Bước | Thành phần trong repo | Bằng chứng |
|---|---|---|
| ① | `automation/moodle-fault-inject.sh`, `moodle-fault-reset.sh` | Marker kịch bản trên host (Prometheus không thấy scenario ID) |
| ② | `ansible/config/moodle_alert_rules.yml`, `ansible/files/moodle-monitoring/moodle-node-probes.sh` | `ALERTS{alertstate="firing"}` trong Prometheus |
| ③ | `agent_src/core/shadow_pipeline.py`, `core/moodle/diagnosis.py`, `core/moodle/planner.py` | Bản ghi `ai_decision` trong `/app/data/evidence.db` của Agent |
| ④ | API `/api/moodle/remediation/<id>/prepare` và `/execute`; `moodle-safe-gate-approval.py`, `moodle-safe-approval.py` | `*-prepared.json`, `*-gate-approval.json`, `*-executor-approval.json` |
| ⑤ | `automation/moodle-safe-executor-api.py`, wrapper `/usr/local/sbin/moodle-safe-executor` trên node | `*-execute-result.json` (`mutated: true`), audit SQLite |
| ⑥ | `automation/moodle-independent-verifier.py`, `core/verification/live_contracts.py`, `moodle-auth-verify.py` | `*-verifier.json`, `*-verifier-attestation.json`, trạng thái `resolved` |

**"AI phân tích" ở đây là gì?** Với Moodle, Agent chẩn đoán bằng **luật đã duyệt
dựa trên bằng chứng đo được**, không dùng LLM để quyết định hành động. Mỗi đề
xuất trỏ về các evidence ID cụ thể và có hash. Nhờ vậy quyết định kiểm chứng
được: có thể chỉ ra *vì sao* Agent đề xuất hành động đó.

---

## 3. Chuẩn bị chung cho cả hai kịch bản

### 3.1. Môi trường Python cho máy điều khiển

Các script ký phê duyệt và verifier cần `cryptography` và các module `core.*` của
Agent. `python3` hệ thống của macOS (3.9) không có các thư viện này. Tạo một venv
riêng **ngoài repo** và kích hoạt nó trước khi chạy drill, vì drill gọi
`python3` theo `PATH`:

```bash
/opt/homebrew/bin/python3.12 -m venv ~/.venvs/moodle-ops
~/.venvs/moodle-ops/bin/pip install --require-hashes -r agent_src/requirements.txt
source ~/.venvs/moodle-ops/bin/activate
python3 -c 'import cryptography, pydantic; print("ops python OK")'
```

Probe trên node dùng `timeout`. Nếu chạy test ở máy Mac thì cài thêm
`coreutils`; drill thật chạy `timeout` trên Linux nên không cần.

### 3.2. Quyền AWS, mạng và trạng thái hệ thống

```bash
aws sts get-caller-identity --profile target-account      # nhập MFA; drill dùng profile này
bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars --check
ansible -i terraform/.artifacts/moodle-inventory.yml all -m ping            # 3 × pong
bash automation/moodle-environment-baseline.sh verify                       # phải đạt
```

Credential của role sống tối đa 4 giờ. Lấy credential mới ngay trước buổi chạy;
drill CON-01 mất khoảng 10–15 phút.

### 3.3. Agent, Executor và các công tắc an toàn

| Kiểm tra | Lệnh | Kỳ vọng |
|---|---|---|
| Image Agent đã ghi nhận | `cat terraform/.artifacts/moodle-agent-image` | Image tag SHA 40 ký tự đã qua CI |
| Khoá của executor | `ls terraform/.artifacts/safe-executor/` | Có `agent-hmac.key`, `safe-action-api-token`, 3 cặp khoá Ed25519 |
| Chế độ Agent | `ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 'sudo docker exec moodle-ai-agent env' \| grep AIOPS_` | `AIOPS_UNIFIED_CORE_MODE=investigate`, `AIOPS_SAFE_ACTION_LIVE_ENABLED=false` |
| Kill switch của executor | `ssh ... monitor-ai-01 'curl -s http://172.17.0.1:8765/healthz'` | `"execution_enabled": false` |
| Alert đang firing | Prometheus `/api/v1/alerts` (qua SSH tunnel, guide mục 14) | Không có alert nào |

Nếu thiếu khoá executor, chạy theo thứ tự (kill switch vẫn tắt):

```bash
bash automation/configure-moodle-safe-executor.sh
bash automation/configure-moodle-safe-executor-api.sh
bash automation/configure-moodle-monitoring.sh --agent-image "$(cat terraform/.artifacts/moodle-agent-image)"
```

**Hai công tắc live chỉ bật trong lúc drill.** Drill CON-01 tự bật cả hai,
rồi **tự tắt lại** khi kết thúc, kể cả khi thất bại (nhờ `trap cleanup EXIT`).
Sau mỗi lần chạy, luôn kiểm tra lại dòng "Kill switch" và "Chế độ Agent" ở bảng
trên.

### 3.4. Mở SSH tunnel để quan sát trực tiếp (tuỳ chọn, nên có khi trình diễn)

```bash
ssh -F terraform/.artifacts/moodle-ssh.config -N \
  -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 monitor-ai-01
```

Mở Grafana `http://127.0.0.1:3000`, Prometheus `http://127.0.0.1:9090/alerts`,
Alertmanager `http://127.0.0.1:9093` trên các tab riêng để khán giả thấy alert
chuyển trạng thái.

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

### 4.2. Chạy

Trong terminal đã kích hoạt venv ở mục 3.1:

```bash
date -u +%FT%TZ                          # ghi giờ bắt đầu
MOODLE_SAFE_EXECUTOR_CONFIRM=staging bash automation/moodle-safe-executor-drill.sh CON-01
date -u +%FT%TZ                          # ghi giờ kết thúc
```

Kết thúc thành công khi in ra:
`CON-01 Agent handoff drill passed; evidence: terraform/.artifacts/moodle-safe-executor/CON-01-<thời điểm>.json`

### 4.3. Diễn biến từng bước (để thuyết minh khi trình diễn)

| Giai đoạn drill | Bước workflow | Điều xảy ra | Quan sát ở đâu |
|---|---|---|---|
| `preflight` | Chuẩn bị | Kiểm tra account AWS khớp ALB, baseline đạt, 2 target healthy | Terminal |
| `preflight-kill-switches` | ④ | Xác nhận **cả hai** công tắc live đang tắt | Terminal |
| `enable-executor-switch`, `enable-agent-switch` | ④ | Bật có chủ đích cho **một** drill staging | Terminal |
| `fault-injection` | **① Lỗi** | `docker stop` web container trên node B | Grafana: node B web = 0 |
| `observe-fault` | **② Alert** | ALB còn 1 target healthy; `MoodleWebContainerMissing` chuyển sang **firing** (đợi tối đa 150 s) | Prometheus `/alerts`, Alertmanager |
| (tìm candidate) | **③ AI** | Agent lưu `remediation_candidate` gắn với CON-01 và các evidence ID | `evidence.db` của Agent |
| `prepare` | **④ Gate** | Agent tạo request `approval_required`, `execution_permitted=false` | `*-prepared.json` |
| `two-independent-approvals` | **④ Gate** | Safety Officer ký `gate_action_sha256`; Operator ký action + idempotency key; TTL 5 phút | `*-gate-approval.json`, `*-executor-approval.json` |
| `agent-execute` | **⑤ Sửa** | Agent gửi cả hai chữ ký → Executor → forced-command trên node B → `compose up` web | `*-execute-result.json`: `mutated: true`, trạng thái `awaiting_verification` |
| `pre-verifier-recovery` | ⑤ → ⑥ | 2 target healthy lại, synthetic transaction đạt, baseline đạt, alert hết firing | Grafana, Prometheus |
| `export-audit-and-independent-verification` | **⑥ Verifier** | Verifier lấy ≥ 5 mẫu probe trong 120 s, ký attestation, verdict `ready_for_runtime_acceptance` | `*-verifier.json`, `*-verifier-attestation.json` |
| `persist-runtime-verification` | **⑥ Verifier** | Attestation gửi về Agent; incident chuyển sang **`resolved`** với event ID và hash | `*-runtime-verification.json.status.json` |
| `disable-live-switches` | Kết thúc | Tắt lại cả hai công tắc và xác nhận đã tắt | Terminal |

**Điểm cần nhấn mạnh khi trình diễn:**
- Agent **đề xuất**, nhưng không tự thực thi: phải có 2 chữ ký độc lập.
- Executor chỉ chạy được hành động trong allowlist (forced-command).
- Chỉ Verifier mới đưa incident sang `RESOLVED`, sau khi ký attestation.

### 4.4. Tiêu chí đạt

- [ ] Drill in `passed`; file evidence có `status: "passed"`.
- [ ] `observed_alert = MoodleWebContainerMissing`.
- [ ] `execution_receipt.mutated = true`, `target_scope = moodle-app-b`.
- [ ] `verifier.verdict = ready_for_runtime_acceptance`, `resolution_eligible = true`, có `attestation.signature`.
- [ ] `runtime_status.status = resolved`.
- [ ] `live_switch = disabled_after_drill`; kiểm tra lại cả hai công tắc đều tắt.
- [ ] `bash automation/moodle-environment-baseline.sh verify` đạt; node B chỉ có `moodle-web`, node A có `moodle-web` + `moodle-cron`.

Tính MTTD và MTTR bằng giờ trong evidence: `fault-injection` → alert firing →
`runtime_status`.

---

## 5. Kịch bản 2: Lỗi luồng xác thực SSO-13

### 5.1. Mô tả

| Mục | Nội dung |
|---|---|
| **Lỗi** | Issuer OAuth 2 **Okta** trong Moodle bị tắt (*Site administration → Server → OAuth 2 services*, biểu tượng con mắt). Theo [SSO-13](MOODLE_SSO_INCIDENT_SCENARIOS.md#sso-13-plugin-oauth-2-hoặc-service-bị-tắt) |
| **Ảnh hưởng mong đợi** | Trang `/login/index.php` **không còn** nút đăng nhập Okta; người dùng SSO không vào được. Đăng nhập bằng tài khoản Moodle cục bộ (admin) vẫn hoạt động. Hạ tầng hoàn toàn khỏe |
| **Điểm khó của sự cố này** | Mọi probe hạ tầng (healthz, ALB, RDS, EFS, container) đều **xanh**. Chỉ một probe hiểu luồng xác thực mới phát hiện được |
| **Alert** | `MoodleSsoLoginOptionMissing` (bổ sung ở 5.2) |
| **Chẩn đoán của Agent** | Hypothesis: lựa chọn đăng nhập SSO biến mất trong khi hạ tầng khỏe, nên nguyên nhân nằm ở cấu hình xác thực của Moodle |
| **Safety Gate** | **HUMAN_ONLY**: thay đổi cấu hình định danh nằm ngoài allowlist của executor. Agent không được tự sửa |
| **Sửa** | Operator bật lại issuer Okta theo runbook |
| **Verifier** | (a) probe OIDC: trang login có link `auth/oauth2/login.php` **và** link đó chuyển hướng tới endpoint authorize của Okta với đúng `client_id`, `redirect_uri`; (b) `moodle-auth-verify.py`: tài khoản Moodle cục bộ hợp lệ đăng nhập được, user không tồn tại (`aiops-invalid-probe-user`) bị từ chối, ổn định ≥ 120 s. Probe (b) đi qua form đăng nhập của Moodle, không qua Okta: nó chứng minh luồng xác thực của Moodle còn nguyên vẹn sau khi sửa, còn (a) chứng minh nhánh OIDC |

**Vì sao chọn SSO-13 mà không chọn lỗi phía Okta?** SSO-13 tạo lỗi và khôi phục
**hoàn toàn trong Moodle**, chỉ bằng một công tắc, nên an toàn và lặp lại được.
Các lỗi phía Okta (SSO-11, SSO-12) phải sửa cấu hình app Okta. SSO-12 còn làm
mất client secret cũ, nên không phù hợp để trình diễn.

### 5.2. Chuẩn bị bắt buộc (chưa có trong hệ thống)

| # | Việc | Ai làm | Ghi chú |
|---|---|---|---|
| P1 | **Khôi phục đăng nhập Okta** trên staging | Infra Engineer | Theo [guide mục 13](DEVOPS_DEPLOY_WORKFLOW_GUIDE.md#13-giai-đoạn-9-khôi-phục-đăng-nhập-okta). Ngày 20/10/2026, trang login staging **chưa có** nút Okta |
| P2 | **Probe và alert xác thực** | Infra Engineer | Thêm vào `configure-moodle-monitoring.yml` và `moodle_alert_rules.yml`, mô tả ở dưới |
| P3 | **Luật chẩn đoán và disposition `human_only`** cho alert mới | AI Engineer | Thêm vào `core/moodle/diagnosis.py` và `core/moodle/planner.py`, theo mẫu `MoodleDatabasePortExposure` → SEC-01 |
| P4 | **Tài khoản test cho verifier** | Infra Engineer | Một tài khoản Moodle **cục bộ** (auth `manual`, không phải user Okta), chỉ dùng cho probe. Hai file chứa username và password, mode 600, nằm ngoài Git. Chiều "bị từ chối" dùng user không tồn tại cố định `aiops-invalid-probe-user`, không cần chuẩn bị |
| P5 | Chạy CI, rollout image Agent mới, chụp lại baseline | Cả hai | Theo quy trình chọn image ở guide mục 10 và 14 |

**P2: Probe và alert.** Thêm một module blackbox kiểm tra nội dung trang login
(chỉ đọc, không đăng nhập):

```yaml
# ansible/playbooks/configure-moodle-monitoring.yml, phần modules của blackbox.yml
http_moodle_sso_option:
  prober: http
  timeout: 5s
  http:
    valid_status_codes: [200]
    fail_if_body_not_matches_regexp:
      - 'auth/oauth2/login\.php\?id=[0-9]+'
```

```yaml
# scrape job mới, cạnh blackbox_moodle_public
- job_name: blackbox_moodle_sso
  metrics_path: /probe
  params: {module: [http_moodle_sso_option]}
  static_configs:
    - targets: ['{{ moodle_monitor_public_url }}/login/index.php']
      labels: {service: moodle, probe: sso_login_option}
  relabel_configs:  # giống blackbox_moodle_public
```

```yaml
# ansible/config/moodle_alert_rules.yml
- alert: MoodleSsoLoginOptionMissing
  expr: probe_success{job="blackbox_moodle_sso"} == 0 and on() probe_success{job="blackbox_moodle_public",probe="login"} == 1
  for: 1m
  labels: {severity: critical, service: moodle}
  annotations:
    summary: Moodle login page is up but the Okta SSO option is missing
```

Điều kiện `and on() ... probe="login" == 1` giúp alert **chỉ** bắn khi trang
login vẫn lên mà thiếu SSO. Nhờ vậy nó không trùng với alert hạ tầng khi
Moodle sập hẳn, và chính điều này giúp Agent khoanh đúng lớp lỗi. Kiểm tra
lại label `probe` của target login hiện có trước khi dùng.

**P3: Luật cho Agent** (việc của AI Engineer, cần review và CI riêng):
- Trong `diagnosis.py`: alert `MoodleSsoLoginOptionMissing` + bằng chứng
  `probe_success` của SSO = 0 **và** của trang login/healthz = 1 → hypothesis
  `sso_login_option_missing`.
- Trong `planner.py`: disposition `human_only`, `catalog_action_id` trỏ tới một
  mục catalog **chỉ dành cho operator** (ví dụ `restore_moodle_oauth2_issuer`,
  `required_role = operator`), `execution_permitted = False`,
  `approval_required = True`.
- **Không** thêm hành động này vào wrapper của executor hay vào
  `_S3_3_REMEDIATIONS`. Ranh giới HUMAN_ONLY phải được giữ ở cả hai nơi.

**Nếu chưa làm P3:** chuỗi vẫn chạy, nhưng ở bước ③ Agent sẽ trả
`insufficient_evidence` ("no evidence-derived decision rule is approved for
this alert") và escalate. Đó vẫn là hành vi fail-closed đúng, nhưng sẽ **không
có đề xuất sửa** để trình diễn.

### 5.3. Chạy (sau khi xong P1–P5)

```bash
# 0. Trạng thái sạch
MOODLE_URL="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"
bash automation/moodle-environment-baseline.sh verify
curl -s "$MOODLE_URL/login/index.php" | grep -c 'auth/oauth2/login.php'   # ≥ 1
date -u +%FT%TZ                                                           # giờ bắt đầu
```

**① Tạo lỗi:** đăng nhập Moodle bằng `admin` (tài khoản cục bộ), vào *Site
administration → Server → OAuth 2 services*, bấm biểu tượng con mắt của issuer
**Okta** để tắt. Ghi giờ.

**② Alert:** sau khoảng 1–2 phút, `MoodleSsoLoginOptionMissing` chuyển sang
**firing** trên Prometheus và Alertmanager. Các alert hạ tầng phải **không**
firing.

**③ AI phân tích:** xem quyết định của Agent cho incident mới:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  "sudo docker exec moodle-ai-agent python -c 'import sqlite3,json; db=sqlite3.connect(\"/app/data/evidence.db\"); [print(r[0], r[1][:300]) for r in db.execute(\"select collected_at, metadata_json from evidence where source=? order by collected_at desc limit 3\", (\"ai_decision\",))]'"
```

Kỳ vọng: hypothesis `sso_login_option_missing`, disposition **`human_only`**,
`execution_permitted: false`, và danh sách evidence ID làm căn cứ.

**④ Safety Gate:** gate trả `human_only`, Agent dừng. Mọi cách gọi tự động đều
phải bị từ chối. Để chứng minh điều này, thử `prepare` trên candidate (nếu Agent
có tạo): kết quả không được là `approval_required` để thực thi tự động.
Executor không có lệnh nào cho hành động này: forced-command trả `exit 64`.

**⑤ Sửa (Operator):** bật lại issuer Okta bằng biểu tượng con mắt. Purge cache nếu
nút chưa hiện lại (*Site administration → Development → Purge caches*). Ghi
giờ.

**⑥ Verifier:**

```bash
# (a) Probe OIDC chỉ đọc: link SSO có, và chuyển hướng tới authorize của Okta
LOGIN_LINK="$(curl -s "$MOODLE_URL/login/index.php" | grep -oE 'auth/oauth2/login\.php\?[^"]+' | head -1 | sed 's/&amp;/\&/g')"
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$MOODLE_URL/$LOGIN_LINK" \
  | grep -E '^30[23] https://trial-3489054\.okta\.com/oauth2/v1/authorize\?' \
  && echo "OIDC redirect OK"

# (b) Hợp đồng đăng nhập form: tài khoản cục bộ được phép, user không tồn tại bị từ chối, ổn định >= 120 s
MOODLE_AUTH_PROBE_BASE_URL="$MOODLE_URL" \
MOODLE_AUTH_TEST_USER_FILE=<file-user> MOODLE_AUTH_TEST_PASSWORD_FILE=<file-password> \
PYTHONPATH=agent_src python3 automation/moodle-auth-verify.py
```

Sau đó đợi `MoodleSsoLoginOptionMissing` hết firing và verify baseline.

### 5.4. Tiêu chí đạt

- [ ] Trong lúc có lỗi: **chỉ** `MoodleSsoLoginOptionMissing` firing; healthz, ALB và RDS vẫn xanh.
- [ ] Agent ghi hypothesis đúng lớp xác thực, disposition `human_only`, có evidence ID.
- [ ] Không có bất kỳ `execute` nào thành công; audit của executor không có bản ghi mới.
- [ ] Sau khi operator sửa: probe OIDC chuyển hướng đúng tới Okta; `moodle-auth-verify.py` đạt cả hai chiều (cho phép và từ chối) qua cửa sổ ≥ 120 s.
- [ ] Đăng nhập thử bằng một user Okta test (`student.test`) qua nút Okta để xác nhận end-to-end bằng tay.
- [ ] Alert hết firing; baseline verify đạt.

Không bao giờ ghi username, password, cookie hay token của Okta vào evidence.

---

## 6. So sánh hai kịch bản

| Câu hỏi nghiên cứu | CON-01 chứng minh | SSO-13 chứng minh |
|---|---|---|
| Agent có phát hiện đúng lớp lỗi không? | Lỗi runtime trên đúng một node | Lỗi xác thực khi mọi chỉ số hạ tầng đều xanh |
| Agent có đề xuất hành động có căn cứ không? | Có, kèm evidence ID và phạm vi `moodle-app-b` | Có hypothesis; hành động được đánh dấu chỉ dành cho người |
| Safety Gate có giới hạn được Agent không? | ALLOW chỉ khi có 2 chữ ký độc lập, TTL 5 phút, gắn action hash | HUMAN_ONLY: Agent không thể tự sửa cấu hình định danh |
| Ai được phép tuyên bố "đã khắc phục"? | Chỉ Verifier, bằng attestation đã ký | Verifier, bằng probe allowed/forbidden và cửa sổ ổn định |

---

## 7. Xử lý khi drill thất bại

Áp dụng phương pháp ở [guide mục 18.1](DEVOPS_DEPLOY_WORKFLOW_GUIDE.md#181-phương-pháp-đừng-sửa-khi-chưa-biết-lỗi-nằm-ở-lớp-nào):
đọc **giai đoạn** drill báo hỏng (`Drill failed at stage '<tên>'`), vì tên giai
đoạn cho biết lỗi nằm ở bước nào của workflow.

| Giai đoạn hỏng | Bước | Giả thuyết đầu tiên | Phép thử |
|---|---|---|---|
| `preflight` | Chuẩn bị | Credential hết hạn; baseline lệch | `aws sts get-caller-identity --profile target-account`; `baseline verify` |
| `preflight-kill-switches` | ④ | Công tắc còn bật từ lần chạy trước | Kiểm tra bảng 3.3; chạy lại 2 lệnh tắt ở mục 3.3 |
| `observe-fault` | ② | Probe trên node chưa chạy (timer), hoặc alert chưa tới hạn | `systemctl status moodle-node-probes.timer` trên node B; Prometheus `/alerts` |
| (không thấy candidate) | ③ | Alertmanager không gửi được webhook (token), hoặc Agent không ở chế độ `investigate` | Log `moodle-alertmanager`, `moodle-ai-agent`; env `AIOPS_UNIFIED_CORE_MODE` |
| `prepare` / `agent-execute` | ④ ⑤ | Chữ ký hết TTL 5 phút; khoá không khớp; executor tắt | Thông điệp trong `*-execute-result.json`; `curl .../healthz` của executor |
| `pre-verifier-recovery` | ⑤ | Web container trên B chưa healthy | `docker compose ps` trên node B |
| `export-audit-and-independent-verification` | ⑥ | Hệ thống chưa ổn định đủ 120 s | Xem `probe_samples` trong `*-verifier.json` |

Khi drill thất bại, `trap` đã tự reset lỗi và tắt lại công tắc. Dù vậy, **luôn**
kiểm tra thủ công ba điều: (1) cả hai công tắc đã tắt, (2) node B chạy
`moodle-web`, (3) baseline verify đạt. **Không chạy lại ngay** khi chưa rõ
nguyên nhân.

---

## 8. Checklist trước buổi trình diễn

```text
Chung
[ ] Venv ~/.venvs/moodle-ops đã kích hoạt (python3 import được cryptography)
[ ] aws sts get-caller-identity --profile target-account → assumed-role/InfraEngineer (vừa lấy)
[ ] IP hiện tại khớp my_ip_cidr; ansible ping 3 × pong
[ ] Baseline verify đạt; không có alert firing
[ ] Agent: investigate + safe action live = false; Executor: execution_enabled = false
[ ] SSH tunnel mở Grafana / Prometheus / Alertmanager

Kịch bản 1 (CON-01)
[ ] Có đủ khoá trong terraform/.artifacts/safe-executor/
[ ] Ghi giờ bắt đầu; chạy drill; lưu đường dẫn file evidence
[ ] Sau drill: hai công tắc đã tắt; node B chỉ có moodle-web; baseline đạt

Kịch bản 2 (SSO-13)
[ ] P1 Okta đã khôi phục (trang login có nút Okta)
[ ] P2 alert MoodleSsoLoginOptionMissing đã triển khai và đã thử (không firing khi bình thường)
[ ] P3 Agent đã có luật human_only (image mới đã qua CI và đã rollout)
[ ] P4 File tài khoản Moodle cục bộ cho probe (mode 600, ngoài Git)
[ ] Ghi giờ; tắt issuer; quan sát alert → quyết định Agent → bật lại → verifier
```
