# Documentation index

Use [AI Engineer status](AI_ENGINEER_STATUS.md) for the current ownership and evidence status.

## Operations and deployment

- [Moodle operations runbook](MOODLE_OPERATIONS_RUNBOOK.md) — Terraform, Ansible, deployment, monitoring, and teardown.
- [DevOps deploy workflow guide](DEVOPS_DEPLOY_WORKFLOW_GUIDE.md) — End-to-end deployment walkthrough.
- [Moodle database and storage](MOODLE_DATABASE_STORAGE.md)
- [Moodle network security](MOODLE_NETWORK_SECURITY.md)
- [AWS IAM access](AWS_IAM_ACCESS_GUIDE.md)
- [CI/CD workflow guide](AIops_CICD.md)

## Architecture and implementation

- [System design diagram](diagrams/system-design-aws.drawio) — draw.io, two pages: AWS deployment and the six-layer AI agent framework (regenerate with `python docs/diagrams/generate_system_design.py docs/diagrams/system-design-aws.drawio`).
- [AI agent architecture and threat model](AI_AGENT_ARCHITECTURE_AND_THREAT_MODEL.md)
- [Runtime agent roles](RUNTIME_AGENT_ROLES.md) — Observer, Diagnosis, Planner, Execution and Verification agents and the stage ledger.
- [Evidence-cited LLM diagnosis](LLM_DIAGNOSIS_DESIGN.md) — modes, citation validator, RAG knowledge links, and the AI evaluation.
- [Hermes agent research](HERMES_AGENT_RESEARCH.md) — read-only MCP prototype, outside the runtime pipeline.
- [Infrastructure design](MOODLE_INFRASTRUCTURE_DESIGN.md)
- [Entra/Okta OIDC implementation](MOODLE_ENTRA_OKTA_OIDC_IMPLEMENTATION.md)
- [SSO incident scenarios](MOODLE_SSO_INCIDENT_SCENARIOS.md)

## Benchmark and evidence

- [Benchmark report and evidence status](../evaluation/benchmark/results/BENCHMARK_REPORT.md)

