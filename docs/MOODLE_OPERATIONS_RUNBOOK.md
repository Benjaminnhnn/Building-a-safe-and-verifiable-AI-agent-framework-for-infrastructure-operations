# Moodle Operations Runbook

Status: canonical operational runbook for the Moodle staging environment.

This document is the single source of operational instructions for provisioning
AWS, preparing hosts, deploying Moodle, enabling monitoring, collecting Sprint
1–2 evidence, and removing the complete staging environment. It replaces the
former separate deployment and sprint runbooks.

Run all commands from the repository root unless a command explicitly says it
runs on a remote host. Never commit `deployment.tfvars`, `terraform.tfstate*`,
`terraform/.artifacts/`, private keys, passwords, PATs, or `.env` files.

## 1. Operating model and ownership

| Layer | Source of truth | Owner | How it changes |
|---|---|---|---|
| AWS resources | `terraform/` | Infrastructure Engineer | Reviewed `terraform plan`, then explicit `apply` |
| EC2 host configuration | `ansible/` and `automation/prepare-moodle-runtime.sh` | Infrastructure Engineer | Ansible from the trusted control workstation |
| Moodle application runtime | `release/moodle/`, immutable GHCR image | Infrastructure Engineer / CD | Initial staging manually; later rolling deployment through `CD Staging Moodle` |
| Monitoring and AI Agent runtime | `ansible/playbooks/configure-moodle-monitoring.yml` | Infrastructure Engineer | Explicit reviewed monitoring rollout |
| Agent source and image | `agent_src/`, `.github/workflows/agent-image.yml` | AI Engineer | CI builds an immutable GHCR image; admin chooses whether to deploy it |
| Sprint evidence | `terraform/.artifacts/` | Test owner | Local, secret-free evidence; never committed |

Terraform and Ansible are intentionally not automatically applied by GitHub
Actions. They can create, replace, or destroy cloud resources and require
reviewed AWS credentials. GitHub Actions is used to build immutable images and
to deploy only the approved Moodle image through the release gate.

## 2. Accepted staging architecture

```text
Internet
  -> Internet-facing ALB in Public A and Public B
    -> Moodle EC2 A, private App A subnet
    -> Moodle EC2 B, private App B subnet

Moodle EC2 A/B
  -> RDS PostgreSQL, private Data subnet (Single-AZ)
  -> Regional EFS through an access point and one mount target per AZ

Administrator / approved AI Engineer
  -> monitor-ai-01 (public EIP, SSH CIDR restricted)
    -> Prometheus, Alertmanager, Grafana, Blackbox, Redis, AI Agent, Celery
    -> SSH ProxyJump to the private Moodle nodes for administrator automation
```

The two Moodle web nodes are fixed EC2 instances, not an Auto Scaling Group.
RDS is private, encrypted, `db.t4g.micro`, and Single-AZ for short staging
experiments; it is not an HA database. EFS provides shared `moodledata`, while
PostgreSQL remains the source of truth for application data.

The live URL must always be read from Terraform output rather than copied from
an old document:

```bash
terraform -chdir=terraform output -json moodle_application | jq -r .url
```

## 3. Prerequisites and local security

Install Terraform, AWS CLI v2, OpenSSH, Ansible, Docker, Docker Compose, `jq`,
`curl`, and `openssl` on the trusted control workstation.

```bash
export AWS_PROFILE=target-account
aws sts get-caller-identity --profile "$AWS_PROFILE"
terraform version
ansible-playbook --version
docker version
```

Create a local SSH key pair if one does not already exist. The public key is
given to Terraform; the private key never leaves the owner workstation.

```bash
ssh-keygen -t ed25519 -a 100 \
  -f ~/.ssh/moodle-aiops \
  -C "infrastructure-owner@example.com"
chmod 600 ~/.ssh/moodle-aiops
```

Use `umask 077` before creating a plan, secrets, or generated SSH inventory.
It makes new files readable only by the current user; this prevents passwords,
private connection data, and plan inputs from becoming group/world-readable.

## 4. Configure Terraform input

Create the ignored local variable file. The helper discovers the current
public IPv4 and updates only the chosen file; it does not run Terraform.

```bash
cp -n terraform/deployment.tfvars.example terraform/deployment.tfvars
bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars
```

Review `terraform/deployment.tfvars`. At minimum set the key paths, a pinned
Amazon Linux 2023 x86_64 AMI, administrator CIDR, and the desired HTTP/TLS
mode. Do not put the content in Git.

