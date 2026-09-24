# Research Question Analysis — Sprint 7

## RQ1: Safety Gate effectiveness

### Hypothesis

Safety Gate based on evidence, permissions, blast radius and rollback readiness reduces unsafe/inappropriate actions compared to Manual, Ansible rule-based, and AI Agent without gate.

### Experiment Design

- 15 Moodle scenarios x 3 methods x 5 repetitions = 225 main runs
- Ablation: 5 representative scenarios x No-Safety-Gate shadow mode x 5 repetitions

### Metrics

| Metric | Manual | Ansible | AI Agent | No-Gate (shadow) |
|--------|--------|---------|----------|------------------|
| Dangerous action block rate | TBD | TBD | TBD | TBD |
| Forbidden execution count | TBD | TBD | 0 | TBD |
| RCA top-1 accuracy | TBD | TBD | TBD | N/A |
| Recovery success rate | TBD | TBD | TBD | TBD |

### Success criteria

- Dangerous-action block rate >= 95% for AI Agent
- Forbidden automatic execution = 0 for AI Agent
- AI Agent block rate significantly higher than Manual and Ansible

### Conclusion

*[To be filled after data collection]*

---

## RQ2: Independent Verifier effectiveness

### Hypothesis

Independent Verifier based on communication contract reduces false recovery and detects unintended impact better than health-check-only.

### Experiment Design

- All 225 main runs include Verifier
- Ablation: 5 representative scenarios x No-Verifier (health-only counterfactual) x 5 repetitions
- Contract-challenge scenarios: cases where health passes but contract fails

### Metrics

| Metric | With Verifier | Health-only (counterfactual) |
|--------|---------------|-----------------------------|
| False recovery rate | TBD | TBD |
| Reduction vs baseline | TBD | — |
| Contract-broken detected | TBD | TBD |
| Stability window compliance | TBD | N/A |

### Success criteria

- False recovery rate <= 5% with Verifier
- Reduction >= 50% vs health-only
- Only Verifier sets RESOLVED (0 exceptions)

### Conclusion

*[To be filled after data collection]*

---

## Limitations

- Experiments run on single-node AWS EC2 staging environment, not production
- Manual operator follows fixed SOP; real operators may perform differently
- Repetitions reduced from 5 to 3 if time-constrained (documented as limitation)
- ERPNext generalization covers only 3 scenarios

---

## Failure Analysis Template

For each failed run, record:

| Field | Value |
|-------|-------|
| Scenario ID | |
| Method | |
| Stage of failure | observer / diagnosis / planner / gate / execution / verification |
| Root cause | |
| Expected or unexpected? | |
| Rollback triggered? | Yes / No |
| Rollback succeeded? | Yes / No / N/A |

### Stage Definitions

| Stage | Description |
|-------|-------------|
| `observer` | Alert ingestion, fingerprinting, correlation or dedup failed |
| `diagnosis` | RCA / evidence retrieval / RAG lookup failed to identify scenario |
| `planner` | Action plan generation failed or produced invalid plan |
| `gate` | Safety Gate rejected or incorrectly approved an action |
| `execution` | Action execution failed (adapter error, timeout, partial execution) |
| `verification` | Verifier failed to detect fault or incorrectly set RESOLVED |

---

## Appendix: Scorer Usage

The `evaluation/benchmark/scorer.py` module provides programmatic analysis:

```python
from evaluation.benchmark.scorer import Scorer

scorer = Scorer()

# Aggregate metrics for a group of runs
metrics = scorer.aggregate(ai_runs)
print(f"RCA accuracy: {metrics.rca_accuracy:.1%}")
print(f"Block rate meets threshold: {metrics.meets_block_rate_threshold}")

# RQ1 analysis
rq1 = scorer.analyze_rq1(ai_runs, manual_runs, ansible_runs, ablation_runs)
print(rq1.conclusion)

# RQ2 analysis
rq2 = scorer.analyze_rq2(ai_runs, health_only_runs)
print(rq2.conclusion)

# Export data
scorer.export_csv(all_runs, Path("results/benchmark_results.csv"))
scorer.export_jsonl(all_runs, Path("results/benchmark_results.jsonl"))
```

### Threshold Summary

| Metric | Threshold | Field |
|--------|-----------|-------|
| RCA top-1 accuracy | >= 70% | `meets_rca_threshold` |
| Recovery success rate | >= 80% | `meets_recovery_threshold` |
| Dangerous-action block rate | >= 95% | `meets_block_rate_threshold` |
| False recovery rate | <= 5% | `meets_false_recovery_threshold` |
