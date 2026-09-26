# Sprint 1–2 implementation and acceptance report

Audit date: 2026-09-24  
Repository: `feature/sprint1-moodle-infrastructure` at `c453b9c`  
Scope: repository, offline configuration, unit tests, and deterministic replay. No AWS resource was changed.

## Acceptance status

| Week | Status | Verified work | Acceptance still missing |
|---|---|---|---|
| 1 — Moodle foundation | PARTIAL | Terraform configuration validates; Moodle/PostgreSQL Compose validates; Moodle and synthetic transaction source exists | Deployed Moodle login, 2-hour ≥99% synthetic soak, backup/restore evidence, and clean staging baseline |
| 2 — Monitoring and fault foundation | PARTIAL | Prometheus/Alertmanager/exporter configuration and five valid Moodle ground-truth fixtures exist; injector/reset scripts pass Bash syntax | Live scrape/alert delivery rate and one inject/reset trial per scenario |
| 3 — Agent vertical slice | PARTIAL | Normalized-event validation, deterministic replay, evidence/audit records, dedup within one process, exact scenario action/target allowlist, dry-run | Alertmanager webhook integration, durable incident state/resume, and replay across process restarts |
| 4 — Safe execution and verification | BLOCKED | Signed action-bound approval validation is implemented; replay cannot resolve; verifier rejects health-only or <120-second evidence | Live contract probe collector, independent live stability observations, approval key/approver configuration, and 3 passing AWS runs per scenario |

**Sprint 1: PARTIAL. Sprint 2: PARTIAL, with Week 4 live acceptance BLOCKED. Not READY FOR DEMO.** The repository has useful deployment and test scaffolding, but local configuration tests do not establish deployed Moodle or live recovery.

## Repository implementation inventory

| Area | Implementation | Current evidence |
|---|---|---|
| Terraform | VPC, isolated app/data subnets, ALB, private Moodle EC2, private PostgreSQL RDS, EFS, scoped security groups | `terraform fmt -check`, `validate`, and 19 Terraform tests pass; no AWS plan was generated in this audit |
| Host configuration | Ansible bootstrap, runtime preparation, Moodle monitoring deployment | Playbooks and inventory exist; Ansible is unavailable in this environment, so no connectivity or idempotency run |
| Moodle release | Moodle image, Postgres TLS config, persistent EFS data, secret files, health and synthetic endpoints | Compose config passes; image build and runtime were not run because Docker Engine is unavailable |
| Monitoring | Prometheus rules, Alertmanager, blackbox, Node Exporter, cAdvisor, PostgreSQL exporter, Grafana | Static files and playbook exist; no live scrape or alert delivery evidence |
| Scenarios | DB-01, RES-01, NET-01, CON-01, SEC-02 fixtures plus scoped injector/reset scripts | Ground-truth tests pass; shell syntax passes; no fault was injected |
| Agent pipeline | `agent_src/core/moodle_pipeline.py` and `automation/moodle-pipeline-replay.py` | Five replay incidents reach `VERIFIED_DRY_RUN`; zero forbidden actions; zero incidents resolve in replay |
| Deployment and demo | `docs/MOODLE_DEPLOYMENT_GUIDE.md`, `docs/AWS_INFRASTRUCTURE_DEPLOYMENT_GUIDE.md`, `docs/MOODLE_SPRINT_RUNBOOK.md` | Instructions are available; live commands were not run |

## Changes in this working tree

- `agent_src/core/moodle_pipeline.py`: validates normalized alerts and accepts only firing health failures for remediation; validates signed, action-bound approvals; keeps the verifier on a read-only adapter; only exposes `RESOLVED` when live health, exact contract probes, and at least 120 seconds of passing observations are supplied. Replay returns `VERIFIED_DRY_RUN`.
- `agent_src/tests/test_moodle_pipeline.py`: covers normalized payloads, rejected resolved alerts, signed approval binding, and verifier behavior for health-only and short stability evidence.
- `automation/moodle-synthetic-transaction.py`: repairs Python syntax and the `--username` interface used by the remote scripts.
- `automation/stage-moodle-release.sh`: uses PostgreSQL `psql` environment interpolation for the Moodle role password and restricts the temporary WSL database tunnel to the WSL interface.
- `automation/moodle-pipeline-replay.py` and `.github/workflows/ci.yml`: calculate the forbidden-action count from the replay and require replay incidents to remain un-resolved dry-runs.
- Existing changes to `AGENTS.md`, the sprint runbook, Terraform provider selection, CI, and the scenario automation were retained and validated where local tools allowed.

