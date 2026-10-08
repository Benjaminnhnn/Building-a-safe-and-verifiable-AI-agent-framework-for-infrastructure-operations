# Kế hoạch triển khai khóa luận 14 tuần

## 1. Muc tieu va nguyen tac (scope/current status updated 2026-10-05)

### Product goal

Build a safe, verifiable AI-agent framework for Moodle incident operations on AWS EC2/Linux/Docker Compose. The pipeline observes, gathers evidence, diagnoses, plans, gates bounded execution, and reports recovery only after the Independent Verifier validates health and the communication contract.

### Research questions

- **RQ1:** Does an evidence-, permission-, blast-radius-, and rollback-aware Safety Gate reduce unsafe or inappropriate actions compared with Manual, Ansible rule-based, and ungated-agent baselines?
- **RQ2:** Does an Independent Verifier using a communication contract reduce false recovery compared with health-only verification?

### Scope

Moodle on AWS EC2 is the primary implementation and evaluation environment, with ALB, RDS PostgreSQL, EFS, Prometheus/Alertmanager, and the AI Agent. ERPNext is an optional generalization extension; it is not a Moodle acceptance requirement and its fixtures do not prove a deployed or evaluated runtime.

In scope: Linux, Docker Compose, Terraform, Ansible, monitoring, evidence storage, RAG, Safety Gate, approval, audit, conditional rollback, Independent Verifier, bounded fault injection, and benchmark methodology. Out of scope: Kubernetes/EKS, OpenStack, Windows, multi-cloud, automatic IAM/security-group changes, business-data changes/deletion, and complex multi-agent fan-out.

### Evaluation rules

- Ground truth is for post-decision scoring only; it must never authorize actions or be supplied as decision evidence.
- Only the Independent Verifier may transition an incident to `RESOLVED`; a health check alone is insufficient.
- Unit tests, fixture replay, shadow, deployed runtime, live fault drills, and benchmark observations are distinct evidence modes.
- Live-recovery acceptance needs a scoped fault, fresh alert/evidence, gate decision, allowlisted action, independent probes and stability window, reset, and provenance-bearing artifacts.
- The thresholds below are targets, not achieved results.

## 2. Current State Assessment (repository snapshot 2026-10-05)

This is a static source-tree inventory. Code or configuration being present does not prove deployment or acceptance.

| Component | Present in checkout | Status boundary | Action |
|---|---|---|---|
| Moodle/AWS | Terraform for ALB, two Moodle EC2 nodes, RDS, EFS and monitor; Moodle image/Compose; Ansible and deployment scripts | Verify AWS state, runtime, and artifact before current-state claims | Keep Moodle as the primary deployment/evaluation path |
| Agent/monitoring | Prometheus/Alertmanager, FastAPI webhook, SQLite evidence, orchestrator, policy, safe-executor boundary, verifier contracts | Tests/replay do not prove live webhook delivery or remediation | Preserve fail-closed boundaries; collect runtime artifacts separately |
| Moodle scenarios | 15 ground-truth contracts, resource inventory, replay and fault/verification automation | Do not assume all 15 injectors/resets/live acceptances are complete | Track each scenario's contract, injector, reset, probes, and live trials |
| CI/CD | CI checks agent, Moodle automation, image builds, Compose config, and offline replay; Moodle staging deploy workflow exists | CI replay is simulated; deployment depends on runner/secrets/runtime | Keep mode and provenance assertions |
| Benchmark | Synthetic-only harness; no committed result dataset | `automation/run-benchmark.py` generates randomized fixture outcomes; latest default n=5 empirical acceptance audit is 0/225 | Do not use generated outcomes as observed Moodle data; collect/freeze raw live trials before RQ conclusions |
| Legacy banking stack | Old README/Compose/workflow files, `app-release-deploy.sh`, Terraform `bank-web-01`/`bank-core-01`, and inventory references | Terraform state may still own resources; removal may destroy EC2/EIPs | Remove from thesis docs/CD first; retire cloud resources only after state-backed plan and review |
| ERPNext | Three fixture contracts and offline demo | No evidence here of deployed runtime or measured generalization | Keep as optional extension, not Moodle acceptance |
| Dated reports | `report.md` and sprint/operations reports contain dated snapshots | Historical, not live status; claims require source artifacts | Retain scope, date, and limitations |