```hcl
aws_region                    = "ap-southeast-1"
project_name                  = "moodle-aiops"
environment                   = "staging"
my_ip_cidr                    = "YOUR_PUBLIC_IP/32"
ci_cd_ssh_cidr_blocks         = []
ai_engineer_monitor_ssh_cidr_blocks = [] # Optional; monitor SSH only.

public_key_path               = "~/.ssh/moodle-aiops.pub"
private_key_path              = "~/.ssh/moodle-aiops"
ec2_ami_id                    = "ami-REPLACE_WITH_PINNED_AL2023_AMI"
enable_legacy_demo            = false
monitor_use_management_subnet = true

monitor_instance_type         = "t3.small"
moodle_instance_type          = "t3.small"
moodle_db_engine_version      = "16.10"
moodle_db_instance_class      = "db.t4g.micro"

# Use HTTP only for short staging smoke tests. HTTPS needs an issued ACM
# certificate in ap-southeast-1 and a hostname covered by that certificate.
moodle_allow_http      = true
moodle_certificate_arn = null
moodle_hostname        = null
```

To grant an AI Engineer SSH access only to `monitor-ai-01`, add their public
IPv4 as a `/32`, review the plan, apply it, and install only their **public**
key with the dedicated playbook. Never open SSH to all Internet clients.

```hcl
ai_engineer_monitor_ssh_cidr_blocks = ["ENGINEER_PUBLIC_IP/32"]
```

```bash
bash automation/authorize-ai-engineer.sh ~/.ssh/ai-engineer.pub
```

## 5. Provision or reconcile AWS infrastructure

Initialize and validate the code first:

```bash
terraform -chdir=terraform init -input=false
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform test
```

Create a saved plan with restrictive permissions, inspect it, then apply only
the exact plan that was reviewed.

```bash
umask 077
mkdir -p terraform/.artifacts

terraform -chdir=terraform plan \
  -input=false \
  -var-file=deployment.tfvars \
  -out=.artifacts/deployment.tfplan

terraform -chdir=terraform show -no-color .artifacts/deployment.tfplan
terraform -chdir=terraform apply .artifacts/deployment.tfplan
```

If Terraform rejects a saved plan as stale, code, inputs, or state changed
after planning. Generate and review a new plan; do not bypass the check.

Export generated connection artifacts and inspect the topology:

```bash
umask 077
terraform -chdir=terraform output -raw moodle_ssh_config \
  > terraform/.artifacts/moodle-ssh.config
terraform -chdir=terraform output -raw moodle_ansible_inventory \
  > terraform/.artifacts/moodle-inventory.yml
chmod 600 terraform/.artifacts/moodle-ssh.config

ansible-inventory -i terraform/.artifacts/moodle-inventory.yml --graph
ansible -i terraform/.artifacts/moodle-inventory.yml all -m ping
terraform -chdir=terraform output -json moodle_application
terraform -chdir=terraform output -json moodle_database
terraform -chdir=terraform output -json moodle_filesystem
```

Before Moodle is deployed, ALB targets are expected to be unhealthy because no
service is listening on port 8080. This is expected and must not be hidden by a
placeholder web server.

## 6. Prepare the Moodle EC2 runtime with Ansible

This idempotent step installs Docker/Compose and EFS tooling, mounts EFS using
TLS plus the Terraform-managed access point, configures the shared data path,
and generates the current SSH/inventory artifacts. It does not build an image,
retrieve RDS credentials, or start Moodle.

```bash
bash automation/prepare-moodle-runtime.sh
```

Confirm EFS is really mounted on each application node:

```bash
for host in moodle-app-a moodle-app-b; do
  ssh -F terraform/.artifacts/moodle-ssh.config "$host" \
    'findmnt --noheadings --output FSTYPE,TARGET --target /mnt/efs/moodledata'
done
```

Expected file-system type is `nfs4` or `efs`. A failed mount is an
infrastructure issue, not an application deployment issue; verify the EFS
mount-target, EFS security group port 2049, route, and access-point policy
before continuing.

## 7. Build or select an immutable Moodle image

Every deploy uses an image tagged with a full 40-character Git commit SHA:

```text
ghcr.io/benjaminnhnn/moodle:<40-character-commit-sha>
```