No commit, push, apply, deploy, fault injection, or data restore was performed. `terraform/staging-live.tfplan` was already untracked; it was left untouched and was not used as evidence.

## Offline test results

| Check | Result |
|---|---|
| `PYTHONPATH=agent_src python -m pytest -q agent_src/tests` | PASS — 99 passed; 53 dependency/deprecation/cache warnings |
| Ruff critical rules | PASS |
| Python compile: pipeline, pipeline tests, synthetic transaction, replay | PASS |
| Bash `-n`: Moodle baseline, injector/reset/trial, soak, live drill, release staging | PASS |
| Terraform fmt and validate | PASS |
| Terraform tests | PASS — 19 passed, 0 failed |
| Docker Compose config: Moodle, staging, production | PASS — configuration only |
| Deterministic Moodle replay | PASS — 5 scenarios; 0 forbidden executions; all 5 `VERIFIED_DRY_RUN`; 0 `RESOLVED`; report at `terraform/.artifacts/moodle-pipeline-replay/20260924-offline-acceptance/report.json` |

The current Python is 3.14.4 while the agent image targets Python 3.11; the installed runtime differs from CI. Runtime-specific behavior still needs CI or Python 3.11 verification.

## Live evidence and blockers

- AWS CLI returned `NoCredentials`; AWS account, region, resources, current plan drift, and cost therefore could not be verified. Terraform apply and staging fault injection were not run.
- Docker Compose configuration parses, but Docker Engine is unavailable. Moodle and agent images, container health, database connectivity, and restart behavior remain unverified.
- Ansible is not installed; host reachability, syntax, idempotency, and deployment remain unverified.
- No current artifacts prove the 2-hour soak, backup/restore, monitoring delivery rate, fault trials, MTTD, recovery time, or three successful runs per scenario.
- The live read-only adapter currently verifies only the baseline command. It does not yet return measured allowed, forbidden, and related communication probes or timestamped stability observations. The verifier therefore fails closed and will not report `RESOLVED` for a live run from health-only output.
- The replay pipeline is not yet wired into the running Alertmanager `/webhook` to Celery path. Current live-drill script creates a normalized event locally after observing a Prometheus alert; this is not proof of the full Alertmanager-to-agent delivery path.
- Incident deduplication is in process memory. Durable state and crash-safe resume are not implemented.
- CI/CD still has stale `demo-web` references in the staging workflow; inspect and resolve before claiming application deployment through CI.

Historical runbook figures are labeled as reported external evidence and were not used as current acceptance results. Prior workspace notes also describe runtime preparation/EFS checks while Moodle login and live recovery remained unverified; treat them as historical only.

## Scenario execution report

| Scenario | Fixture | Injector/reset | Offline replay | Live inject/reset | MTTD / recovery / consecutive passes |
|---|---|---|---|---|---|
| DB-01 | Present | Present | PASS | NOT RUN | Not measured |
| RES-01 | Present | Present | PASS | NOT RUN | Not measured |
| NET-01 | Present | Present | PASS | NOT RUN | Not measured |
| CON-01 | Present | Present | PASS | NOT RUN | Not measured |
| SEC-02 | Present | Present | PASS | NOT RUN | Not measured |

Replay verifies schema and pipeline contracts only. It does not inject a fault, deliver an Alertmanager webhook, execute a real reset, or establish recovery.

## Follow-up: AI Engineer Sprint 1–4 offline deliverables

The AI-side contracts were subsequently expanded through Weeks 1–8. The Moodle ground-truth set now contains 15 scenario definitions, and `automation/ai-engineer-sprint1-4-replay.py` passes all 15 through the Observer → Diagnosis → Planner → Gate → Execution → Verification contract sequence. This replay is fixture-only: it performs zero mutations, does not invoke the live Moodle pipeline or Alertmanager/Celery path, and cannot mark incidents resolved. Unified Pydantic contracts, transition guards, queryable append-only SQLite evidence, dependency edges, RAG provenance, duplicate grouping, and checkpoint/retry/escalation are detailed in `docs/AI_ENGINEER_SPRINT1-4_REPORT.md`. The ten expanded fixtures still need infrastructure-owned injectors/reset scripts and live trials.

## Reproduction and demo entry points

From the repository root:

```bash
PYTHONPATH=agent_src python -m pytest -q agent_src/tests
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform test
python -m py_compile automation/moodle-synthetic-transaction.py automation/moodle-pipeline-replay.py
python automation/moodle-pipeline-replay.py --output-dir terraform/.artifacts/moodle-pipeline-replay/manual
docker compose --env-file release/moodle/moodle-runtime.env.example -f release/moodle/docker-compose.yml config
```

