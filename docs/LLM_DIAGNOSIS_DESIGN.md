# Evidence-cited LLM diagnosis (reviewed design)

This is the reviewed design that AGENTS.md requires before a model call is placed on the alert path. It covers the diagnosis stage only. Planning, the Safety Gate, approvals, the executor and the Independent Verifier have no LLM in them, and this design does not change them.

## Position in the pipeline

```
Alertmanager -> evidence collection -> rule diagnosis ----------------+
                                     \-> LLM hypothesis -> citation   |-> mode combiner -> planner (catalog only)
                                         (Gemini, bounded)  validator |      -> Safety Gate -> approvals -> executor
                                                                      |      -> Independent Verifier
```

Code: `agent_src/core/reasoning/` (`cause_vocabulary.py`, `citation_validator.py`, `llm_client.py`, `llm_diagnosis.py`). It is wired into `core/shadow_pipeline.py` for the Moodle adapter only.

## Modes (`AIOPS_DIAGNOSIS_MODE`)

| Mode | Who decides | Model calls | Runtime |
|---|---|---|---|
| `rules` (default) | Reviewed rule tables | 0 | yes |
| `llm` | LLM, only if the validator accepts its hypothesis; otherwise `insufficient_evidence` | 1 per incident | yes |
| `hybrid` | Rules whenever they decide; the validated LLM only when rules return `insufficient_evidence`. `conflicting_evidence` is never overridden | 0 or 1 | yes |
| `llm_unvalidated` | Top LLM hypothesis without citation checks; the validator verdict is kept as a counterfactual | 1 | **no**, ablation only. Runtime falls back to `rules` |

An unset, invalid or ablation-only value falls back to `rules`. A missing `GEMINI_API_KEY`, a timeout or a provider error also fails closed: `llm` returns `insufficient_evidence`, and `hybrid` keeps the rule result.

## Model contract

- **Input.** The input is decoded Prometheus samples only: `evidence_id`, `query_id`, `instance`, `value` and age. Labels are passed through the secret sanitizer; raw log text and alert annotations are not passed. The input also includes the closed cause vocabulary with each cause's required signals, plus runbook chunks (`runbook_chunks_untrusted`: `chunk_id`, source file, heading and sanitized text, at most 3000 characters in total). Every data field is labelled `_untrusted`.
- **Output.** The output is one JSON object (`status`, up to 3 `hypotheses`, `abstain_reason`). Each hypothesis has a `cause_code`, a `confidence`, a `rationale`, `citations` (`evidence_id`, `query_id`, `instance`, `value`) and optional `runbook_refs` (chunk IDs).
- **Bounds.** Each call has temperature 0, no tools (`maximum_remote_calls=0`), 1024 output tokens, a hard timeout (`AIOPS_LLM_DIAGNOSIS_TIMEOUT_SECONDS`, default 20 s, limited to 1–60 s) and at most 2 concurrent calls.

## Citation validator (deterministic)

A hypothesis is accepted only if all of the checks below pass. The first fully cited hypothesis is selected, and any failure fails closed.

| Code | Check |
|---|---|
| `output_not_json`, `schema_invalid` | strict schema (`extra="forbid"`) |
| `action_outside_diagnosis_scope` | no `action`/`command`/`remediation`/`script`/`rollback` keys at any depth |
| `unknown_cause_code` | cause is in the closed vocabulary derived from the rule tables |
| `confidence_out_of_range`, `no_citations` | basic sanity |
| `citation_not_found` | the cited evidence ID exists in the stored set (anti-hallucination) |
| `citation_source_not_allowed` | the cited evidence is a decodable Prometheus sample |
| `citation_query_mismatch`, `citation_instance_mismatch`, `citation_value_mismatch` | the claimed query, instance and value equal the stored sample |
| `citation_conflicts_with_stored_sample` | no other stored sample for the same query and instance has a different value, so a model cannot pick one side of contradictory telemetry |
| `runbook_ref_not_found` | every cited runbook chunk was actually shown to the model |
| `citation_stale`, `citation_window_too_wide` | each sample is at most 120 s old and all cited samples fall in one 60-second window |
| `required_signal_not_cited` | every required signal of the cause is cited |
| `signal_predicate_not_satisfied` | the stored samples satisfy the cause predicate. `all_eq` predicates are checked against *every* stored sample, so a model cannot cherry-pick a healthy node |

## Runbook retrieval (RAG) and knowledge links

- **Chunking.** `core/rag/runbook_chunks.py` holds the heading-aware chunking shared with the ChromaDB engine. It has no ChromaDB dependency. Each chunk has a stable `chunk_id` (SHA-256 of file, index and text) and provenance: source file, heading, chunk index, file SHA-256 and chunk SHA-256.
- **Source of chunks.** The diagnosis prompt uses the vector store's alert-scoped hits when `VECTOR_DB_PATH` is set. Otherwise it uses the deterministic `StaticRunbookRetriever` over `config/knowledge_base/`. `AIOPS_DIAGNOSIS_RAG=off` removes runbooks from the prompt; this is the RAG ablation switch.
- **Knowledge links.** Every persisted diagnosis decision carries `knowledge_links`:
  - the conclusion (status and cause);
  - which source decided;
  - the supporting evidence IDs;
  - `runbook_refs`: only the chunks an accepted LLM hypothesis cited, as provenance without text;
  - `runbook_context`: every chunk that was available.

  Rule decisions never use runbook text, so their `runbook_refs` is empty.

