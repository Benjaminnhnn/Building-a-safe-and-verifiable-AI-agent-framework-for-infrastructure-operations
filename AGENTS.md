# AGENTS.md

Repo-specific guidance for AI agents working in this codebase. Verify against config before trusting prose.

## Agent model policy

- The main agent must keep the model selected for the current user session. Do not override or replace the main agent's model.
- Every sub-agent must use `gpt-5.6-luna` with reasoning effort `xhigh`.
- Whenever spawning a sub-agent, explicitly set `model` to `gpt-5.6-luna` and `reasoning_effort` to `xhigh`; do not rely on inherited defaults.
- This policy applies to all sub-agent roles and to sub-agents spawned by other sub-agents.

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
pip install --require-hashes -r agent_src/requirements.txt
pip install pytest ruff "httpx<0.28"

PYTHONPATH=agent_src            pytest -q agent_src/tests

# Validate automation scripts used by CI
python -m py_compile automation/moodle-synthetic-transaction.py automation/moodle-pipeline-replay.py automation/aiops-unified-replay.py automation/moodle-safe-executor-api.py automation/moodle-safe-approval.py automation/moodle-safe-executor-request.py automation/harvest-benchmark-results.py automation/inspect_live_db.py automation/trigger_live_agent.py automation/run-benchmark.py automation/moodle-auth-verify.py automation/auth-lab/scripts/auth-probe.py automation/auth-lab/scripts/auth-actuator.py automation/sprint6-acceptance.py evaluation/benchmark/statistical_analysis.py
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

## Architecture and thesis scope

Moodle on AWS EC2 is the primary thesis environment. The source tree contains Terraform for the Moodle ALB, two application nodes, RDS PostgreSQL, EFS, and the monitoring host; Ansible host configuration; Moodle image/Compose; and the agent's safety-gated incident pipeline. Verify AWS state and dated artifacts before making claims about what is currently deployed.

1. **Terraform** (`terraform/`) owns cloud resources. `terraform.tfstate*` and `terraform.tfvars` are sensitive. Existing state also has optional legacy `bank-web-01`/`bank-core-01` resources; do not disable or remove them without a fresh state-backed plan and reviewed retirement impact.
2. **Ansible** (`ansible/`) owns host configuration and deployment prerequisites. `ansible/inventory.ini` contains real addresses and is sensitive.
3. **Moodle runtime** (`moodle/`, `release/moodle/`) defines the Moodle image and Compose deployment. `automation/github-deploy-moodle.sh` is used by Moodle staging CD; verify its target hosts, environment, and rollback behavior before changing it.
4. **Agent and evidence** (`agent_src/`, `evaluation/`) implement the incident contracts, policy, evidence, verifier, Moodle ground truth, and offline replay.
5. **CI/CD** (`.github/workflows/`) runs CI, publishes AI/Moodle images, and deploys Moodle staging. CI replay is offline simulation. It does not prove live recovery.

### Legacy release files

`release/docker-compose.staging.yml`, `release/docker-compose.production.yml`, `automation/app-release-deploy.sh`, and their banking/payment API references are a legacy application lane. They are not the Moodle thesis deployment gate. Preserve them only as needed for existing deployments; do not use them to claim Moodle deployment. The stale generic production workflow and saved staging workflow were removed because they referenced missing `demo-web` build contexts.

### Evidence and safety invariants

- Separate unit/contract tests, offline replay, deployed shadow runtime, and live fault-drill evidence.
- Ground-truth fixtures are post-decision scoring data, never authorization input.
- Unknown or disallowed actions fail closed; approval is distinct from execution.
- Only the Independent Verifier may transition an incident to `RESOLVED`.
- Live recovery requires fresh allowed/forbidden/related probes and the required stability interval, plus reset and preserved artifacts.

## AI Agent internals (`agent_src/`)

Python 3.11. Entrypoint: `core.main:app` (FastAPI, served by uvicorn on port 8000). `docker-entrypoint.sh` starts four processes in one container: `monitoring/log_watcher.py`, `monitoring/service_monitor.py`, Celery worker (`core.celery_app`), and the FastAPI server. In release compose, these are split: `ai-agent` runs FastAPI, `celery-worker` runs Celery, `log-watcher` runs log monitoring — each its own container.

