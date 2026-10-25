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

CI runs lint -> AI-agent tests -> automation validation -> unified replay -> AI image build + ground-truth exclusion check -> Moodle image build + secret-file and web-root checks -> Sprint 2 pipeline replay -> Moodle compose validation. Run locally in this order:

```bash
# 1. Lint — critical-rule subset only (undefined names, syntax)
ruff check agent_src --select E9,F63,F7,F82

# 2. Tests — use the agent package on PYTHONPATH; httpx must be <0.28
pip install --require-hashes -r agent_src/requirements.txt
pip install pytest ruff "httpx<0.28"

PYTHONPATH=agent_src            pytest -q agent_src/tests

# Validate automation scripts and replays used by CI (the exact file lists live in
# the "Validate Moodle experiment and operator automation" step of ci.yml)
bash automation/moodle-scenario-verify.sh SEC-01; test $? -eq 64   # HUMAN_ONLY must be refused
python automation/aiops-unified-replay.py all                     # CI pins each scenario's outcome (fail-closed counts as correct)
PYTHONPATH=agent_src python automation/moodle-pipeline-replay.py  # CI asserts 15 VERIFIED_DRY_RUN

# 3. Build the images (the AI agent build context is the repo root)
docker build -f agent_src/Dockerfile -t local/ai-agent:ci .
docker build -t local/moodle:ci moodle

# 4. Validate the Moodle compose file
MOODLE_RUNTIME_ENV_FILE=./moodle-runtime.env.example \
docker compose --env-file release/moodle/moodle-runtime.env.example \
	-f release/moodle/docker-compose.yml config > /dev/null
```

On macOS, a few probe tests need GNU `timeout` on PATH (`brew install coreutils`);
CI runs on Linux.

### Single test

```bash
PYTHONPATH=agent_src pytest -q agent_src/tests/test_alert_dedup.py::test_name
```

## Architecture and thesis scope

Moodle on AWS EC2 is the primary thesis environment. The source tree contains Terraform for the Moodle ALB, two application nodes, RDS PostgreSQL, EFS, and the monitoring host; Ansible host configuration; Moodle image/Compose; and the agent's safety-gated incident pipeline. Verify AWS state and dated artifacts before making claims about what is currently deployed.

1. **Terraform** (`terraform/`) owns cloud resources. Operators use the Git-ignored `terraform/deployment.tfvars` (copied from `deployment.tfvars.example`); `terraform.tfstate*` and `deployment.tfvars` are sensitive. Role credentials require MFA; pass them to Terraform with `aws configure export-credentials` (see `docs/AWS_IAM_ACCESS_GUIDE.md`).
2. **Ansible** (`ansible/`) owns host configuration and deployment prerequisites. The Moodle playbooks use the inventory and SSH config that Terraform writes to the Git-ignored `terraform/.artifacts/`; there is no static inventory in the repo.
3. **Moodle runtime** (`moodle/`, `release/moodle/`) defines the Moodle image and Compose deployment. `automation/github-deploy-moodle.sh` is used by Moodle staging CD; verify its target hosts, environment, and rollback behavior before changing it.
4. **Agent and evidence** (`agent_src/`, `evaluation/`) implement the incident contracts, policy, evidence, verifier, Moodle ground truth, and offline replay.
5. **CI/CD** (`.github/workflows/`) runs CI, publishes AI/Moodle images, and deploys Moodle staging. CI replay is offline simulation. It does not prove live recovery.

### Retired legacy lane

The bank/payment demo (bank-web/bank-core hosts, payment-api, frontend, `release/docker-compose.{staging,production}.yml`, `automation/app-release-deploy.sh`, its Ansible stack) and the local OpenLDAP auth lab were removed on 2026-10-14. The legacy alert-diagnosis branch in `core/tasks.py` (WebEndpointDown, FrontendAPIProxyDown, PaymentAPIEndpointDown, ...) no longer receives alerts and is pending a separate refactor; do not extend it.

### Evidence and safety invariants

- Separate unit/contract tests, offline replay, deployed shadow runtime, and live fault-drill evidence.
- Ground-truth fixtures are post-decision scoring data, never authorization input.
- Unknown or disallowed actions fail closed; approval is distinct from execution.
- Only the Independent Verifier may transition an incident to `RESOLVED`.
- Live recovery requires fresh allowed/forbidden/related probes and the required stability interval, plus reset and preserved artifacts.

## AI Agent internals (`agent_src/`)

Python 3.11. The image `CMD` serves `core.main:app` (FastAPI) with uvicorn on port 8000. On `monitor-ai-01`, `ansible/playbooks/configure-moodle-monitoring.yml` runs it as `moodle-ai-agent`, plus `moodle-agent-worker` (Celery, `core.celery_app`) and `moodle-agent-redis`. The image is published as `ghcr.io/<owner>/moodle-ai-agent:<sha>`.

