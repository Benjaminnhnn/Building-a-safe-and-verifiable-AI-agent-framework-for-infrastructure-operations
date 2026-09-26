# AGENTS.md

Repo-specific guidance for AI agents working in this codebase. Verify against config before trusting prose.

## Critical: Markdown and sensitive files

`.gitignore` ignores `*.md` by default, but explicitly allows `README.md`, `AGENTS.md`, and files under `docs/` (including `docs/AIops_CICD.md`). Keep the allow-list in sync when adding Markdown outside those paths.

Never commit: `agent_src/vector_db/` (ChromaDB runtime data), any `.env*` file, `terraform.tfstate*`, `*.pem`/`*.pub`/`id_rsa*`.

## Commands

### Verify before pushing (mirrors `.github/workflows/ci.yml`)

CI runs lint -> AI-agent tests -> automation validation -> Docker builds -> compose config validation. Run locally in this order:

```bash
# 1. Lint — critical-rule subset only (undefined names, syntax)
ruff check agent_src --select E9,F63,F7,F82

# 2. Tests — use the agent package on PYTHONPATH; httpx must be <0.28
pip install -r agent_src/requirements.txt
pip install pytest ruff "httpx<0.28"

PYTHONPATH=agent_src            pytest -q agent_src/tests

# Validate automation scripts used by CI
python -m py_compile automation/moodle-synthetic-transaction.py automation/moodle-pipeline-replay.py
bash -n automation/lib/moodle-fault-common.sh automation/moodle-environment-baseline.sh
bash -n automation/moodle-fault-inject.sh automation/moodle-fault-reset.sh automation/moodle-fault-trial.sh
bash -n automation/moodle-synthetic-soak.sh automation/moodle-sprint2-live-drill.sh

# 3. Build the release images
docker build -t local/ai-agent:ci agent_src
docker build -t local/moodle:ci moodle

# 4. Validate release compose (CI copies .env.example to .env.staging/.env.production first)
cp release/.env.example release/.env.staging
cp release/.env.example release/.env.production
docker compose -f release/docker-compose.staging.yml    config > /dev/null
docker compose -f release/docker-compose.production.yml config > /dev/null
MOODLE_RUNTIME_ENV_FILE=./moodle-runtime.env.example \
docker compose --env-file release/moodle/moodle-runtime.env.example \
	-f release/moodle/docker-compose.yml config > /dev/null
```

### Single test

```bash
PYTHONPATH=agent_src pytest -q agent_src/tests/test_alert_dedup.py::test_name
```

### Local dev stack (not a release path)

```bash
docker compose -f platform-config/docker-compose.dev.yml up -d
```

Brings up the monitoring and AI-agent development services. Dev port mapping differs from staging/production ports (see `platform-config/docker-compose.dev.yml`).

## Architecture

Four layers; each has its own tooling and must not be conflated:

1. **Terraform** (`terraform/`) — AWS VPC/subnets/SG/EIP and three EC2: `monitor-ai-01`, `bank-web-01`, `bank-core-01`. State in `terraform.tfstate` (never commit).
2. **Ansible** (`ansible/`) — host bootstrap, Docker, monitoring stack (Prometheus/Alertmanager/Grafana/Node Exporter/Blackbox), and release-runtime files on EC2. Inventory is `ansible/inventory.ini` (contains real EC2 IPs + private IPs; treat as sensitive). Run with `ansible-playbook -i ansible/inventory.ini ansible/playbooks/<name>.yml`.
3. **Release images** (`release/`, `moodle/`) — versioned GHCR images pulled by EC2 via `automation/app-release-deploy.sh`. The release compose files (`release/docker-compose.staging.yml`, `release/docker-compose.production.yml`, and `release/moodle/docker-compose.yml`) are the source of truth for the deployed stack.
4. **CI/CD** (`.github/workflows/`) — build/test on PR and push to `develop`/`main`/`feature/**`; staging deploy on `develop` push, production deploy on `v*` tag push.

### Deploy roles and per-host ownership

The intended deployment roles are `monitor`, `core`, and `web`, mapped to the corresponding groups in `ansible/inventory.ini`. The current `cd-staging.yml` still contains `demo-web/` build contexts and path filters, but that directory is absent from this checkout. Treat those references as stale and verify the workflow before relying on application-role deployment.

| Role  | Image                      | Build context          | Trigger paths                              |
|-------|----------------------------|------------------------|--------------------------------------------|
| monitor | `aws-hybrid-ai-agent` | `agent_src/` | `agent_src/` |
| core/web | workflow references `demo-web` | missing in this checkout | verify before changing CD |

Changes under `release/`, `automation/`, or the workflow file itself trigger all roles. Each role deploys to its dedicated EC2 host group (`monitor`/`core`/`web`) via `automation/github-deploy-role.sh`.

