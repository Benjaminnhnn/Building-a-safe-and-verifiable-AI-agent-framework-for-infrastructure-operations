# Sprint 3.3 — Safe action adapters and staging executor

Status: **not currently accepted**. This document's 2026-09-29 completion
statement is historical and is not supported by the referenced five trial
records in the current checkout. The alert handler remains observation-only;
an authenticated operator endpoint now revalidates candidates and can call
`SafeActionLiveWorkflow` after two distinct signed approvals. No current live
remediation, rollback, or independent recovery result is claimed.

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

The Moodle alert path runs read-only `investigate` mode and never dispatches
from the alert. The authenticated operator handoff prepares and submits the
request through `SafeActionLiveWorkflow`. The Safety Gate approval is verified
with a public-only Ed25519 key; the Executor requires a separate action-bound
Ed25519 approval. A successful reset returns `awaiting_verification`, never
`resolved`.

The operator-side verifier is `automation/moodle-independent-verifier.py`. It
requires the incident/request/scenario tuple and a valid matching executed
receipt in the hash-chained audit DB. It invokes only the reviewed
`automation/moodle-scenario-verify.sh` probe with the scenario and request IDs;
the CLI does not accept a caller-supplied command. It validates that overall
status, exit code, and individual checks agree, and that each sample contains
exactly the fixed scenario contract. It samples for at least 120 seconds with
no gap over 60 seconds, then emits a signed attestation containing the typed
`VerificationResult`, samples, and executed-receipt hash. It has no executor
credential and does not write a resolved state. The Agent verifies the
dedicated Ed25519 signature, rechecks the receipt and pending request in its
runtime audit DB, applies `IncidentStateMachine.resolve_verified`, and appends
the transition to that authoritative audit chain.

On the operator workstation, export a consistent audit snapshot over the
existing SSH-only monitor path, then run the verifier against the scenario
read-only checks. Use the incident and request IDs from the prepared request
and executor receipt:

```bash
python automation/export-moodle-safe-action-audit.py \
  --output terraform/.artifacts/safe-executor/audit-snapshot.sqlite3
python automation/moodle-independent-verifier.py \
  --audit-db terraform/.artifacts/safe-executor/audit-snapshot.sqlite3 \
  --signing-key terraform/.artifacts/safe-executor/verifier-ed25519-private.pem \
  --incident-id <incident-id> \
  --request-id <request-id> \
  --scenario-id DB-01
```

The snapshot uses SQLite's online backup API and travels over SSH; the verifier
checks the receipt before and after the stability window and prints a signed
attestation. Submit it to `POST /api/moodle/verifications/{request_id}`, then
query `GET /api/moodle/incidents/{incident_id}/requests/{request_id}/status`
with the Agent API bearer token. Only a runtime audit-backed `resolved` result
counts as resolution. Offline tests do not prove AWS connectivity, staging
recovery, or live probe results.

Terraform can provision an opt-in Route 53 TCP check against the private RDS
endpoint (`moodle_external_rds_probe_enabled=true` in the staging tfvars). It
adds a billable health check; review current Route 53 pricing before apply. The
verifier requires fresh failures from at least three distinct Route 53 checker
IPs. Any successful connection or missing/stale observation fails verification.
This independent check is read directly from the Route 53 API; the in-VPC
Prometheus RDS probe is not used as evidence that Internet access is blocked.

## Provision and deploy

1. Confirm the Terraform-derived inventory and SSH config refer to the current
   Moodle staging nodes; use the `target-account` AWS profile for AWS CLI checks.
2. Run `bash automation/configure-moodle-safe-executor.sh` if the constrained
   `aiops-executor` SSH identity has not yet been installed.
3. Run `bash automation/configure-moodle-safe-executor-api.sh`. It creates
   ignored, mode-restricted material under `terraform/.artifacts/safe-executor/`,
   including separate Executor, Safety Gate, and verifier keypairs plus an
   Agent API bearer token. The verifier private key stays on the verifier
   workstation; only its public key is installed for the Agent. Both approval
   private keys remain local, and the Executor live switch defaults to off.
4. Build and publish the Agent image from CI, then deploy its immutable
   commit-tagged image with:

   ```bash
   bash automation/configure-moodle-monitoring.sh \
     --agent-image ghcr.io/benjaminnhnn/moodle-ai-agent:<40-character-commit-sha>
   ```

   The Agent API and Celery worker receive only the HMAC transport credential;
   the API also receives the Gate public key and the API bearer token. Do not
   put either approval private key or the executor SSH key in the image,
   Compose environment, or GitHub secrets.

The Agent's existing localhost-only port exposes authenticated operator
endpoints. Prepare a fresh request using the `remediation_candidate` evidence
ID returned in the investigation report:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $(<terraform/.artifacts/safe-executor/safe-action-api-token)" \
  -X POST http://127.0.0.1:8000/api/moodle/remediation/<candidate-evidence-id>/prepare \
  > terraform/.artifacts/safe-executor/prepared-request.json
```

Review the request and both returned digests. Sign `gate_action_sha256` with
`moodle-safe-gate-approval.py` and the local Safety Gate private key; sign the
scenario, action, scope, and idempotency key with `moodle-safe-approval.py` and
the separate Executor private key. Submit both signatures with the exact
prepared `request` to `/execute`. The API rechecks evidence freshness and
candidate equality before calling the Gate and Executor. The Agent live switch
and Executor API kill switch both default to off. Enable the Agent switch only
for a reviewed drill with `moodle_safe_action_live_enabled=true` and
`moodle_safe_action_live_confirm=staging`; enable the Executor separately with
`SAFE_EXECUTOR_LIVE_ENABLED=true SAFE_EXECUTOR_LIVE_CONFIRM=staging`. Disable
both immediately after the drill. Neither endpoint can resolve an incident.

## Integrated CON-01 drill procedure

The integrated drill is restricted to CON-01. It checks the Agent candidate,
confirms both live switches are off, enables them for one staging drill, injects
the fault, prepares a fresh request, signs separate Safety Gate and Executor
approvals, then calls the Agent execute endpoint. It copies the append-only
audit database and runs the Independent Verifier for at least 120 seconds with
30-second sampling. The protected artifact links the candidate, request,
approvals, execution receipt, verifier result, and probe samples. Cleanup
disables both switches and resets the fixture if execution did not complete.

Before running, confirm AWS identity matches the staging ALB account, the
immutable Agent image reference and operator keys exist, baseline passes, and
SSH reset access works. The script checks identity, baseline, ALB health, and
both current kill-switch states before fault injection. A failed preflight exits
before injecting a fault.

Run only:
MOODLE_SAFE_EXECUTOR_CONFIRM=staging bash automation/moodle-safe-executor-drill.sh CON-01

A successful command is not sufficient unless its artifact has passed status,
verifier status `resolved`, a unique matching executed receipt, all probe samples,
and persisted verifier status. Replay and unit tests remain offline evidence.

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
