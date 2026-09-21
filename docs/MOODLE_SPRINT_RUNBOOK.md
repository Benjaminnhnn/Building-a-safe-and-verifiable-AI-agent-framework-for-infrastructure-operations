# Moodle infrastructure and Sprint 1–2 runbook

This is the operational runbook for a clean Moodle staging environment. Run
commands from the repository root. Terraform owns AWS resources; Ansible owns
host configuration; the release scripts own Moodle runtime files; no secret,
state file, private key, or `.artifacts` file is committed.

## 1. Preconditions

Install Terraform, AWS CLI, OpenSSH, Ansible, Docker, `jq`, `curl`, and
`openssl` on the control workstation. Configure a profile named
`target-account` (or export `AWS_PROFILE` with an equivalent profile) and a
local SSH key pair. The AWS principal needs permission for the Terraform
resources, Secrets Manager read for the RDS master secret during staging, and
the normal EC2/RDS/EFS/ELB read permissions used by the checks.

```bash
export AWS_PROFILE=target-account
aws sts get-caller-identity --profile "$AWS_PROFILE"
terraform version
ansible-playbook --version
```

Create the local variable file once. It is ignored by Git.

```bash
cp -n terraform/deployment.tfvars.example terraform/deployment.tfvars
bash automation/update-infrastructure.sh --var-file terraform/deployment.tfvars
```

Review `terraform/deployment.tfvars` and set at least:

- `public_key_path` and `private_key_path` to the local key pair, using a
  user-relative path such as `~/.ssh/moodle-aiops` rather than a hardcoded home.
- `ec2_ami_id` to a pinned Amazon Linux 2023 x86_64 AMI for the selected region.
- `enable_legacy_demo=false`, intended instance classes, and `moodle_allow_http`
  only for an HTTP staging smoke test. Use a certificate and hostname for HTTPS.

Never put the private key, passwords, PATs, `terraform.tfstate*`, or
`deployment.tfvars` in Git.

## 2. Provision AWS infrastructure from zero

```bash
terraform -chdir=terraform init -input=false
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform test

umask 077
mkdir -p terraform/.artifacts
terraform -chdir=terraform plan \
  -input=false \
  -var-file=deployment.tfvars \
  -out=.artifacts/deployment.tfplan
terraform -chdir=terraform show -no-color .artifacts/deployment.tfplan
```

Apply only the plan you reviewed. If its inputs, code, or state changed, create
a new plan; a saved plan is intentionally rejected as stale.

```bash
terraform -chdir=terraform apply .artifacts/deployment.tfplan
```

Export connection artifacts and validate the topology:

```bash
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

Expected resources are a public monitor node, two private Moodle nodes behind
an ALB, PostgreSQL RDS, and EFS mount targets. ALB targets are expected to be
unhealthy until Moodle is deployed.

## 3. Configure Moodle hosts and deploy Moodle

Prepare Docker, Compose, EFS TLS/access-point mount, and generated Ansible
inventory. This is idempotent and does not build an image on EC2.

```bash
bash automation/prepare-moodle-runtime.sh
```

Use a GitHub PAT classic limited to `read:packages` when the Moodle package is
private, then configure pull access on both application nodes. Enter the PAT
only when prompted; it is not accepted as a command-line argument.

```bash
bash automation/configure-moodle-ghcr-access.sh \
  ghcr.io/benjaminnhnn/moodle:<moodle-image-commit-sha>

bash automation/moodle-preflight.sh \
  ghcr.io/benjaminnhnn/moodle:<moodle-image-commit-sha>
```

Create root-only runtime secrets and stage the RDS application role, CA bundle,
Compose file, and runtime environment. The stage command does not start Moodle.

```bash
umask 077
mkdir -p terraform/.artifacts/moodle-secrets
openssl rand -hex 32 > terraform/.artifacts/moodle-secrets/moodle-db-password
openssl rand -base64 32 > terraform/.artifacts/moodle-secrets/moodle-admin-password
chmod 600 terraform/.artifacts/moodle-secrets/moodle-*-password

bash automation/stage-moodle-release.sh \
  ghcr.io/benjaminnhnn/moodle:<moodle-image-commit-sha> \
  terraform/.artifacts/moodle-secrets/moodle-db-password \
  terraform/.artifacts/moodle-secrets/moodle-admin-password
```

Run the installer once on node A, then run the web/cron role on A and the web
role on B. Do not run the installer concurrently on both nodes.

```bash
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
  'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
    -f /opt/moodle/release/docker-compose.yml --profile installer run --rm moodle-install'

ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-a \
  'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
    -f /opt/moodle/release/docker-compose.yml --profile cron up -d --wait moodle-web moodle-cron'
ssh -F terraform/.artifacts/moodle-ssh.config moodle-app-b \
  'sudo docker compose --env-file /opt/moodle/release/moodle-runtime.env \
    -f /opt/moodle/release/docker-compose.yml up -d --wait moodle-web'

curl --fail --silent "$(terraform -chdir=terraform output -json moodle_application | jq -r .url)/healthz.php"
```

The local Moodle administrator password is readable, not executable:

```bash
cat terraform/.artifacts/moodle-secrets/moodle-admin-password
```

## 4. Start or restore the monitoring system

Deploy the isolated Moodle observability stack: Node Exporter/cAdvisor on both
Moodle nodes; Prometheus, Alertmanager, Grafana, Blackbox Exporter, synthetic
runner, and optional agent/Redis/Celery on the monitor node. Supply an immutable
40-character agent image SHA when the agent webhook is enabled.

```bash
bash automation/configure-moodle-monitoring.sh \
  --agent-image ghcr.io/benjaminnhnn/moodle-ai-agent:<agent-commit-sha>