### Baseline evidence policy

Old baseline/test counts elsewhere in this plan are historical and are not current status. For every acceptance claim, record commit, command, environment, artifact, and evidence mode. Before scoring benchmark data, reconcile run IDs, duplicate rows, data-generation logic, and provenance.

## 3. Deliverable và tiêu chí thành công

### Deliverable bắt buộc

1. Architecture sáu lớp và threat/safety model.
2. Resource, Evidence, Incident, Typed Action schemas.
3. Evidence Store, dependency graph và RAG provenance.
4. Observer, Diagnosis, Planner, Execution, Verification Agent.
5. Sequential Orchestrator và structured message contract.
6. Safety Policy Engine, catalog, RBAC, approval, audit, conditional rollback.
7. Independent Verifier và communication contract.
8. Moodle và 15 fault scenarios.
9. (Optional) ERPNext three-scenario generalization after Moodle acceptance is established.
10. Manual/Ansible/AI benchmark, hai ablation, raw data và phân tích RQ1/RQ2.
11. Báo cáo, slide, demo runbook, video backup và reproduction guide.

### Success criteria

- 15/15 Moodle scenario có ground truth, injector, reset và schema hợp lệ.
- Main benchmark: `15 × 3 phương pháp × 5 repetitions = 225 runs`.
- Safety ablation và verifier ablation: 10 scenario Moodle đại diện × 5 repetitions mỗi nhóm, đạt ít nhất 50 runs cho từng ablation. Mười cụm scenario cũng cho phép exact sign test hai phía đạt p<0.05 sau Holm cho ba so sánh RQ1 khi các cụm cùng chiều; không hạ số cụm để chạy nhanh. Tập mặc định: DB-01/02/03, RES-01/02, NET-01/02, CON-01/02 và SEC-02.
- RCA top-1 accuracy ≥ 70%; recovery success ≥ 80%.
- Dangerous-action block rate ≥ 95%; forbidden automatic execution = 0.
- False recovery rate ≤ 5% và giảm ít nhất 50% so với health-only; paired exact sign test ở cấp scenario phải đạt p<0.05, theo chiều verifier giảm false recovery.
- Rollback success 100% trên action được khai báo reversible.
- Audit completeness 100%; chỉ Verifier được transition `RESOLVED`.
- MVP tuần 4: MTTD ≤ 60 giây, recovery ≤ 10 phút, pass ba lần liên tiếp.
- Mọi run lưu detection/remediation time, số thao tác, LLM calls, latency và AWS cost.

## 4. Ownership và nguyên tắc review

- **AI Engineer (O):** unified models; Evidence Store; context/RAG; dependency graph; năm agent; Orchestrator; policy logic phối hợp; ground truth; benchmark; metrics; ablation; phân tích.
- **Infrastructure Engineer (O):** AWS/EC2/Linux; Docker Compose; Terraform; Ansible; CI/CD; monitoring/logging/secrets; Moodle/ERPNext; adapters; probes; fault injection; rollback; cost; demo runbook.
- Schema, interface, policy, integration test, fault injection, experiment và report luôn có một owner chính và một reviewer chéo.
- Tổng effort dự kiến: **140 person-day**, chia đều **70 AI / 70 Infrastructure**.

## 5. Kế hoạch 7 sprint / 14 tuần

### Sprint 1 — Moodle foundation trước framework
O: Owner - Chịu trách nhiệm chính triển khai và hoàn thành hạng mục

R: Reviewer - Người review chéo, kiểm tra thiết kế, chất lượng và mức độ chấp nhận

