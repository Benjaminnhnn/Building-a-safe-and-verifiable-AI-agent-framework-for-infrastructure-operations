# AI Engineer: Moodle and OpenLDAP recovery

Status checked against this checkout on 2026-10-04. A local Moodle/PostgreSQL/
OpenLDAP LDAPS lab and live synthetic login probe now run in Docker. This is
not evidence of an AWS deployment or staging trial.

## Component and ownership map

| Component | Current implementation | Owner / input and output | Evidence and status |
|---|---|---|---|
| Moodle PHP runtime | `moodle/Dockerfile` includes PHP LDAP; `automation/auth-lab/docker-compose.yml` runs disposable Moodle and PostgreSQL. | Local lab owns its disposable DB and test credentials; staging/release remain Infrastructure Engineer owned. | Moodle image built and installed in local Docker. |
| OpenLDAP/LDAPS | Local OpenLDAP seed image, time-valid local CA/server cert generation, and CA verification. | `automation/auth-lab/prepare.ps1` creates local-only material; no AWS host, SG or Ansible role has been added. | OpenLDAP is Docker healthy; PHP/OpenSSL successfully verified the LDAPS chain. |
| Synthetic login | `automation/auth-lab/scripts/auth-probe.py` exports successful real logins; `agent_src/core/probes.py` provides the independent recovery verifier. | Test account is confined to the lab and never passed to the agent. | Moodle DB confirms test account `auth=ldap`; Prometheus observed valid login=1, endpoint health=1; independent 120-second verifier passed in the local lab. |
| AI pipeline and action gate | AUTH alerts route through Alertmanager -> `/webhook` -> Redis/Celery -> `agent_src/core/auth_alert_pipeline.py`. The only enabled local action is a fixed request to `recovery-actuator`. | Runtime gate requires the exact AUTH-01 labels, failing LDAPS and login probes, healthy Moodle, and the `local-auth-lab` environment. Agent and worker have no Docker socket. A separate verifier process with test credentials checks LDAPS trust, valid login, invalid-login denial, Moodle health, and 120-second stability. | Two actual local AUTH-01 fault trials executed the fixed OpenLDAP start action and received verifier verdicts with 9 observations and all checks true. The first exposed a stale-alert state downgrade; code now ignores alerts after verifier resolution and Redis refuses to downgrade a verifier-resolved AUTH incident. After the fix, the second trial remained `RESOLVED` in Redis after stale firing and Alertmanager recovery notifications. No Telegram is configured; no AWS action or production recovery is implemented. |
| Ground truth | `evaluation/ground_truth/auth/AUTH-01..03.json`. | Evaluation harness only; never pass expected root cause or scenario file to the observer/agent as incident evidence. | Schema validation is covered by tests. Fixtures do not represent executed trials. |

## AUTH scenarios and safe decision boundary

| ID | Fault | Agent may | Agent must not | Reset owner |
|---|---|---|---|---|
| AUTH-01 | OpenLDAP unavailable | In the disposable Docker lab only, run the fixed start/restart action after the exact runtime gate; resolve only after the separate verifier passes all checks. | Restart arbitrary services, change directory records/policy, or act outside local-auth-lab. The actuator's Docker socket is root-equivalent and is not approved for staging/production. | Operator may inject/restore the lab fault; production directory ownership remains with Infrastructure Engineer |
| AUTH-02 | Moodle-to-LDAPS path unavailable | Observe the endpoint and authentication probes. | Change a live security group. A local bad-endpoint setting is available, but no network fault injector exists. | Operator restores the recorded lab setting |
| AUTH-03 | Synthetic account disabled | Classify as an identity-state issue and escalate. | Enable/disable accounts, change passwords, groups, roles, or directory policy. | Directory owner restores only the pre-provisioned synthetic account |

The expected root cause in each fixture is post-decision scoring data. It is
not an authorization input. AUTH alerts bypass the legacy Moodle oracle/Gemini
path. The local AUTH-01 path uses only runtime probes and a fixed action; it is
not a general-purpose action framework and does not establish production
readiness.

## Run modes

### Offline fixture checks

From the repository root in PowerShell:

```powershell
$env:PYTHONPATH = 'agent_src'
python -m pytest -q agent_src/tests/test_auth_ground_truth.py agent_src/tests/test_probes.py agent_src/tests/test_live_adapters.py
python automation/ai-engineer-sprint1-4-replay.py --help
```

The replay utility covers the existing 15 Moodle scenarios; it does not run
AUTH fixtures, contact LDAP, deliver Alertmanager webhooks, or mutate services.

### Local runtime

The local lab and its multi-terminal commands are in
[`AI_ENGINEER_AUTH_LAB.md`](AI_ENGINEER_AUTH_LAB.md). The release Moodle
Compose file still requires prepared RDS/EFS mounts and secret files. The lab
is separate from both release and AWS:

```powershell
docker build -t local/moodle:auth-ldap-check moodle
docker run --rm --entrypoint php local/moodle:auth-ldap-check -m
```

The second command must list `ldap`. That extension check alone proves no
login; the separate local-lab runtime currently proves actual LDAP login.

