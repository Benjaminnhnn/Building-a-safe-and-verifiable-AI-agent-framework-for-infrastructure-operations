# Safe and Verifiable AIOps for Moodle on AWS

This repository contains the thesis implementation and operational tooling for a safety-gated AI agent that observes and diagnoses Moodle infrastructure incidents, proposes bounded actions, and relies on an independent verifier before reporting recovery.

## Thesis scope

- **Primary environment:** Moodle on AWS EC2, backed by RDS PostgreSQL and EFS, with ALB, Prometheus, Alertmanager, and the AI agent.
- **Research questions:** whether an evidence- and policy-based Safety Gate reduces unsafe actions (RQ1), and whether independent contract probes reduce false recovery reports compared with health-only checks (RQ2).
- **Incident lifecycle:** alert → evidence → diagnosis → action plan → Safety Gate → approved/bounded execution → independent verification.
- **Resolution rule:** only the Independent Verifier may mark an incident `RESOLVED`; health checks alone are insufficient.
- **Evidence boundary:** fixture replay validates code and contracts, not live fault recovery. Shadow mode observes and records decisions without remediating infrastructure.
- **Generalization:** `evaluation/ground_truth/erpnext/` contains optional ERPNext fixtures. They do not establish a deployed or empirically evaluated ERPNext environment.

## Repository map

| Path | Responsibility |
|---|---|
| `terraform/` | AWS network, monitor/Moodle hosts, ALB, RDS, EFS, and security groups. Review state and plan before any apply or retirement. |
| `ansible/` | Host bootstrap, monitoring, Moodle runtime preparation, and safe-executor configuration. |
| `moodle/`, `release/moodle/` | Moodle image, health/synthetic probes, and Moodle Compose runtime. |
| `agent_src/core/` | Incident contracts, evidence store, observer/diagnosis/planner, safety policy, executor boundary, orchestrator, and verifier. |
| `evaluation/ground_truth/moodle/` | The 15 Moodle scenario contracts; resource/topology catalogs are under `evaluation/resources/`. |
| `automation/` | Moodle baseline, fault/reset drills, replay, safe-executor workflows, evidence harvesting, and deployment helpers. |
| `.github/workflows/` | CI validation, AI/Moodle image publishing, and Moodle staging deployment. |
| `docs/` | Planning, runbooks, architecture, operational evidence, and sprint reports. Treat dated reports as historical snapshots. |

Start with [AI Engineer status](docs/AI_ENGINEER_STATUS.md) for the current ownership and evidence gaps.
[Architecture and threat model](docs/AI_AGENT_ARCHITECTURE_AND_THREAT_MODEL.md) records the six-layer design against the code that is actually wired today. Moodle `shadow` mode observes only; `investigate` adds evidence-derived diagnosis and read-only probe proposals. Both modes prohibit execution and resolution. The former ground-truth-backed `live` route remains fail-closed.
The Moodle monitoring playbook authenticates Alertmanager webhook delivery with a generated bearer token. Deployments that do not use that playbook must configure the equivalent token-file settings before exposing `/webhook` beyond a trusted private network.

Telegram webhooks require `TELEGRAM_WEBHOOK_SECRET`; runbook approval and feedback also require authorized numeric Telegram user IDs in `TELEGRAM_ADMIN_USER_IDS`. Configure these in the release environment only when enabling inbound Telegram actions.

## Main workflows

### Local validation and offline replay

Follow the commands in `AGENTS.md`. CI runs the critical Ruff checks and agent tests, validates Moodle automation, builds the agent and Moodle images, checks Compose configuration, and runs the 15-scenario replay. The replay asserts that it remains simulated and cannot authorize live execution.

### Moodle staging deployment and live drills

`cd-staging.yml` publishes an immutable Moodle image and deploys it to the two Moodle nodes through the configured private self-hosted runner. Live fault drills use the scripts under `automation/moodle-*`; they require the intended AWS account, staging environment, bounded scenario, baseline, and reset checks. Do not infer a live recovery from CI replay or shadow records.

### Safe execution and verification

The agent proposes typed actions. The Safety Gate checks policy and execution context; unknown or disallowed actions fail closed. Approval and live execution are separate from replay. Recovery requires fresh allowed, forbidden, and related probes plus the configured stability window. See `docs/MOODLE_OPERATIONS_RUNBOOK.md` and `docs/SPRINT3_S3_3_SAFE_EXECUTOR.md` for operational detail.

## Evidence and benchmark status

`automation/aiops-unified-replay.py` produces offline simulation artifacts. `automation/run-benchmark.py` is a synthetic harness and its generated metrics must not be presented as observed Moodle outcomes. Synthetic benchmark result files were removed from the active results directory; see the [benchmark evidence status](evaluation/benchmark/results/BENCHMARK_REPORT.md). Use raw, provenance-bearing live trial records for empirical RQ1/RQ2 results; keep fixture, shadow, deployed-runtime, and live-acceptance results distinct.

The checked-in `report.md` and files under `docs/` are dated evidence snapshots, not live status pages. Confirm their artifact paths and timestamps before reusing any operational claim. In particular, a successful Moodle staging shadow drill demonstrates alert-to-agent processing and observation, not AI remediation.

## Legacy components

The repository still contains legacy banking/payment release files and Terraform-managed `bank-web-01` / `bank-core-01` resources. They are not part of the Moodle thesis path. Terraform state may still own those resources; do not remove or disable them in infrastructure code until a fresh state-backed plan and data/availability review establishes the retirement impact. Legacy release Compose files are retained for reference and are not the thesis deployment workflow.

## Core invariants

- Terraform owns cloud resources; Ansible owns host configuration.
- CI builds images; EC2 pulls versioned images and does not build application source.
- Keep staging and production port/environment isolation.
- The verifier is read-only and is the sole authority for `RESOLVED`.
- Never use ground truth to authorize actions; it is for post-decision scoring.
- Do not claim live acceptance from plans, scripts, fixture replay, or shadow mode.
