# Sprint 3–5 Completion Report

Status: **Sprint 4 and Sprint 5 acceptance passed locally and on the pushed feature branch. Sprint 5's approved CON-01 live rollback passed; PR review/merge remains pending.**
Last updated: 2026-09-30.

This is the combined record for Sprint 3, Sprint 4, and Sprint 5 stage outcomes. Runtime
evidence under `terraform/.artifacts/` is local, ignored, and must not be
committed. This work does not authorize autonomous execution or production
changes.

## Sprint 4 — Agent and Orchestrator

| Stage | Scope | Result | Evidence / remaining gate |
|---|---|---|---|
| S4.0 — Entry gate and code audit | Reconcile existing AI implementation and Sprint 3 handoff | Complete: Observer/Diagnosis/Planner/CheckpointOrchestrator and offline 15-fixture replay existed; audit found shadow ground-truth leakage and missing runtime collectors, both addressed in this work | Regressions are covered by `test_moodle_alert_integration.py`, `test_shadow_pipeline.py`, and `test_evidence_collectors.py`; remote CI/merge remains pending |
| S4.1 — Observer | Alert validation, grouping, duplicate suppression, evidence requests | Passed offline contract: 100 identical fingerprint alerts group into one incident (99% incident-count compression); unknown resource mapping fails closed | `agent_src/tests/test_unified_agents.py::test_observer_compresses_duplicate_burst_to_one_incident`; unknown-resource negative test; full suite below |
| S4.2 — Evidence collection | Read-only metric/log/config/topology evidence, UTC timestamps, sanitization | Passed code and contract tests: fixed allowlisted PromQL only, 120s freshness bound, UTC timestamps, common-secret redaction, bounded log excerpt, hashed packaged config/topology provenance, append-only evidence | `agent_src/core/evidence_collectors.py`; `agent_src/tests/test_evidence_collectors.py`; live Prometheus connectivity was not exercised in this run |
| S4.3 — Diagnosis | Evidence-linked ranked hypothesis and fail-closed missing-evidence behavior | Passed offline contracts: hypothesis cites evidence refs; missing/ambiguous evidence stays not-ready; the live shadow route deliberately does not infer RCA from ground truth or incomplete telemetry | `agent_src/core/agents.py`; `agent_src/tests/test_unified_agents.py`; shadow regression tests; runtime reports `awaiting_evidence` if collector coverage is incomplete |
| S4.4 — Typed Planner | Capability/catalog-bound typed plan; no arbitrary commands | Passed fixture contract: all 15 plans are typed, bound to declared targets/evidence, and dry-run only; malformed/not-ready plans fail closed | `agent_src/core/agents.py`; `agent_src/tests/test_unified_agents.py`; 15-fixture replay report |
| S4.5 — Orchestrator | Ordered stages, durable checkpoints, bounded retries, idempotent resume | Passed: 15/15 follow `observe → triage → diagnose → plan → gate_request → execute → verify`; checkpoint resume creates no duplicate evidence/audit records; malformed planner output is rejected | `agent_src/tests/test_checkpoint_orchestrator.py`; `terraform/.artifacts/sprint4-acceptance-20260929/report.json` |
| S4.6 — Acceptance and handoff | Full quality gates, 15-fixture replay, status and limitations | Local acceptance passed: 345 tests, critical Ruff, compile, Agent image build, 15/15 replay, 0 live mutations. No runtime rollout or remote CI was performed | Exact outcomes and remaining operational gate are in the final acceptance record below |

## Sprint 3 — Consolidated stage outcomes

| Stage | Scope | Result | Evidence |
|---|---|---|---|
| S3.0 — Freeze baseline | Pin starting repository/runtime boundary | Passed | `sprint3-baseline-20260927`; baseline details retained in Git history |
| S3.1 — Resource/capability contract | Align 15 scenarios, resources, capabilities, and action catalog | Passed | Contract validation tests; `docs/SPRINT3_S3_1_CONTRACT.md` |
| S3.2 — Shadow rollout and live-slice preparation | Deploy immutable Agent image in shadow; keep executor separately gated | Passed for shadow boundary | Runtime image/mode and kill-switch state are recorded in the consolidated acceptance report and local evidence artifacts |
| S3.3 — Safe action adapter | Signed, scoped staging reset path for five approved cases | Passed for five-case staging slice | Five passed drill records under ignored `terraform/.artifacts/moodle-safe-executor/`; executor off after each drill |
| S3.4 — Scenario integration | Bind all 15 offline scenarios; exercise five exact live allowlisted scenarios | Passed for 15 offline + five live scenarios; latest drill records and all 20 verifier probes passed | Per-scenario records below; replay/drill artifacts are under ignored `terraform/.artifacts/`; technical mapping remains in `docs/SPRINT3_S3_4_SCENARIO_INTEGRATION.md` |
| S3.5 — Acceptance and handoff | Final read-only checks, drift plan, handoff | Local acceptance passed; formal closure pending CI/review on handoff branch | Point-in-time status, limits, and closeout gate are consolidated in this report; runtime evidence remains in ignored `terraform/.artifacts/` |

