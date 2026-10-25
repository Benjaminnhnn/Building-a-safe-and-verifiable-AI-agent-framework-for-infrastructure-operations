# Moodle AIOps architecture and threat model

**Scope:** Moodle on AWS EC2, as represented by the current checkout. This is a
source/configuration review, not a claim that every component is deployed or
that a live AI remediation was accepted. The implementation status below is
deliberately separated from the intended design.

## Intended six-layer architecture and current implementation

| Layer | Responsibility | Current source and boundary |
|---|---|---|
| 1. Alert intake | Receive, normalize, validate, deduplicate, and queue Alertmanager events. | FastAPI `/webhook`, canonical contracts in `core/schema/event_schema.py`, Redis/Celery queue in `agent_src/core/main.py` and `tasks.py`. Bearer-token file authentication is supported; the Moodle monitoring playbook requires it and shares a host-generated token with Alertmanager. Other deployments must configure the same mechanism before exposing the endpoint. |
| 2. Observation and evidence | Bind signals to known resources; collect time-bounded metrics, probes, and sanitized excerpts; preserve evidence provenance. | Platform-neutral core in `core/shadow_pipeline.py`, `core/evidence/evidence_collectors.py`, `core/evidence/evidence_store.py`, decoupled from domain via `core/platform_adapter.py`. Shadow mode observes. Investigate mode uses stored fresh evidence for bounded hypotheses and may produce a catalogued read-only probe proposal or staging remediation candidate. Diagnosis and proposal decisions are separate hash-addressed `decision` evidence records linked to input evidence. Neither mode executes or resolves; unsupported alert types remain observe/escalate only. |
| 3. Context and knowledge | Retrieve scoped operational knowledge and dependency context, with source/time/hash provenance. | `core/rag/rag_engine.py` (`retrieve_runbook_with_provenance`), Moodle runbooks under `agent_src/config/knowledge_base/moodle/`, `core/evidence/dependency_graph.py` (`confirmed_affected_resources` vs. `potentially_affected_resources`), and canonical schemas in `core/schema/`. Staging shadow/investigate reports include structured `evidence_provenance`, `runbook_provenance`, `remediation_disposition`, and `explanation`; dynamic incident memory is excluded, and runbook text does not enter diagnosis or action selection. |
| 4. Diagnosis and planning | Form evidence-supported hypotheses and produce a typed candidate action; insufficient or conflicting evidence escalates. | Platform-specific logic encapsulated in `core/moodle/` (`MoodlePlatformAdapter`) and `core/erpnext/` (`ERPNextPlatformAdapter`), registered with `PlatformAdapterRegistry`. Emits catalogued verifier-role read-only probes across all 18 Moodle alert types, explicit 15-scenario dispositions (`SCENARIO_REMEDIATION_DISPOSITION`), or bounded staging remediation candidates for human review. Offline fixture replay lives in `evaluation/agent_replay/moodle_pipeline.py` and is prohibited from executing. |
| 5. Safety and execution | Apply action catalog, role, environment, scope, evidence, approval, idempotency, timeout, and kill-switch checks before a bounded adapter. | `core/safety/action_catalog.py` (with reciprocal `rollback_action` pairs for all catalogued mutations), `core/safety/safe_execution_gate.py`, `core/safety/safe_action_live.py`, and `core/safety/safe_action_audit.py` provide a reviewed S3.3 staging slice (`DB-01`, `RES-01`, `NET-01`, `CON-01`, `SEC-02`) and hash-chain-bound conditional rollback orchestration. Live remediation requires explicit operator handoff (`core/moodle/live_handoff.py`) with two distinct signatures (`ApprovalRecord`), completely separated from the alert intake path. |
| 6. Independent verification and reporting | Read-only allowed/forbidden/related probes plus stability; only the verifier may mark an incident resolved. | `core/verification/verification_agent.py`, `core/verification/verifier_contract.py`, `core/verification/state_machine.py`, and `core/safety/verifier_attestation.py`. The Independent Verifier runs externally, signing results with dedicated Ed25519 key; only signed attestation accepted by Agent transitions state to `RESOLVED`. |

## Data flow and single alert runtime path

