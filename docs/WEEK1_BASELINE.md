# Week 1 baseline checklist

Status: offline configuration checks completed; staging baseline not run

This checklist records the evidence required for Sprint 1. It intentionally
contains no fabricated runtime result. Run the commands from the repository
root after confirming the staging target and credentials.

## Required baseline

| Check | Command or evidence | Status |
|---|---|---|
| Terraform validation | `terraform -chdir=terraform validate` | PASS (local) |
| Terraform tests | `terraform -chdir=terraform test` | PASS (19/19 local) |
| Ansible inventory reachability | `ansible -i terraform/.artifacts/moodle-inventory.yml all -m ping` | NOT RUN (Ansible unavailable) |
| Moodle ALB health | `curl --fail --silent <Moodle URL>/healthz.php` | NOT RUN (AWS credentials unavailable) |
| Both ALB targets healthy | `moodle-environment-baseline.sh verify` | NOT RUN |
| Prometheus targets up and no active alerts | `moodle-environment-baseline.sh verify` | NOT RUN |
| Synthetic transaction success | `moodle-environment-baseline.sh verify` | NOT RUN |
| Moodle cron running | `moodle-environment-baseline.sh verify` | NOT RUN |
| EFS mounted on both nodes | `moodle-environment-baseline.sh verify` | NOT RUN |
| Baseline manifest retained | `terraform/.artifacts/moodle-experiment-baseline/manifest.json` | NOT RUN |

Compose configuration was validated for Moodle, staging, and production, but no image was built and no container was started because Docker Engine is unavailable.

## Soak evidence

Run only after the baseline passes:

```bash
bash automation/moodle-synthetic-soak.sh start --duration-seconds 7200
bash automation/moodle-synthetic-soak.sh status
bash automation/moodle-synthetic-soak.sh collect
```

Acceptance requires a completed two-hour authenticated soak with secret-free
output and no unexplained failed transactions.

## Restore evidence

If restore is included in the Sprint 1 acceptance package:

```bash
bash automation/moodle-rds-restore-drill.sh
```

Retain the JSON result and verify that the temporary restored database was
removed before closing the task.
