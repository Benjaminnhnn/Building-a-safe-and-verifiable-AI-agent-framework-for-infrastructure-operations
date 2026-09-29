# Sprint 3.4 — Scenario integration map

## Purpose and boundary

This document maps the 15 Moodle ground-truth scenarios to typed remediation
actions, target scopes, recovery evidence, and the S3.3 staging execution
boundary. It is a design/validation artifact; generating or checking this map
does not inject faults or call AWS.

The integration path is:

```text
ground truth (`evaluation/ground_truth/moodle/*.json`)
  -> capability/resource contract (`agent_src/config/`)
  -> typed action catalog (`agent_src/core/action_catalog.py`)
  -> exact staging scenario/action/target allowlist (S3.3)
  -> reviewed adapter + scenario-specific verifier (S3.4 follow-up)
```

The ground truth's `allowed_remediation` entries are not, by themselves,
permission to execute. Live staging execution additionally requires an exact
scenario, action, target, environment, and role match in the S3.3 allowlist.
Production remains denied.

## Scenario matrix

| Scenario | Ground-truth remediation → target | Recovery evidence from ground truth | Execution lane |
|---|---|---|---|
| DB-01 | `remove_scoped_db_reject` → `staging_moodle_nodes` | Synthetic transaction, TLS DB probe, alert resolved | Live staging slice |
| DB-02 | `restore_database_connection_capacity` → `staging_rds_postgresql` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| DB-03 | `restore_approved_database_endpoint` → `staging_moodle_configuration` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| RES-01 | `remove_named_cpu_load_container` → `moodle-app-b` | Named load absent, CPU alert resolved, synthetic transaction | Live staging slice |
| RES-02 | `remove_named_memory_pressure_container` → `moodle-app-b` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| RES-03 | `remove_named_disk_fixture` → `staging_moodle_data_volume` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| NET-01 | `recreate_moodle_web_from_reviewed_compose` → `staging_moodle_nodes` | DB hostname resolves, synthetic transaction, both ALB targets healthy | Live staging slice |
| NET-02 | `remove_scoped_moodle_db_port_reject` → `staging_moodle_nodes` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| NET-03 | `remove_scoped_netem_profile` → `staging_moodle_node` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| CON-01 | `start_reviewed_compose_service` → `moodle-app-b` | Container healthy, both ALB targets healthy, synthetic transaction | Live staging slice |
| CON-02 | `start_moodle_reverse_proxy` → `staging_moodle_proxy` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| CON-03 | `restore_previous_approved_release` → `staging_moodle_app` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| SEC-01 | `remove_tagged_database_ingress_rule` → `staging_database_security_group` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |
| SEC-02 | `restore_fixture_directory_mode` → `moodledata_synthetic_fixture_directory` | Fixture mode restored, synthetic transaction, EFS mount metric = 1 | Live staging slice |
| SEC-03 | `restore_approved_proxy_configuration` → `staging_moodle_configuration` | Synthetic transaction, fixture removed, communication contract | Offline/shadow only |

The live slice also has one read-only verifier action per scenario:

| Scenario | Verifier action → target |
|---|---|
| DB-01 | `verify_tls_database_connection` → `staging_moodle_nodes` |
| RES-01 | `verify_node_cpu_recovers` → `moodle-app-b` |
| NET-01 | `verify_database_hostname` → `staging_moodle_nodes` |
| CON-01 | `wait_for_alb_target_health` → `moodle-app-b` |
| SEC-02 | `verify_efs_write` → `moodle-synthetic-transaction` |

## Validation results

- Ground truth, capability mapping, and typed action catalog cover the same 15
  scenario IDs; all remediation actions and target scopes resolve.
- All 15 scenarios have catalogued remediation, but only five have an exact
  S3.3 live scope. The remaining ten are intentionally offline/shadow-only;
  this stage does not widen the allowlist.
- The five live scenarios each have one remediation and one read-only verifier
  action in the allowlist (10 tuples total).
- Offline replay, including a passing simulated step, is not evidence that a
  live Moodle resource recovered. `ContractProbeRunner.run_offline()` correctly
  leaves `resolution_eligible` false.

## Integration gap to resolve before expanding live drills

The current live `MoodleReadOnlyVerificationAdapter` provides generic Moodle
HTTP and monitoring probes. Ground-truth criteria also require scenario-bound
checks such as EFS mount metrics, both ALB target states, a named fixture being
absent, CPU alert resolution, and communication-boundary invariants. Before
using it to close additional scenario drills, the pipeline must bind each
scenario's recovery criteria to explicit read-only probes and require all
required evidence before it reports recovery. Until then, this mapping is not
a claim that all 15 live verifiers are implemented.

## Reproduce the contract checks

```bash
PYTHONPATH=agent_src pytest -q \
  agent_src/tests/test_moodle_capability_contract.py \
  agent_src/tests/test_action_catalog.py
```

No fault injection, Terraform operation, deployment, or infrastructure write
is part of this stage.

## S3.4.2 — Offline/shadow integration

`build_moodle_scenario_binding()` is the shared read-only mapper used by the
Moodle shadow alert path and `automation/aiops-unified-replay.py`. It checks
that each ground-truth remediation resolves to a typed catalog entry and a
declared target scope, records the adapter/role and exact S3.3 allowlist match,
and fails closed for unknown actions or targets. The shadow planner consumes
these validated action/target pairs and stores the binding as audit evidence;
the binding itself never grants execution permission.

The offline replay now includes this binding per scenario and explicitly
labels the result `offline_simulation`, `live_verification_executed: false`,
and `recovery_claim_scope: simulated_fixture_only`. Reports summarize the
five scenarios with a live allowlisted route and the ten that remain
offline/shadow-only. Even for the five, the replay still invokes no staging
adapter and makes no live recovery claim.

Focused regression coverage:

```bash
PYTHONPATH=agent_src pytest -q \
  agent_src/tests/test_moodle_capability_contract.py \
  agent_src/tests/test_action_catalog.py \
  agent_src/tests/test_moodle_alert_integration.py
```

## S3.4.3 — Safety and regression coverage

The integration tests exercise all 15 ground-truth scenarios through the
shadow alert path and assert that none routes to `_route_live`, obtains
execution permission, or becomes resolution-eligible. Negative cases prove
that unknown catalog actions and target scopes fail closed before evidence is
written or planning begins. CI also validates the offline replay report's
simulation-only claim and the unchanged 5/10 live-scope split.