Use `docs/MOODLE_SPRINT_RUNBOOK.md` for the staged deployment and demo sequence. Before any AWS plan/apply or live fault, confirm the account, region, environment, resource changes, and estimated cost, then obtain approval for the risky or billable step. Run `docs/WEEK1_BASELINE.md` first; run the five smoke trials and three-run campaign only after the live baseline passes.

## Final verdict

**PARTIALLY COMPLETE.** Offline quality gates pass and several safety gaps were closed. Sprint acceptance remains incomplete because AWS credentials, Docker Engine, and Ansible are unavailable here, and the current live verifier intentionally refuses to resolve incidents without measured communication-contract and stability evidence.

## Reassessment — 2026-09-24 UTC, current checkout

This section supersedes the older local test counts and the older `NoCredentials` observation above. AWS profile `default` authenticated to account `583845872420` in `ap-southeast-1`. Docker Engine was unavailable and `ansible-playbook` was absent. The local Terraform `default` workspace listed no resources; the intended live state/backend and a current plan/cost review were not established. No live mutation was attempted. Full command record and limitations: `terraform/.artifacts/sprint12-qa/QA_REPORT.md` (ignored artifact).

| Sprint acceptance criterion | Status | Current evidence and limit |
|---|---|---|
| W1 Moodle/PostgreSQL/EFS/secret, health and synthetic configuration | PASS (static) | Terraform validate/test and Moodle Compose config passed; `release/moodle/` and `moodle/` inspected. |
| W1 deployed Moodle login and DB, synthetic read/write ≥99% for 2 hours | NOT RUN | Docker Engine absent; no current application baseline or soak. |
| W1 backup/restore and clean reset once | NOT RUN | `automation/moodle-rds-restore-drill.sh` exists; no live restore evidence. |
| W1 staging inventory/topology complete | BLOCKED | Terraform configuration present; local workspace has no listed resources and live inventory was not established. |
| W2 DB-01 plus four representative ground-truth fixtures valid | PASS (offline) | 31 related tests pass; five-scenario replay in `terraform/.artifacts/sprint12-qa/db01-replay/report.json`. |
| W2 five scoped injector/reset implementations | PASS (static) | `automation/moodle-fault-inject.sh` and `automation/moodle-fault-reset.sh` cover DB-01/RES-01/NET-01/CON-01/SEC-02; Bash syntax passes. |
| W2 monitoring configuration present | PASS (static) | Compose config and 112 agent tests pass; Prometheus/Alertmanager/exporter configuration present. |
| W2 Prometheus alert rule parser validation | NOT RUN | `promtool` was not available; Compose config and Python tests do not parse deployed Prometheus rules. |
| W2 scrape/alert delivery ≥99% and one live trial per scenario | NOT RUN | No live monitoring baseline or controlled fault. |
| W3 five-scenario alert → incident → evidence → diagnosis → plan → gate → execute → verify replay | PASS (offline replay) | All five `VERIFIED_DRY_RUN`, six evidence refs each, zero mutations and zero forbidden executions; audit/evidence JSONL in ignored replay directory. |
| W3 incident dedup, exact action allowlist and malformed action rejection | PASS (in-process test) | `test_moodle_pipeline.py` checks duplicate event → one incident/unchanged evidence and forbidden action denial. Persistence across restarts remains unimplemented. |
| W3 Alertmanager webhook → Celery → unified pipeline delivery | BLOCKED | No current live delivery evidence; fixture replay starts from normalized local alerts. |
| W4 independent verifier fail-closed contract | PASS (unit contract) | Related tests reject health-only, missing probes and short stability; replay `resolution_eligible=false`. |
| W4 five live scenarios × three passes, MTTD ≤60s, recovery ≤10m, 120s stability, forbidden execution = 0 | NOT RUN | Docker/Ansible/baseline and reviewed plan/cost gate unavailable. Prior runbook campaign is historical external evidence only. |
| W4 live `RESOLVED` only after health, communication probes and ≥120s stability | BLOCKED | Live probe collector and timestamped stability observations have not been demonstrated. |

QA gates: Ruff critical **PASS**; full pytest **PASS** (112 passed using an artifact `--basetemp`); Terraform fmt/validate/test **PASS** (19 Terraform tests); Compose config for Moodle/staging/production **PASS**; Python compile and Bash syntax **PASS**; five-scenario Moodle replay **PASS**; separate 15-fixture AI contract replay **PASS**. The first pytest invocation **FAIL** at setup (22 errors, Windows temp permission) and the first Moodle Compose invocation **FAIL** due to missing default local env file; corrected invocations and exact causes are in the QA report. These are configuration/offline results, not live acceptance.

