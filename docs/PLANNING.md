# Kế hoạch triển khai khóa luận 14 tuần

## 1. Mục tiêu và nguyên tắc

### Product goal

Xây dựng framework AI Agent an toàn và có thể xác minh cho vận hành hạ tầng trên AWS EC2/Linux/Docker Compose. Framework phải quan sát, chẩn đoán, lập kế hoạch, thực thi có kiểm soát và chỉ kết luận `RESOLVED` khi Independent Verifier xác nhận cả health check lẫn communication contract.

### Research questions

- **RQ1:** Safety Gate dựa trên bằng chứng, quyền, blast radius và rollback readiness có làm giảm hành động không an toàn/không phù hợp so với Manual, Ansible rule-based và AI Agent không có gate hay không?
- **RQ2:** Independent Verifier dựa trên communication contract có làm giảm false recovery và phát hiện tác động ngoài dự kiến tốt hơn health-check-only hay không?

### Phạm vi

**In-scope:** AWS EC2, Linux, Docker Compose, Terraform, Ansible, Prometheus, Alertmanager, Grafana, Moodle, ERPNext, PostgreSQL/MariaDB/Redis, AI Agent, Evidence Store, RAG, Safety Gate, approval, audit, rollback có điều kiện, Independent Verifier, fault injection và benchmark.

**Out-of-scope:** Kubernetes/EKS, OpenStack evaluation, Windows, multi-cloud, tự động sửa IAM/security group, thay đổi nghiệp vụ, xóa dữ liệu, drop/truncate database, rollback dữ liệu nghiệp vụ, custom incident UI và multi-agent fan-out phức tạp.

### Quy tắc scope

- Moodle là môi trường đánh giá chính; ERPNext chỉ dùng cho ba scenario generalization.
- Legacy EKS fixtures và OpenStack được quarantine/defer.
- Chỉ Independent Verifier được phép chuyển incident sang `RESOLVED`.
- Health check thành công một mình không đủ để kết luận phục hồi.
- Mọi feature chỉ được coi là Done khi có test hoặc execution evidence.

## 2. Current State Assessment

| Thành phần | Hiện trạng đã kiểm chứng | Tái sử dụng | Cần sửa/xây mới | Quyết định |
|---|---|---|---|---|
| AWS VPC, subnet, EIP, 3 EC2 | Có trong `terraform/`; `terraform validate` pass | VPC, role monitor/web/core, IMDSv2, encrypted gp3 | Đổi tên resource-neutral; giới hạn public ingress; bổ sung experiment tags | Refactor |
| Security group | Có nhưng mở Prometheus/Alertmanager/AI API ra Internet | SG-to-SG traffic | Restrict CIDR, không cho Agent tự sửa SG | Refactor P0 |
| Ansible bootstrap/monitoring | Có Docker, exporters, Prometheus, Alertmanager, Grafana và dashboard | Playbook và templates | Pin image/version, idempotency test, Moodle/ERPNext role | Keep + refactor |
| Release Compose | Có staging/production, health check, role-based deployment và rollback tag | Compose project, env files, health scripts | App map Moodle/ERPNext, environment isolation, policy-aware execution | Keep + refactor |
| AI ingress | FastAPI webhook, Redis/Celery, fingerprint/correlation/dedup đã có | Webhook, queue, retry, metrics | Gọi validation trước enqueue; loại raw sensitive data | Refactor P0 |
| Incident | Redis context và active-incident key có một phần | Dedup, TTL, Telegram report | Incident model, state machine, append-only event log | Refactor |
| Resource/Evidence/Action model | Event schema V2 đang được phát triển; action còn `{action, host}` | Fingerprint, event ID, entities | Unified models, provenance, permission, blast radius, rollback | Build |
| RAG/knowledge | ChromaDB và bốn runbook đang có | Runbook retrieval, incident memory | Evidence Store riêng, source/time/hash, dependency graph | Keep + refactor |
| Agents | `tasks.py` đang trộn diagnosis, notification, storage và scheduling | Deterministic diagnosis, Gemini fallback | Observer, Diagnosis, Planner, Execution, Verification và Orchestrator | Replace flow |
| Execution | Có diagnostic tools và release script; chưa có adapter an toàn | Docker/Ansible scripts | Action catalog, typed adapters, dry-run, idempotency, least privilege | Replace P0 |
| Safety | Có kiểm tra destructive text cho admin feedback/runbook | Một số forbidden markers | Safety Policy Engine, ALLOW/REQUIRE_APPROVAL/DENY, RBAC, audit | Build P0 |
| Verification | Prometheus checker mới kiểm tra metric/health; Alertmanager resolved có thể đánh dấu incident | Prometheus queries | Independent Verifier, contract probes, stability window, state authority | Replace P0 |
| Moodle | Không xuất hiện trong source/config hiện tại | PostgreSQL, monitoring, EC2 topology | Compose, seed, probes, synthetic transaction, runbooks | Build P0 |
| ERPNext | Không xuất hiện trong source/config hiện tại | Docker/monitoring/adapter interface | Minimal environment và 3 scenario đại diện | Build P1 |
| Ground truth | Chỉ có hai JSON nháp; thiếu initial state/trigger/signals/recovery/contract/rollback | Validator, fixture loader | 15 Moodle scenarios + ERPNext subset + reset/checksum | Expand |
| Tests | 85 agent tests + 4 backend tests; môi trường hiện tại không green | Schema, dedup, RAG, webhook tests | Lockfile, clean CI env, E2E, contract, fault tests | Fix P0 |
| Working tree | Có staged/unstaged schema, fixture; ChromaDB runtime bị track; nhiều docs bị xóa | Các thay đổi schema sau review | Tách commit, khôi phục tài liệu cần thiết, untrack runtime DB | Stabilize W1 |