| Tuần | Mục tiêu và task đủ nhỏ cho GitHub Issue | AI Engineer | Infrastructure Engineer | Dependency | Deliverable kiểm tra được | Acceptance/test | Rủi ro và dự phòng | Effort |
|---|---|---|---|---|---|---|---|---|
| 1 | Cài Moodle + PostgreSQL tối thiểu trên EC2; xác minh login, DB, backup, reset; audit repo song song để không mất context | O: định nghĩa Moodle Resource Model, synthetic transaction và DB-01 ground truth; R: triển khai | O: Terraform/Ansible/Compose deploy Moodle trên EC2; volume, secrets, health; R: model | AWS EC2 hiện có | Moodle staging instance, topology v0, restore/reset script | Moodle login và synthetic read/write pass ≥99% trong 2h; backup/restore 1 lần thành công; audit inventory 100% | Moodle image/plugin chậm → bản tối thiểu, local Compose trước khi đẩy EC2 | AI 5 + Infra 5 = 10 PD |
| 2 | Gắn monitoring và fault-injection foundation cho Moodle; chốt schema/ground truth đầu tiên | O: Resource/Evidence/Incident/Action V1; DB-01 và bốn scenario đại diện còn lại; R: topology | O: Prometheus/Alertmanager/blackbox/exporter; alert rules Moodle; injector/reset cho DB, resource, network, container, config; R: schema | W1 Moodle healthy | Moodle monitored, 5 representative scenario fixtures, baseline report | Scrape/alert delivery ≥99%; 5 scenario có injector/reset và chạy thử ít nhất 1 lần; ground truth validation 100% | Alert labels sai → contract test và static config validation; chưa dựng ERPNext | AI 5 + Infra 5 = 10 PD |

### Sprint 2 — MVP vertical slice

| Tuần | Mục tiêu và task đủ nhỏ cho GitHub Issue | AI Engineer | Infrastructure Engineer | Dependency | Deliverable kiểm tra được | Acceptance/test | Rủi ro và dự phòng | Effort |
|---|---|---|---|---|---|---|---|---|
| 3 | Skeleton pipeline trên Moodle: `alert→incident→evidence→diagnosis→plan→gate→execute→verify`; dedup và dry-run | O: normalizer validation, Incident Core, Evidence Store tối thiểu, deterministic diagnosis/planner, checkpoint orchestrator; R: adapter | O: Docker adapter dry-run/start/restart, contract probes, fault/reset cho 5 scenario; R: action schema | Moodle + W2 schema | Replayable Moodle pipeline và 5 scenario replay bundle | Mỗi scenario tạo đúng một incident; ≥3 evidence refs; replay không duplicate; malformed action reject 100% | Agent tách chậm → interface tách trước, tạm dùng cùng process | AI 6 + Infra 4 = 10 PD |
| 4 | MVP Moodle có thể thực hiện các scenario thử nghiệm, không chỉ DB-01 | O: ALLOW/APPROVAL/DENY tối thiểu, verifier interface, audit report; R: execution | O: chạy fault/reset cho ít nhất 5 scenario đại diện; exact allowlisted actions; allowed/forbidden/related probes; R: policy | W3 slice | Live Moodle MVP và scenario execution report | Ít nhất 5 scenario Moodle chạy end-to-end, mỗi scenario pass ≥3 lần; MTTD ≤60s; recovery ≤10m; forbidden execution = 0; stability ≥120s | Fault làm hỏng môi trường → snapshot/checksum và reset tự động; planner deterministic | AI 5 + Infra 5 = 10 PD |

### Sprint 3 — Unified core và infrastructure adapter

| Tuần | Mục tiêu và task đủ nhỏ cho GitHub Issue | AI Engineer | Infrastructure Engineer | Dependency | Deliverable kiểm tra được | Acceptance/test | Rủi ro và dự phòng | Effort |
|---|---|---|---|---|---|---|---|---|
| 5 | Hoàn thiện unified model và state machine | O: Pydantic schemas, transition guard, evidence provenance, action lifecycle; R: infra fields | O: resource snapshot, capability schema, environment isolation; R: model tests | MVP | Schema V1, migration, transition table | 100% message validate; invalid ID/state bị reject; action gắn incident/evidence/target | Over-modeling → chỉ giữ field cần cho 15 scenario/metrics | AI 6 + Infra 4 = 10 PD |
| 6 | Evidence/context và adapter ổn định | O: append-only Evidence Store, query, dependency graph, RAG source/time/hash; R: probes | O: Docker/Ansible read/execute adapters, dry-run, timeout, idempotency, sanitization; R: evidence | W5 model | Evidence Store, graph, adapter contract | Mọi evidence có source/time/hash/resource; retry không duplicate action; secret leakage = 0 | Chroma lẫn fact/evidence → SQLite/PostgreSQL làm source of truth | AI 5 + Infra 5 = 10 PD |

### Sprint 4 — Agent và Orchestrator