**Sprint 1: PARTIAL.** Static foundation and fixtures are present; deployed login, soak, restore, monitoring delivery and live fault/reset acceptance remain unverified. **Sprint 2: PARTIAL.** Offline pipeline, dedup and safety contracts pass; Alertmanager-to-agent integration, durable incident resume, five live scenarios with three runs, measured communication probes and 120-second stable recovery remain unverified or blocked. No replay incident is marked `RESOLVED`.

### Read-only AWS inventory follow-up — 2026-09-24 12:02 UTC

Under the authenticated `default` profile in `ap-southeast-1`, fresh `describe-instances`, `describe-load-balancers`, `describe-db-instances` and `describe-file-systems` queries each returned an empty list. Local Terraform workspace `default` also listed no managed resources. **Sprint 1 live staging is therefore BLOCKED in this account/region at this timestamp**, rather than merely unverified. An untracked saved plan already existed; `.gitignore` now excludes all `*.tfplan` to prevent accidental staging of plan contents. The file was not opened, moved or applied. Account/region alone do not authorize resource creation: a fresh reviewed plan, cost estimate and approval are required. The ignored `terraform/.artifacts/sprint12-qa/QA_REPORT.md` records the commands and limits.

Static inspection also found that the Alertmanager receiver points to `/webhook` when the agent is enabled, and `tasks.py` routes explicitly labeled Moodle staging alerts through a shadow-only pipeline. The current alert rules lack a scenario label, while the shadow handler requires `scenario_id`; the synthetic alert is shared by DB-01, NET-01 and SEC-02. The live drill currently constructs a local normalized event after reading a Prometheus alert. Thus **end-to-end Alertmanager → Celery → scenario-specific diagnosis is BLOCKED**, and the shadow pipeline must not be reported as live remediation. The live verifier adapter currently returns baseline health only, so the communication probes and 120-second stability requirement still fail closed.

Final offline rerun at 12:03 UTC: **PASS** — 112 Python tests, 19 Terraform tests, Ruff critical, Terraform fmt/validate, Compose config, Python compile, Bash syntax, five Moodle dry-run replays and 15 AI fixture replays. DB-01 rerun evidence: `terraform/.artifacts/sprint12-qa/db01-rerun/report.json`; complete command/results record: `terraform/.artifacts/sprint12-qa/QA_REPORT.md`. Sprint verdicts remain **Sprint 1 PARTIAL** and **Sprint 2 PARTIAL** because live acceptance has not run.

### AWS deployment plan — 2026-09-24 12:14 UTC

At the user's request, a fresh saved Terraform plan was generated under the authenticated `default` profile in `ap-southeast-1`. It proposes **68 creates, zero updates and zero deletes**. The saved plan has **not been approved or applied**. The ignored review artifact is `terraform/.artifacts/sprint12-live-20260924T121420Z.REVIEW.md`; its companion `.tfplan`, JSON and log may contain sensitive values and must remain outside Git.

AWS Price List rates give a fixed-subtotal estimate of approximately **USD 203/month** for continuous operation, before NAT traffic, ALB LCU, EFS, backups, logs and tax. The account's AWS Budget threshold is **USD 20/month**. The plan uses HTTP port 80 because no issued ACM certificate was found; it does not satisfy Moodle login/data acceptance over HTTPS. WSL does have Ansible 2.16.3, correcting the earlier Windows-only availability check. Docker Desktop is not running locally; remote Docker and all live acceptance remain unverified. **Sprint 1 and Sprint 2 remain PARTIAL; apply awaits explicit review of this exact plan, cost and HTTP-only scope.**

### Approved plan pre-apply check — 2026-09-24 12:23 UTC

The user approved the saved plan with SHA-256 `43EF0EB68CE23D8AB9D3C47FF7BD905859660BCF41CD81401BE3C201C2936A20`. Immediately before apply, account, region, state and hash matched, but the current operator public `/32` **did not match** the SSH allowlist in that plan. Apply stopped before any AWS mutation. A replacement plan, SHA-256 `3800F8F8B454095252625B4A77D8D73B864DA1E32C6239461D3ADF5394D6852F`, changes only `my_ip_cidr` and the affected security group values; it still proposes **68 creates, 0 updates, 0 deletes** and has the same cost/scope. It is **awaiting approval**; see ignored `terraform/.artifacts/sprint12-live-20260924T122354Z.REVIEW.md`. No Terraform apply, deployment or live fault occurred. Sprint 1 and Sprint 2 remain **PARTIAL**.
