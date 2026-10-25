# Safe and Verifiable AIOps for Moodle on AWS

Thesis implementation and operations tooling for an AI agent that observes Moodle incidents, proposes bounded actions, and uses an independent verifier before reporting recovery.

## Start here

- [Current AI status and evidence gaps](docs/AI_ENGINEER_STATUS.md)
- [Documentation index](docs/README.md)
- [Repository and agent instructions](AGENTS.md)

## Find the code

| Path | Contents |
|---|---|
| `terraform/` | AWS network, monitor/Moodle hosts, ALB, RDS, EFS, and security groups. Review state and plan before any apply or retirement. |
| `ansible/` | Host bootstrap, monitoring, Moodle runtime preparation, and safe-executor configuration. |
| `moodle/`, `release/moodle/` | Moodle image, health/synthetic probes, and Moodle Compose runtime. |
| `release/erpnext/` | Isolated ERPNext generalization runtime; Terraform is opt-in. |
| `agent_src/core/` | Alert handling, evidence, diagnosis/planning, safety gates, execution boundaries, and verification. |
| `evaluation/ground_truth/` | `moodle/` holds the 15 Moodle scenario contracts used by the Moodle pipeline, acceptance and benchmark; the top-level `<ID>-*.json` files are the unified-replay fixtures for the same scenarios. The resource inventory mirror is in `evaluation/resources/`. |
| `evaluation/agent_replay/` | Offline replay roles, domain model, coordination, and independent verification used by the replay harnesses; not imported by the runtime image. |
| `automation/` | Moodle baseline, fault/reset drills, replay, safe-executor workflows, evidence harvesting, and deployment helpers. |
| `.github/workflows/` | CI validation, AI/Moodle image publishing, and Moodle staging deployment. |
| `docs/` | Planning, runbooks, architecture, operational evidence, and sprint reports. Treat dated reports as historical snapshots. |

## Common workflows

- **Validate locally:** follow the ordered commands in `AGENTS.md`.
- **Run offline replay:** `python automation/aiops-unified-replay.py all --output-dir <output-dir>`; replay is simulated evidence, not live recovery.
- **Deploy Moodle staging / run live drills:** use the Moodle-specific workflow and scripts documented in [Moodle operations](docs/MOODLE_OPERATIONS_RUNBOOK.md). Verify AWS identity, target environment, baseline, and reset path first.
- **Understand evidence status:** read [AI Engineer status](docs/AI_ENGINEER_STATUS.md) and [benchmark evidence status](evaluation/benchmark/results/BENCHMARK_REPORT.md). Keep fixture, shadow, deployed runtime, and live acceptance claims separate.

## Project boundaries

Moodle on AWS is the thesis environment. Terraform owns cloud resources; Ansible owns host configuration. CI builds images and EC2 pulls them. Only the Independent Verifier may mark an incident `RESOLVED`; fixtures are for post-decision evaluation, never authorization.

Follow the commands in `AGENTS.md`. CI runs the critical Ruff checks and agent tests, validates Moodle automation, builds the agent and Moodle images, checks the Moodle Compose configuration, and runs two 15-scenario replays (unified control plane and Sprint 2 pipeline). The replay asserts that it remains simulated and cannot authorize live execution.

### Moodle staging deployment and live drills

`cd-staging.yml` publishes an immutable Moodle image and deploys it to the two Moodle nodes through the configured private self-hosted runner. Live fault drills use the scripts under `automation/moodle-*`; they require the intended AWS account, staging environment, bounded scenario, baseline, and reset checks. Do not infer a live recovery from CI replay or shadow records.

### Safe execution and verification

The agent proposes typed actions. The Safety Gate checks policy and execution context; unknown or disallowed actions fail closed. Approval and live execution are separate from replay. Recovery requires fresh allowed, forbidden, and related probes plus the configured stability window. See `docs/MOODLE_OPERATIONS_RUNBOOK.md` for operational detail, `docs/RUNTIME_AGENT_ROLES.md` for the role agents and stage ledger, and `docs/LLM_DIAGNOSIS_DESIGN.md` for the evidence-cited LLM diagnosis and its evaluation.

## Evidence and benchmark status

`automation/aiops-unified-replay.py` produces offline simulation artifacts. `automation/run-benchmark.py` is a synthetic harness and its generated metrics must not be presented as observed Moodle outcomes. Synthetic benchmark result files were removed from the active results directory; see the [benchmark evidence status](evaluation/benchmark/results/BENCHMARK_REPORT.md). Use raw, provenance-bearing live trial records for empirical RQ1/RQ2 results; keep fixture, shadow, deployed-runtime, and live-acceptance results distinct.

Files under `docs/` are dated evidence snapshots, not live status pages. Confirm their artifact paths and timestamps before reusing any operational claim. In particular, a successful Moodle staging shadow drill demonstrates alert-to-agent processing and observation, not AI remediation.

## ERPNext generalization

The approved proposal also requires ERPNext generalization after Moodle acceptance. The ERPNext adapter (`agent_src/core/erpnext/`), Terraform (`terraform/erpnext.tf`), Compose (`release/erpnext/`), playbook (`ansible/playbooks/deploy-erpnext-staging.yml`) and fault scripts (`automation/erpnext-fault-*.sh`) are prepared, but no AWS deployment or empirical recovery evidence is claimed.

## Legacy components

The banking/payment demo (bank-web-01 / bank-core-01, payment-api, frontend, its release Compose files, deploy script and Ansible stack) and the local OpenLDAP auth lab were removed on 2026-10-14. The two legacy security groups are destroyed by the next reviewed `terraform apply`. Its remaining agent code (the legacy alert-diagnosis branch in `agent_src/core/tasks.py`, the nginx/PostgreSQL/Redis/Docker runbooks and the host log watcher) was removed on 2026-10-25. Dated documents may still describe the removed components.

## Core invariants

- Terraform owns cloud resources; Ansible owns host configuration.
- CI builds images; EC2 pulls versioned images and does not build application source.
- Keep staging and production port/environment isolation.
- The verifier is read-only and is the sole authority for `RESOLVED`.
- Never use ground truth to authorize actions; it is for post-decision scoring.
- Do not claim live acceptance from plans, scripts, fixture replay, or shadow mode.