| Tuần | Mục tiêu và task đủ nhỏ cho GitHub Issue | AI Engineer | Infrastructure Engineer | Dependency | Deliverable kiểm tra được | Acceptance/test | Rủi ro và dự phòng | Effort |
|---|---|---|---|---|---|---|---|---|
| 7 | Observer và Diagnosis Agent | O: grouping, evidence request, hypotheses/top-1/confidence; R: signal quality | O: collectors metric/log/config/topology, timestamp sync, sanitized fixtures; R: diagnosis | Evidence Store | Observer/Diagnosis modules | Duplicate burst compression ≥90%; diagnosis luôn dẫn evidence; thiếu evidence không được success | Log/token volume → bounded excerpt và deterministic feature | AI 6 + Infra 4 = 10 PD |
| 8 | Planner và Orchestrator tích hợp | O: TypedAction, structured messages, sequential stages, retry/checkpoint/escalation; R: executable plan | O: adapter registry/capability discovery và deployment; R: retry semantics | W7 + adapters | Observer→Diagnosis→Planner→Gate request pipeline | 15 fixture replay đúng stage order; malformed output reject 100%; resume không duplicate | LLM variability → temperature 0, catalog parser, cached fixtures | AI 6 + Infra 4 = 10 PD |

### Sprint 5 — Execution và Safety

| Tuần | Mục tiêu và task đủ nhỏ cho GitHub Issue | AI Engineer | Infrastructure Engineer | Dependency | Deliverable kiểm tra được | Acceptance/test | Rủi ro và dự phòng | Effort |
|---|---|---|---|---|---|---|---|---|
| 9 | Safety Policy Engine, action catalog, RBAC, approval | O: rules, evidence threshold, confidence, blast radius, reversibility, approval TTL; R: operational feasibility | O: least-privilege identities, approval auth, environment boundary; R: policy bypass | Typed plan | Policy Engine V1 và signed approval record | Forbidden matrix DENY 100%; expired/wrong actor reject; Planner không có executor credential | Policy quá phức tạp → rule engine YAML/Python, không dùng LLM làm gate | AI 5 + Infra 5 = 10 PD |
| 10 | Execution Agent, audit, rollback | O: action state/output, conditional rollback, audit schema; R: adapter | O: exact allowlist, pre/post snapshot, rollback, kill switch, fault sandbox; R: audit | Gate | Safe execution và rollback | Action ngoài catalog = 0; audit completeness 100%; rollback 10/10 | Partial failure/destructive action → shadow mode, HUMAN_ONLY hoặc DENY | AI 4 + Infra 6 = 10 PD |

### Sprint 6 — Independent Verification và Moodle benchmark

| Tuần | Mục tiêu và task đủ nhỏ cho GitHub Issue | AI Engineer | Infrastructure Engineer | Dependency | Deliverable kiểm tra được | Acceptance/test | Rủi ro và dự phòng | Effort |
|---|---|---|---|---|---|---|---|---|
| 11 | Independent Verifier và communication contract | O: verdict model, canonical state authority, false-recovery tests; R: probe semantics | O: allowed/forbidden/related probes, read-only credentials, stability window; R: state guard | Execution result | Verifier độc lập hoàn chỉnh | Chỉ Verifier transition `RESOLVED` 100%; health-only trap phát hiện ≥90%; Verifier không có execute permission | Coupling với Executor → process/module và credential riêng | AI 5 + Infra 5 = 10 PD |
| 12 | Hoàn thiện 15 scenario và benchmark harness | O: ground truth, scorer, manual/Ansible/AI harness, metrics recorder; R: injection | O: 15 injector/reset/playbook, checksum, Moodle runs; R: scoring | Verifier | Fault suite và main dataset | 15/15 reset pass; 225 main runs hoặc quyết định n=3 có ghi limitation; không thiếu metric bắt buộc | Chậm chạy → nightly parallel runs, ưu tiên deterministic scenarios | AI 5 + Infra 5 = 10 PD |

### Sprint 7 — Generalization, ablation và bảo vệ