## S3.4 live drill evidence

Latest S3.4 staging evidence (2026-09-29):

| Scenario | Drill record under `terraform/.artifacts/moodle-safe-executor/` | Verifier |
|---|---|---|
| DB-01 | `DB-01-20260929T102751Z.json` | Passed |
| RES-01 | `RES-01-20260929T103144Z.json` | Passed |
| NET-01 | `NET-01-20260929T103419Z.json` | Passed |
| CON-01 | `CON-01-20260929T142136Z.json` | Passed |
| SEC-02 | `SEC-02-20260929T142616Z.json` | Passed |

All five records include successful scenario binding, alert resolution, reset,
post-reset baseline, and executor-off state. The five companion verifier files
record 20/20 criterion probes passed. The artifacts are ignored/local and are
not committed to Git.

- Alert duplicate burst compression is at least 90% under the documented replay
  workload; unknown resources fail closed.
- Diagnosis hypotheses are evidence-linked; missing, stale, or contradictory
  evidence cannot become planning-ready.
- Metric, log/context, configuration, and topology evidence carries UTC time,
  source/resource identity, integrity hash, and redaction status. Collector
  failures are explicit and do not block the alerting path.
- All 15 fixtures traverse the required ordered stages in offline mode.
- Malformed stage output is rejected; retry/resume is bounded and does not
  duplicate evidence or action records.
- The running AI Agent remains shadow-only; no autonomous action reaches the
  Safe Executor during Sprint 4 acceptance.

## Sprint 4 acceptance criteria

Local verification on 2026-09-29:

- Agent tests: **345 passed**, 7 upstream `httpx` deprecation warnings.
- Critical lint: `ruff check agent_src --select E9,F63,F7,F82` passed.
- Python compilation: the touched collector, shadow pipeline, and alert
  integration modules and CI-listed automation Python scripts compiled
  successfully; CI-listed Bash scripts passed `bash -n`.
- Agent image: built successfully with the same repository-root build command
  used by CI: `docker build -f agent_src/Dockerfile -t local/ai-agent:sprint4-check-20260929 .`.
- Unified fixture replay: **15/15 passed**, exact required stage ordering,
  5 live-allowlisted and 10 offline-only bindings, zero forbidden live
  executions. Report and SQLite evidence are in the ignored local path
  `terraform/.artifacts/sprint4-acceptance-20260929/`.
- The first manual image-build attempt used `agent_src` as build context and
  failed because this Dockerfile copies `agent_src/` and `evaluation/` from the
  repository root. Re-running the CI's actual `-f agent_src/Dockerfile ... .`
  command passed; no source change was needed for this build-context error.
- `git diff --check` passed. No AWS, Terraform apply/destroy, Ansible run,
  image push, or staging rollout was performed.
- The scenario verifier's negative contract passed: offline-only `DB-02` was
  rejected with the required exit code 64.

Runtime limitation: evidence collection is wired into shadow ingress, but this
acceptance did not query the live Prometheus endpoint or deploy the changed
image. Therefore it proves the collector contracts and safe fallback, not
live telemetry availability or diagnosis accuracy against staging signals.
Shadow responses never authorize execution or mark an incident resolved.

## Sprint 5 — Safety and controlled execution

| Stage | Scope | Result | Evidence / remaining gate |
|---|---|---|---|
| S5.0 — Entry audit and freeze | Review existing policies and pin runtime boundary | Passed locally; found unknown-role fail-open path in the legacy Safety Policy Engine and missing typed-action/catalog binding in SafeExecutionGate | Code audit and post-change regression tests; Terraform CIDR update is recorded above/below under infrastructure verification |
| S5.1 — Policy and RBAC | Forbidden matrix, evidence, confidence, environment, role decisions | Passed local contract: unknown role now denies; blocked unrestricted shell maps to `HUMAN_ONLY`; ordinary forbidden actions remain denied | `agent_src/core/safety_engine.py`; safety policy/gate tests |
| S5.2 — Signed approval | Actor, signature, action binding, expiry and TTL | Passed local negative tests for wrong actor, tampered signature, expired/naive expiry, invalid TTL, and action mismatch | `agent_src/core/safe_execution_gate.py`; `test_safe_execution_gate.py`; the live executor API signature path was not exercised against staging |
| S5.3 — Typed action/catalog boundary | Match TypedAction kind to exact reviewed catalog action; retain exact staging scope, timeout, RBAC and kill switch | Passed local tests; mismatched/unknown action, role, scope or production request is rejected before an adapter call | `SafeExecutionGate`; dry-run/live-workflow and adapter tests |
| S5.4 — Audit and snapshots | Append-only hash-chain, redaction, pre/post snapshots | Passed local tests for chain integrity, append-only behavior and secret redaction; runtime audit completeness was not measured against live executions | `agent_src/core/safe_action_audit.py`; `test_safe_action_audit.py` |
| S5.5 — Rollback and kill switch | Failure path, rollback intent, idempotency and stop control | Passed **10/10 simulated timeout cases** and **1/1 explicitly approved live CON-01 rollback drill**; live API kill switch was confirmed off afterward. This is one live smoke drill, not 10 live repetitions | `test_safe_action_audit.py`, `test_safe_execution_gate.py`; `terraform/.artifacts/moodle-safe-executor/CON-01-20260930T080134Z.json` (ignored local evidence) |
| S5.6 — Acceptance and handoff | Agent suite, replay, image, Terraform drift, staging health and handoff | Local acceptance passed: 354 tests, Ruff, syntax/compile, Agent image, 15/15 unified replay, Terraform `No changes`; live read-only verification passed 5/5 scenarios (20/20 probes), plus one live rollback drill; GitHub CI and Agent image workflows passed on the pushed feature commit | Run links and remaining PR review/merge gate below |

