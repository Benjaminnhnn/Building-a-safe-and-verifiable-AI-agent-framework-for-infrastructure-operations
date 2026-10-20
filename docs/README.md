# Documentation index

Use [AI Engineer status](AI_ENGINEER_STATUS.md) for the current ownership and evidence gaps. Other status, demo, and sprint reports are dated snapshots; check their dates and referenced artifacts before treating them as current.

## Operations and deployment

- [ERPNext staging validation runbook](ERPNEXT_STAGING_RUNBOOK.md) — dedicated EC2 deployment and ERP-01/02/03 drill protocol.

- [Master runbook](MASTER_RUNBOOK.md) — entry point for operating the environment.
- [Moodle operations runbook](MOODLE_OPERATIONS_RUNBOOK.md) — Terraform, Ansible, deployment, monitoring, and teardown.
- [Moodle operational baseline](MOODLE_OPERATIONAL_BASELINE_REPORT.md) — baseline report snapshot.
- [Moodle database and storage](MOODLE_DATABASE_STORAGE.md)
- [Moodle network security](MOODLE_NETWORK_SECURITY.md)
- [AWS IAM access](AWS_IAM_ACCESS_GUIDE.md)
- [AI engineer Moodle test runbook](AI_ENGINEER_MOODLE_TEST_RUNBOOK.md)

## Architecture and implementation

- [AI agent architecture and threat model](AI_AGENT_ARCHITECTURE_AND_THREAT_MODEL.md)
- [Infrastructure design](MOODLE_INFRASTRUCTURE_DESIGN.md)
- [Safe executor](SPRINT3_S3_3_SAFE_EXECUTOR.md)
- [Scenario integration](SPRINT3_S3_4_SCENARIO_INTEGRATION.md)
- [Scenario contracts](SPRINT3_S3_1_CONTRACT.md)
- [Entra/Okta OIDC implementation](MOODLE_ENTRA_OKTA_OIDC_IMPLEMENTATION.md)
- [SSO incident scenarios](MOODLE_SSO_INCIDENT_SCENARIOS.md)

## Benchmark and evidence

- [Benchmark report and evidence status](../evaluation/benchmark/results/BENCHMARK_REPORT.md)
- [Benchmark data dictionary](BENCHMARK_DATA_DICTIONARY.md)
- [RQ analysis template](RQ_ANALYSIS_TEMPLATE.md)
- [CI/CD workflow guide](AIops_CICD.md)
- [Staging SSO demo report](MOODLE_STAGING_DEMO_REPORT_VI.md) — dated report.

## Planning and sprint history

- [Planning](PLANNING.md)
- [Week 1 scenarios](WEEK1_SCENARIOS.md)
- [Sprint 3 baseline](SPRINT3_BASELINE.md)
- [Sprint 3/Sprint 4 completion report](SPRINT3_SPRINT4_COMPLETION_REPORT.md)
- [Sprint 7 demo report](SPRINT7_DEMO_REPORT.md)
- [Okta/Entra OIDC demo plan](OKTA_ENTRA_MOODLE_OIDC_DEMO_PLAN.md)

## Separate legacy release lane

The banking/payment release files under `release/` and `automation/app-release-deploy.sh` belong to a legacy application lane. Moodle staging uses `release/moodle/` and `automation/github-deploy-moodle.sh`; do not use legacy files as evidence of Moodle deployment.

The proposal requires ERPNext as the generalization environment. Its isolated runtime is under `release/erpnext/`; Terraform creation is disabled by default, and live evidence remains outstanding.