| Tuần | Mục tiêu và task đủ nhỏ cho GitHub Issue | AI Engineer | Infrastructure Engineer | Dependency | Deliverable kiểm tra được | Acceptance/test | Rủi ro và dự phòng | Effort |
|---|---|---|---|---|---|---|---|---|
| 13 | Moodle ablation và phân tích theo cụm scenario; ERPNext chỉ mở sau khi Moodle acceptance đạt | O: safety/no-gate và verifier/health-only analysis; R: phương pháp | O: thu hai tập ablation Moodle, mỗi tập ≥50 run, cùng protocol và provenance; ERPNext runtime chỉ khi được duyệt sau Moodle acceptance | Stable core + frozen Moodle benchmark | Hai tập ablation Moodle và phân tích thống kê; ERPNext result là deliverable tùy chọn | ≥50 safety-ablation + ≥50 verifier-ablation runs; không sửa core schema; ERPNext không chặn DoD Moodle | Nếu live collection chưa sẵn sàng, không tạo dữ liệu thay thế; ghi rõ blocked evidence và giữ RQ inconclusive | AI 6 + Infra 4 = 10 PD |
| 14 | Phân tích, báo cáo, slide, demo và rehearsal | O: statistics, RQ conclusions, limitations, thesis chapters, figures; R: infra claims | O: reproduction package, final demo, video backup, cost report, freeze; R: metrics | Frozen dataset | Thesis package | Mọi bảng truy nguyên raw data; hai rehearsal pass; backup demo ≤10m; zero P0 | AWS/demo lỗi → local replay, recorded demo, pre-generated result bundle | AI 5 + Infra 5 = 10 PD |

## 6. Scenario matrix bắt buộc

Trạng thái chung trước mỗi run: Moodle login, synthetic read/write, DB, cron và monitoring healthy; environment checksum được lưu. Mỗi scenario phải có `initial_state`, `fault_trigger`, `observed_signals`, `expected_root_cause`, `allowed_actions`, `forbidden_actions`, `recovery_criteria`, `communication_contract` và `rollback_plan`.

| Nhóm | Ba biến thể |
|---|---|
| Database | DB-01 PostgreSQL stopped; DB-02 connection exhaustion; DB-03 sai DB endpoint/config |
| Resource exhaustion | RES-01 CPU hog; RES-02 memory pressure/OOM; RES-03 disk fill bằng known fixture |
| Network/DNS | NET-01 mất DNS alias; NET-02 scoped port block Moodle→DB; NET-03 latency/loss bằng `tc netem` |
| Container/dependency | CON-01 Moodle stopped; CON-02 reverse proxy stopped/wrong upstream; CON-03 crash-loop do bad image/env |
| Security/configuration | SEC-01 accidental DB port exposure; SEC-02 sai ownership/mode `moodledata`; SEC-03 sai trusted proxy/wwwroot/security config |

For the deployed ALB + two Moodle EC2 + RDS PostgreSQL + EFS staging topology,
the ten not-yet-live variants use the architecture-specific definitions in
`evaluation/ground_truth/moodle/*.json` (updated 2026-09-30). In particular,
CON-02 is Apache router drift on one Moodle node, not a nonexistent Nginx
container; RES-03 is a bounded isolated scratch-filesystem ENOSPC drill, not
an attempt to fill elastic EFS; SEC-01 is an unauthorized **security-group
source** on private RDS, never public `0.0.0.0/0`, and is HUMAN_ONLY. These
variants remain **not live-approved** until the matching injector, reset,
node-local/SG observation, and independent recovery probes pass review. The
five previously reviewed variants retain their existing live scope.

The separate identity case study adds `AUTH-01` (directory service unavailable),
`AUTH-02` (Moodle-to-directory path unavailable), and `AUTH-03` (synthetic test
account disabled). These do not replace or rename the 15 Moodle infrastructure
scenarios. Their fixtures live under `evaluation/ground_truth/auth/` and remain
post-decision scoring data. Current implementation and staging blockers are
recorded in `docs/AI_ENGINEER_AUTH_RECOVERY.md`.

Communication contract tối thiểu:

- Allowed: client→Moodle; Moodle→database; monitor→exporter; Alertmanager→Agent.
- Forbidden: Internet→PostgreSQL/Redis; Planner→Executor bypass; app→metadata/admin endpoint.
- Related: monitoring, queue và dịch vụ không thuộc fault vẫn hoạt động.