```text
Alertmanager
  -> POST /webhook (FastAPI, token auth, normalize to core/schema/event_schema.py, ingress dedup)
  -> Redis/Celery queue (CELERY_QUEUE_DEPTH check, process_alerts_task)
  -> process_single_alert (tasks.py: concurrency limit, identity dedup, cooldown)
       |
       +--> resolved alert: clear cooldown, notify pending verification, exit
       |
       +--> firing alert: process_moodle_alert (core/moodle/alert_integration.py)
              |
              |-- non-Moodle or non-firing: returns None -> logged unsupported, no assistant/executor
              |-- non-staging environment: returns status="escalated"
              |-- AIOPS_UNIFIED_CORE_MODE=live: fail-closed escalation (handoff is separate operator path)
              |-- off/disabled/invalid mode: fail-closed escalation (no legacy fallback)
              |-- AIOPS_UNIFIED_CORE_MODE=shadow / investigate:
                    -> run_shadow_if_enabled (core/shadow_pipeline.py via PlatformAdapter)
                         |-- Identify resource via MoodlePlatformAdapter
                         |-- Claim checkpoint run in SQLiteEvidenceStore
                         |-- Collect fresh bounded Prometheus evidence
                         |-- Rule-based conservative diagnosis (core/moodle/diagnosis.py)
                         |-- Propose read-only probe plan (core/moodle/planner.py)
                         |-- In investigate mode only: propose candidate if exact fault marker matches
                         |-- Persist decisions as hash-addressed Evidence records
                         |-- Retrieve authored runbook context (read-only advisory)
                         |-- Complete checkpoint run in SQLiteEvidenceStore
                         +-> Return observation report (execution_permitted=False, resolution_eligible=False)
```

The default unified mode is disabled; firing Moodle alerts then escalate before
the legacy alert-analysis path. Shadow results are observations; investigate
results are evidence-derived hypotheses/proposals, not action or recovery
evidence. The legacy fixture replay reads ground truth to
construct deterministic dry-run outputs; those outputs are not evaluation
trials and cannot execute actions. Ground truth is excluded from the runtime
image and remains an offline scoring/test input.

The operational Moodle RAG corpus contains authored runbooks, not the
`evaluation/ground_truth/` scenario answers. Runbook chunks are filtered by
configured alert name and carry source path, source timestamp, indexing
timestamp, and SHA-256. The ChromaDB integration smoke verified retrieval
locally; this is functional software evidence, not a measure of diagnostic
accuracy.

## Assets and trust boundaries

- **Assets:** Moodle availability and learner data; RDS/EFS integrity; EC2 and
  container control; AWS credentials and runtime secrets; incident/evidence
  integrity; alert queue and notification availability.
- **Untrusted inputs:** Alertmanager JSON/labels/annotations; log excerpts;
  metrics and probe responses; external repository metadata; retrieved runbook
  text and admin feedback. These inputs may be malformed, stale, misleading,
  or attacker-controlled.
- **Trust boundaries:** public ALB to Moodle; Moodle nodes to RDS/EFS; monitor
  network to agent webhook; agent to Redis/ChromaDB; agent control plane to the
  separate safe-executor API; operator approval to execution. The Moodle
  playbook binds the agent port to loopback and authenticates Alertmanager with
  a shared bearer token. Source configuration does not prove the deployed
  security-group or firewall state.
- **Ground-truth boundary:** scenario answers are for post-decision scoring
  only. They must not be prompt context, diagnostic evidence, action-selection
  input, or verifier input in an empirical run.

## Threats, controls, and residual gaps