### Baseline audit

- `ruff` critical rules: pass.
- `terraform fmt -check` và `terraform validate`: pass.
- Docker Compose staging/production `config -q`: pass; Docker daemon chưa chạy nên chưa có runtime evidence.
- Full agent test trong `.venv` hiện tại: không collect được vì thiếu `google-genai` và `chromadb`.
- Backend test: 4 fail do `httpx 0.28.1` không tương thích TestClient; CI yêu cầu `httpx<0.28`.
- Ground-truth test: 30 pass, 2 fail do regex coi khóa `no_ip_or_secret` là sensitive.
- Ansible chưa chạy trên máy audit vì chưa cài Ansible.

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
9. ERPNext với 3 scenario generalization.
10. Manual/Ansible/AI benchmark, hai ablation, raw data và phân tích RQ1/RQ2.
11. Báo cáo, slide, demo runbook, video backup và reproduction guide.

### Success criteria

- 15/15 Moodle scenario có ground truth, injector, reset và schema hợp lệ.
- Main benchmark: `15 × 3 phương pháp × 5 repetitions = 225 runs`.
- Safety ablation và verifier ablation: tối thiểu 5 scenario đại diện × 2 cấu hình × 5 repetitions mỗi nhóm.
- RCA top-1 accuracy ≥ 70%; recovery success ≥ 80%.
- Dangerous-action block rate ≥ 95%; forbidden automatic execution = 0.
- False recovery rate ≤ 5% và giảm ít nhất 50% so với health-only.
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
| 13 | ERPNext generalization và hai ablation | O: adapter mapping không đổi core schema; safety/no-gate và verifier/health-only analysis; R: ERP | O: ERPNext Compose; MariaDB/Redis/Nginx probes; 3 scenario; safe shadow ablation; R: dataset | Stable core + Moodle data | ERPNext result và ablation data | 3 ERP scenario pass; ≥50 safety-ablation + ≥50 verifier-ablation runs; không sửa core schema | ERPNext nặng → DB stopped, Redis dependency, Nginx/config; single-node Compose | AI 6 + Infra 4 = 10 PD |
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

Communication contract tối thiểu:

- Allowed: client→Moodle; Moodle→database; monitor→exporter; Alertmanager→Agent.
- Forbidden: Internet→PostgreSQL/Redis; Planner→Executor bypass; app→metadata/admin endpoint.
- Related: monitoring, queue và dịch vụ không thuộc fault vẫn hoạt động.

ERPNext dùng ba scenario tương ứng: MariaDB stopped, Redis dependency stopped và Nginx/configuration drift.

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

Ba phương pháp chạy cùng snapshot, alert, runbook, seed và reset procedure:

1. Manual operator theo SOP cố định.
2. Ansible rule-based playbook viết trước.
3. AI Agent đầy đủ Observer→Diagnosis→Planner→Gate→Execution→Verifier.

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
| Dependency không reproducible | Python 3.11 image, lockfile/hash, CI clean install |
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
4. ERPNext còn ba scenario đại diện.
5. Giảm repetition từ 5 xuống 3 nếu bắt buộc và ghi limitation.
6. Planner có thể deterministic hoàn toàn nếu LLM không ổn định.

Không được cắt Moodle, 15 scenario Moodle, Safety Gate, Independent Verifier, ba baseline, hai ablation hoặc quy tắc chỉ Verifier được `RESOLVED`.

## 12. Backlog ưu tiên

### P0

Baseline/lockfile; working-tree cleanup; Moodle; monitoring; unified schemas; state machine; Evidence Store; adapters; five agent contracts; Orchestrator; Safety Gate; approval; audit; rollback; Verifier; 15 scenarios; benchmark; report.

### P1

ERPNext three-scenario setup; dependency graph visualization; RAG provenance; cost dashboard; richer Telegram approval; nightly experiment workflow.

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
- ERPNext three-scenario generalization pass.
- Không có forbidden automatic execution.
- Mọi bảng/figure truy nguyên được về run ID, commit SHA, image digest.
- Báo cáo trả lời RQ1/RQ2, có limitations và failure analysis.
- Slide, demo script, video backup, manifest và checksum hoàn chỉnh.

## 14. GitHub Epics và Issue đề xuất

- **E0 Baseline:** inventory/gap, working-tree stabilization, dependency lock, CI baseline, scope ADR.
- **E1 Environments:** Moodle Compose, PostgreSQL/exporter, synthetic transaction, reset/checksum, ERPNext minimal.
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
| 13 | ERPNext và hai ablation |
| 14 | Full defense rehearsal và contingency drill |

## 16. Artifact phục vụ báo cáo khoa học

Architecture/threat model; requirement traceability; schema/interface; action catalog/policy matrix; 15 ground truth; injector/reset/checksum; manual/Ansible/AI protocols; structured traces; policy/audit logs; contract probe results; raw CSV/JSONL; data dictionary; metric formulas; model/prompt/image versions; LLM/AWS cost log; statistical script; confusion matrix; failure cases; ablation; ERPNext result; reproduction guide; demo runbook; slide; video backup.

## 17. Mapping mục tiêu và RQ

| Mục tiêu | Sprint | Deliverable | Metric |
|---|---|---|---|
| Unified framework/model | S1–S3 | Architecture và 4 schemas | Schema/transition pass rate |
| Context, knowledge, evidence | S3–S4 | Evidence Store, graph, RAG provenance | Provenance coverage, retrieval quality |
| Năm agent và coordination | S2–S4 | Agent modules, contract, Orchestrator | Stage-order, idempotency, RCA accuracy |
| Safe execution | S5 | Catalog, Gate, RBAC, approval, audit, rollback | Block rate, forbidden execution, rollback success |
| Independent verification và evaluation | S6–S7 | Verifier, contracts, benchmark, ablation | False recovery, recovery, timing, cost, generalization |

| RQ | Experiment | Baseline/ablation | Dữ liệu cần thu |
|---|---|---|---|
| RQ1 | 15 Moodle scenarios × Manual/Ansible/AI | No Safety Gate shadow mode | Action, decision, permission, blast radius, unsafe count, block count, rollback, time |
| RQ2 | Contract-challenge scenarios | Independent Verifier vs health-only | Health, allowed/forbidden/related verdict, stability, false recovery, unintended impact |
