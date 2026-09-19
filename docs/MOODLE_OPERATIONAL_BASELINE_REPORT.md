# Moodle Operational Baseline Report

## Scope

This report records the completion work for the Moodle operational baseline:
Apache routing, cron scheduling, Terraform convergence, an RDS restore drill,
and secret-free monitoring evidence. Runtime secrets and Terraform state are
not included in this report or in Git.

## 1. Apache Moodle Router

### Initial error

`admin/cli/checks.php` reported `CRITICAL: Router configuration`. Apache used
`/var/www/html/public` as its document root but did not define a fallback for
virtual Moodle routes. Direct PHP URLs worked while deep links returned the
generic Apache 404 response.

### Remediation

- Added an Apache `Directory` block for `/var/www/html/public`.
- Configured `DirectoryIndex index.php` and `FallbackResource /r.php`.
- Enabled the configuration during the image build and ran `apache2ctl -t`.
- Set `$CFG->routerconfigured = true` only in the image that contains the
  working Apache configuration.

### Verification

- Local Docker build: passed.
- Apache configuration test: `Syntax OK`.
- Live deep-link and Moodle CLI results are recorded after rolling deployment.

## 2. Moodle Cron Schedule

### Initial error

Moodle reported a gap of approximately four minutes between cron starts. The
site setting `cron_keepalive` was 180 seconds, and the container wrapper added
another fixed 60-second sleep after `cron.php` returned.

### Remediation

- Invoke `cron.php --keep-alive=0` so one wrapper iteration is one cron run.
- Measure execution time and sleep only the remaining part of the configured
  60-second interval.
- If a run itself exceeds 60 seconds, start the next run immediately instead
  of adding another sleep.

### Verification

The live cron log and `admin/cli/checks.php` result are recorded after rolling
deployment and an observation period.

## 3. Terraform Convergence

### Initial errors

- `rds.force_ssl` was configured with `apply_method = "immediate"`, while AWS
  reports this static parameter as `pending-reboot`.
- `deployment.tfvars` did not pass `terraform fmt -check`.
- Two unattached legacy security groups still used an obsolete administrator
  CIDR.

### Remediation and result

- Changed `rds.force_ssl` to `pending-reboot`.
- Formatted `deployment.tfvars`.
- Reviewed and applied a saved plan containing only the two legacy security
  group CIDR updates.
- Apply result: `0 added, 2 changed, 0 destroyed`.
- Follow-up plan result: `No changes. Your infrastructure matches the configuration.`
- Terraform test result: 19 passed, 0 failed.

## 4. RDS Restore Drill

The restore drill uses the latest manual snapshot unless a snapshot identifier
is supplied explicitly. It restores a private temporary `db.t4g.micro`, uses
the existing RDS security group and subnet group, verifies a TLS connection as
`moodle_app`, records secret-free JSON evidence, and deletes the temporary DB.

Run:

```bash
bash automation/moodle-rds-restore-drill.sh
```

The execution result is stored outside Git at:
`terraform/.artifacts/moodle-baseline/rds-restore-drill.json`.

## 5. Baseline Evidence

Run:

```bash
bash automation/capture-moodle-baseline.sh
```

The command records ALB target health, RDS status and manual snapshots, EFS
status and backup policy, both Moodle runtimes, public endpoint status,
Prometheus targets and alerts, and provisioned Grafana health/dashboard data.
It does not record database, Moodle administrator, GHCR, or Grafana passwords.

Evidence is stored under `terraform/.artifacts/moodle-baseline/`, which is
ignored by Git.

## Final Acceptance

The baseline is complete only when all conditions below pass:

- Both ALB targets are healthy.
- Moodle health, login, and a routed deep link work through the ALB.
- Moodle CLI no longer reports router or cron scheduling errors.
- Terraform validation and tests pass and plan reports `No changes`.
- The RDS restore drill reports `passed` and confirms cleanup `deleted`.
- Prometheus has no down targets or firing baseline alerts.
- Grafana reports database `ok`, Prometheus datasource `OK`, and the Moodle
  dashboard is provisioned.