For a new Moodle image, push a change under `moodle/**` or invoke **CD Staging
Moodle** manually. The workflow builds the immutable GHCR image. It can deploy
the image only when a correctly configured self-hosted runner with labels
`self-hosted`, `linux`, and `moodle-staging` is available.

The staging workflow requires these GitHub Environment secrets:

- `MOODLE_APP_A_HOST`, `MOODLE_APP_B_HOST`: host names/IPs reachable by the
  self-hosted runner;
- `MOODLE_PUBLIC_URL`: the current Terraform ALB URL or custom HTTPS URL;
- `SSH_PRIVATE_KEY`, optionally `SSH_PORT`;
- `GHCR_USERNAME` and `GHCR_TOKEN` with package pull permission.

The workflow rolls node B then node A through
`automation/github-deploy-moodle.sh`, preserving one healthy ALB target while
the other node updates. GitHub-hosted runners must not be granted SSH access to
the private Moodle nodes.

For a first deployment or a deliberately reviewed manual rollout, continue
with the next sections.

## 8. Configure GHCR, RDS role, runtime secrets, and Moodle release

If the package is private, configure a GitHub classic PAT limited to
`read:packages` interactively. The PAT is not written in this repository.

```bash
export MOODLE_IMAGE='ghcr.io/benjaminnhnn/moodle:REPLACE_WITH_40_CHAR_SHA'

bash automation/configure-moodle-ghcr-access.sh "$MOODLE_IMAGE"
bash automation/moodle-preflight.sh "$MOODLE_IMAGE"
```

Create new root-local passwords for a new environment. These are secret files,
not executable scripts, so read them with `cat` when needed.

```bash
umask 077
mkdir -p terraform/.artifacts/moodle-secrets
openssl rand -hex 32 > terraform/.artifacts/moodle-secrets/moodle-db-password
openssl rand -base64 32 > terraform/.artifacts/moodle-secrets/moodle-admin-password
chmod 600 terraform/.artifacts/moodle-secrets/moodle-*-password
```

Stage the release. The script uses the RDS-managed master secret only in
memory, creates/rotates the least-privilege `moodle_app` PostgreSQL role,
verifies TLS, stages the CA bundle/secret files/Compose/runtime environment on
both nodes, and does **not** start Moodle.

```bash
bash automation/stage-moodle-release.sh \
  "$MOODLE_IMAGE" \
  terraform/.artifacts/moodle-secrets/moodle-db-password \
  terraform/.artifacts/moodle-secrets/moodle-admin-password
```

Install Moodle only once, on application node A. On an existing database, the
installer correctly reports that tables already exist; do not force a second
installation or run it on node B.

```bash
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
  'sudo docker compose \
    --env-file /opt/moodle/release/moodle-runtime.env \
    -f /opt/moodle/release/docker-compose.yml \
    --profile installer run --rm moodle-install'
```

Start web plus cron on node A, and web only on node B. There must be exactly
one cron runner to prevent duplicate Moodle scheduled tasks.

```bash
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
  'sudo docker compose \
    --env-file /opt/moodle/release/moodle-runtime.env \
    -f /opt/moodle/release/docker-compose.yml \
    --profile cron up -d --wait moodle-web moodle-cron'

ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-b \
  'sudo docker compose \
    --env-file /opt/moodle/release/moodle-runtime.env \
    -f /opt/moodle/release/docker-compose.yml \
    up -d --wait moodle-web'
```

Validate the ALB and discover the canonical public URL from Terraform:

```bash
MOODLE_URL="$(terraform -chdir=terraform output -json moodle_application | jq -r .url)"
curl --fail --silent --show-error "$MOODLE_URL/healthz.php"

TARGET_GROUP_ARN="$(terraform -chdir=terraform output -json moodle_application | jq -r .target_group_arn)"
aws elbv2 describe-target-health \
  --profile "$AWS_PROFILE" \
  --region ap-southeast-1 \
  --target-group-arn "$TARGET_GROUP_ARN"
```

Acceptance is HTTP 200 from `/healthz.php` and two `healthy` ALB targets. The
Moodle login is at `$MOODLE_URL/login/index.php`, with administrator account
`admin` and the locally protected password file created above.

## 9. Deploy monitoring and the AI Agent

Monitoring deploys Node Exporter/cAdvisor on Moodle A/B, and Prometheus,
Alertmanager, Grafana, Blackbox Exporter, synthetic transaction runner, Redis,
AI Agent API, and Celery worker on `monitor-ai-01`.