### Staging verifier (read-only)

Prerequisites: an approved staging deployment with OpenLDAP on a dedicated
private EC2, Moodle LDAP authentication configured for LDAPS with verified CA,
a pre-provisioned non-personal test account, and operator-managed username and
password files available only to the verifier workstation. The Moodle runtime
must already serve HTTPS. This checkout does not create those prerequisites.

PowerShell, in the verifier terminal; set file paths without displaying file
contents:

```powershell
$env:PYTHONPATH = 'agent_src'
$env:MOODLE_AUTH_PROBE_BASE_URL = 'https://<approved-staging-moodle-host>'
$env:MOODLE_AUTH_TEST_USER_FILE = '<protected-path-to-synthetic-username-file>'
$env:MOODLE_AUTH_TEST_PASSWORD_FILE = '<protected-path-to-synthetic-password-file>'
$env:MOODLE_AUTH_CA_BUNDLE = '<approved-CA-bundle-path>'
python automation/moodle-auth-verify.py
```

This collector prints one non-identifying JSON observation per 15 seconds for
at least 120 seconds. Exit code 0 means its login/denial/health probes passed;
exit code 1 means recovery is not eligible; exit code 2 means the verifier
could not complete. It never changes incident state and does not authorize a
fault or remediation.

Do not put a password, account name, URL credential, AWS secret, or raw Moodle
response in an alert, ground-truth fixture, command argument, or terminal
transcript. Restrict file access using the workstation's file ACLs. Remove
temporary test-account files under the owner's retention policy after the run.

## Manual multi-terminal staging observation sequence

This section describes AWS staging and is **not currently executable end-to-end**.
Use `AI_ENGINEER_AUTH_LAB.md` for the runnable local Docker drill. Do not
interpret a local lab pass as an AWS staging trial.

After those prerequisites are independently verified, the supported
observation commands are:

1. **Terminal A — secure dashboard tunnel**, from the operator workstation:

   ```bash
   ssh -F terraform/.artifacts/moodle-ssh.config -N \
     -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 \
     -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 monitor-ai-01
   ```

   Observe Grafana at `http://127.0.0.1:3000`, Prometheus at
   `http://127.0.0.1:9090`, Alertmanager at `http://127.0.0.1:9093`, and
   agent health at `http://127.0.0.1:8000/health`. These are existing
   monitoring endpoints; an AUTH alert rule is not yet configured.

2. **Terminal B — Moodle logs**, from the operator workstation:

   ```bash
   ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
     'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env -f /opt/moodle/release/docker-compose.yml logs --follow moodle-web'
   ```

   The deployment path and Compose service name are from the existing Moodle
   operations runbook. This shows web logs, not proof of LDAP login success.

3. **Terminal C — independent login verifier**, use the PowerShell setup and
   command in “Staging verifier” above. Keep the stream visible during the
   incident. This is the only component in this sequence that can produce
   synthetic login evidence; it cannot resolve an incident in the current
   pipeline.

4. **Terminal D — directory logs and fault operation**: the command is
   intentionally not supplied yet. The directory host, service name, image,
   certificate mounts, allowlist, reset, and access route have not been
   implemented or reviewed. The directory owner must define these and the
   matching AUTH-specific alert before a manual live drill can be considered
   runnable.

For AUTH-03, only the directory owner may change the pre-provisioned synthetic
account state and reset it. For AUTH-02, stop before fault injection until an
isolated, time-bounded network harness and symmetric reset have passed local
fake tests. If a reset fails, stop the trial, keep the incident unresolved,
contact the Infrastructure Engineer, and verify baseline login, denial,
health, alert state, and a fresh 120-second stability window after repair.

## Acceptance evidence still required

The Infrastructure Engineer must provide (1) approved account/region and
staging environment identity, (2) Terraform-reviewed private directory host
and Moodle-SG-only LDAPS ingress, (3) certificate trust and key rotation plan,
(4) non-admin read-only directory lookup design and approved handling for the
bind secret, (5) Moodle LDAP plugin configuration and seeded synthetic account,
(6) alert rule and AUTH-specific bounded injector/reset, and (7) captured raw
timestamps for baseline, fault, alert, agent redacted diagnosis/gate, reset,
valid login, invalid-login denial, health and 120-second stability. Repeat each
approved live scenario at least three times before claiming live acceptance.

Current status: offline contract tests pass; the local Moodle/OpenLDAP runtime
is live; two automatic AUTH-01 fault/recovery trials completed. Both passed
the separate verifier's valid login, invalid-login denial, LDAPS, health, and
120-second stability checks (9 observations). The second trial also confirmed
that stale firing and recovery notifications do not downgrade the resolved
Redis incident. AWS deployed runtime and live trials are not run; AUTH-02/03
runtime trials and AUTH benchmark results do not exist.

Evidence retention: the two local trial outcomes above are documented as
operator observations from 2026-10-04, but raw per-trial logs, probe captures,
and verifier records are not present in this checkout. They are not
independently auditable from retained artifacts and must not be reported as
benchmark results or AWS/staging acceptance.