Nếu được triển khai như phần generalization tùy chọn sau Moodle acceptance, ERPNext dùng ba scenario: MariaDB stopped, Redis dependency stopped và Nginx/configuration drift.

## 7. Safety Gate

### Decision rules

| Điều kiện | Decision |
|---|---|
| Read-only probe, target allowlisted, read-only credential | ALLOW |
| Staging, confidence ≥0.80, evidence đủ, một resource, reversible, rollback ready, contract không bị phá | ALLOW |
| Confidence 0.65–0.79 hoặc blast radius trung bình, action reversible | REQUIRE_APPROVAL |
| Production, shared dependency, host restart hoặc action ảnh hưởng nhiều service | REQUIRE_APPROVAL / HUMAN_ONLY |
| IAM/security group, business-data rollback, drop/truncate, xóa volume, unrestricted shell/SQL | DENY hoặc HUMAN_ONLY; Agent không tự thực thi |
| Forbidden action, target sai environment, permission thiếu, approval hết hạn, action ngoài catalog, rollback không sẵn sàng | DENY |

### RBAC

- Observer/Diagnosis/Planner: không có execution credential.
- Safety Engine: chỉ quyết định, không chạy command.
- Executor: credential giới hạn theo adapter/action catalog.
- Verifier: read-only credential/process riêng.
- Approver: identity xác thực; approval gắn action hash và TTL.

## 8. Benchmark và ablation

Ba phương pháp chạy trên cùng baseline, fault definition, telemetry, operational runbook, quyền hạn và reset procedure; thứ tự scenario được randomize và lưu seed. Người thực nghiệm/operator không được thấy scenario ID được inject hoặc expected root cause/action trong ground truth cho đến khi dự đoán RCA và action đã được ghi, khóa thời gian và thu raw evidence. Manual/Ansible/AI chỉ được dùng operational runbook/capability allowlist đã review; ground truth chỉ dùng hậu kiểm. Reset script chỉ chạy sau khi raw trial đã được freeze, ghi log riêng và không được tính là remediation của baseline nào:

**Live blinding is not yet established.** The source alert rules no longer attach
scenario IDs to alerts or expose a fault-marker metric; drill ownership is kept
in a root-only host file and alert checks use symptoms only. Before a blind
comparative run, the operator role must be prevented from reading that host file,
scenario-specific drill command lines, ground-truth files, or controller logs.
The experiment controller must retain the private run-to-scenario mapping for
post-decision scoring. Do not claim a blinded run until role separation has been
verified in the deployed environment.

1. Manual operator theo SOP cố định.
2. Ansible rule-based playbook viết trước.
3. AI Agent đầy đủ Observer→Diagnosis→Planner→Gate→Execution→Verifier.

Với staging Moodle hiện tại, benchmark giữ đủ 15 × 3 × n ô nhưng không đồng
nhất "có ô benchmark" với "được phép thực thi". Safe Executor chỉ có 5
scenario đã duyệt; 9 scenario Moodle mới ở nhánh AI phải DENY và handoff cho
Manual operator, không tính recovery tự động. SEC-01 là HUMAN_ONLY: cả nhánh
AI và Ansible DENY/handoff, chỉ Manual operator được thay đổi Security Group.
Các lượt DENY vẫn cần raw evidence, timestamp, kiểm chứng độc lập và attribution
recovery của người vận hành; smoke test/Shadow không thay được empirical run.

### Ablation

- **No Safety Gate:** shadow mode; ghi lại hành động “would execute”, destructive action vẫn bị sandbox intercept.
- **No Independent Verifier:** ghi `counterfactual_verdict` từ health-only; không được sửa canonical incident thành `RESOLVED`.

### Metrics

RCA accuracy, recovery success, false recovery rate, detection time, remediation time, dangerous-action block rate, rollback success, số thao tác, số lần gọi LLM, runtime và AWS cost.

Timestamp bắt buộc: `t_inject`, `t_detect`, `t_incident`, `t_plan`, `t_gate`, `t_execute_start`, `t_execute_end`, `t_verify`, `t_resolved`.

## 9. Critical path

```text
Baseline green
→ Moodle
→ Unified model/state machine
→ Evidence Store + adapters
→ Agents + Orchestrator
→ Safety Gate + Execution
→ Independent Verifier
→ 15 scenarios
→ Benchmark + ablation
→ RQ analysis
→ Thesis/demo
```

