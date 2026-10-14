# AI Agent implementation

The agent is a component of the Moodle-on-AWS thesis system. The current research path is the typed, evidence-backed incident pipeline in `core/`, exercised against Moodle scenario contracts and, where configured, deployed in shadow or safety-gated staging mode.

## Runtime and code map

- `core/main.py`: FastAPI health and Alertmanager webhook endpoints.
- `core/moodle_alert_integration.py`, `core/shadow_pipeline.py`: Moodle alert binding and non-mutating shadow processing.
- `core/schema/`, `core/evidence_store.py`: typed incident/action/evidence contracts and SQLite evidence/checkpoint persistence.
- `core/dependency_graph.py`: validated upstream/downstream impact traversal and deterministic Mermaid export from caller-supplied Resource contracts.
- `core/agents.py`, `core/orchestrator.py`: Observer, Diagnosis, Planner, and ordered incident stages.
- `core/safety_policy_engine.py`, `core/safe_execution_gate.py`, `core/action_catalog.py`: fail-closed policy and allowlisted action decisions.
- `core/safe_action_live.py`, `core/safe_executor_client.py`, `core/live_adapters.py`: bounded live execution boundary; deployment/configuration is required before this is a live capability.
- `core/verification_agent.py`, `core/verifier_contract.py`, `core/probes.py`: independent recovery probes and resolution contract.
- `core/rag_engine.py`, `core/runbook_registry.py`: reviewed runbooks and retrieval support for the legacy alert-assistant path.
- `tests/`: unit and contract coverage; passing tests do not prove AWS deployment or live recovery.

## Evidence modes

| Mode | What it establishes |
|---|---|
| Unit/contract tests | Local logic and interface behavior. |
| Offline replay | Fixture-driven stage and policy behavior; no live infrastructure evidence. |
| Shadow | Deployed alert/evidence processing without remediation. |
| Live drill | A bounded Moodle fault, allowed action, independent probes, stability, and reset backed by timestamped artifacts. |

Only the Independent Verifier may transition an incident to `RESOLVED`. Simulated or dry-run outcomes must remain labeled as such. Ground-truth fixtures are scoring inputs and must never authorize execution.

The generic `ExecutionAgent` is dry-run only and checks that its safety decision is bound to the unchanged action and incident. Incident status changes must pass through the state machine. Evidence objects and metadata are immutable after validation, retain observation and collection times, reject unredacted secrets, and support type filtering through `EvidenceStore.query()`. Shadow processing uses a SQLite lease claim and checkpoint per fingerprint/`startsAt` episode to suppress concurrent and repeated collection. Safe-action audit snapshots redact nested credential fields and embedded secrets. The live Safe Action workflow has conditional rollback orchestration for audited partial mutations, but the production catalog has no qualifying reciprocal action pair, the remote executor has no rollback operation, and the alert route is not wired; live mutations remain fail-closed.

For setup and CI commands, use the root `AGENTS.md`. For operational acceptance and Moodle live workflows, see `docs/MOODLE_OPERATIONS_RUNBOOK.md` and `docs/AI_ENGINEER_MOODLE_TEST_RUNBOOK.md`.
