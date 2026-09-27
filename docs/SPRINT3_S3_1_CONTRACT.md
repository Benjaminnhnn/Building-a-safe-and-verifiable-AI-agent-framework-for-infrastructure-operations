# Sprint 3.1 — Moodle Resource and Capability Contract

Status: implemented offline. This document defines the safe integration
boundary for subsequent Sprint 3 work; it does not provision AWS resources or
authorize an Agent to mutate them.

## Source of truth

| Artifact | Purpose |
| --- | --- |
| `agent_src/config/moodle_resource_inventory.json` | Logical Moodle staging resources; mirrored to `evaluation/resources/` for replay fixtures. |
| `agent_src/config/moodle_capability_contract.json` | Staging-only target scopes, capabilities and evidence boundary. |
| `agent_src/core/action_catalog.py` | Typed action allow-list, role, blast radius, rollback and environment restrictions. |
| `evaluation/ground_truth/moodle/` | Fifteen scenario-specific remediation and forbidden-action contracts. |
| `agent_src/core/moodle_contract.py` | Offline validator joining all four inputs. |

## Resource mapping

| Logical resource | Real deployment boundary | Observation sources |
| --- | --- | --- |
| `moodle-app` | Moodle web/cron Compose services on `moodle-app-a` and `moodle-app-b` | Docker, Node Exporter, cAdvisor, synthetic transaction |
| `moodle-web-endpoint` / `moodle-proxy` | Internet-facing ALB and its target group | ALB/CloudWatch, Blackbox, public health endpoint |
| `postgres-db` | Private RDS PostgreSQL instance | RDS/CloudWatch, TCP/TLS probe, synthetic transaction |
| `moodledata-volume` | EFS filesystem and access point mounted by both application nodes | EFS/CloudWatch, mount check, write probe |
| `moodle-rds-security-group` | RDS network boundary managed by Terraform | Terraform state/plan review and AWS security-group evidence |
| `prometheus-monitor`, `alertmanager`, `ai-agent` | Monitoring services on `monitor-ai-01` | Prometheus and Alertmanager API, container health |

`moodle-proxy` is retained as a semantic compatibility identifier for existing
scenario data. In the deployed Moodle topology it denotes the ALB boundary,
not an additional Nginx or Apache proxy host.

## Capability boundary

The Agent may only reason over the finite capabilities in the JSON contract:

- `collect_*` and `verify_recovery` are read-only.
- `reset_staging_fault` and `restore_reviewed_configuration` are scoped-write
  intents only. Actual execution remains disabled until S3.3 and must pass the
  typed Action Catalog, evidence gate, approval policy, timeout and
  idempotency controls.
- There is no unrestricted-shell capability, no generic AWS CLI capability,
  and no production scope.

Every one of the 15 Moodle scenarios has declared required capabilities. Each
`allowed_remediation.action` must exist in the typed catalog and every target
must resolve through `target_scopes` to one or more known resources. A
forbidden scenario action must not appear in that catalog.

## Evidence and action contract

Evidence uses `core.schema.evidence.Evidence`: `evidence_id`, `incident_id`,
`resource_id`, source, collection time, summary, content hash and redaction
flag are mandatory. The contract permits only Alertmanager, Prometheus,
Blackbox, Docker, Node Exporter, cAdvisor, ALB/RDS/EFS and synthetic
transaction sources. Passwords, secrets, tokens, private keys and
authorization metadata are prohibited.

Actions use `core.schema.action.TypedAction` and must bind an incident,
target resource, staging environment, evidence references, idempotency key,
preconditions, expected outcome and rollback plan. Adapter results remain
sanitized and are audit/evidence inputs; neither an alert nor an LLM can pass
raw shell text to an adapter.

## Verification

Run without any cloud credentials or live infrastructure:

```bash
PYTHONPATH=agent_src pytest -q agent_src/tests/test_moodle_capability_contract.py
PYTHONPATH=agent_src python -c \
  'from core.moodle_contract import assert_moodle_capability_contract; assert_moodle_capability_contract()'
```

These tests are discovered by the existing CI pytest step. The next phase,
S3.2, may add real evidence collectors only after staging is explicitly
provisioned and their credentials/permissions are reviewed.