## 10. Top risks và mitigation

| Risk | Mitigation |
|---|---|
| Dependency không reproducible | Python 3.11 image, `agent_src/requirements.in` direct dependencies, universal Python 3.11 hash-locked `agent_src/requirements.txt`; Docker và CI cài bằng `--require-hashes`. Regenerate lock bằng `uv pip compile --generate-hashes --python-version 3.11 --universal --output-file agent_src/requirements.txt agent_src/requirements.in`, rồi xác minh clean install/CI. |
| Moodle trễ | Local Compose trước, bỏ plugin/theme, reuse PostgreSQL/monitoring |
| Mất working-tree change | Backup branch, commit nhỏ, không reset destructive |
| Agent chạy action nguy hiểm | Fail-closed gate, allowlist, kill switch, least privilege |
| False recovery | Verifier độc lập, contract probes, stability window |
| State nhiễm giữa experiment | Reset script, checksum, run ID, randomized order |
| LLM rate limit/biến động | Deterministic fallback, temperature 0, bounded retry, cached fixtures |
| Secret/PII leakage | Redact trước prompt/store; negative tests |
| AWS cost/host thiếu disk | Budget alert, schedule stop, disk preflight |
| Public exposure | Restrict CIDR/SG, auth, đổi Grafana default password |

## 11. Scope-cutting strategy

1. Defer OpenStack, EKS và GitHub auto-discovery.
2. Bỏ custom UI; dùng Grafana/API/Telegram.
3. Giữ RAG retrieval có provenance, bỏ dynamic learning nâng cao.
4. ERPNext là extension tùy chọn sau Moodle acceptance; nếu làm thì giới hạn ở ba scenario đại diện.
5. Giảm repetition từ 5 xuống 3 nếu bắt buộc và ghi limitation.
6. Planner có thể deterministic hoàn toàn nếu LLM không ổn định.

Không được cắt Moodle, 15 scenario Moodle, Safety Gate, Independent Verifier, ba baseline, hai ablation hoặc quy tắc chỉ Verifier được `RESOLVED`.

## 12. Backlog ưu tiên

### P0

Baseline evidence; working-tree cleanup; Moodle; monitoring; unified schemas; state machine; Evidence Store; adapters; five agent contracts; Orchestrator; Safety Gate; approval; audit; rollback; Verifier; 15 scenarios; benchmark; report. The dependency lock is now present at `agent_src/requirements.txt`; evidence-derived read-only Moodle investigation is available under `AIOPS_UNIFIED_CORE_MODE=investigate`. The local code path is tested, while its AWS runtime behavior and empirical acceptance remain unverified. See `docs/AI_ENGINEER_STATUS.md` for evidence.

### P1

ERPNext three-scenario setup (optional; defer until Moodle empirical acceptance); richer Telegram approval/security (implemented: webhook secret plus numeric user allowlist); nightly experiment workflow (not automated until a fault-injection schedule is explicitly approved); cost dashboard (generator implemented in `automation/benchmark-cost-dashboard.py`, which shows only acceptance-ready empirical main rows and otherwise emits an incomplete status without metric totals).

### P2

OpenStack; EKS fixtures; GitHub tool discovery; auto-generated runbook; custom UI; parallel agent execution.

## 13. Definition of Done

### Demo tuần 4

- Moodle monitored trên AWS.
- DB-01 có injector/reset.
- Một alert batch tạo đúng một Incident.
- Incident có ≥3 Evidence có source/time/hash.
- Typed Action dẫn evidence và qua Gate.
- Executor chỉ chạy allowlist.
- Verifier kiểm tra health + allowed + forbidden + related + stability.
- Audit đủ stage và Telegram/API report có timeline.
- Ba run pass, MTTD ≤60s, recovery ≤10m, forbidden execution = 0.

### Khóa luận tuần 14

- CI green, P0 đóng, 15 Moodle scenario và reset pass.
- Main benchmark và hai ablation có raw data đầy đủ.
- ERPNext three-scenario generalization is optional and starts only after Moodle acceptance; it is not a thesis DoD gate.
- Không có forbidden automatic execution.
- Mọi bảng/figure truy nguyên được về run ID, commit SHA, image digest.
- Báo cáo trả lời RQ1/RQ2, có limitations và failure analysis.
- Slide, demo script, video backup, manifest và checksum hoàn chỉnh.

