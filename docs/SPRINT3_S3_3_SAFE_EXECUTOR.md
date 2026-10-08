# Sprint 3.3 — Safe action adapters and staging executor

Status: **not currently accepted**. This document's 2026-09-29 completion
statement is historical and is not supported by the referenced five trial
records in the current checkout. A later catalog audit found no complete
reciprocal executable rollback pair for the S3.3 mutating actions; the Safety
Gate denies them, and the Moodle alert route has no caller for
`SafeActionLiveWorkflow`. No current live remediation or rollback is claimed.

The executor must remain disabled unless a separately reviewed staging drill
is underway. An executor receipt is not incident resolution; only an
Independent Verifier can confirm recovery.

## Boundary

```text
typed live intent
  → SafeExecutionGate (catalog, staging target, evidence, RBAC, confidence,
    timeout, idempotency, optional gate approval)
  → append-only SafeActionAuditStore
  → Agent Celery worker — HMAC transport key only
  → private monitor API — action catalog + one-use Ed25519 approval + kill switch
  → aiops-executor SSH key with forced command
  → fixed Moodle reset action on exact staging node(s)
  → independent recovery verification (not performed by the Executor)
```

The worker never receives the executor SSH key or operator approval private
key. The monitor API stores only the Ed25519 public key and a separate HMAC
transport key. Each approval is bound to the scenario, catalog action, target
scope, staging environment, and unique idempotency key, and expires within 15
minutes. API idempotency/nonce state is stored on the monitor node. Production,
unknown actions, arbitrary shell commands, and unknown node targets are rejected.

The existing Moodle alert path remains in `shadow` mode and does not call the
live workflow automatically. `SafeActionLiveWorkflow` is an explicit call site;
it requires both the Safety Gate verdict and the separately signed API
approval. A successful reset returns `awaiting_verification`, never `resolved`.

## Provision and deploy

1. Confirm the Terraform-derived inventory and SSH config refer to the current
   Moodle staging nodes; use the `target-account` AWS profile for AWS CLI checks.
2. Run `bash automation/configure-moodle-safe-executor.sh` if the constrained
   `aiops-executor` SSH identity has not yet been installed.
3. Run `bash automation/configure-moodle-safe-executor-api.sh`. It creates
   ignored, mode-restricted material under `terraform/.artifacts/safe-executor/`,
   provisions pinned node host keys, and installs a hardened systemd service
   bound only to the monitor Docker bridge. The live switch defaults to off.
4. Build and publish the Agent image from CI, then deploy its immutable
   commit-tagged image with:

   ```bash
   bash automation/configure-moodle-monitoring.sh \
     --agent-image ghcr.io/benjaminnhnn/moodle-ai-agent:<40-character-commit-sha>
   ```

   Only the Celery worker receives the read-only mounted HMAC transport key and
   `host.docker.internal:8765` endpoint. Do not put the approval private key or
   executor SSH key in the image, Compose environment, or GitHub secrets.

## Controlled drill procedure

Run one scenario at a time. Each invocation checks the Moodle baseline, enables
the API kill switch, opens an SSH-only tunnel, creates a short-lived
action/idempotency-bound Ed25519 approval, injects the known staging fault,
checks its Prometheus alert, requests exactly the catalogued reset, verifies
ALB and synthetic recovery, waits for alert resolution, and disables the switch
in cleanup.

```bash
MOODLE_SAFE_EXECUTOR_CONFIRM=staging \
  bash automation/moodle-safe-executor-drill.sh DB-01
```

Supported first-slice scenarios: `DB-01`, `RES-01`, `NET-01`, `CON-01`, and
`SEC-02`. Do not broaden the allowlist until each scenario has a passed reset
and post-reset baseline. Evidence is written with mode 0600 under
`terraform/.artifacts/moodle-safe-executor/` and must not be committed.

The Agent client timeout is 105 seconds to cover the API's maximum two
sequential 45-second SSH actions plus response overhead. `NET-01` starts the
reviewed Compose service without waiting inside the executor; the drill's
independent ALB, synthetic, baseline, and alert-resolution checks remain the
recovery gate. A client timeout is not proof that a server-side action stopped;
inspect the executor ledger and staging state before retrying with a new
idempotency key.

The kill switch may be enabled manually only for a reviewed staging drill:

```bash
SAFE_EXECUTOR_LIVE_ENABLED=true SAFE_EXECUTOR_LIVE_CONFIRM=staging \
  bash automation/configure-moodle-safe-executor-api.sh
```

Disable it immediately after manual debugging by rerunning the configuration
without those variables. Check `/healthz`; the expected final response is
`{"execution_enabled":false,"status":"healthy"}`. Prefer the drill wrapper,
which performs this cleanup automatically.

## Completion evidence

Sprint 3.3 is not complete until all five scenario evidence files report
`status: passed`, list the expected completed executor nodes, confirm alert
resolution and baseline after reset, the Agent image contains the explicit
live workflow/client, CI passes, and the monitor API reports the live switch
off. This stage does not authorize autonomous remediation from alerts.
