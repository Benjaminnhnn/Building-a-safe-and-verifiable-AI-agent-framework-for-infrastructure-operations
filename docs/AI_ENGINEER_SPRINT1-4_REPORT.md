# AI Engineer — Sprint 1–4 completion report

Date: 2026-09-24  
Scope: AI Engineer-owned deliverables in Weeks 1–8 of `docs/PLANNING.md`.

## Completed implementation

| Week | Work completed | Evidence |
|---|---|---|
| 1 | Added a typed Moodle-neutral `Resource` contract; retained the DB-01 ground truth, synthetic transaction entry point, and baseline artifacts from the existing Moodle foundation work. | `agent_src/core/unified_core.py`; `evaluation/ground_truth/moodle/DB-01.json`; `automation/moodle-synthetic-transaction.py` |
| 2 | Expanded ground truth from five representatives to the full 15-scenario Moodle matrix. Existing event normalization, sensitive-data checks, and V1/V2 compatibility remain in place. | `evaluation/ground_truth/moodle/*.json`; `agent_src/core/event_schema.py`; `agent_src/tests/test_ground_truth.py` |
| 3 | Kept deterministic diagnosis/planning and the Moodle safety-gated pipeline; added persistent evidence/checkpoint storage, restart-safe stage resume, bounded retry, escalation, and structured stage messages. Evidence can be queried by incident, resource, and kind. | `agent_src/core/moodle_pipeline.py`; `agent_src/core/unified_core.py` |
| 4 | Retained ALLOW / REQUIRE_APPROVAL / DENY, action-bound signed approval, audit records, and the independent read-only verifier; replay remains dry-run and cannot resolve an incident. | `agent_src/core/moodle_pipeline.py`; `agent_src/tests/test_moodle_pipeline.py` |
| 5 | Added Pydantic Resource, Evidence, Incident, TypedAction, and AgentMessage contracts; incident and action lifecycle transition guards; timezone-aware evidence provenance and verifier-only `RESOLVED` transition. | `agent_src/core/unified_core.py`; `agent_src/tests/test_unified_core.py` |
| 6 | Added append-only SQLite evidence with update/delete guards, indexed query, dependency graph edges, and source/time/hash provenance in Chroma RAG ingestion and retrieved context. | `agent_src/core/unified_core.py`; `agent_src/core/rag_engine.py`; `agent_src/tests/test_rag_engine.py` |
| 7 | Added deterministic Observer grouping by fingerprint/window, duplicate suppression, evidence-required diagnosis with ranked hypotheses and confidence, and evidence-backed typed planning. | `agent_src/core/unified_core.py`; `agent_src/tests/test_unified_core.py` |
| 8 | Added sequential Observer → Diagnosis → Planner → Gate → Execution → Verification runner, persistent successful-stage checkpoints, retry/resume, and fail-closed escalation. Added a replay command for all 15 ground-truth fixtures. | `automation/ai-engineer-sprint1-4-replay.py`; `terraform/.artifacts/ai-sprint1-4-replay/manual/report.json` |

## Verification performed

- Agent suite: **107 passed** (53 dependency/deprecation/cache warnings; Python 3.14 runtime).
- Ruff critical rules and Python compilation: passed for new core, tests, and replay utility.
- 15-fixture AI replay: **15 passed, 0 failed**, with six ordered stage outputs per scenario, three evidence references per scenario, zero infrastructure mutations, and zero resolution-eligible incidents. Report: `terraform/.artifacts/ai-sprint1-4-replay/final/report.json`.
- The previously existing five-scenario Moodle replay remains separate and stays at `VERIFIED_DRY_RUN`.

## Acceptance still pending

This completes the AI-side offline implementation and contract replay. It does **not** establish the shared live acceptance criteria in the plan:

- The ten added scenario fixtures have definitions and reset intentions, but infrastructure-owned injectors/reset scripts and runtime trials are not implemented or verified for them.
- The new 15-fixture replay validates AI stage contracts against fixture signals; it does not inject faults or pass through a live Alertmanager → webhook/Celery delivery path.
- AWS credentials, Docker Engine, and Ansible were unavailable in the prior environment check. Moodle login, monitoring delivery, backup/restore, stability, live adapter probes, MTTD/recovery times, and three live passes per scenario remain unverified.
- The Moodle live verifier intentionally fails closed until exact communication-contract probes and timestamped stability observations are supplied.

Therefore the **AI Engineer Sprint 1–4 implementation is complete for offline contracts and fixture replay; overall Sprint 1–4 acceptance remains partial/blocked on shared infrastructure and live evidence**. No Terraform apply, deploy, fault injection, restore, commit, or push was performed in this turn.