```

After an EC2 stop/start, Docker containers normally restart through their
`unless-stopped` policy. If the monitoring stack was manually stopped, start it
explicitly and enable its system timers:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config monitor-ai-01 \
  'sudo docker compose -f /opt/moodle-observability/docker-compose.yml up -d && \
   sudo systemctl enable --now moodle-synthetic-transaction.timer && \
   sudo systemctl status --no-pager moodle-synthetic-transaction.timer'

for host in moodle-app-a moodle-app-b; do
  ssh -F terraform/.artifacts/moodle-ssh.config "$host" \
    'sudo systemctl enable --now moodle-efs-metric.timer && \
     sudo systemctl status --no-pager moodle-efs-metric.timer'
done
```

Use an SSH tunnel to inspect services without exposing their ports publicly:

```bash
ssh -F terraform/.artifacts/moodle-ssh.config -N \
  -L 9090:127.0.0.1:9090 \
  -L 9093:127.0.0.1:9093 \
  -L 3000:127.0.0.1:3000 monitor-ai-01
```

Open Prometheus at `http://127.0.0.1:9090`, Alertmanager at port `9093`, and
Grafana at `http://127.0.0.1:3000`. Grafana's password remains root-only on the
monitor node; retrieve it only in a private terminal.

## 5. Sprint 1 evidence

Capture a clean baseline first. The verification checks runtime checksum,
container health, EFS, ALB, synthetic transaction, cron, Prometheus targets and
alerts.

```bash
bash automation/moodle-environment-baseline.sh capture
bash automation/moodle-environment-baseline.sh verify
```

Run the two-hour authenticated synthetic soak and collect its secret-free
evidence:

```bash
bash automation/moodle-synthetic-soak.sh start --duration-seconds 7200
bash automation/moodle-synthetic-soak.sh status
# After the service finishes:
bash automation/moodle-synthetic-soak.sh collect
```

Run an initial fault/reset smoke trial for each reviewed scenario. Every trial
injects only a scoped staging fault, waits for an expected alert, resets it,
and verifies the baseline.

```bash
for scenario in DB-01 RES-01 NET-01 CON-01 SEC-02; do
  bash automation/moodle-fault-trial.sh "$scenario"
done
```

Optional restore evidence must use a manual RDS snapshot and the helper deletes
the temporary restored database after the connectivity check:

```bash
bash automation/moodle-rds-restore-drill.sh
```

## 6. Sprint 2 pipeline and live evidence

First run deterministic local replay. It must create one incident per scenario,
at least three evidence references, hash-chained audit entries, allowlisted
dry-run action, and an independent-verifier verdict.

```bash
python3 automation/moodle-pipeline-replay.py \
  --output-dir terraform/.artifacts/moodle-pipeline-replay
```

For live evidence, the harness performs baseline -> scoped inject -> fresh
Prometheus alert after the injection timestamp -> gated scoped reset ->
independent verification -> 120-second stability baseline. It stops on an SLO
failure and its cleanup trap removes the injected fault.

```bash
MOODLE_SPRINT2_CAMPAIGN=final-evidence-$(date -u +%Y%m%dT%H%M%SZ) \
  bash automation/moodle-sprint2-live-drill.sh all \
  --runs 3 \
  --stability-seconds 120
```

Summarize one campaign. All three values are acceptance criteria.

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

Accept Sprint 2 only when every scenario has `runs=3`, `passed=3`, MTTD at most
60 seconds, recovery at most 600 seconds, and stability at least 120 seconds.

Finish with an infrastructure drift check:

```bash
terraform -chdir=terraform plan \
  -var-file=deployment.tfvars \
  -out=.artifacts/sprint2-final.tfplan
```

The expected result is `No changes`. Keep all evidence under
`terraform/.artifacts/`; it is intentionally not committed.

## 7. Verified Sprint 2 evidence record

The final campaign `final-evidence-20260921T160700Z` completed 15 live runs
with fresh Prometheus alerts, scoped resets, verifier verdicts, and a clean
baseline after every 120-second stability window.

| Scenario | Passes | Worst MTTD | Worst recovery | Stability |
|---|---:|---:|---:|---:|
| DB-01 | 3/3 | 43s | 44s | 120s |
| RES-01 | 3/3 | 56s | 32s | 120s |
| NET-01 | 3/3 | 38s | 86s | 120s |
| CON-01 | 3/3 | 38s | 61s | 120s |
| SEC-02 | 3/3 | 51s | 45s | 120s |

All results meet the Sprint 2 limits: MTTD ≤60 seconds, recovery ≤600 seconds,
stability ≥120 seconds, and zero forbidden execution. The raw per-run evidence
is deliberately retained only under `terraform/.artifacts/moodle-sprint2-live/`.

## 8. Safe failure handling

- Do not rerun an injector manually after a harness failure. Run
  `bash automation/moodle-environment-baseline.sh verify` first; its reset
  counterpart removes all reviewed faults if needed.
- Do not destroy or change RDS/EFS/ALB to resolve a scenario. The action catalog
  only permits scoped reset operations.
- If Terraform reports a stale saved plan, regenerate and review it. Never
  apply a stale plan.
- Stop EC2 only after collecting evidence. RDS, EFS, NAT gateways, ALB, EIPs,
  snapshots and storage can still incur charges while EC2 is stopped.