## 14. GitHub Epics và Issue đề xuất

- **E0 Baseline:** inventory/gap, working-tree stabilization, dependency lock, CI baseline, scope ADR.
- **E1 Environments:** Moodle runtime, PostgreSQL/exporter, synthetic transaction, reset/checksum; ERPNext is an optional extension.
- **E2 Models:** Resource, Evidence, Incident state machine, Typed Action, compatibility.
- **E3 Evidence:** Store, collectors, dependency graph, RAG provenance, redaction.
- **E4 Agents:** Observer, Diagnosis, Planner, Orchestrator, structured contract.
- **E5 Safety/Execution:** catalog, policy matrix, RBAC, approval, Docker adapter, Ansible adapter, audit, rollback.
- **E6 Verification:** contract schema, probe runner, verifier isolation, `RESOLVED` guard, false-recovery suite.
- **E7 Evaluation:** ground truth, 5 fault groups, manual protocol, Ansible baseline, AI harness, metrics, ablations.
- **E8 Reporting:** timeline API, Grafana panels, Telegram report, demo runbook.
- **E9 Thesis:** dataset freeze, statistics, RQ chapters, figures, slides, rehearsal.

Mỗi issue phải có owner, reviewer, dependency, acceptance criteria, test evidence và effort; issue lớn hơn 2 person-day phải tách nhỏ.

## 15. Lịch review hằng tuần

| Tuần | Review/demo |
|---|---|
| 1 | Audit, baseline, scope ADR |
| 2 | Moodle healthy, monitoring, DB-01 ground truth |
| 3 | Dry-run vertical pipeline |
| 4 | Live MVP DB stopped → verified recovery |
| 5 | Schema/state transition |
| 6 | Evidence provenance và adapter idempotency |
| 7 | Observer/Diagnosis golden cases |
| 8 | Planner/Orchestrator crash-resume |
| 9 | Safety matrix và approval negative tests |
| 10 | Safe execution, audit, rollback |
| 11 | Health-green nhưng contract-broken demo |
| 12 | 15 Moodle scenario và benchmark progress |
| 13 | Hai Moodle ablation và phân tích; ERPNext tùy chọn sau Moodle acceptance |
| 14 | Full defense rehearsal và contingency drill |

## 16. Artifact phục vụ báo cáo khoa học

Architecture/threat model; requirement traceability; schema/interface; action catalog/policy matrix; 15 ground truth; injector/reset/checksum; manual/Ansible/AI protocols; structured traces; policy/audit logs; contract probe results; raw CSV/JSONL; [benchmark data dictionary](BENCHMARK_DATA_DICTIONARY.md); metric formulas; model/prompt/image versions; LLM/AWS cost log; statistical script; confusion matrix; failure cases; both Moodle ablations; optional ERPNext result after Moodle acceptance; reproduction guide; demo runbook; slide; video backup.

## 17. Mapping mục tiêu và RQ

| Mục tiêu | Sprint | Deliverable | Metric |
|---|---|---|---|
| Unified framework/model | S1–S3 | Architecture và 4 schemas | Schema/transition pass rate |
| Context, knowledge, evidence | S3–S4 | Evidence Store, graph, RAG provenance | Provenance coverage, retrieval quality |
| Năm agent và coordination | S2–S4 | Agent modules, contract, Orchestrator | Stage-order, idempotency, RCA accuracy |
| Safe execution | S5 | Catalog, Gate, RBAC, approval, audit, rollback | Block rate, forbidden execution, rollback success |
| Independent verification và Moodle evaluation | S6–S7 | Verifier, contracts, 225-run benchmark, hai ablation | False recovery, recovery, timing, cost; ERPNext generalization là extension tùy chọn |

| RQ | Experiment | Baseline/ablation | Dữ liệu cần thu |
|---|---|---|---|
| RQ1 | 15 Moodle scenarios × Manual/Ansible/AI | No Safety Gate shadow mode | Action, decision, permission, blast radius, unsafe count, block count, rollback, time |
| RQ2 | Contract-challenge scenarios | Independent Verifier vs health-only | Health, allowed/forbidden/related verdict, stability, false recovery, unintended impact |
