# Local cloud test environment

This lab combines the existing Moodle/OpenLDAP runtime in
`automation/auth-lab/docker-compose.yml` with Floci as a local AWS control
plane. It uses no AWS account or AWS resources. Terraform in
`terraform/local-floci/` is separately configured with fake credentials and
loopback-only endpoints; the AWS provider cannot be pointed at a remote host.

## What runs locally

- Two Moodle web replicas, OpenLDAP, Redis/Celery, Prometheus, Grafana,
  Alertmanager, Blackbox Exporter, and the AI agent run as Docker Compose
  services.
- Floci provisions an ALB and a real PostgreSQL Docker-backed RDS instance;
  Moodle uses the Floci DB proxy. The ALB health-checks both replicas. Floci
  currently does not provide data-plane cookie stickiness for Moodle login, so
  interactive Moodle use is published directly on the primary node at
  `127.0.0.1:18082`; the second node remains running for component-level checks.
  Moodle sessions use the shared RDS database and its data directory uses a
  shared local Docker volume.
- The existing AUTH-01 lab path observes real probes, stores evidence, applies
  its fixed local recovery gate, restarts only the labeled OpenLDAP service,
  and lets the independent login/health/LDAPS verifier own `RESOLVED`.
- Floci runs on `127.0.0.1:4566`. Terraform provisions a VPC, subnet, security
  group, IAM role/profile, and one Docker-backed EC2 compatibility probe.

Floci EFS is metadata-only and has no NFS data plane, so the Moodle data
directory uses a shared local Docker volume. Floci EC2 instances are Docker
containers; the existing EC2 instance is a compatibility probe, not a Moodle
host, and this lab does not claim guest OS equivalence. Floci security groups
are not used as the lab's packet filter. Docker network boundaries and the
AUTH-01 fixed actuator provide the workload isolation and recovery path.

## Start the complete local stack

From PowerShell at the repository root:

```powershell
& .\automation\select-cloud-profile.ps1 -Target floci -Action start
```

Confirm the local endpoints and workloads:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:4566/_floci/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18081/healthz.php
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18082/login/index.php
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:19090/-/healthy
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:19093/-/healthy
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:13000/api/health
docker compose --env-file automation/auth-lab/.env.local -f automation/auth-lab/docker-compose.yml ps
terraform -chdir=terraform/local-floci output
```

The startup script runs preparation only if local credentials do not exist,
starts Floci, provisions its RDS and ALB resources, installs Moodle only when
the database is empty, starts the whole Compose stack, and registers both
Moodle replicas as ALB targets. It keeps generated credentials under ignored
`.env.local` and `secrets/` files. Open `http://127.0.0.1:18082` for Moodle;
the ALB health endpoint is `http://127.0.0.1:18081/healthz.php`. The legacy
PostgreSQL container is stopped but its volume is retained. Grafana is at
`http://127.0.0.1:13000`; it is local-only, read-only, and opens the provisioned
Moodle AIOps dashboard by default. The dashboard shows Moodle, ALB, LDAPS,
synthetic login, firing AUTH alerts, webhook activity, and Celery queue state.

The profile selector keeps backend choice explicit. Use
`.\automation\select-cloud-profile.ps1 -Target floci -Action show` to inspect
the local profile. The AWS profile requires a named `AWS_PROFILE`, rejects
`AWS_ENDPOINT_URL`, and supports a read-only Terraform plan; applying AWS
resources remains in the reviewed staging workflow.

## Run the local AUTH-01 incident

After the startup checks pass, run the one-scenario manual AUTH-01 script:

```powershell
& .\automation\auth-lab\run-auth01-manual.ps1
```

The script checks Moodle, ALB, LDAPS and valid-login probes before it injects
the fault. It selects an empty Redis database so a previous resolved incident
cannot satisfy this run, recreates the AI worker against that clean state, and
stops only the Compose `openldap` service. On success, the local agent requests
the fixed recovery action and the independent verifier checks valid login,
invalid-login denial, Moodle health, LDAPS, and at least 120 seconds of stable
observations. A JSON evidence file is saved under the ignored
`automation/auth-lab/secrets/` directory. If the run fails or times out, the
script restarts OpenLDAP for safety and reports failure without claiming
verifier resolution.

