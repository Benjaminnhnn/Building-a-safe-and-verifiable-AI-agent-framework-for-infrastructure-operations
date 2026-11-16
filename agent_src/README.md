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
- core/rag: runbooks, structured retrieval provenance (`retrieve_runbook_with_provenance`), incident memory, and feedback support.
- core/coordination: GitHub tool discovery and review only.
- evaluation/agent_replay: offline replay, fixtures, benchmark helpers, legacy comparisons.

The Agent and executor live switches default off. The integrated staging drill covers DB-01, RES-01, NET-01, CON-01, BIZ-01 and BIZ-02; only CON-01 has passed end to end on staging. Replay, tests, or a green CI run do not establish live recovery. See AGENTS.md, docs/BAO_CAO_KIEN_TRUC_AI_AGENT_VA_AN_TOAN.md and docs/BAO_CAO_KICH_BAN_LOI.md for repository and drill procedures.


## Local AIHS data-collection lab (`lab/moodle-local/`)

A Docker lab that mirrors the Moodle staging stack produces the train/validation/test data for the AIHS LDA/GBDT comparison (Phase 1). It is evidence mode `lab`: it is never staging evidence and its records are never summed with staging, replay or synthetic rows. AWS staging is used only for external validation and live runs.

- **Stack**: PostgreSQL (RDS stand-in), two Moodle web nodes with a shared moodledata volume (EFS stand-in), cron on node A, an HAProxy load balancer with active `/healthz.php` checks (ALB stand-in), Prometheus with the staging alert rules, blackbox probes, the unmodified `automation/moodle-synthetic-transaction.py` and a per-node probe (`node_probe.py`) that publishes the staging `moodle_node_*` signals (DB TCP, DNS, web running, router, CPU, cron heartbeat, ad-hoc backlog, fixture directory mode).
- **Control**: `lab/moodle-local/lab.sh build | up | status | down | run ... | batch ... | progress | stop-batch` (header comment lists usage). Several labs can run side by side with `LAB_PROJECT`/`LAB_PREFIX`/`LAB_PROM_PORT`.
- **Faults and actions**: `lab_faults.py` mirrors the injectors/resets for DB-01, NET-01, RES-01, CON-01, SEC-02 and BIZ-02 (mutates only named fixtures and tagged rules). `lab_executor.py` re-implements the staging safe-executor catalog semantics (exact scenario/action/scope allowlist, action digest, signed expiring approval, nonce and idempotency claims, per-node sequential execution) for the six reviewed actions; `evaluation/aihs/action_ladder.py` is the single source of truth for the predictor action space (`wait_and_recheck` < `restart_moodle_cron_service` < `start_reviewed_compose_service` < `recreate_moodle_web_from_reviewed_compose` < {`remove_named_cpu_load_container` = `remove_scoped_db_reject` = `restore_fixture_directory_mode`} < `call_human`).
- **Collector** (`lab_collector.py`): writes metrics, firing alerts and redacted logs with `detected_at`, `evidence_cutoff_at`, capture times and SHA-256 per part before any proposal exists, enforces `evidence_cutoff_at <= predicted_at`, and rejects scenario markers, answer-bearing run ids, ground truth, signatures and post-decision fields in features and raw evidence.
- **Runner** (`lab_runner.py`): baseline verify, optional background load, one fault or an ambiguous/overlapping pair, detection, snapshot, behavior-policy action (matching action plus controlled exploration over the listed actions only), execution, allowed/forbidden/related probes over a stability window, outcome (`success`, `failure`, `not_executed`, `denied`, `timeout`, `cancelled`, `missing_evidence`), reset and baseline re-verify. Each episode is an artifact directory with a checksum manifest under the Git-ignored `evaluation/aihs/.artifacts/lab-campaign/`.
- **Export and labels**: `python evaluation/aihs/a2_export.py --source lab --campaign <name>` writes schema-valid A2 records and a manifest under `evaluation/aihs/.artifacts/new-a2-export/` (the staging exporter is unchanged and remains the default). `evaluation/aihs/lab_label_form.py` writes EMPTY independent-label forms (the `b2_freeze_dataset.py --label-review` shape and a two-part proposal/recovery form); labels must come from a separate human reviewer.

- **Blind labelling tool**: `python evaluation/aihs/label_tool.py --campaign campaign-1 --reviewer <id> [--subset-fraction 0.2 --subset-seed N]` serves a local (127.0.0.1) web UI showing only admissible evidence (snapshot charts, executed action, post-action probes, redacted logs; never `episode.json`, runner outcome or policy fields). Answers are saved atomically to `evaluation/aihs/.artifacts/labels/<id>/labels.jsonl`. `label_tool.py export-review --reviewer <id>` writes `independent-label-review.jsonl`, `proposal-safety-labels.jsonl` and `labels-summary.json`; `label_tool.py agreement --a <id1> --b <id2>` computes Cohen's kappa. `b2_freeze_dataset.py` records a human/runner outcome mismatch as the exclusion `independent_label_disagrees_with_observed_outcome` (manifest `label_disagreements`) instead of raising.
- **Deviations from staging**: PostgreSQL TLS is off; the lab has no Safety Gate, human Operator or Independent Verifier (the lab approval is an automated key and probe stability replaces the Verifier), so `gate_decision` is null and `operator_approved` is false in lab records; node CPU comes from `docker stats` (2 vCPU per node) instead of node_exporter, with the same 70% threshold; the staging `moodle_fault_*` marker metrics are not exported; Prometheus scrapes every 5 s and `MoodleAdhocTaskQueueStuck` uses `for: 30s`; both nodes share one Docker host, so the DB-01 DOCKER-USER rule is scoped to the database address on the app network.

## AIHS-on-Moodle baseline training (`evaluation/aihs/moodle_aihs/`)

Offline research tooling, not part of the Agent image; model output is an untrusted proposal and nothing here executes an action. Install `evaluation/aihs/requirements-moodle-aihs.txt` (Python 3.11) first.

- Baseline ids: `aihs-paper-repro-moodle-v1` (the C0: Predictor on successful executed non-transient episodes, cost-ordered Controller fallback) and `aihs-public-prototype-as-is-v1` (reference only: Predictor on all executed rows, prototype fallback bug). Results of the two are never mixed.
- Mapper deviations from the paper: sklearn batch variational-Bayes LDA (alpha 0.1, beta 0.01) instead of Gibbs sampling; the six severity levels are train-only quantile levels per attribute, not operator-assigned severities.
- CLI `evaluation/aihs/train_moodle_aihs.py`: `split` (from a READY `b2_freeze_dataset.py` manifest, or `--dev-without-independent-labels`), `train` (K in {5,10,20} on validation only), `lock` (writes `model-lock.json`), `propose` (proposals on test snapshots; no labels opened, nothing scored), `repro-check`, `evaluate` (train/validation; test only with a matching lock).
- One shot after the campaign: `bash evaluation/aihs/run_phase2_after_batch.sh`. It stops with exit 3 at the freeze until independent human labels exist; `--dev-without-independent-labels` produces a `DEV_MODEL_NOT_FROZEN_C0` model on runner outcomes.
- A controller is trained for an action only with at least 2 successful and 2 failed executed rows; other actions are recorded as `controller_missing` limitations and their proposals are passed on unvetted and skipped as fallback alternatives.
