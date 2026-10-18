# Safe and Verifiable AIOps for Moodle on AWS

Thesis implementation and operations tooling for an AI agent that observes Moodle incidents, proposes bounded actions, and uses an independent verifier before reporting recovery.

## Start here

- [Current AI status and evidence gaps](docs/AI_ENGINEER_STATUS.md)
- [Documentation index](docs/README.md)
- [Repository and agent instructions](AGENTS.md)
- [Thesis report snapshot](report.md) — historical evidence; check dates and artifacts before reusing claims.

## Find the code

| Path | Contents |
|---|---|
| `agent_src/core/` | Alert handling, evidence, diagnosis/planning, safety gates, execution boundaries, and verification. |
| `evaluation/` | Scenario ground truth, resource catalogs, benchmark code, and evidence reports. |
| `automation/` | Moodle replay, operational baselines, fault/reset drills, verification, and deployment helpers. |
| `terraform/` | AWS resource definitions. Review state-backed plans before infrastructure changes. |
| `ansible/` | Host configuration and deployment prerequisites. |
| `moodle/`, `release/moodle/` | Moodle image, web probes, and Moodle Compose runtime. |
| `release/erpnext/` | Isolated ERPNext generalization runtime; Terraform is opt-in. |
| `.github/workflows/` | CI, image publishing, and Moodle staging deployment workflows. |
| `docs/` | Current guides and dated architecture, operations, planning, and sprint records. |

## Common workflows

- **Validate locally:** follow the ordered commands in `AGENTS.md`.
- **Run offline replay:** `python automation/aiops-unified-replay.py all --output-dir <output-dir>`; replay is simulated evidence, not live recovery.
- **Deploy Moodle staging / run live drills:** use the Moodle-specific workflow and scripts documented in [Moodle operations](docs/MOODLE_OPERATIONS_RUNBOOK.md). Verify AWS identity, target environment, baseline, and reset path first.
- **Understand evidence status:** read [AI Engineer status](docs/AI_ENGINEER_STATUS.md) and [benchmark evidence status](evaluation/benchmark/results/BENCHMARK_REPORT.md). Keep fixture, shadow, deployed runtime, and live acceptance claims separate.

## Project boundaries

Moodle on AWS is the thesis environment. Terraform owns cloud resources; Ansible owns host configuration. CI builds images and EC2 pulls them. Only the Independent Verifier may mark an incident `RESOLVED`; fixtures are for post-decision evaluation, never authorization.

The approved proposal also requires ERPNext generalization after Moodle acceptance. See [ERPNext staging runbook](docs/ERPNEXT_STAGING_RUNBOOK.md); its runtime and drill tooling are prepared, but no AWS deployment or empirical recovery evidence is claimed.

Legacy banking/payment release files and Terraform-managed bank hosts are separate from the Moodle thesis deployment. Keep them intact until a state-backed retirement review establishes their impact.