Observe Prometheus at `http://127.0.0.1:19090`, Alertmanager at
`http://127.0.0.1:19093`, and AI logs with:

```powershell
docker compose --env-file automation/auth-lab/.env.local -f automation/auth-lab/docker-compose.yml logs --since 10m ai-agent celery-worker recovery-actuator auth-verifier
docker compose --env-file automation/auth-lab/.env.local -f automation/auth-lab/docker-compose.yml ps openldap moodle-web moodle-web-b
```

This is a real local Docker runtime drill for the existing AUTH-01 path. It is
not an AWS trial, the full 15-scenario Moodle acceptance suite, or a thesis
benchmark observation. Other Moodle alerts still use the current fail-closed
shadow/investigate path and do not execute remediation.

## Telegram incident notifications and admin feedback

The bot is an incident notifier and admin-feedback endpoint, not a general
chatbot. The outbound path is `Alertmanager -> /webhook -> Celery -> incident
result -> Telegram sendMessage`. The inbound path is Telegram webhook to
`/telegram/webhook`; it accepts `/feedback <incident_id> <comment>` only from a
configured chat and allowlisted Telegram user. Feedback is reviewed and may be
saved to local incident memory; it does not directly execute a remediation.

The AI Agent and Celery worker load `agent_src/.env` (ignored by Git). For
outbound notifications, set these there:

```dotenv
TELEGRAM_TOKEN=<token from BotFather>
TELEGRAM_CHAT_ID=<your Telegram private chat ID or group chat ID>
```

Start the bot and send `/start` before testing a private chat. To receive
`/feedback` commands, also set `TELEGRAM_ADMIN_USER_IDS` to the numeric Telegram
user ID(s) allowed to submit feedback. Configure `TELEGRAM_WEBHOOK_SECRET` as a
random 1-256 character value using only letters, digits, `_` or `-`. Telegram
cannot call localhost, so `AI_AGENT_PUBLIC_URL` must be the HTTPS URL of a
public tunnel that forwards to `http://127.0.0.1:18000`. On AI-agent startup,
the app registers `https://<tunnel-host>/telegram/webhook` with Telegram.

After editing `agent_src/.env`, recreate both containers so the sender gets the
token/chat ID and the API registers the webhook:

```powershell
docker compose --env-file automation/auth-lab/.env.local -f automation/auth-lab/docker-compose.yml up -d --force-recreate ai-agent celery-worker
```

The pasted AUTH-01 run reached `RESOLVED` with independent verifier proof, but
its Celery log reported missing Telegram variables. That run proves recovery
and verification; it does not prove Telegram delivery. Never paste the bot
token or webhook secret into chat or commit `.env.local`.

## Recorded local run

On 2026-10-07, the local AUTH-01 fault drill stopped OpenLDAP and triggered the
Prometheus alert while Moodle health remained available. The actuator restarted
the labeled OpenLDAP container. The independent verifier passed valid-login,
invalid-login denial, Moodle health, LDAPS, and 122 seconds of stability across
9 observations, then marked the incident `RESOLVED` with
`resolution_authority=independent_verifier`. Prometheus probes returned healthy
and Alertmanager had no firing alerts afterward. The persisted evidence records
and hashes were checked in the local evidence store. This confirms this single
local AUTH-01 flow only; it does not establish AWS behavior or general Moodle
incident remediation.

## Stop and reset

```powershell
terraform -chdir=terraform/local-floci destroy -auto-approve
docker compose --env-file automation/auth-lab/.env.local -f automation/auth-lab/docker-compose.yml down
```

`down` retains named volumes. To erase this lab's databases and evidence as
well, run `docker compose --env-file automation/auth-lab/.env.local -f
automation/auth-lab/docker-compose.yml down --volumes` only when intentionally
resetting the lab. Terraform state and Floci storage are local and ignored; do
not copy them into benchmark evidence.
