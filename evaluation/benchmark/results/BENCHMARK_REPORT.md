# Benchmark evidence status

Checked 2026-10-07. This directory contains only this status report; no empirical Moodle benchmark dataset is present. The latest default n=5 Sprint 6 acceptance audit found **0/225 empirical runs**; no RQ1/RQ2 metric or conclusion is established.

The former committed CSV/JSONL and summary were outputs of `automation/run-benchmark.py`, a randomized fixture simulation. They were removed from this active results directory because they were not observations from Moodle, human operators, Ansible, or the Independent Verifier. Two ignored SQLite evidence stores contained only synthetic Alertmanager/Prometheus/topology fixture rows; they were moved to `terraform/.artifacts/legacy-synthetic-results/` and are not empirical data. The CSV/JSONL historical row-count mismatch is recorded in `docs/MASTER_RUNBOOK.md`; no synthetic outcome rates are retained here.

For harness development only, the runner writes to ignored local artifacts by default:

Every legacy `RunResult` row and its recorder summary now carry the fixed
classification `synthetic_simulation_not_empirical`. That schema cannot
represent `empirical_live`; live acceptance uses the separate provenance and
evidence-manifest gate.

```powershell
python automation/run-benchmark.py --synthetic-smoke --output-dir terraform/.artifacts/sprint6-benchmark-simulation
```

Treat any output from that command as `synthetic_simulation_not_empirical`. Do not place it in the live benchmark dataset or use it for thesis findings. Live records must go through the producer, provenance, and acceptance requirements in `automation/sprint6-acceptance.py` before analysis.
