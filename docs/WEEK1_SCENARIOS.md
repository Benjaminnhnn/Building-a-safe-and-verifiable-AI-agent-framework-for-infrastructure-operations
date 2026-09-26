# Week 1 scenario checklist

Status: five scenario definitions and injector/reset scripts exist; Bash syntax passed; runtime trials are NOT RUN

Sprint 1 requires one scoped inject/reset smoke trial for each reviewed
scenario. Each trial must start from a clean baseline and finish with baseline
verification.

| Scenario | Ground truth | Injector/reset | Required evidence | Status |
|---|---|---|---|---|
| DB-01 | `evaluation/ground_truth/moodle/DB-01.json` | `moodle-fault-inject.sh DB-01` / `moodle-fault-reset.sh DB-01` | alert, recovery, clean baseline | pending manual run |
| RES-01 | `evaluation/ground_truth/moodle/RES-01.json` | `moodle-fault-inject.sh RES-01` / `moodle-fault-reset.sh RES-01` | alert, recovery, clean baseline | pending manual run |
| NET-01 | `evaluation/ground_truth/moodle/NET-01.json` | `moodle-fault-inject.sh NET-01` / `moodle-fault-reset.sh NET-01` | alert, recovery, clean baseline | pending manual run |
| CON-01 | `evaluation/ground_truth/moodle/CON-01.json` | `moodle-fault-inject.sh CON-01` / `moodle-fault-reset.sh CON-01` | alert, recovery, clean baseline | pending manual run |
| SEC-02 | `evaluation/ground_truth/moodle/SEC-02.json` | `moodle-fault-inject.sh SEC-02` / `moodle-fault-reset.sh SEC-02` | alert, recovery, clean baseline | pending manual run |

Run the smoke trials only after a clean baseline:

```bash
for scenario in DB-01 RES-01 NET-01 CON-01 SEC-02; do
  bash automation/moodle-fault-trial.sh "$scenario"
done
```

Do not rerun an individual injector after a failure. Verify or reset the
baseline according to `docs/MOODLE_SPRINT_RUNBOOK.md` first. Store only
secret-free result artifacts under `terraform/.artifacts/`.