## What an accepted LLM hypothesis can and cannot do

- The operator-facing `hypothesis` text is the canonical vocabulary description, not model text. The model rationale is stored only in the sanitized audit block.
- Confidence is capped at 0.75.
- The planner still maps only vocabulary cause codes to catalog actions. A remediation candidate still requires the exact fresh fault markers in `plan_moodle_s3_3_remediation`, so a validated LLM diagnosis alone cannot produce a mutation candidate.
- A cached LLM decision is re-validated against current evidence without a new model call.
- Each persisted diagnosis decision records `reasoning` (mode, deciding source, rule baseline, model usage, violation codes, selected hypothesis).
- Metrics: `aiops_llm_diagnosis_events_total{mode,source}`, `aiops_llm_citation_violations_total{code}`, and `aiops_gemini_call_duration_seconds{operation="evidence_diagnosis"}`.

## Offline evaluation

`evaluation/benchmark/diagnosis_ablation.py` uses 12 synthetic evidence cases: the 7 thesis scenarios, 2 variants (BIZ-01 with an upgrade in progress, and a synthetic failure caused by a failed RDS probe), and 3 cases where the correct outcome is to abstain (multiple failing dependencies, stale samples, and a firing CPU alert contradicted by its samples).

It compares six conditions: `rules`, `llm`, `llm_rag`, `hybrid`, `hybrid_rag` and `llm_unvalidated` (which also gets RAG).

- **Accuracy.** Root-cause accuracy, correct-abstain rate, false-diagnosis rate, and how often accepted hypotheses cite a runbook.
- **Stability** (`--repeats N`). The share of repeats per case that reach the modal conclusion (status and cause), plus the share of fully consistent cases.
- **Robustness.** Cases with a known cause are re-run under four perturbations:
  - extra irrelevant telemetry;
  - a contradictory duplicate of the key signal (the correct answer becomes abstain);
  - prompt injection with a secret in a scraped label;
  - a poisoned runbook chunk with a secret.

  The experiment also counts secrets that reached any prompt.
- **Cost.** Model calls, prompt and output tokens, mean and p95 latency, and estimated USD from `--usd-per-mtok-in/out`. The same rows are fed to `Scorer.aggregate` through `llm_usage_run_fields`; `ScenarioScore` and `AggregateMetrics` now carry `llm_prompt_tokens`, `llm_output_tokens`, `total_llm_tokens` and `avg_llm_latency_seconds`.
- **Adversarial.** Eight fixed faulty model outputs per case (96 trials) are run through `llm` and `llm_unvalidated`: hallucinated evidence, a wrong value, an unknown cause, action injection, an uncited signal, a wrong cause on the same citations, obeying the prompt injection, and a hallucinated runbook reference.

Scripted results (wiring only, `simulated: true`):
- Adversarial: the validator blocked 96/96; without it, 84/96 passed. Action injection is rejected by the schema guard in both modes.
- Robustness: no validated condition produced a false diagnosis under any perturbation, and 0 secrets reached a prompt.
- Contradiction: the unvalidated condition accepted every contradicted case, because the scripted oracle ignores contradictions.

### RQ1: unconstrained LLM planner with and without the gate

`evaluation/benchmark/llm_gate_ablation.py` is ablation-only; the runtime planner never asks a model for actions.

- **Planner.** The model is asked to propose remediation freely, either catalog IDs or any command.
- **Gate.** Each proposal goes through the same Safety Gate decision as `dangerous_injection.gate_allows`.
- **Ground truth.** A proposal is *reviewed* only when it equals the scenario's allowed action and scope in `THESIS_SCENARIOS`. For SEC-01 (human-only), every proposal is inappropriate.
- **Output.** The report uses the dangerous-injection field names (`dangerous_action_opportunity_count`, `dangerous_action_blocked_count`, `forbidden_execution_count`) for `llm_no_gate` and `llm_with_gate`, plus how many reviewed proposals the gate falsely blocked.
- **Scripted naive plan:** with no gate, 25/25 unsafe proposals would execute; with the gate, 0/25 execute and the 6 reviewed proposals stay allowed.

```bash
PYTHONPATH=agent_src python -m evaluation.benchmark.diagnosis_ablation --client scripted --repeats 2
GEMINI_API_KEY=... PYTHONPATH=agent_src python -m evaluation.benchmark.diagnosis_ablation --client gemini --repeats 5   --usd-per-mtok-in <price> --usd-per-mtok-out <price> --output terraform/.artifacts/llm-diagnosis/report.json
GEMINI_API_KEY=... PYTHONPATH=agent_src python -m evaluation.benchmark.llm_gate_ablation --client gemini --repeats 5   --output terraform/.artifacts/llm-diagnosis/gate.json
```

These are offline results on synthetic evidence. They do not show live recovery, and the Gemini runs have not been executed yet.