### Release flow & health checks

`automation/app-release-deploy.sh <staging|production> <image-tag> <monitor|web|core>` is the single release gate. It pulls images, runs `docker compose up -d`, health-checks every service, and **auto-rolls back to the previous tag on any health failure**. Health endpoints differ by environment (staging uses 1xxxx ports, production uses 0xxxx ports — see the script's case statement for exact URLs).

### Staging vs production live on the same EC2 hosts

Staging and production stacks run concurrently on the same EC2 instances with **different host port mappings** (staging: 18000/18080/18081; production: 8000/8080/3000). Do not "simplify" by collapsing these — port separation is what allows both environments to coexist without conflict.

## AI Agent internals (`agent_src/`)

Python 3.11. Entrypoint: `core.main:app` (FastAPI, served by uvicorn on port 8000). `docker-entrypoint.sh` starts four processes in one container: `monitoring/log_watcher.py`, `monitoring/service_monitor.py`, Celery worker (`core.celery_app`), and the FastAPI server. In release compose, these are split: `ai-agent` runs FastAPI, `celery-worker` runs Celery, `log-watcher` runs log monitoring — each its own container.

- **Alert flow**: Alertmanager -> `/webhook` (FastAPI) -> enqueue to Redis -> Celery `process_alerts_task` -> RAG retrieval (ChromaDB) -> Gemini LLM (tool calls gated by `GEMINI_MAX_REMOTE_CALLS`) -> Telegram notification -> scheduled verify.
- **Cooldown/dedup**: identity from Alertmanager `fingerprint`, else hash of `alertname`/`instance`/`job`/`service`/`target`. Redis key `alert-ai-cooldown:<identity>` with TTL `ALERT_AI_COOLDOWN_SECONDS` (default 900). Falls back to in-memory cooldown if Redis is down. `resolved` alerts clear the cooldown.
- **Gemini quota**: defaults are intentionally conservative (`GEMINI_MAX_ATTEMPTS=1`, `GEMINI_FALLBACK_MODELS=` empty, `GEMINI_MAX_REMOTE_CALLS=1`). Do not raise these without reason — alert storms can exhaust free-tier quota and cause 503/429.
- **RAG collections** (ChromaDB at `VECTOR_DB_PATH`, default `/app/vector_db`): `standard_runbooks` (from `config/knowledge_base/*.md`, Git-tracked, split into heading-aware chunks at startup) and `incident_memory` (runtime learning + accepted admin feedback, not written back to Markdown). `tools/inspect_rag_db.py` inspects collections inside a running container.
- **Telegram feedback**: admins reply with `/feedback <incident_id> <text>` or reply to a report; only `TELEGRAM_CHAT_ID` chat is accepted. Feedback is Gemini-evaluated, then stored to `incident_memory`. Requires `AI_AGENT_PUBLIC_URL` (HTTPS) for webhook registration.

## Moodle runtime (`moodle/` and `release/moodle/`)

The Moodle image uses Apache with `/var/www/html/public` as its document root. Public health and synthetic-transaction entry points are `healthz.php` and `synthetic-transaction.php`. The entrypoint supports database credentials through `MOODLE_DB_PASSWORD_FILE`; keep secrets in mounted files rather than command-line arguments or image layers.

CI verifies the Moodle image's secret-file handling, PHP syntax, public web-root layout, and the Moodle compose configuration. Preserve those checks when changing `moodle/`, `release/moodle/`, or the related automation scripts.

## Conventions to preserve

- **No production image builds on EC2.** EC2 only pulls GHCR artifacts that CI produced. Do not copy application source to EC2 for releases.
- **`automation/app-release-deploy.sh` is the only release gate.** Do not add parallel deploy paths.
- **Terraform owns cloud resources; Ansible owns host config.** Don't mix concerns.
- **Ruff lint uses only critical rules** (`E9,F63,F7,F82`) in CI — undefined names and syntax. Broader style rules are not enforced; do not fail a change for style unless it breaks CI.
- **`httpx` must stay `<0.28`** for FastAPI `TestClient` compatibility.
- **Google Gemini package is `google-genai`**, not `google-generativeai` — the latter has caused import confusion.

## Reference docs (already in repo)

- `README.md` — architecture overview, deploy flow, secrets list, endpoints.
- `docs/` — Moodle deployment, infrastructure, operational baseline, runbooks, and CI/CD documentation.
- `docs/AWS_INFRASTRUCTURE_DEPLOYMENT_GUIDE.md` — Terraform + Ansible provisioning steps.
- `agent_src/README.md` — AI agent internals, alert dedup, RAG storage, Gemini quota defaults.
- `agent_src/RAG_SYSTEM_GUIDE.md` — RAG engine details.
