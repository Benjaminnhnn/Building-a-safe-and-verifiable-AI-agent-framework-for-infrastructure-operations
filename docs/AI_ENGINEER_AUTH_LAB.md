# Local Moodle and OpenLDAP authentication lab

Updated on 2026-10-07 to use local Floci-backed RDS and ALB. This lab is
isolated from AWS: it uses PostgreSQL, Moodle, OpenLDAP
over LDAPS with a locally generated CA, Prometheus, Alertmanager, the AI API,
Redis, Celery, a scoped recovery actuator, a separate verifier, a TCP/HTTP
blackbox exporter, and a synthetic Moodle login exporter. Only Moodle,
Prometheus, Alertmanager, and the AI API publish loopback ports. OpenLDAP,
PostgreSQL, Redis, verifier, actuator, and worker are not published to host.

The synthetic account and its fixed sample password are test-only. Do not
reuse them outside this disposable lab. `auth-probe` performs a real Moodle
login every 15 seconds with that account; these requests create ordinary
Moodle sessions and login records. The probe never logs credentials.

## First startup

Run these in PowerShell from the repository root. Preparation creates ignored
local secrets, a local CA/server certificate, and Docker images; it does not
touch AWS.

```powershell
& .\automation\auth-lab\start-floci-moodle.ps1
```

The script prepares local secrets and certificates when needed, provisions
Floci resources, and starts PostgreSQL, Moodle, two Moodle web replicas, cron,
OpenLDAP, Redis/Celery, the AI agent, and monitoring. The manual scenario is
documented in `docs/LOCAL_CLOUD_TEST_ENVIRONMENT.md` and runs with:

```powershell
& .\automation\auth-lab\run-auth01-manual.ps1
```

Open `http://127.0.0.1:18082` for interactive Moodle use,
`http://127.0.0.1:19090` for Prometheus, and
`http://127.0.0.1:19093` for Alertmanager. The synthetic test login is
`authlab-user` / `lab-only-password`.

## Multi-terminal AUTH-01 drill

Keep the working directory at `automation/auth-lab`; define `$dc` as above in
each new PowerShell terminal.

**Terminal 1 — OpenLDAP service log**

```powershell
docker compose @dc logs --follow --tail 30 openldap
```

**Terminal 2 — Moodle web log**

```powershell
docker compose @dc logs --follow --tail 30 moodle-web
```

**Terminal 3 — metrics and Alertmanager delivery logs**

```powershell
docker compose @dc logs --follow --tail 30 auth-probe blackbox prometheus alertmanager
```

**Terminal 4 — AI API and worker logs.** The worker output records the live gate decision, fixed action result, and
independent-verifier verdict:

```powershell
docker compose @dc logs --follow --tail 30 ai-agent celery-worker recovery-actuator auth-verifier redis
```

The lab sets `GEMINI_MAX_REMOTE_CALLS=0`; AUTH-01 uses runtime probes, not
Gemini or fixture ground truth. The agent has no Docker socket; the separate
actuator exposes one token-protected restart route for this Compose project's
OpenLDAP container. That actuator still has root-equivalent access to the
local Docker daemon, so it is for the disposable lab only. Telegram
credentials are not configured, so escalation is visible in worker logs and
the SQLite evidence store only.

**Terminal 5 — dashboards.** Watch Moodle health and both alert rules in
Prometheus at `http://127.0.0.1:19090/graph`; useful queries are
`probe_success{job="openldap_ldaps"}`, `probe_success{job="moodle"}`, and
`auth_lab_valid_login_success`. Watch firing/resolved alerts at
`http://127.0.0.1:19093`.

**Terminal 6 — independent verifier.** From the repository root, then keep
this terminal visible for the full 120-second stability window:

```powershell
$env:PYTHONPATH = 'agent_src'
$env:MOODLE_AUTH_PROBE_BASE_URL = 'http://127.0.0.1:18082'
$env:MOODLE_AUTH_TEST_USER_FILE = (Resolve-Path 'automation/auth-lab/secrets/auth-user').Path
$env:MOODLE_AUTH_TEST_PASSWORD_FILE = (Resolve-Path 'automation/auth-lab/secrets/auth-password').Path
$env:MOODLE_AUTH_ALLOW_LOCAL_HTTP = 'true'
python automation/moodle-auth-verify.py
```

First run it against the healthy baseline. Expected evidence is
`valid_login=true`, `invalid_login_denied=true`, `health=true`, and exit code 0.
The probe submits a valid and an intentionally non-existent username at the
start and end, polls health every 15 seconds, and prints only booleans and
timestamps.

**Terminal 7 — operator-owned local fault and reset.** Run only after the
baseline verifier passes:

```powershell
docker compose @dc stop openldap
```

The manual script performs the fault injection and waits for the verifier-owned
decision. It selects an unused Redis DB, checks healthy baselines first, and
writes a JSON evidence artifact under the ignored `automation/auth-lab/secrets`
directory. If the test fails or times out, it restarts OpenLDAP for safety and
reports that the trial failed. Do not manually restart OpenLDAP during the run.

## AUTH-02 local configuration-path drill

This changes only the lab Moodle database setting; it does not alter network
rules. Record the current Moodle setting, set a deliberately unresolvable
lab-only endpoint, observe login failure while the LDAP service remains
healthy, then restore the recorded value:

```powershell
docker compose @dc exec -T moodle-web php /var/www/html/admin/cli/cfg.php --component=auth_ldap --name=host_url
docker compose @dc exec -T moodle-web php /var/www/html/admin/cli/cfg.php --component=auth_ldap --name=host_url --set=ldaps://authlab-unreachable.invalid:636
# Observe alert and verifier result, then reset immediately:
docker compose @dc exec -T moodle-web php /var/www/html/admin/cli/cfg.php --component=auth_ldap --name=host_url --set=ldaps://openldap:636
docker compose @dc exec -T moodle-web php /var/www/html/admin/cli/purge_caches.php
```

Run the independent verifier again after resetting the endpoint. This is a
bounded local configuration fault, not a network partition or AWS security
group drill.

## Cleanup and evidence boundaries

Stop services and retain local database state:

```powershell
docker compose @dc down
```

For a full lab reset, first check that the current directory is
`automation/auth-lab`; then `docker compose @dc down --volumes` deletes only
the Compose project's local named volumes. `prepare.ps1` and the installer
must be run again for a fresh install. Do not delete the `secrets` directory
until the stack is stopped.

Implemented evidence is local live runtime: actual LDAPS certificate
verification succeeded; an LDAP simple bind over port 389 returned
`Confidentiality required`; Moodle installed against PostgreSQL; Moodle stored
the synthetic user with `auth=ldap`; and Prometheus scraped the actual login
and health probes.

Two automatic local AUTH-01 fault/recovery trials were executed on 2026-10-04.
Each trial stopped OpenLDAP, observed real login and LDAPS failures while
Moodle health remained green, delivered the firing alert through
Alertmanager/Redis/Celery, and let the fixed actuator start OpenLDAP. The
independent verifier then passed LDAPS trust, valid login, invalid-login
denial, Moodle health, and 120 seconds of stability across 9 observations.
The first trial exposed a delayed grouped-alert race that downgraded resolved
state; after the fix, the second trial remained `RESOLVED` in Redis after both
stale firing and Alertmanager recovery notifications. No Telegram message is
sent because credentials are absent. This is local-only, not a benchmark or
staging acceptance. The Docker socket actuator is root-equivalent and must not
be promoted to production. AWS deployment, AUTH-02/03 runtime trials, and AUTH
benchmark results remain unverified.

Evidence retention: those trial outcomes are recorded in the dated operator
notes above, but the raw per-trial logs, probe captures, and verifier records
are not present in this checkout. Treat the local-runtime runs as historical
operator-reported evidence; they cannot be independently audited or reproduced
from the retained artifacts here. Do not count them as benchmark observations
or AWS/staging acceptance.
