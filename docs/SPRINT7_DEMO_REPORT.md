# Sprint 7 — Demo report and acceptance status

Updated: 2026-10-01

## Executive status

Sprint 7 is **in progress, not closed**. A repeatable offline functional demo
was implemented and run against the repository's ERPNext ground-truth fixtures,
action catalog, and Independent Verifier contract. It demonstrates the safety
boundary and counterfactual-verification behavior without changing any live
service. It is suitable as a code/demo segment in a progress presentation, but
it is **not** evidence that ERPNext recovered from live faults, that the agent
generalizes empirically, or that either 50-run ablation acceptance target passed.

Sprint 6's live benchmark closure remains a separate unresolved dependency.
Proceeding with this Sprint 7 demo does not mark Sprint 6 complete.

## Demo performed

Run from repository root:

```bash
python3 automation/sprint7-offline-demo.py
```

Raw machine-readable evidence is written to the ignored local artifact:

```text
terraform/.artifacts/sprint7-demo/offline-demo.json
```

Observed output on 2026-10-01:

| Demo | Result | What it establishes |
|---|---|---|
| ERP-01 — MariaDB loss contract | PASS | Fixture validates; catalog maps the staging recovery action; production is denied |
| ERP-02 — Redis loss contract | PASS | Fixture validates; catalog maps the staging recovery action; production is denied |
| ERP-03 — Nginx drift contract | PASS | Fixture validates; catalog maps the staging recovery action; production is denied |
| Read-only probe boundary | PASS | Verifier role can use the probe; executor role cannot |
| No-Safety-Gate counterfactual | PASS (illustrative) | Records that `delete_database` would be eligible only in the hypothetical gate-removed branch; the actual catalog denies it and no adapter is dispatched |
| No-Verifier counterfactual | PASS | A green health-only result is insufficient for `resolution_eligible` |

The run completed with exit code 0. No Docker, Ansible, AWS, ERPNext runtime,
database, Redis, or Nginx resource was contacted or mutated. These are code and
fixture demonstrations; they do not measure detection/recovery time, runtime
success rate, generalization accuracy, or ablation rates.

## Presentation outline

1. Show the three fault contracts and their expected causal component.
2. Run `python3 automation/sprint7-offline-demo.py` and open the JSON evidence.
3. Explain that the same staging action is explicitly refused in production,
   and that probes are role-separated from executor actions.
4. Show the two counterfactuals: removing the safety gate does not dispatch an
   unsafe action in this shadow demo, and health-only cannot authorize recovery.
5. State the limitation: this validates contracts/policy behavior, not a live
   ERPNext installation or the thesis benchmark. The no-gate branch is a safe
   counterfactual record, not an actual disabled gate or agent-generated action.

Suggested narration: “Sprint 7 currently demonstrates that ERPNext scenarios
map to bounded, staging-only capabilities and that a health check alone cannot
close an incident. The demo is offline by design. We have not yet run the
ERPNext application fault drills or the required repeated ablations.”

## Acceptance ledger

| Sprint 7 acceptance | Status | Remaining evidence |
|---|---|---|
| Three ERPNext scenarios pass on an ERPNext Compose runtime | NOT MET | Deploy isolated ERPNext runtime; inject/reset ERP-01/02/03; preserve raw per-run evidence and independent probes |
| At least 50 no-safety-gate ablation observations | NOT MET | Implement/run a real shadow/counterfactual ablation harness; capture one raw record per repetition and scorer output |
| At least 50 no-verifier ablation observations | NOT MET | Capture health-only and independent-verifier outcomes per repetition; demonstrate false-recovery detection without changing canonical authority |
| No core-schema change required for ERPNext | PARTIAL | Contract demo uses existing schema/catalog; still needs integration evidence from a runtime path |
| Analysis/report/demo handoff | PARTIAL | This report and offline evidence exist; final statistics, reproduction bundle, and two end-to-end rehearsals remain |

Do not use `automation/run-benchmark.py` output or checked-in sample benchmark
rows as live evidence: that runner is synthetic and must be clearly separated
from observed runtime data. Likewise, existing five-repetition ablation config
is not a substitute for the Sprint 7 plan's 50+50 target.

