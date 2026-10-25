# Runtime agent roles

The runtime pipeline is split into five role agents (`agent_src/core/agents/`). They exchange typed messages and record each stage in a hash-chained ledger. This maps the thesis architecture (Observation, Context & Knowledge, Agents, Policy & Security, Execution & Tools, Infrastructure Adapters) onto the code that actually runs.

| Agent | Stage(s) | Input → output message | Implementation it wraps |
|---|---|---|---|
| `observer-agent` | `observe` | alert → `ObservationReport` | `collect_shadow_evidence` (Alertmanager, inventory, Prometheus) |
| `diagnosis-agent` | `diagnose` | `ObservationReport` + evidence → `DiagnosisReport` | rule tables plus optional evidence-cited LLM with RAG (`core/reasoning`) |
| `planner-agent` | `plan` | `DiagnosisReport` → `PlanProposal` | catalog-only read-only probe and the exact-fault remediation candidate |
| `safety-gate` / `execution-agent` | `gate_request`, `execute` | candidate → `GateRequest` → `ExecutionReport` | `prepare_/execute_moodle_live_handoff` (Ed25519 approvals, `SafeExecutionGate`, safe executor) |
| `verification-agent` | `verify` | signed attestation → `VerificationReport` | `accept_verifier_attestation`, the only path to `RESOLVED` |

## Shared identity

Every message extends `AgentMessage`, which has these fields:
- `incident_id`, `run_id` and `stage`;
- `produced_by`;
- `evidence_ids`;
- `action_id`, which is the candidate's or the read-only plan's action and is carried from `plan` through `execute`;
- `produced_at`.

`digest()` hashes the message without its timestamp, so replays can compare outputs.

## Stage ledger

`stage_event` turns a message into a `StageAuditEvent`, which records the outcome, actor, evidence refs, action ID and output digest. Events go into the evidence store's append-only, hash-chained `incident_audit_events` table.

- **Alert path** (`core/shadow_pipeline.py`):
  - The ledger is run `incident_id`, with stages observe → diagnose → plan.
  - A `gate_request` stage with outcome `awaiting_approval` is added only when a remediation candidate exists.
  - The checkpoint embeds this ledger, and `load_latest_checkpoint` rejects any divergence. The result's `agent_trace` summarizes the same events.
- **Operator handoff** (`core/main.py` endpoints):
  - The ledger is run `<incident_id>:handoff`, with stages `gate_request` (prepare), `execute` and `verify`. It is kept separate so the checkpointed alert-path ledger never grows after completion.
  - An append failure is logged and never masks the execution or verifier result. The safe-action audit database stays the authoritative execution record.

## Invariants kept

- Alert handling never dispatches an action, and the agents do not change the planner, the Safety Gate or the verifier logic.
- Only `verification-agent` can report `resolved`, and only from a verifier attestation whose signature verifies.
- Replay roles in `evaluation/agent_replay/roles/` remain the offline simulation; this package is what the API and worker run.
