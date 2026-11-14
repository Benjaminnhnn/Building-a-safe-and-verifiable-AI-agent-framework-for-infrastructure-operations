# Hướng dẫn đo RTO và RPO cho DB-01 và NET-01: nhánh Agent không có Gate, Executor, Verifier

**Cập nhật:** 14/11/2026 · **Commit:** `795d1a20` trên `develop` · **Môi trường:** Moodle staging trên AWS, điều
khiển từ WSL Ubuntu
**Dành cho:** người trực tiếp chạy đo trên staging để lấy số liệu RTO/RPO cho phần đối chứng (ablation) của luận văn.

> **Trạng thái 14/11/2026:** toàn bộ staging đã bị `terraform destroy`, state rỗng. Phải làm **Phần B (dựng lại)**
> trước khi đo. Hạ tầng tính tiền theo giờ ngay sau `terraform apply`; đo xong thì gỡ theo Phần H.

---

## Mục lục

1. [Nhánh đo này là gì](#1-nhánh-đo-này-là-gì)
2. [Các chỉ số và cách script đo](#2-các-chỉ-số-và-cách-script-đo)
3. [Phần A: Chuẩn bị máy và code (một lần)](#3-phần-a-chuẩn-bị-máy-và-code-một-lần)
4. [Phần B: Dựng lại staging](#4-phần-b-dựng-lại-staging)
5. [Phần C: Kiểm tra trước mỗi lượt đo](#5-phần-c-kiểm-tra-trước-mỗi-lượt-đo)
6. [Phần D: Chạy DB-01](#6-phần-d-chạy-db-01)
7. [Phần E: Chạy NET-01](#7-phần-e-chạy-net-01)
8. [Phần F: Lặp nhiều lượt và tổng hợp số liệu](#8-phần-f-lặp-nhiều-lượt-và-tổng-hợp-số-liệu)
9. [Phần G: Khi một lượt thất bại](#9-phần-g-khi-một-lượt-thất-bại)
10. [Phần H: Kết thúc phiên](#10-phần-h-kết-thúc-phiên)

---

## 1. Nhánh đo này là gì

Luồng đầy đủ của hệ thống là: lỗi → alert → Agent chẩn đoán và đề xuất → **Safety Gate + chữ ký Operator** →
**Safe Executor** → **Independent Verifier** → `RESOLVED`. Nhánh đối chứng bỏ ba khâu in đậm để đo xem hệ thống
phục hồi nhanh đến đâu khi **không** có chúng:

```text
 ① gây lỗi (moodle-fault-inject.sh)
 ② alert firing trên Prometheus
 ③ Agent (mode investigate, shadow) ghi remediation_candidate
 ⑤ script áp dụng NGAY đúng hành động Agent đề xuất bằng harness reset đã review
    (không prepare, không chữ ký, không Safe Executor)
 ⑥' coi là phục hồi khi đủ 3 synthetic transaction liên tiếp thành công (không Verifier)
```

Script thực hiện: [`automation/moodle-rto-rpo-ablation.sh`](../automation/moodle-rto-rpo-ablation.sh).

**Giới hạn an toàn của script (đọc trước khi chạy):**

- Chỉ chạy trên staging: phải đặt `MOODLE_FAULT_CONFIRM=staging`, và URL phải khớp `moodle-staging-*`.
- Hai công tắc live (Agent và Executor) **phải tắt**; bật thì script dừng với exit 77.
- Script chỉ thực thi hai thủ tục reset đã review: `DB-01` gỡ rule iptables chặn 5432, `NET-01` tạo lại container
  `moodle-web` từ Compose. Nếu Agent đề xuất hành động khác, script **ghi lại và không thực thi**, rồi tự reset lỗi.
- Nhấn `Ctrl+C` giữa chừng: `trap` tự dừng probe nền và reset lỗi.

**Phân loại bằng chứng:** `staging_runtime_ablation`. Đây là số liệu chạy thật trên staging của một nhánh **cố ý bỏ
qua chuỗi an toàn**, không phải bằng chứng hệ thống khôi phục an toàn. Không gộp chung với số liệu của luồng đầy đủ.

---

## 2. Các chỉ số và cách script đo

| Chỉ số | Công thức | Nguồn mốc thời gian |
|---|---|---|
| **MTTD** | `t_detect − t_inject` | `t_inject`: ngay trước khi gây lỗi. `t_detect`: `activeAt` của alert trên Prometheus (lúc alert bắt đầu *pending*) |
| Alert → đề xuất | `t_diagnose − t_detect` | `collected_at` của `remediation_candidate` đầu tiên của Agent sau `t_inject` |
| Thực thi | `t_execute_end − t_execute_start` | Thời gian chạy harness reset |
| **RTO người dùng** | `t_recovered − t_inject` | `t_recovered`: lúc **bắt đầu** synthetic thành công đầu tiên trong chuỗi 3 lần liên tiếp |
| **RTO vận hành** | `t_recovered − t_detect` | Như trên |
| Trang login gián đoạn | Lần đầu ≠ 200 → lần đầu 200 trở lại | Probe `GET /login/index.php` mỗi 2 s |
| **RPO** | Khoảng thời gian của các bản ghi **đã xác nhận ghi** nhưng biến mất | Heartbeat: mỗi 2 s ghi 1 dòng vào bảng `config_plugins` qua container web của node A (cùng đường tới RDS mà Moodle dùng) |
| Thời gian không ghi được | FAIL đầu tiên sau `t_inject` → ACK đầu tiên sau đó | Log heartbeat |

**Kỳ vọng về RPO:** cả DB-01 (chặn mạng tới RDS) và NET-01 (sai phân giải tên) đều **không chạm vào dữ liệu**. Khi DB
không với tới được, lệnh ghi trả FAIL chứ không ACK, nên kỳ vọng `RPO = 0 s` (`0/N` bản ghi mất). Đó là kết quả
đo được, không phải giả định: script so từng bản ghi đã ACK với dữ liệu thật trong RDS sau khi phục hồi. Chỉ số phản
ánh mức gián đoạn ghi là **thời gian không ghi được**. Khi viết luận văn, ghi rõ cả hai.

Kết quả của mỗi lượt nằm trong `terraform/.artifacts/moodle-rto-rpo/ABL-<kịch bản>-<thời điểm UTC>/`:

| File | Nội dung |
|---|---|
| `result.json` | Toàn bộ mốc thời gian, chỉ số, quyết định của Agent, `baseline_after_run` |
| `login-probe.tsv` | `epoch  http_code` mỗi 2 s |
| `heartbeat-writer.tsv` | `epoch  seq  ACK/FAIL` |
| `acked.tsv`, `heartbeat-present.tsv`, `lost.tsv` | Đối chiếu RPO |
| `baseline-after.err` | Lý do baseline chưa sạch sau lượt chạy (nếu có) |

Thư mục `terraform/.artifacts/` bị Git bỏ qua; muốn lưu bằng chứng lâu dài thì tự sao ra nơi khác.

---

## 3. Phần A: Chuẩn bị máy và code (một lần)

### A1. Đưa code lên GitHub để CI build image Agent

Staging chỉ chạy image do CI build (không build trên EC2). `develop` ở máy bạn đang **đi trước `origin/develop` 5
commit** (Okta bị gỡ, Verifier mới, script đo này). Nếu không push, `pick_image` (bước B6) sẽ không tìm thấy image
Agent khớp code hiện tại.

```bash
# Windows hoặc WSL, ở thư mục gốc repo
git log --oneline origin/develop..develop      # phải thấy 5 commit, mới nhất 795d1a20
git push origin develop
```

Sau khi push, mở tab **Actions** trên GitHub và chờ **cả hai** workflow xanh cho commit `795d1a20`:

- `ci.yml` (lint, test, replay, build kiểm tra)
- `agent-image.yml` (build và publish `moodle-ai-agent`)

✅ **Cổng A1:** hai workflow trên đều `success`. Đỏ thì dừng, sửa xong mới làm tiếp.

### A2. Mở môi trường WSL

Mọi lệnh từ đây chạy trong **WSL Ubuntu**, không chạy trong PowerShell hay Git Bash.

```bash
source ~/moodle-ops/env.sh          # cd vào repo, đặt AWS_PROFILE, ANSIBLE_CONFIG, PATH
pwd                                 # /mnt/a/KLTN/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations
git log --oneline -1                # 795d1a20 (hoặc mới hơn)
for c in terraform ansible-playbook aws jq ssh python3 base64 awk; do command -v "$c" >/dev/null || echo "THIẾU $c"; done
```

✅ **Cổng A2:** không có dòng `THIẾU`, và commit đúng.

### A3. Sửa CRLF cho các file Python sẽ được đẩy lên host

Repo nằm trên ổ Windows với `core.autocrlf=true`, nên file `.py` trong working tree có kết thúc dòng CRLF. Ansible
đẩy nguyên file đó lên host, và script hỏng với lỗi `/usr/bin/env: 'python3\r'`. File `.sh` thì không bị, vì
`.gitattributes` đã ép LF.

```bash
grep -l $'\r' automation/*.py automation/lib/*.py 2>/dev/null       # liệt kê file đang CRLF
sed -i 's/\r$//' automation/moodle-synthetic-transaction.py automation/moodle-safe-executor-api.py
grep -c $'\r' automation/moodle-synthetic-transaction.py automation/moodle-safe-executor-api.py   # phải là 0 và 0
git status --short                                                   # vẫn sạch (Git so sánh sau khi chuẩn hoá)
```

Phải làm lại bước này mỗi khi Git ghi lại hai file đó (checkout, pull, đổi nhánh).

---

## 4. Phần B: Dựng lại staging

Phần này là bản rút gọn của [báo cáo triển khai](BAO_CAO_HUONG_DAN_TRIEN_KHAI.md). Khi một bước báo lỗi, đọc mục
tương ứng ở đó; **không bỏ qua cổng kiểm tra** nào.

### B1. Credential AWS (mục 5)

`~/moodle-ops/env.sh` đã đặt `AWS_PROFILE=target-account` và trỏ vào file credential của Windows. Trên máy này,
`target-account` là **IAM user dùng trực tiếp** (không có `role_arn`, không hỏi MFA), và lần dựng ngày 13/11 đã chạy
bằng chính profile này.

```bash
aws sts get-caller-identity --query Arn --output text    # ghi lại ARN; phải là danh tính bạn đã dùng để dựng trước đây
```

Nếu sau này `target-account` được đổi thành role `InfraEngineer` có MFA như mục 5 của báo cáo triển khai, thì làm
theo đúng mục đó:

```bash
eval "$(aws configure export-credentials --profile target-account --format env)"
unset AWS_PROFILE
export AWS_REGION=ap-southeast-1
aws sts get-caller-identity --query Arn --output text    # chứa assumed-role/InfraEngineer
```

### B2. Đầu vào Terraform (mục 6)

```bash
bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars   # cập nhật my_ip_cidr theo IP hiện tại
grep -E '^(my_ip_cidr|ec2_ami_id)' terraform/deployment.tfvars                     # AMI phải được ghim
```

### B3. Terraform plan và apply (mục 7)

Chạy Terraform **từ WSL** với thư mục dữ liệu riêng (chạy từ Windows sẽ ghi đường dẫn `C:/` vào output).

```bash
export TF_DATA_DIR="$HOME/moodle-ops/tfdata"
terraform -chdir=terraform init -input=false
terraform -chdir=terraform validate
umask 077; mkdir -p terraform/.artifacts
terraform -chdir=terraform plan -input=false -var-file=deployment.tfvars -out=.artifacts/deployment.tfplan
terraform -chdir=terraform show -no-color .artifacts/deployment.tfplan | grep -E '^Plan:'   # đọc kỹ; 0 to destroy
terraform -chdir=terraform apply .artifacts/deployment.tfplan                              # 15–25 phút
```

Nếu apply báo phiên bản RDS không còn được AWS cung cấp (lần dựng 13/11 đã phải đổi sang `16.15`), xem các bản còn
hỗ trợ, đặt `moodle_db_engine_version` trong `deployment.tfvars` rồi plan lại:

```bash
aws rds describe-db-engine-versions --engine postgres --query 'DBEngineVersions[?starts_with(EngineVersion,`16.`)].EngineVersion' --output text
grep moodle_db_engine_version terraform/deployment.tfvars
```

### B4. Host key và kết nối (mục 8)

```bash
umask 077
terraform -chdir=terraform output -raw moodle_ssh_config        > terraform/.artifacts/moodle-ssh.config
terraform -chdir=terraform output -raw moodle_ansible_inventory > terraform/.artifacts/moodle-inventory.yml
chmod 600 terraform/.artifacts/moodle-ssh.config
```

Làm **đúng toàn bộ mục 8.3** của báo cáo triển khai: xoá key cũ, so fingerprint với console output, rồi mới thêm
key. Không tắt `StrictHostKeyChecking`.

✅ **Cổng B4:**

```bash
ansible -i terraform/.artifacts/moodle-inventory.yml all -m ping     # 3 host đều "pong"
```

### B5. Chuẩn bị host (mục 9)

```bash
bash automation/prepare-moodle-runtime.sh
for h in moodle-app-a moodle-app-b; do
  ssh -F terraform/.artifacts/moodle-ssh.config "$h" 'findmnt -no FSTYPE --target /mnt/efs/moodledata'
done                                                                 # nfs4 hoặc efs, cả hai node
```

### B6. Chọn image Moodle và Agent (mục 10.2, 10.3, 13.1)

```bash
source automation/lib/image-select.sh
SHA="$(pick_image moodle moodle)" && ci_ok "$SHA" && export MOODLE_IMAGE="ghcr.io/benjaminnhnn/moodle:$SHA"
AGENT_SHA="$(pick_image moodle-ai-agent agent_src)" && ci_ok "$AGENT_SHA" \
  && export AGENT_IMAGE="ghcr.io/benjaminnhnn/moodle-ai-agent:$AGENT_SHA"
echo "MOODLE_IMAGE=$MOODLE_IMAGE"; echo "AGENT_IMAGE=$AGENT_IMAGE"
git merge-base --is-ancestor 795d1a20 "$AGENT_SHA" && echo "Agent image có code mới" || echo "SAI: image Agent cũ"
bash automation/moodle-preflight.sh "$MOODLE_IMAGE"
```

✅ **Cổng B6:** hai biến đều có giá trị, dòng cuối là `Agent image có code mới`. Nếu báo `SAI`, quay lại A1: CI
chưa publish image cho commit mới.

### B7. Secret, stage, cài đặt và khởi động Moodle (mục 11)

Database là database mới, nên dùng lại cặp mật khẩu trong `~/moodle-ops/secrets` hay tạo mới đều được. Dưới đây dùng
lại.

```bash
bash automation/stage-moodle-release.sh "$MOODLE_IMAGE" \
  ~/moodle-ops/secrets/moodle-db-password ~/moodle-ops/secrets/moodle-admin-password
```

Nếu script báo không gọi được Docker (Docker Desktop không tích hợp WSL), chạy lại với `docker.exe` bị loại khỏi
PATH để script dùng `psql` cài sẵn trong `~/.local/bin`:

```bash
PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vi docker | paste -sd:)" \
  bash automation/stage-moodle-release.sh "$MOODLE_IMAGE" \
  ~/moodle-ops/secrets/moodle-db-password ~/moodle-ops/secrets/moodle-admin-password
```

Cài Moodle **một lần, chỉ trên node A**, rồi khởi động (node A chạy web + cron, node B chỉ chạy web):

```bash
SSH_CFG=terraform/.artifacts/moodle-ssh.config
ssh -F "$SSH_CFG" moodle-app-a 'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
  -f /opt/moodle/release/docker-compose.yml --profile installer run --rm moodle-install'
ssh -F "$SSH_CFG" moodle-app-a 'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
  -f /opt/moodle/release/docker-compose.yml --profile cron up -d --wait moodle-web moodle-cron'
ssh -F "$SSH_CFG" moodle-app-b 'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
  -f /opt/moodle/release/docker-compose.yml up -d --wait moodle-web'
```

✅ **Cổng B7 (mục 12):**

```bash
U="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"
curl --fail --silent --show-error "$U/healthz.php"; echo
TG_ARN="$(terraform -chdir=terraform output -json moodle_application | jq -r .target_group_arn)"
aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query 'TargetHealthDescriptions[].TargetHealth.State' --output text      # healthy healthy
```

### B8. Monitoring, Safe Executor và Agent (mục 13.2, 13.4)

Với monitor **mới dựng**, thứ tự là: monitoring **chưa có Agent** (bước này cài Docker) → hai playbook Safe Executor
→ monitoring **có Agent**. Nhớ làm lại bước A3 (CRLF) ngay trước khối này.

```bash
bash automation/configure-moodle-monitoring.sh
bash automation/configure-moodle-safe-executor.sh
bash automation/configure-moodle-safe-executor-api.sh
bash automation/configure-moodle-monitoring.sh --agent-image "$AGENT_IMAGE"
```

Safe Executor được **cài nhưng để tắt**: nhánh đo không dùng nó, và trạng thái tắt là điều kiện script kiểm tra.
Không đặt `MOODLE_SAFE_ACTION_LIVE_ENABLED` khi chạy các lệnh trên.

### B9. Tunnel và baseline (mục 13.2)

Mở terminal **T2** trong WSL và để nó chạy suốt phiên:

```bash
bash ~/moodle-ops/tunnel.sh          # Grafana :3000, Agent :8000, Prometheus :9090, Alertmanager :9093
```

Trở lại terminal chính:

```bash
bash automation/moodle-environment-baseline.sh capture
bash automation/moodle-environment-baseline.sh verify       # "Moodle experiment baseline verification passed."
```

✅ **Cổng B9:** `verify` đạt. Baseline yêu cầu **không còn alert nào**; một alert còn treo sẽ chặn mọi lượt đo.

---

## 5. Phần C: Kiểm tra trước mỗi lượt đo

Bố trí ba terminal WSL, terminal nào cũng `source ~/moodle-ops/env.sh` trước:

| Terminal | Dùng để |
|---|---|
| **T1** | Chạy script đo |
| **T2** | Tunnel (bước B9) |
| **T3** | Quan sát: dán **một lần** bộ hàm ở [báo cáo kịch bản lỗi, mục 2.4](BAO_CAO_KICH_BAN_LOI.md#24-bộ-lệnh-kiểm-tra-t3) (`pq`, `alerts`, `tg`, `switches`, `agent_decisions`, `agent_logs`, `executor_log`) |

Ở **T3**, trước **mỗi** lượt:

```bash
aws sts get-caller-identity --query Arn --output text   # còn hạn
switches                 # {"agent_live":false} và {"executor_live":false}
alerts                   # không có dòng nào
tg                       # 2 dòng healthy
executor_log             # GHI LẠI số này (N0); sau lượt đo phải không đổi
ssh -F "$SSH_CFG" monitor-ai-01 'sudo docker inspect moodle-ai-agent --format "{{.Config.Image}}"'   # đúng $AGENT_IMAGE
ssh -F "$SSH_CFG" monitor-ai-01 'sudo docker exec moodle-ai-agent printenv AIOPS_UNIFIED_CORE_MODE'  # investigate
```

✅ **Cổng C:** đủ cả 7 điều kiện. `executor_log` không đổi sau lượt đo là bằng chứng Safe Executor **không** tham
gia. Ghi số `N0` vào sổ tay.

---

## 6. Phần D: Chạy DB-01

**Lỗi:** chèn rule `iptables` REJECT TCP 5432 tới IP của RDS trên **cả hai** node (chain `DOCKER-USER`).
**Alert:** `MoodleNodeRdsTcpFailed` (probe node mỗi 30 s, `for: 30s`).
**Hành động Agent phải đề xuất:** `remove_scoped_db_reject`, phạm vi `staging_moodle_nodes`.

### D1. Chạy (T1)

```bash
MOODLE_FAULT_CONFIRM=staging bash automation/moodle-rto-rpo-ablation.sh DB-01 2>&1 \
  | tee "terraform/.artifacts/ablation-DB-01-$(date -u +%Y%m%dT%H%M%SZ).log"; echo "exit=${PIPESTATUS[0]}"
```

Dòng cuối `exit=<số>` là exit code của script (không phải của `tee`). Ghi số này vào sổ tay.

### D2. Những gì sẽ thấy ở T1 (một lượt khoảng 5–8 phút)

| Dòng in ra | Ý nghĩa | Thời gian thường gặp |
|---|---|---|
| `preflight: baseline, live switches, heartbeat table` | Kiểm tra baseline, hai công tắc tắt, dọn heartbeat cũ | 30–90 s |
| `warm-up 30s (...)` | Probe login và heartbeat chạy; phải có 200 và ACK | 30 s |
| `① inject DB-01` | Gây lỗi; mốc `t_inject` | tức thì |
| `② waiting for MoodleNodeRdsTcpFailed to fire` | Chờ alert firing | khoảng 30–90 s |
| `③ waiting for the Agent's remediation proposal` | Chờ Agent ghi `remediation_candidate` | vài giây đến khoảng 1 phút |
| `Agent proposed: remove_scoped_db_reject (...)` | Đề xuất đúng; in `cause_code`, `confidence`, số bằng chứng | |
| `⑤ executing remove_scoped_db_reject directly (no gate/executor)` | Gỡ rule bằng harness reset | vài giây |
| `⑥' waiting for 3 consecutive synthetic successes (no Verifier)` | Chờ 3 synthetic liên tiếp thành công | khoảng 30–60 s |
| `recovered at ...` | Mốc `t_recovered` | |
| Bảng `== DB-01 | agent_without_gate_executor_verifier ==` | Kết quả (xem D4) | |

### D3. Quan sát song song (T3, tuỳ chọn)

```bash
alerts                 # trong lúc lỗi: MoodleNodeRdsTcpFailed pending → firing (cột 4 = activeAt)
agent_decisions        # diagnosis rồi remediation_candidate: remove_scoped_db_reject
pq 'moodle_node_rds_tcp_success{job="moodle_node"}'     # 0 khi lỗi, 1 sau khi gỡ
```

Có thể thấy thêm `MoodleSyntheticTransactionFailed`, vì synthetic cũng hỏng khi DB không với tới được. Đây là triệu
chứng đi kèm, không phải lỗi của lượt đo.

### D4. Đọc kết quả

```text
== DB-01 | agent_without_gate_executor_verifier ==
MTTD (inject→alert activeAt) : <s>
Alert → Agent proposal       : <s>
Direct execution             : <s>
RTO user (inject→recovered)  : <s>
RTO ops  (detect→recovered)  : <s>
Login page outage (probe)    : <s>
RPO                          : 0 s (0/<N> acknowledged writes lost)
Write unavailability         : <s>
Baseline after run           : passed
evidence: terraform/.artifacts/moodle-rto-rpo/ABL-DB-01-<UTC>/result.json
```

✅ **Cổng D (một lượt hợp lệ):** exit code `0`, `Baseline after run: passed`, và `executor_log` ở T3 vẫn bằng `N0`.

---

## 7. Phần E: Chạy NET-01

**Lỗi:** thêm dòng `127.0.0.1 <endpoint RDS>` vào `/etc/hosts` của container `release-moodle-web-1` trên **cả hai**
node. Health check `healthz.php` có thể vẫn trả 200, nhưng mọi đường cần DB đều hỏng.
**Alert:** `MoodleSyntheticTransactionFailed` (synthetic mỗi 30 s, `for: 5s`).
**Hành động Agent phải đề xuất:** `recreate_moodle_web_from_reviewed_compose`, phạm vi `staging_moodle_nodes`.

### E1. Trước khi chạy

- Nếu vừa chạy DB-01, chờ đến khi `alerts` ở T3 **trống** và làm lại toàn bộ Phần C.
- Lượt NET-01 tạo lại container web, nên thường kéo theo alert nhiễu `MoodleNodeMemoryPressure` trong 10–80 s sau
  khi container khởi động. Script đã chịu được việc này (xem E4).

### E2. Chạy (T1)

```bash
MOODLE_FAULT_CONFIRM=staging bash automation/moodle-rto-rpo-ablation.sh NET-01 2>&1 \
  | tee "terraform/.artifacts/ablation-NET-01-$(date -u +%Y%m%dT%H%M%SZ).log"; echo "exit=${PIPESTATUS[0]}"
```

Các bước in ra giống D2, với alert `MoodleSyntheticTransactionFailed` và hành động
`recreate_moodle_web_from_reviewed_compose`. Bước ⑤ chậm hơn DB-01 vì phải tạo lại container trên hai node và chờ
`--wait` healthy.

### E3. Quan sát song song (T3, tuỳ chọn)

```bash
alerts
agent_decisions        # remediation_candidate: recreate_moodle_web_from_reviewed_compose
ps_node moodle-app-a   # sau bước ⑤: container release-moodle-web-1 "Up ... (healthy)", tuổi chỉ vài giây
pq 'moodle_synthetic_success{job="moodle_synthetic"}'
```

### E4. Đặc thù khi đọc kết quả NET-01

- Heartbeat ghi qua container web của node A. Khi container đó được tạo lại ở bước ⑤, các lệnh ghi trong vài giây
  sẽ FAIL. Chúng làm tăng *thời gian không ghi được*, **không** làm tăng RPO, vì không có bản ghi đã ACK nào mất.
- `Login page outage` có thể là `null` nếu trang login vẫn trả 200 trong suốt thời gian lỗi. Đây chính là hiện
  tượng "health xanh nhưng giao dịch hỏng" mà NET-01 muốn chứng minh; RTO vẫn đo bằng synthetic có đăng nhập.
- Nếu `Baseline after run: failed` và script thoát với exit **3**: số đo vẫn hợp lệ và đã nằm trong `result.json`.
  Mở `baseline-after.err` để xem alert nào còn treo (thường là `MoodleNodeMemoryPressure`). Chờ `alerts` trống rồi
  chạy `bash automation/moodle-environment-baseline.sh verify` cho tới khi đạt, rồi mới chạy lượt tiếp.

✅ **Cổng E:** giống cổng D; với exit 3 thì phải có `verify` đạt trước lượt kế tiếp.

---

## 8. Phần F: Lặp nhiều lượt và tổng hợp số liệu

### F1. Bao nhiêu lượt

Một lượt chỉ là một mẫu. Chạy **ít nhất 5 lượt cho mỗi kịch bản**, xen kẽ DB-01 và NET-01 (DB-01, NET-01, DB-01,
...) để giảm ảnh hưởng của biến động theo thời gian. Giữa hai lượt:

1. Chờ `alerts` trống (thường 1–3 phút).
2. Làm lại Phần C, ghi lại `N0`.
3. Kiểm tra credential AWS còn hạn (mỗi lượt cần khoảng 10 phút).

Ghi sổ tay cho mỗi lượt: `run_id`, exit code, `N0` trước và sau, và mọi bất thường (alert nhiễu, phải chạy lại...).
Lượt **không hợp lệ** (exit ≠ 0 và ≠ 3, Agent đề xuất sai, Executor bị đổi số) thì giữ lại làm bằng chứng nhưng
**không** đưa vào thống kê.

### F2. Tổng hợp

Từ gốc repo trong WSL:

```bash
python3 - <<'PY'
import glob, json, statistics, sys
root = "terraform/.artifacts/moodle-rto-rpo"
rows = []
for path in sorted(glob.glob(f"{root}/ABL-*/result.json")):
    r = json.load(open(path, encoding="utf-8"))
    if "seconds" not in r:          # agent_action_mismatch: không phải một phép đo
        print(f"skip {path}: {r.get('status')}", file=sys.stderr); continue
    s, p = r["seconds"], r["rpo"]
    rows.append((r["scenario_id"], r["run_id"], s["mttd"], s["detect_to_diagnosis"], s["execution"],
                 s["rto_user"], s["rto_ops"], s["login_page_outage"], p["rpo_seconds"],
                 f'{p["lost_acknowledged_writes"]}/{p["acknowledged_writes"]}', p["write_unavailability_s"],
                 r.get("baseline_after_run", "-")))
head = ("scenario", "run_id", "mttd", "alert_to_proposal", "exec", "rto_user", "rto_ops",
        "login_outage", "rpo", "lost/acked", "write_unavail", "baseline_after")
print("\t".join(head))
for row in rows:
    print("\t".join("-" if v is None else str(v) for v in row))
print()
print("scenario\tn\tmetric\tmedian\tmin\tmax")
for sc in sorted({r[0] for r in rows}):
    sel = [r for r in rows if r[0] == sc]
    for name, idx in (("mttd", 2), ("rto_user", 5), ("rto_ops", 6), ("rpo", 8), ("write_unavail", 10)):
        vals = [float(r[idx]) for r in sel if r[idx] is not None]
        if vals:
            print(f"{sc}\t{len(vals)}\t{name}\t{statistics.median(vals):.1f}\t{min(vals):.1f}\t{max(vals):.1f}")
PY
```

Đoạn này đọc mọi `result.json` trong thư mục. Trước khi tổng hợp, chuyển các lượt không hợp lệ (theo sổ tay) sang
thư mục khác, ví dụ `terraform/.artifacts/moodle-rto-rpo-excluded/`.

### F3. Trình bày trong luận văn

- Báo **trung vị, min, max và n** cho mỗi kịch bản, không chỉ một con số. Với n nhỏ, không tính khoảng tin cậy
  kiểu phân phối chuẩn.
- Ghi rõ **phân loại**: nhánh đối chứng chạy thật trên staging, bỏ qua Safety Gate, chữ ký Operator, Safe Executor
  và Independent Verifier; tiêu chí phục hồi là 3 synthetic liên tiếp.
- Khi so với luồng đầy đủ: luồng đầy đủ có thêm thời gian chờ người duyệt và cửa sổ ổn định 120 s của Verifier, nên
  RTO dài hơn là **chủ ý**. So sánh công bằng nhất là tách RTO thành: phát hiện (MTTD), Agent đề xuất, chờ người,
  thực thi, kiểm chứng. Không trình bày nhánh đối chứng như "nhanh hơn nên tốt hơn": cái giá là không có ai chặn
  được một đề xuất sai và không có bằng chứng phục hồi được ký.

---

## 9. Phần G: Khi một lượt thất bại

Script luôn tự reset lỗi khi thoát giữa chừng (trừ khi chính lệnh reset hỏng). Sau **mọi** lượt thất bại, kiểm tra
ở **T3** (cần biến `SSH_CFG` từ bộ hàm mục 2.4) rằng lỗi đã được gỡ hết:

```bash
for h in moodle-app-a moodle-app-b; do
  echo "== $h"
  ssh -F "$SSH_CFG" "$h" 'sudo iptables -S DOCKER-USER | grep moodle-fault-DB-01 || echo "DB-01: sạch"'
  ssh -F "$SSH_CFG" "$h" 'sudo docker exec release-moodle-web-1 grep -c "moodle-fault-NET-01" /etc/hosts || true'   # 0 = sạch
done
```

Còn sót thì reset tay rồi kiểm tra baseline:

```bash
MOODLE_FAULT_CONFIRM=staging bash automation/moodle-fault-reset.sh DB-01     # hoặc NET-01
bash automation/moodle-environment-baseline.sh verify
```

| Thông báo / exit code | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| exit **64** | Thiếu `MOODLE_FAULT_CONFIRM=staging` hoặc sai tên kịch bản | Chỉ nhận `DB-01` hoặc `NET-01` |
| exit **77**, `Live switches must be off` | Agent hoặc Executor đang bật live | Triển khai lại monitoring **không** kèm `MOODLE_SAFE_ACTION_LIVE_ENABLED` (bước B8), kiểm tra `switches` |
| exit **77**, `Refusing fault operation outside the Moodle staging URL` | Output Terraform không phải staging | Kiểm tra `terraform output -json moodle_application` |
| exit **66**, `Missing SSH configuration` | Chưa có `moodle-ssh.config` | Làm lại bước B4 |
| Lỗi ở bước `preflight` (baseline `verify`) | Còn alert, target chưa healthy, synthetic chưa xanh | Xem `alerts`, `tg`; chờ hoặc sửa rồi `verify` lại |
| `Login probe never returned 200 during warm-up` | Moodle chưa phục vụ được, hoặc IP của bạn bị security group chặn | `curl -I "$U/login/index.php"`; IP đổi thì chạy lại bước B2 + plan/apply |
| `Heartbeat writer acknowledged nothing during warm-up` | Không SSH được node A, hoặc container web không ghi được DB | `ssh -F "$SSH_CFG" moodle-app-a hostname`; `ps_node moodle-app-a` |
| `Prometheus alert did not fire within 300s` | Probe node hoặc timer synthetic không chạy | `pq 'up'`; kiểm tra timer trên node và monitor |
| `Agent produced no remediation proposal within 300s` | Agent không ở mode `investigate`, image cũ, worker lỗi, hoặc alert không tới Agent | `agent_logs 15m`; kiểm tra lại bước B6 và Phần C |
| `Agent proposed '<x>', expected '<y>'; not executed` | Agent chẩn đoán khác kỳ vọng | Lượt **không hợp lệ**. Giữ `result.json` (status `agent_action_mismatch`) làm bằng chứng; xem `agent_decisions` |
| `Service did not recover within 900s` | Hành động không khôi phục được, hoặc còn lỗi khác | Kiểm tra lỗi còn sót như trên; xem `alerts`, `ps_node` |
| exit **3**, `Baseline is not clean after the run` | Alert nhiễu còn treo sau khi phục hồi | Số đo vẫn hợp lệ (mục E4); chờ rồi `verify` |
| `Unable to locate credentials` / `ExpiredToken` | Credential AWS hết hạn | Làm lại bước B1 rồi chạy lại lượt |

---

## 10. Phần H: Kết thúc phiên

1. Sao `terraform/.artifacts/moodle-rto-rpo/` và các file log `ablation-*.log` ra nơi lưu trữ bằng chứng.
2. Chạy bước tổng hợp F2 và lưu bảng kết quả.
3. Nếu không chạy tiếp trong ngày: gỡ hạ tầng theo
   [báo cáo triển khai, phụ lục C](BAO_CAO_HUONG_DAN_TRIEN_KHAI.md#phụ-lục-c-gỡ-toàn-bộ-hạ-tầng-chi-tiết).
   2 NAT Gateway vẫn tính tiền khi EC2 đã dừng.