The AI Agent workflow builds, but does not automatically deploy, this image:

```text
ghcr.io/benjaminnhnn/moodle-ai-agent:<40-character-commit-sha>
```

After the AI Engineer's commit is reviewed and its image build succeeds, the
Infrastructure Engineer performs the explicit rollout:

```bash
export AGENT_IMAGE='ghcr.io/benjaminnhnn/moodle-ai-agent:REPLACE_WITH_40_CHAR_SHA'

bash automation/configure-moodle-monitoring.sh \
  --agent-image "$AGENT_IMAGE"
```

The wrapper derives the active Moodle ALB URL, RDS endpoint, generated
inventory, and synthetic password from the local Terraform state/artifacts.
It configures Alertmanager's internal webhook to the Agent. Do not use a
mutable image tag or build a production image directly on EC2.

If monitoring services or timers need to be restored after a manual stop:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  'sudo docker compose -f /opt/moodle-observability/docker-compose.yml up -d && \
   sudo systemctl enable --now moodle-synthetic-transaction.timer && \
   sudo systemctl status --no-pager moodle-synthetic-transaction.timer'
```

Access dashboards through an SSH tunnel; do not expose their ports to the
Internet:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config -N \
  -L 3000:127.0.0.1:3000 \
  -L 8000:127.0.0.1:8000 \
  -L 9090:127.0.0.1:9090 \
  -L 9093:127.0.0.1:9093 \
  monitor-ai-01
```

| Service | Local URL |
|---|---|
| Grafana | `http://127.0.0.1:3000` |
| Prometheus | `http://127.0.0.1:9090` |
| Alertmanager | `http://127.0.0.1:9093` |
| AI Agent health | `http://127.0.0.1:8000/health` |

Retrieve Grafana credentials only from the owner-controlled terminal:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  'sudo cat /opt/moodle-observability/secrets/grafana-admin-password'
```

## 10. Baseline before every experiment

Capture a new baseline after an application, infrastructure, or monitoring
change. Verification checks image/runtime checksums, web health, EFS, ALB,
authenticated synthetic transaction, Moodle cron, Prometheus targets, and
firing alerts.

```bash
bash automation/moodle-environment-baseline.sh capture
bash automation/moodle-environment-baseline.sh verify
```

Do not start an experiment if `verify` fails. Fix the actual state or, when a
reviewed deployment intentionally changed checksums, recapture only after all
health checks are clean. Baseline evidence is local under
`terraform/.artifacts/` and is ignored by Git.

## 11. Sprint 1: reliability baseline and controlled faults

### 11.1 Two-hour authenticated synthetic soak

The runner logs in as Moodle `admin`, safely creates/reads/updates a fixture,
logs out, and records success/latency every 30 seconds. It runs on the monitor
node and uses the protected synthetic password file.

```bash
bash automation/moodle-synthetic-soak.sh start --duration-seconds 7200
bash automation/moodle-synthetic-soak.sh status
# After completion:
bash automation/moodle-synthetic-soak.sh collect
```

The result must show success rate at least 99%. Never copy the synthetic
password or transaction evidence outside `terraform/.artifacts/`.

### 11.2 One smoke trial for every approved scenario

Each trial follows baseline -> scoped injection -> expected symptom/alert ->
allowlisted reset -> clean baseline verification. It has a cleanup trap, but
the owner must still review its result.

```bash
for scenario in DB-01 RES-01 NET-01 CON-01 SEC-02; do
  bash automation/moodle-fault-trial.sh "$scenario"
done
```

| Scenario | Fault boundary | Expected alert |
|---|---|---|
| `DB-01` | Reject Moodle-to-RDS PostgreSQL traffic only | `MoodleSyntheticTransactionFailed` |
| `RES-01` | Bounded CPU-load container on Moodle B | `MoodleNodeCpuHigh` |
| `NET-01` | Remove the staging Moodle DNS alias only | `MoodleSyntheticTransactionFailed` |
| `CON-01` | Stop one Moodle web container only | `MoodleWebContainerMissing` |
| `SEC-02` | Scoped Moodledata permission drift | `MoodleSyntheticTransactionFailed` |

Optional restore evidence uses a manual RDS snapshot and a temporary restored
database. The helper removes that temporary database after connectivity checks:

```bash
bash automation/moodle-rds-restore-drill.sh
```

## 12. Sprint 2: reproducible pipeline and live SLO campaign

First validate the Agent's policy/evidence pipeline without mutating cloud or
runtime infrastructure:

```bash
python3 automation/moodle-pipeline-replay.py \
  --output-dir terraform/.artifacts/moodle-pipeline-replay
