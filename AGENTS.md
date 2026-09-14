# AGENTS.md

Repo-specific guidance for AI agents working in this codebase. Verify against config before trusting prose.

## Critical: `.gitignore` ignores `*.md`

`.gitignore` contains `*.md` with allow-list exceptions (`!README.md`, `!AIops_CICD.md`). Any new or modified Markdown file — including this one — is **ignored by default**. To commit Markdown, add a `!path/to/file.md` exception to `.gitignore` or use `git add -f`. Many existing `.md` files (runbooks, guides) are tracked because they pre-date the rule; do not assume new Markdown is tracked.

Never commit: `agent_src/vector_db/` (ChromaDB runtime data), any `.env*` file, `terraform.tfstate*`, `*.pem`/`*.pub`/`id_rsa*`.

## Commands

### Verify before pushing (mirrors `.github/workflows/ci.yml`)

CI runs lint -> test (both packages) -> docker build -> compose config validation. Run locally in this order:

```bash
# 1. Lint — critical-rule subset only (undefined names, syntax)
ruff check agent_src demo-web/backend/app --select E9,F63,F7,F82

# 2. Tests — PYTHONPATH differs per package, httpx must be <0.28
pip install -r agent_src/requirements.txt
pip install -r demo-web/backend/requirements.txt
pip install pytest ruff "httpx<0.28"

PYTHONPATH=agent_src            pytest -q agent_src/tests
PYTHONPATH=demo-web/backend     pytest -q demo-web/backend/tests

# 3. Build the three release images
docker build -t local/ai-agent:ci    agent_src
docker build -t local/payment-api:ci demo-web/backend
docker build -t local/frontend:ci    demo-web/frontend

# 4. Validate release compose (CI copies .env.example to .env.staging/.env.production first)
cp release/.env.example release/.env.staging
cp release/.env.example release/.env.production
docker compose -f release/docker-compose.staging.yml    config > /dev/null
docker compose -f release/docker-compose.production.yml config > /dev/null
```

### Single test / single package

```bash
PYTHONPATH=agent_src pytest -q agent_src/tests/test_alert_dedup.py::test_name
PYTHONPATH=demo-web/backend pytest -q demo-web/backend/tests/test_health.py
```

### Local dev stack (not a release path)

```bash
docker compose -f platform-config/docker-compose.dev.yml up -d
```

Brings up Postgres, FastAPI backend, frontend, Prometheus, Alertmanager, Grafana, Redis, node-exporter, blackbox-exporter, and the AI agent. Dev port mapping differs from staging/production ports (see `platform-config/docker-compose.dev.yml`).

## Architecture

Four layers; each has its own tooling and must not be conflated:

1. **Terraform** (`terraform/`) — AWS VPC/subnets/SG/EIP and three EC2: `monitor-ai-01`, `bank-web-01`, `bank-core-01`. State in `terraform.tfstate` (never commit).
2. **Ansible** (`ansible/`) — host bootstrap, Docker, monitoring stack (Prometheus/Alertmanager/Grafana/Node Exporter/Blackbox), and release-runtime files on EC2. Inventory is `ansible/inventory.ini` (contains real EC2 IPs + private IPs; treat as sensitive). Run with `ansible-playbook -i ansible/inventory.ini ansible/playbooks/<name>.yml`.
3. **Release images** (`release/`) — versioned GHCR images pulled by EC2 via `automation/app-release-deploy.sh`. The release compose files (`release/docker-compose.staging.yml`, `release/docker-compose.production.yml`) are the source of truth for the deployed stack.
4. **CI/CD** (`.github/workflows/`) — build/test on PR and push to `develop`/`main`/`feature/**`; staging deploy on `develop` push, production deploy on `v*` tag push.

### Deploy roles and per-host ownership

CD builds and deploys **per role**, not as a monolith. Change detection in `cd-staging.yml` maps paths to roles:

| Role  | Image                      | Build context          | Trigger paths                              |
|-------|----------------------------|------------------------|--------------------------------------------|
| monitor | `aws-hybrid-ai-agent`     | `agent_src/`           | `agent_src/`                               |
| core  | `aws-hybrid-payment-api`   | `demo-web/backend/`    | `demo-web/backend/`, `demo-web/database/`  |
| web   | `aws-hybrid-frontend`      | `demo-web/frontend/`    | `demo-web/frontend/`                        |

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

## Payment API (`demo-web/backend/`)

FastAPI backend. Two distinct health endpoints — do not treat them as interchangeable:
- `/api/health` — process liveness only.
- `/api/ready` — readiness, includes a real PostgreSQL connection check (returns 503 if DB unreachable).

Tests mock `app.main.engine.connect` to verify both states; keep that pattern when touching readiness logic. DB schema init/seed in `demo-web/database/*.sql`, mounted into both dev and release compose.

## Conventions to preserve

- **No production image builds on EC2.** EC2 only pulls GHCR artifacts that CI produced. Do not copy application source to EC2 for releases.
- **`automation/app-release-deploy.sh` is the only release gate.** Do not add parallel deploy paths.
- **Terraform owns cloud resources; Ansible owns host config.** Don't mix concerns.
- **Ruff lint uses only critical rules** (`E9,F63,F7,F82`) in CI — undefined names and syntax. Broader style rules are not enforced; do not fail a change for style unless it breaks CI.
- **`httpx` must stay `<0.28`** for FastAPI `TestClient` compatibility. This is called out in `AIops_CICD.md` as a known breakage.
- **Google Gemini package is `google-genai`**, not `google-generativeai` — the latter has caused import confusion.

## Reference docs (already in repo)

- `README.md` — architecture overview, deploy flow, secrets list, endpoints.
- `AIops_CICD.md` — CI/CD design rationale, staging/production isolation, rollback mechanics.
- `AWS_INFRASTRUCTURE_DEPLOYMENT_GUIDE.md` — Terraform + Ansible provisioning steps.
- `agent_src/README.md` — AI agent internals, alert dedup, RAG storage, Gemini quota defaults.
- `agent_src/RAG_SYSTEM_GUIDE.md` — RAG engine details.