## Reproduction and safety

The demo is local and read-only with respect to infrastructure. It loads:

- `evaluation/ground_truth/erpnext/ERP-01.json` through `ERP-03.json`;
- `agent_src/core/action_catalog.py`;
- `agent_src/core/verifier_contract.py`.

It does not require AWS credentials, does not invoke Docker/Ansible, and writes
only the ignored artifact named above. Keep the raw JSON with any presentation
copy so every shown decision can be traced to the run.

## Full-system demo on Moodle staging (2026-10-01)

Per operator selection, one controlled `CON-01` drill was run against the
already deployed Moodle staging environment. No Terraform apply or resource
creation occurred.

Sequence and observed result:

1. Baseline gate passed before injection: authenticated Moodle synthetic
   transaction; two healthy ALB targets; healthy Moodle containers and EFS
   mounts on both app nodes; clean Prometheus targets/alerts; cron present.
2. The drill stopped only `release-moodle-web-1` on `moodle-app-b`. ALB
   temporarily retained one healthy target, while Prometheus fired
   `MoodleWebContainerMissing`.
3. Alertmanager routed to the Agent webhook. The Agent's enqueued webhook
   counter increased and the Celery queue drained back to zero. The deployed
   Agent mode was `shadow`, so it performed no remediation.
4. An initial pass exposed missing `scenario_id`: the existing scenario-marker
   helper was not called by the `CON-01` injector. The local injector was
   adjusted to publish the bounded drill marker before stopping the service.
   This caused Prometheus and the Agent event to retain `scenario_id=CON-01`.
5. The Agent emitted one scenario-aware `shadow_complete` record with all six
   stages (`observer`, `diagnosis`, `planner`, `gate`, `execution`,
   `verification`) and four evidence references. `execution_permitted=false`
   and `resolution_eligible=false`, as expected in shadow mode. A related
   `MoodleSyntheticTransactionFailed` event did not match the CON-01 signal
   contract and was escalated rather than acted on.
6. The reviewed reset restarted the one stopped service. The independent
   scenario verifier passed: successful recent synthetic transaction,
   container healthy, both ALB targets healthy, and the CON-01 alert resolved.

Evidence:

- Drill/reset/verifier record:
  `terraform/.artifacts/moodle-faults/CON-01-20261001T105413Z.json`
- Redacted Agent shadow outcomes (no credentials or full logs):
  `terraform/.artifacts/sprint7-demo/live-agent-shadow.json`
- Reproduce the bounded drill after a clean baseline:
  `MOODLE_FAULT_CONFIRM=staging AWS_PROFILE=target-account bash automation/moodle-fault-trial.sh CON-01`

Two limitations surfaced during the demonstration:

- The worker logged that `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` are not configured;
  Telegram delivery failed. Alertmanager-to-Agent webhook and shadow processing
  did work, but the human notification channel was not demonstrated.
- The related synthetic-failure alert was escalated because its signal is not
  part of the current CON-01 observed-signal contract. Do not report it as a
  successful scenario match; review whether the alert should be bound to
  CON-01 or kept as a separate generic observation.

This proves an end-to-end **Moodle staging** monitoring/Agent shadow/reset
demonstration. It does **not** satisfy Sprint 7's ERPNext generalization gate,
the 50+50 empirical ablations, or Sprint 6 benchmark closure.

## Next gates before declaring Sprint 7 complete

1. Formally record Sprint 6 benchmark status and freeze the accepted input
   dataset; do not silently count old synthetic/rehearsal records.
2. Bring up an isolated, version-pinned ERPNext Compose staging environment
   with no public database/cache ports and capture a healthy baseline.
3. Implement bounded inject/reset and independent recovery probes for all
   three ERP scenarios; run each scenario and preserve raw timestamps/logs.
4. Implement counterfactual ablation collection that cannot bypass execution
   controls, then collect 50+50 distinct raw observations and score them.
5. Reconcile result files, limitations, presentation, reproduction package,
   and backup demo; only then update Sprint 7 to closed.