```

Expected replay evidence: one incident per scenario, at least three evidence
references, hash-chained audit records, an allowlisted dry-run action, and an
independent verifier verdict.

For a formal live campaign, the harness performs baseline -> scoped injection
-> fresh Prometheus alert -> Agent shadow observation -> allowlisted reset ->
independent verification -> stable baseline. In `shadow` mode the Agent does
not execute arbitrary remediation; the approved harness performs the reset.

```bash
MOODLE_SPRINT2_CAMPAIGN=final-evidence-$(date -u +%Y%m%dT%H%M%SZ) \
  bash automation/moodle-sprint2-live-drill.sh all \
  --runs 3 \
  --stability-seconds 120
```

Accept Sprint 2 only if every scenario has `runs=3` and `passed=3`, with:

- MTTD ≤60 seconds;
- recovery ≤600 seconds (10 minutes);
- post-recovery stability ≥120 seconds.

Summarize a campaign without exposing secrets:

```bash
campaign=terraform/.artifacts/moodle-sprint2-live/<campaign-id>
find "$campaign" -name result.json -print0 | xargs -0 jq -s '
  sort_by(.scenario_id) | group_by(.scenario_id) |
  map({
    scenario: .[0].scenario_id,
    runs: length,
    passed: map(select(.status == "passed")) | length,
    max_mttd_seconds: map(.mttd_seconds) | max,
    max_recovery_seconds: map(.recovery_seconds) | max,
    min_stability_seconds: map(.stability_seconds) | min
  })'
```

## 13. End-of-session recovery and handoff

If an injector or campaign fails, do not repeat the fault immediately. Reset
the allowlisted scenarios and return to the baseline first:

```bash
bash automation/moodle-environment-baseline.sh reset
bash automation/moodle-environment-baseline.sh verify
```

Finish every infrastructure/test session with a drift check. `No changes` is
the expected result.

```bash
terraform -chdir=terraform plan \
  -input=false \
  -var-file=deployment.tfvars \
  -detailed-exitcode
```

Record the active Moodle and Agent image commit SHA, campaign ID, acceptance
metrics, result artifact paths, any alert observations, and the final Terraform
result. Redact credentials, IPs not needed for the report, secret ARNs, and
private keys.

## 14. Destroy the complete staging infrastructure

This is a destructive operation. It removes every resource Terraform currently
manages for this staging state: Moodle EC2 instances, monitor EC2/EIP, ALB and
target group, RDS PostgreSQL instance, EFS/access point/mount targets, NAT
Gateways/EIPs, VPC subnets/routes/internet gateway, security groups, and
Terraform-managed CloudWatch resources. It does not remove GitHub images,
repository source, local keys, or arbitrary resources absent from Terraform
state.

Before creating a destroy plan:

1. Stop active test/soak work and capture the evidence required for the report.
2. Decide whether this is a retained teardown or a complete removal. The
   current RDS configuration has `skip_final_snapshot=true` and
   `delete_automated_backups=true`, so a normal destroy does **not** create a
   final RDS snapshot and removes automated RDS backups.
3. If retention is required, create and wait for a manual snapshot before
   proceeding. It incurs snapshot storage cost and must be retained explicitly.
4. For a complete removal, inventory manual RDS snapshots and EFS AWS Backup
   recovery points before destroy, then delete only the reviewed identifiers
   after Terraform has removed their primary resources.
5. Confirm that the active AWS account, workspace, and Terraform state are the
   intended staging environment. Never use a console VPC deletion as a shortcut
   around Terraform state.

Optional manual snapshot, only after reviewing its name and cost:

```bash
export AWS_PROFILE=target-account
export AWS_REGION=ap-southeast-1
DB_IDENTIFIER="$(terraform -chdir=terraform output -json moodle_database | jq -r .instance_identifier)"
SNAPSHOT_ID="${DB_IDENTIFIER}-final-$(date -u +%Y%m%dT%H%M%SZ)"

aws rds create-db-snapshot \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --db-instance-identifier "$DB_IDENTIFIER" \
  --db-snapshot-identifier "$SNAPSHOT_ID"