### Sprint 5 infrastructure change

Using AWS profile `target-account` (IAM user `hviet` in account `583845872420`),
Terraform planned the updated administrator CIDR as three in-place security
group updates: `core_sg`, `monitor_sg`, and `web_sg`; **0 added, 0 destroyed**.
The saved plan `terraform/.artifacts/sprint5-cidr-20260930.tfplan` was applied
successfully. A fresh post-apply plan returned **No changes**. This changes
ingress allowlists only; no Terraform resource was recreated. No separate
Moodle deployment or live safe-action execution was performed.

Local verification on 2026-09-30:

- Agent suite: **354 passed**; 8 non-blocking deprecation warnings (7 from
  `httpx` and one existing `datetime.utcnow()` call in the log watcher).
- Critical Ruff, Python compilation, Bash syntax checks, and
  `git diff --check` passed. Verification dependencies were installed only in
  a temporary virtual environment under `/tmp` (not globally).
- Agent image built as `local/ai-agent:sprint5-closeout-20260930`; packaged
  Moodle ground-truth catalog validated at 15 scenarios.
- Unified replay: **15/15 passed**, required stage order, 0 forbidden live
  executions; report and evidence DB are in ignored
  `terraform/.artifacts/sprint5-closeout-20260930/`.
- Fresh Terraform plan using `AWS_PROFILE=target-account` and
  `deployment.tfvars`: **No changes**.
- Live read-only checks on Moodle staging: **5/5 scenario verifiers and 20/20
  individual probes passed** for DB-01, RES-01, NET-01, CON-01, and SEC-02.
  Checks confirmed a fresh successful synthetic transaction, resolved scenario
  alerts, PostgreSQL TLS / DNS, both ALB targets healthy, the app-B container
  healthy, the RES-01 fixture absent, and the reviewed EFS fixture permissions
  and mount metric. JSON results are in ignored
  `terraform/.artifacts/sprint5-live-readonly-20260930/`.
- Approved live CON-01 rollback drill: **passed 1/1**. The drill stopped only
  `release-moodle-web-1` on `moodle-app-b`; the Agent observed the expected
  alert, submitted the signed catalog-bound action, and restored node `b`.
  Post-reset synthetic, both ALB targets, node health, alert resolution, and
  baseline verification passed. The API was healthy with
  `EXECUTOR_LIVE_ENABLED=false` after cleanup. Full record:
  `terraform/.artifacts/moodle-safe-executor/CON-01-20260930T080134Z.json`.
- `git diff --check` passed. No live executor, SSH mutation, or fault injection
  was invoked during Sprint 5 local-only acceptance; the separately approved
  CON-01 staging drill above was the sole live fault/reset in this closeout.

### Sprint 5 remaining acceptance boundary

The code path is locally hardened and tested, and staging health/recovery
probes pass, and one real rollback succeeded. The 10/10 result above is
simulated timeout and rollback-planning coverage; the live result is only one
CON-01 smoke drill, not 10/10 repeated live trials. Further live repetitions
require explicit operator authorization for the scenario/run count and a
verified pre-drill baseline. The newly built image has not been pushed or
deployed.

## Remaining formal closeout

Sprint 3–5 code changes must be committed on a feature branch, pass GitHub CI
there, and merge through a reviewed PR. The successful earlier `develop` CI
does not satisfy that gate. Sprint 4 shadow collector rollout remains a
separate deployment gate. The Sprint 5 closeout changed the requested CIDR
allowlist and performed one reversible Moodle staging CON-01 fault/reset; no
resource was destroyed and no production deployment was performed.

Remote check on 2026-09-30: commit
`7963eb4608cf574810b87ab7edf4d422f45bccd8` is pushed to
`moodle-framework/feature/sprint3-acceptance-handoff`; the worktree is clean.
The branch CI run
[36689372894](https://github.com/Benjaminnhnn/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations/actions/runs/36689372894)
completed successfully in 3m22s, and Agent image workflow
[36689372872](https://github.com/Benjaminnhnn/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations/actions/runs/36689372872)
completed successfully in 1m42s. No PR currently points at this branch/commit;
formal merge-based closeout remains pending creation, review, and merge of the
PR. Sprint 5 acceptance itself is complete on the feature branch; the 10/10
rollback evidence remains simulated, with one separate live CON-01 rollback
smoke drill passed.
