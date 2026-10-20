# Agent runtime core

The core directory is the deployable runtime. Its contract set is core/schema; Moodle-specific alert and infrastructure logic stays behind core/moodle.

## Alert and remediation path

main.py accepts Alertmanager payloads and queues work. tasks.py sends firing Moodle alerts to moodle/alert_integration.py. In staging investigate mode, shadow_pipeline.py stores alert/evidence, derives a diagnosis, and can create a catalog-bound candidate. The alert task does not dispatch actions. Unsupported alerts are recorded and returned without calling the retired generic incident assistant.

Live handoff is a separate operator path: authenticated prepare returns the current request and digests; two different actors sign the request; execute rechecks candidate freshness and evidence before calling the constrained staging executor. The handoff remains pending verification. The schema-based state machine accepts RESOLVED only from an eligible Independent Verifier result.

The Independent Verifier runs outside the Agent, signs its request-bound live probe result with a dedicated Ed25519 key, and does not write runtime state. The Agent validates that attestation against the executed receipt, applies the canonical state machine, and appends the result to its own audit ledger. Operators confirm resolution through the Agent status endpoint.

## Runtime packages

- schema/: canonical Pydantic contracts and Alertmanager normalization.
- evidence/: collection, evidence store, integrity checks, and safe adapters.
- moodle/: Moodle inventory, diagnosis, planner, alert integration, and handoff.
- safety/: catalog, policy gate, approvals, executor transport, and audit ledger.
- verification/: read-only probes, verifier contracts, and incident state transitions.
- rag/: runbook retrieval and feedback-backed incident memory.
- coordination/: GitHub tool discovery/review helper.

Offline replay, ground truth, legacy role orchestration, and duplicate domain models live in evaluation/agent_replay/; the runtime does not import them. main.py also retains unrelated runbook and feedback endpoints, which are not part of incident remediation.
