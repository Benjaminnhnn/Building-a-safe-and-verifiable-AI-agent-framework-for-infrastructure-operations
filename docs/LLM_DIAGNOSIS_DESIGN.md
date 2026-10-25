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

- **Input.** The input is decoded Prometheus samples only: `evidence_id`, `query_id`, `instance`, `value` and age. Raw log text and alert annotations are not passed. The input also includes the closed cause vocabulary with each cause's required signals, plus the sanitized runbook context (at most 3000 characters). Every data field is labelled `_untrusted`.
- **Output.** The output is one JSON object (`status`, up to 3 `hypotheses`, `abstain_reason`). Each hypothesis has a `cause_code`, a `confidence`, a `rationale` and `citations` (`evidence_id`, `query_id`, `instance`, `value`).
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
| `citation_stale`, `citation_window_too_wide` | each sample is at most 120 s old and all cited samples fall in one 60-second window |
| `required_signal_not_cited` | every required signal of the cause is cited |
| `signal_predicate_not_satisfied` | the stored samples satisfy the cause predicate. `all_eq` predicates are checked against *every* stored sample, so a model cannot cherry-pick a healthy node |

## What an accepted LLM hypothesis can and cannot do

- The operator-facing `hypothesis` text is the canonical vocabulary description, not model text. The model rationale is stored only in the sanitized audit block.
- Confidence is capped at 0.75.
- The planner still maps only vocabulary cause codes to catalog actions. A remediation candidate still requires the exact fresh fault markers in `plan_moodle_s3_3_remediation`, so a validated LLM diagnosis alone cannot produce a mutation candidate.
- A cached LLM decision is re-validated against current evidence without a new model call.
- Each persisted diagnosis decision records `reasoning` (mode, deciding source, rule baseline, model usage, violation codes, selected hypothesis).
- Metrics: `aiops_llm_diagnosis_events_total{mode,source}`, `aiops_llm_citation_violations_total{code}`, and `aiops_gemini_call_duration_seconds{operation="evidence_diagnosis"}`.

## Offline evaluation

`evaluation/benchmark/diagnosis_ablation.py` uses 12 synthetic evidence cases: the 7 thesis scenarios, 2 variants (BIZ-01 with an upgrade in progress, and a synthetic failure caused by a failed RDS probe), and 3 cases where the correct outcome is to abstain (multiple failing dependencies, stale samples, and a firing CPU alert contradicted by its samples).

- **Accuracy experiment.** Every case is run through each mode. The scores are root-cause accuracy, correct-abstain rate, false-diagnosis rate, model calls, tokens and latency. Use `--client gemini --repeats 5` for the real model. The scripted oracle only checks the wiring, and its report is marked `simulated: true`.
- **Adversarial experiment.** Six fixed faulty model outputs per case (72 trials) are run through `llm` and `llm_unvalidated`. Scripted result: the validator blocked 72/72, and without it 60/72 unsupported diagnoses were accepted. The other 12 are action-injection outputs, which the schema guard rejects in both modes.

```bash
PYTHONPATH=agent_src python -m evaluation.benchmark.diagnosis_ablation --client scripted
GEMINI_API_KEY=... PYTHONPATH=agent_src python -m evaluation.benchmark.diagnosis_ablation --client gemini --repeats 5 --output terraform/.artifacts/llm-diagnosis/report.json
```

These are offline diagnosis-layer results on synthetic evidence. They do not show live recovery, and the Gemini run has not been executed yet.