- **Alert flow**: Alertmanager -> `/webhook` (FastAPI) -> Celery `process_alerts_task` -> Moodle evidence investigation. Unsupported/non-Moodle firing alerts do not fall back to a generic assistant or executor. Live remediation requires the operator handoff and two approvals; only a signed Independent Verifier attestation accepted by the Agent can persist `RESOLVED` in the runtime audit.
- **Cooldown/dedup**: identity from Alertmanager `fingerprint`, else hash of `alertname`/`instance`/`job`/`service`/`target`. Redis key `alert-ai-cooldown:<identity>` with TTL `ALERT_AI_COOLDOWN_SECONDS` (default 900). Falls back to in-memory cooldown if Redis is down. `resolved` alerts clear the cooldown.
- **Gemini usage**: Gemini reviews admin feedback and, only when `AIOPS_DIAGNOSIS_MODE` is `llm` or `hybrid` (default `rules`), proposes evidence-cited diagnoses that the deterministic citation validator in `core/reasoning/` must accept (reviewed design: `docs/LLM_DIAGNOSIS_DESIGN.md`). It is never used for alert routing, planning, remediation, gating, or verification. Keep external model calls bounded; extend the model's role only through a reviewed design.
- **RAG collections** (ChromaDB at `VECTOR_DB_PATH`, default `/app/vector_db`): `standard_runbooks` (from `config/knowledge_base/*.md`, Git-tracked, split into heading-aware chunks at startup) and `incident_memory` (runtime learning + accepted admin feedback, not written back to Markdown). `tools/inspect_rag_db.py` inspects collections inside a running container.
- **Telegram feedback**: admins reply with `/feedback <incident_id> <text>` or reply to a report; only `TELEGRAM_CHAT_ID` chat is accepted. Feedback is Gemini-evaluated, then stored to `incident_memory`. Requires `AI_AGENT_PUBLIC_URL` (HTTPS) for webhook registration.

## Moodle runtime (`moodle/` and `release/moodle/`)

The Moodle image uses Apache with `/var/www/html/public` as its document root. Public health and synthetic-transaction entry points are `healthz.php` and `synthetic-transaction.php`. The entrypoint supports database credentials through `MOODLE_DB_PASSWORD_FILE`; keep secrets in mounted files rather than command-line arguments or image layers.

CI verifies the Moodle image's secret-file handling, PHP syntax, public web-root layout, and the Moodle compose configuration. Preserve those checks when changing `moodle/`, `release/moodle/`, or the related automation scripts.

## Conventions to preserve

- **No production image builds on EC2.** EC2 only pulls GHCR artifacts that CI produced. Do not copy application source to EC2 for releases.
- Moodle staging deploys through `.github/workflows/cd-staging.yml` and `automation/github-deploy-moodle.sh` (self-hosted runner `moodle-staging`), or manually per `docs/MOODLE_OPERATIONS_RUNBOOK.md` §8. Run operator scripts from the repository root.
- **Terraform owns cloud resources; Ansible owns host config.** Don't mix concerns.
- **Ruff lint uses only critical rules** (`E9,F63,F7,F82`) in CI — undefined names and syntax. Broader style rules are not enforced; do not fail a change for style unless it breaks CI.
- **`httpx` must stay `<0.28`** for FastAPI `TestClient` compatibility.
- **Google Gemini package is `google-genai`**, not `google-generativeai` — the latter has caused import confusion.

## Reference docs (already in repo)

- `README.md` — Moodle thesis scope, repository map and evidence modes.
- `docs/MOODLE_ENTRA_OKTA_OIDC_IMPLEMENTATION.md`, `docs/MOODLE_SSO_INCIDENT_SCENARIOS.md` — Okta/Entra SSO deployment and incident scenarios.
- `docs/AWS_IAM_ACCESS_GUIDE.md` — InfraEngineer/AIEngineer role-based AWS access.
- `docs/AI_ENGINEER_STATUS.md` — AI-owned implementation, evidence status, and remaining cross-role dependencies.
- `docs/` — Moodle deployment, infrastructure, operational baseline, runbooks, and CI/CD documentation.
- `docs/MOODLE_OPERATIONS_RUNBOOK.md` — Terraform + Ansible provisioning, deployment, monitoring and teardown steps.
- `docs/DEVOPS_DEPLOY_WORKFLOW_GUIDE.md` — Vietnamese end-to-end deploy walkthrough for new DevOps engineers (tools, gates, troubleshooting); defers to the runbook.
- `agent_src/README.md` — current agent modules and evidence-mode boundaries.
- `agent_src/RAG_SYSTEM_GUIDE.md` — RAG engine details.
