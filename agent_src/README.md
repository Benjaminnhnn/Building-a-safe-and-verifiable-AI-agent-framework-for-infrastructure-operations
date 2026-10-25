# Agent runtime (agent_src/)

This directory builds the deployable Agent API and Celery worker. Replay, fixtures, the former generic role orchestrator, and its duplicate domain models live under top-level evaluation and are not packaged into the runtime.

## Runtime path

Alertmanager sends to the FastAPI webhook. Celery forwards firing Moodle alerts to the Moodle alert integration. In staging investigate mode, the Agent collects evidence, derives a diagnosis, and may persist a remediation candidate. Alert handling never invokes an executor.

An operator prepares a fresh candidate through the remediation prepare endpoint, obtains separate Safety Gate and Executor signatures, then submits the same request to execute. The safe executor returns a receipt. An independent verifier checks the receipt, allowed/forbidden/related probes, and a stable observation window; only an eligible verifier result can transition the incident to RESOLVED.

## Package map

- core/schema: canonical Incident, Resource, Evidence, TypedAction, and Verification contracts.
- core/evidence: evidence collection, SQLite store, dependency graph, adapters, and integrity checks.
- core/moodle: Moodle adapter, alert investigation, diagnosis, 15-scenario disposition & planning, and live handoff.
- core/erpnext: ERPNext adapter (`ERPNextPlatformAdapter`) validating multi-platform generalizability over the platform-neutral core.
- core/safety: action catalog (with reciprocal safe-baseline compensation pairs), policy, approvals, audit, verifier attestation, and executor client.
- core/verification: probes, verifier contracts, and schema-based incident state machine.
- core/reasoning: optional evidence-cited LLM diagnosis (`AIOPS_DIAGNOSIS_MODE`), closed cause vocabulary, and the deterministic citation validator; see docs/LLM_DIAGNOSIS_DESIGN.md.
- core/rag: runbooks, structured retrieval provenance (`retrieve_runbook_with_provenance`), incident memory, and feedback support.
- core/coordination: GitHub tool discovery and review only.
- evaluation/agent_replay: offline replay, fixtures, benchmark helpers, legacy comparisons.

The Agent and executor live switches default off. The integrated staging drill is limited to CON-01. Replay, tests, or a green CI run do not establish live recovery. See AGENTS.md and docs/SPRINT3_S3_3_SAFE_EXECUTOR.md for repository and drill procedures.