- **Alert flow**: Alertmanager -> `/webhook` (FastAPI) -> Celery `process_alerts_task`. The task routes AUTH alerts through `core/auth_alert_pipeline.py`, Moodle alerts through `core/moodle_alert_integration.py` and the unified/shadow path, and legacy service alerts through the RAG/Gemini assistant path. These paths have different execution authorities; do not describe them as one self-remediating pipeline.
- **Cooldown/dedup**: identity from Alertmanager `fingerprint`, else hash of `alertname`/`instance`/`job`/`service`/`target`. Redis key `alert-ai-cooldown:<identity>` with TTL `ALERT_AI_COOLDOWN_SECONDS` (default 900). Falls back to in-memory cooldown if Redis is down. `resolved` alerts clear the cooldown.
- **Gemini quota**: defaults are intentionally conservative (`GEMINI_MAX_ATTEMPTS=1`, `GEMINI_FALLBACK_MODELS=` empty, `GEMINI_MAX_REMOTE_CALLS=1`). Do not raise these without reason — alert storms can exhaust free-tier quota and cause 503/429.
- **RAG collections** (ChromaDB at `VECTOR_DB_PATH`, default `/app/vector_db`): `standard_runbooks` (from `config/knowledge_base/*.md`, Git-tracked, split into heading-aware chunks at startup) and `incident_memory` (runtime learning + accepted admin feedback, not written back to Markdown). `tools/inspect_rag_db.py` inspects collections inside a running container.
- **Telegram feedback**: admins reply with `/feedback <incident_id> <text>` or reply to a report; only `TELEGRAM_CHAT_ID` chat is accepted. Feedback is Gemini-evaluated, then stored to `incident_memory`. Requires `AI_AGENT_PUBLIC_URL` (HTTPS) for webhook registration.

## Moodle runtime (`moodle/` and `release/moodle/`)

The Moodle image uses Apache with `/var/www/html/public` as its document root. Public health and synthetic-transaction entry points are `healthz.php` and `synthetic-transaction.php`. The entrypoint supports database credentials through `MOODLE_DB_PASSWORD_FILE`; keep secrets in mounted files rather than command-line arguments or image layers.

CI verifies the Moodle image's secret-file handling, PHP syntax, public web-root layout, and the Moodle compose configuration. Preserve those checks when changing `moodle/`, `release/moodle/`, or the related automation scripts.

## Conventions to preserve

- **No production image builds on EC2.** EC2 only pulls GHCR artifacts that CI produced. Do not copy application source to EC2 for releases.
- `automation/app-release-deploy.sh` belongs to the legacy banking/payment lane. Moodle staging uses its Moodle-specific workflow and deployment helper; keep each lane explicit.
- **Terraform owns cloud resources; Ansible owns host config.** Don't mix concerns.
- **Ruff lint uses only critical rules** (`E9,F63,F7,F82`) in CI — undefined names and syntax. Broader style rules are not enforced; do not fail a change for style unless it breaks CI.
- **`httpx` must stay `<0.28`** for FastAPI `TestClient` compatibility.
- **Google Gemini package is `google-genai`**, not `google-generativeai` — the latter has caused import confusion.

## Reference docs (already in repo)

- `README.md` — Moodle thesis scope, repository map, evidence modes, and legacy-lane boundaries.
- `docs/AI_ENGINEER_STATUS.md` — AI-owned implementation, evidence status, and remaining cross-role dependencies.
- `docs/` — Moodle deployment, infrastructure, operational baseline, runbooks, and CI/CD documentation.
- `docs/MOODLE_OPERATIONS_RUNBOOK.md` — Terraform + Ansible provisioning, deployment, monitoring and teardown steps.
- `agent_src/README.md` — current agent modules and evidence-mode boundaries.
- `agent_src/RAG_SYSTEM_GUIDE.md` — RAG engine details.
