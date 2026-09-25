# Moodle AI Engineer test-access runbook

This runbook is for the AI Engineer testing the agent against the **staging**
Moodle environment. It is an operational access guide, not an authorization to
alter AWS infrastructure. Run commands from a private workstation unless a
section explicitly says to run them on `monitor-ai-01`.

## 1. Scope and safety boundary

The environment consists of two private Moodle application nodes behind an
Application Load Balancer (ALB), PostgreSQL RDS, EFS, and one public monitoring
node, `monitor-ai-01`. Prometheus, Alertmanager, Grafana, Redis, and the AI
agent run on the monitoring node.

The Engineer may:

- work on a personal Git branch and open a pull request to `develop`;
- build and test the agent source locally or through GitHub Actions;
- SSH to the monitoring node, inspect the agent, and view monitoring data;
- run the reviewed staging scenarios when the infrastructure owner is present
  and has approved the test window.

The Engineer must not:

- run `terraform apply`, `terraform destroy`, or alter security groups, RDS,
  EFS, ALB, VPC, production resources, or IAM policies;
- retrieve or copy RDS passwords, Moodle administrator passwords, GitHub PATs,
  SSH private keys, Grafana passwords, Terraform state, or `.artifacts`;
- place AWS credentials, tokens, passwords, or private keys in source code,
  `.env` files, GitHub issues, pull requests, or commit history;
- run an arbitrary Docker command, database command, or fault injector outside
  the reviewed scripts.

All live fault tests are constrained to the Moodle staging URL and to five
allowlisted scenarios. The test harness restores the known fault before it
reports success, but the Engineer must still verify a clean baseline at the
end of every test session.

## 2. Access that is already configured

The Engineer's public SSH key is installed for user `ec2-user` on
`monitor-ai-01`. Its verified SHA-256 fingerprint is:

```text
SHA256:Zic/J1u2wxBAW3rVqmx9VhjsP6RnlzrQNvKQ2tpOpaY
```

SSH ingress to the monitor is restricted to `104.28.205.71/32`. If the
Engineer changes Internet connection and SSH times out, they must provide the
new public IPv4 address to the infrastructure owner. Do not broaden the rule
to `0.0.0.0/0`.

The Engineer has no direct SSH access to the private Moodle application nodes.
That separation prevents unreviewed changes to Moodle, RDS, and EFS.

## 3. Clone the source and validate it locally

Clone the repository and work from a dedicated branch. Do not commit generated
artifacts or credentials.

```bash
git clone https://github.com/Benjaminnhnn/Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations.git
cd Building-a-safe-and-verifiable-AI-agent-framework-for-infrastructure-operations
git switch develop
git pull --ff-only
git switch -c ai-engineer/<short-topic>
```

Run the required agent checks before pushing. The pinned HTTPX constraint is
important for FastAPI's test client.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r agent_src/requirements.txt
pip install pytest ruff 'httpx<0.28'

ruff check agent_src --select E9,F63,F7,F82
PYTHONPATH=agent_src pytest -q agent_src/tests
docker build -t local/moodle-ai-agent:check agent_src
```

Push the branch and create a pull request. The immutable deployment candidate
is the image tagged with the full commit SHA:

```text
ghcr.io/benjaminnhnn/moodle-ai-agent:<40-character-commit-sha>
```

Do not use `latest` or a mutable tag for a live test. The infrastructure owner
reviews the commit and deploys the immutable image using the monitoring
configuration workflow.

## 4. SSH to the monitor node

The Engineer keeps the **private key corresponding to the installed public
key** only on their own workstation. Never send it to another person or add it
to this repository.

```bash
ssh -i ~/.ssh/ai-engineer -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  ec2-user@18.143.246.124
```

Before accepting a new host key, verify its fingerprint with the infrastructure
owner through a separate trusted channel. The monitor EIP may change only if
the infrastructure is rebuilt; obtain the current address from the owner or
from the Terraform output rather than guessing.

On the monitor, perform read-only health checks:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:9090/-/healthy
curl -fsS http://127.0.0.1:9093/-/healthy
curl -fsS http://127.0.0.1:3000/api/health

sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
sudo systemctl status --no-pager moodle-synthetic-transaction.timer
```

Expected services include `moodle-ai-agent`, its Celery worker, Redis,
Prometheus, Alertmanager, Grafana, and Blackbox Exporter. A healthy response
does not prove that a remediation is permitted: live execution remains subject
to the approved scenario harness.

## 5. Access Prometheus, Grafana, and Alertmanager securely

On the Engineer workstation, create an SSH tunnel and keep that terminal open:

```bash
ssh -i ~/.ssh/ai-engineer -o IdentitiesOnly=yes -N \
  -L 3000:127.0.0.1:3000 \
  -L 8000:127.0.0.1:8000 \
  -L 9090:127.0.0.1:9090 \
  -L 9093:127.0.0.1:9093 \
  ec2-user@18.143.246.124
```

Open only on the local computer:

| Service | Local URL | Purpose |
|---|---|---|
| Grafana | `http://127.0.0.1:3000` | Moodle / ALB / RDS / EFS dashboards |
| Prometheus | `http://127.0.0.1:9090` | Targets, alert rules, metric queries |
| Alertmanager | `http://127.0.0.1:9093` | Alert status and routing |
| AI Agent | `http://127.0.0.1:8000/health` | Agent health only |

The Grafana password is a protected operational secret. Obtain it from the
infrastructure owner through a private channel; do not retrieve it from the
server or paste it into a ticket.

Useful read-only Prometheus checks:

```promql
up
ALERTS{alertstate="firing"}
moodle_synthetic_success
sum(aiops_unified_shadow_events_total{status="observed"})
```

## 6. AWS CLI access

Use a **separate credential issued only for the Engineer**. Do not copy the
infrastructure owner's existing `hviet` access key. If a temporary second key
for the shared `hviet` user is unavoidable, it has the same effective IAM
permissions as that user and must be disabled and deleted after testing.

Configure the received credential on the Engineer workstation, never on a
shared machine or in the repository:

```bash
aws configure --profile hviet-ai-engineer
aws sts get-caller-identity --profile hviet-ai-engineer
```

The preferred long-term option is IAM Identity Center/SSO and a dedicated,
read-only permission set. At minimum, the CLI role should be limited to
describing EC2, ALB, RDS, EFS, CloudWatch, CloudWatch Logs, and tags. It must
not grant IAM administration, Secrets Manager reads, KMS decryption, Terraform
apply/destroy authority, RDS deletion/restore, or network/security-group
modification.

Examples of approved read-only discovery commands:

```bash
export AWS_PROFILE=hviet-ai-engineer
export AWS_REGION=ap-southeast-1

aws sts get-caller-identity
aws ec2 describe-instances --filters Name=tag:Project,Values=moodle-aiops-staging
aws rds describe-db-instances --db-instance-identifier moodle-staging-postgres
aws elbv2 describe-load-balancers --names moodle-staging-alb
```

## 7. Approved test sequence

The infrastructure owner performs the commands below from their trusted
control workstation because they require the deployment SSH configuration and
contain evidence files under `terraform/.artifacts/`. The Engineer observes
Prometheus/Grafana, checks agent behavior, and records findings. Do not run a
fault injector directly on the monitor.

### 7.1 Pre-test gate

```bash
bash automation/moodle-environment-baseline.sh verify
```

The gate must confirm: both ALB targets healthy, authenticated synthetic
transaction succeeds, RDS connectivity works, EFS is mounted, Moodle cron is
running, Prometheus targets are up, and no alert remains firing.

### 7.2 Reproducible agent-only replay

This replay has no infrastructure fault and is the safest way to validate the
agent's evidence and policy behavior after a source change.

```bash
python3 automation/moodle-pipeline-replay.py \
  --output-dir terraform/.artifacts/moodle-pipeline-replay
```

Expected outcome: one incident per reviewed scenario, at least three evidence
references, hash-chained audit records, allowlisted dry-run action, and an
independent verification verdict.

### 7.3 One controlled live trial

Run only after the owner approves the window. Each command injects one scoped
staging fault, observes the expected alert, resets the fault, and verifies the
baseline again.

```bash
for scenario in DB-01 RES-01 NET-01 CON-01 SEC-02; do
  bash automation/moodle-fault-trial.sh "$scenario"
done
```

| Scenario | Controlled symptom | Expected alert |
|---|---|---|
| `DB-01` | Moodle-to-RDS PostgreSQL connectivity is rejected | `MoodleSyntheticTransactionFailed` |
| `RES-01` | Bounded CPU-load container runs on Moodle node B | `MoodleNodeCpuHigh` |
| `NET-01` | Moodle staging DNS alias is removed | `MoodleSyntheticTransactionFailed` |
| `CON-01` | One Moodle web container stops; another target remains healthy | `MoodleWebContainerMissing` |
| `SEC-02` | Moodledata permissions are changed in a scoped manner | `MoodleSyntheticTransactionFailed` |

The AI layer is deliberately in `shadow` mode. It observes and records the
Alertmanager event; it does not receive authority to execute arbitrary
remediation. The reviewed harness performs the scoped reset.

### 7.4 Sprint 2 acceptance campaign

For a formal result, run each scenario three times with a 120-second stable
post-recovery period:

```bash
MOODLE_SPRINT2_CAMPAIGN=ai-engineer-$(date -u +%Y%m%dT%H%M%SZ) \
  bash automation/moodle-sprint2-live-drill.sh all \
  --runs 3 \
  --stability-seconds 120
```

Acceptance requires every scenario to have three passed runs, MTTD at most
60 seconds, recovery at most 600 seconds, and stability at least 120 seconds.
The owner captures raw evidence under
`terraform/.artifacts/moodle-sprint2-live/`; it must not be committed.

## 8. End-of-session checklist

The infrastructure owner runs:

```bash
bash automation/moodle-environment-baseline.sh verify
terraform -chdir=terraform plan -var-file=deployment.tfvars -detailed-exitcode
```

The expected Terraform result is `No changes`. If a fault test fails, do not
re-run its injector repeatedly. First request a reset and baseline verification:

```bash
bash automation/moodle-environment-baseline.sh reset
bash automation/moodle-environment-baseline.sh verify
```

The Engineer then provides the owner with: commit SHA/image reference, test
time window, scenario IDs, Grafana/Prometheus observations, agent evidence
identifier, observed MTTD and recovery, and any failure logs with credentials
redacted.

## 9. Escalation

Stop testing and notify the infrastructure owner immediately if any of these
occur:

- SSH source IP changes or access fails;
- an AWS command returns `AccessDenied` or requests a broader permission;
- an alert remains firing after a reset;
- fewer than two ALB targets become healthy after recovery;
- the synthetic transaction fails in the clean baseline;
- a command would reveal a secret or mutate AWS resources outside the
  allowlisted harness.

For the complete infrastructure and Sprint 1–2 operational procedures, see
[Moodle Sprint Runbook](MOODLE_SPRINT_RUNBOOK.md).