aws rds wait db-snapshot-available \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --db-snapshot-identifier "$SNAPSHOT_ID"
```

Create and inspect a saved destroy plan. The plan must contain only the
expected Moodle staging resources and no resource outside the intended state.

EFS has `lifecycle.prevent_destroy=true` as a data-loss guardrail. For an
explicit full teardown, temporarily remove that `lifecycle` block from
`terraform/efs.tf`, run the reviewed destroy plan below, then restore the block
in source control once destroy completes. Do not use `-target` to bypass the
guardrail.

```bash
umask 077
mkdir -p terraform/.artifacts

terraform -chdir=terraform plan \
  -destroy \
  -input=false \
  -var-file=deployment.tfvars \
  -out=.artifacts/moodle-destroy.tfplan

terraform -chdir=terraform show -no-color .artifacts/moodle-destroy.tfplan
```

After explicit review, apply precisely that plan:

```bash
terraform -chdir=terraform apply .artifacts/moodle-destroy.tfplan
```

Verify Terraform no longer tracks resources:

```bash
terraform -chdir=terraform state list
```

### Remove snapshots and recovery points for a complete teardown

Terraform does not delete manual RDS snapshots or EFS recovery points managed
by AWS Backup. Run this section only when the explicit intent is to remove all
recoverable Moodle data, after the Terraform destroy succeeds. It is
irreversible.

First list the exact targets. Do not delete a snapshot/recovery point merely
because its name looks similar; verify its database identifier or EFS resource
ARN and creation time.

```bash
export AWS_PROFILE=target-account
export AWS_REGION=ap-southeast-1
DB_IDENTIFIER='moodle-staging-postgres' # Record the actual identifier before destroy.

aws rds describe-db-snapshots \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --snapshot-type manual \
  --query "DBSnapshots[?DBInstanceIdentifier=='$DB_IDENTIFIER'].{id:DBSnapshotIdentifier,status:Status,created:SnapshotCreateTime}" \
  --output table
```

Delete only a reviewed manual RDS snapshot identifier:

```bash
aws rds delete-db-snapshot \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --db-snapshot-identifier 'REVIEWED_MANUAL_SNAPSHOT_ID'
```

For EFS, record its ARN before destroy or retrieve it from the captured
baseline. The loop below is deliberately scoped to the EFS automatic backup
vault and the one reviewed Moodle EFS ARN. First run the listing command and
inspect every returned ARN; only then run the deletion loop.

```bash
EFS_ARN='arn:aws:elasticfilesystem:ap-southeast-1:ACCOUNT_ID:file-system/fs-REVIEWED_ID'
BACKUP_VAULT='aws/efs/automatic-backup-vault'

aws backup list-recovery-points-by-backup-vault \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --backup-vault-name "$BACKUP_VAULT" \
  --query "RecoveryPoints[?ResourceArn=='$EFS_ARN'].{arn:RecoveryPointArn,created:CreationDate,status:Status}" \
  --output table
```

```bash
mapfile -t RECOVERY_POINTS < <(
  aws backup list-recovery-points-by-backup-vault \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --backup-vault-name "$BACKUP_VAULT" \
    --query "RecoveryPoints[?ResourceArn=='$EFS_ARN'].RecoveryPointArn" \
    --output text | tr '\t' '\n'
)

for recovery_point in "${RECOVERY_POINTS[@]}"; do
  test -n "$recovery_point" || continue
  aws backup delete-recovery-point \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --backup-vault-name "$BACKUP_VAULT" \
    --recovery-point-arn "$recovery_point"
done
```

The default AWS-managed EFS backup vault can contain an explicit resource-policy
`Deny` for `backup:DeleteRecoveryPoint`. If deletion returns
`AccessDenied ... explicit deny`, do not silently weaken the shared vault
policy. An account administrator must separately review and approve a narrowly
scoped policy/retention change, then verify the recovery point is gone.

Finally remove local secret/artifact copies in accordance with the
workstation's approved secure-deletion policy. Do not commit them as a backup.

## 15. Cost and shutdown reminder

Stopping EC2 reduces instance compute charges but does not eliminate RDS,
EFS, NAT Gateway, ALB, EIP, backup/snapshot, or storage costs. Preserve Sprint
evidence first. When the staging environment is no longer needed, review a
Terraform destroy plan explicitly; never use a broad/manual AWS deletion as a
substitute for Terraform state management.