| Threat | Existing control/evidence | Residual risk or required proof |
|---|---|---|
| Forged or replayed alert floods the agent or triggers costly analysis | Payload validation, fingerprint-based ingress reservation/dedup, queue limits, conservative Gemini call limits. Moodle deployment requires a 256-bit bearer token read from a mounted secret file and rejects missing/invalid credentials before enqueue. | Other deployments that do not configure `ALERTMANAGER_WEBHOOK_AUTH_REQUIRED` remain unauthenticated. Verify deployed token file permissions and network reachability; the authenticated Moodle Ansible playbook has not been run against AWS in this task. |
| Malformed labels or injected log/runbook/feedback text causes unsafe action selection | Typed schemas, sensitive-data checks, bounded/redacted collectors, and finite action catalogs. Both legacy Gemini calls place alert, runbook, feedback, prior analysis, and retrieved memory in redacted JSON user content under static system policies. Legacy alert analysis exposes only three bounded local read-only tools and validates its final-line proposal schema; admin review has no tools, validates its output schema, and uses deterministic fallback on malformed output. Mutations remain behind separate gates. | Prompt-injection and malformed-input tests do not prove robustness against all hostile content. Legacy Gemini suggestions and feedback review remain advisory; review quality and live runtime behavior are not empirically established. |
| Ground-truth oracle leakage inflates RCA/recovery results | Removed ground-truth-backed live route; fixture replay rejects execution; runtime Dockerfile no longer copies evaluation answers; empirical acceptance rejects non-live classifications. | Offline replay remains deliberately oracle-driven and must stay excluded from benchmark/claims. Any future live decision path needs a separate policy/evidence source and must be audited before re-enabling execution. |
| RAG poisoning or stale procedures mislead responders | Standard runbooks and reviewed/published feedback are separate collections; Moodle alert filtering and source hashes/timestamps are recorded; actions still require an external gate. | Hashes establish content identity, not authorship or freshness approval. Restrict write access to published runbooks/feedback and review retrieved context against current infrastructure configuration. |
| Forged Telegram updates or an untrusted group member publishes a runbook / submits feedback | Webhook delivery must match Telegram's configured secret-token header using constant-time comparison; feedback and runbook callback actions additionally require an allowlisted numeric Telegram user ID and an allowed chat. | `TELEGRAM_WEBHOOK_SECRET` and `TELEGRAM_ADMIN_USER_IDS` must be provisioned before webhook use. This is code-level enforcement; no live bot registration or EC2 rollout has been verified. |
| Secret or learner-data leakage into prompts, evidence, or logs | Key-based metadata exclusions, regex redaction, bounded excerpts, file-mounted runtime secrets, sensitive-field tests. | Redaction is pattern-based and may miss novel formats. Verify deployed log access/retention and do not treat test fixtures as proof of production data hygiene. |
| Excessive privilege or destructive action | Exact staging catalog/scopes, role checks, signed action-bound approval, idempotency, timeout, kill-switch support, separate executor API, HUMAN_ONLY security changes. | `investigate` can persist an evidence-derived candidate for a separate operator handoff; the alert handler never dispatches. The CON-01 staging drill is implemented but has not passed in AWS. Keep approver private keys with separate owners and verify executor host controls, credentials, firewall rules, and kill-switch state before a live trial. |
| False recovery from a health-only signal | Independent verifier checks allowed, forbidden, related probes and a stability window; only verifier-authorized state transition can set `RESOLVED`. | Live AI recovery is not currently wired; verifier fixture tests and a separate manually operated drill do not establish end-to-end AI recovery. |
| Evidence alteration or checkpoint replay | SQLite evidence append-only triggers, content hashes, idempotent identifiers, checkpointed orchestration. | Local DB administrators can alter the database or schema; no external immutable audit sink is proven. Snapshot and protect evidence artifacts before experiments. |
| LLM/provider outage or quota exhaustion | Low retry/call defaults, deterministic fallback, queueing, explicit failure/escalation behavior. | Local tests do not prove provider availability, rate limits, cost, or runtime latency. Collect those metrics in authorized trials. |

## Minimum gates before enabling live AI remediation

1. Authenticate Alertmanager delivery and prove the agent endpoint is reachable
   only from approved monitoring sources.
2. Connect current read-only evidence to diagnosis; require evidence references
   and contradictory-signal handling. Do not load scenario answers in this path.
3. Derive a typed candidate action from a reviewed policy catalog, then run the
   independent safety gate. Unknown scenario/resource/action must escalate.
4. Preserve separate operator authorization, API action-bound signature,
   exact target scope, idempotency, timeout, and kill-switch checks.
5. Execute only in approved staging; capture pre/post snapshots and raw traces.
6. Verify allowed, forbidden, and related communication probes for at least
   120 seconds; only the independent verifier may mark recovery.
7. Collect matched Manual/Ansible/AI rows and both ablations under the
   acceptance checker before making RQ claims.

## Evidence status at review

This document records architecture and threat analysis only. Current empirical
benchmark acceptance is 0/225 for the default n=5 design. No current
end-to-end AI remediation acceptance, matched comparative matrix, or RQ
conclusion is asserted. Earlier AWS Moodle shadow and operator-run fault
outcomes are claimed in dated reports, but their `moodle-faults` and
`moodle-sprint2-live` raw artifact directories are empty in the 2026-10-07
checkout audit. Treat those historical outcomes as unverified here; they do not
prove AI remediation. See
[AI Engineer status](AI_ENGINEER_STATUS.md) and
[Planning](PLANNING.md) for the remaining evidence requirements.
