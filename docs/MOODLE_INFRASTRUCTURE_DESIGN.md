# Moodle Infrastructure Design

Status: Accepted for Sprint 1 implementation
Date: 2026-09-16
Owner: Infrastructure Engineer

## 1. Objective

Deploy Moodle as a near-stateless application across two Availability Zones on
AWS. The infrastructure must support monitoring and controlled fault-injection
experiments without allowing infrastructure automation to hide experiment
results.

The canonical diagram is
[`diagram/moodle-aiops-alb-rds-efs-redis.drawio`](../diagram/moodle-aiops-alb-rds-efs-redis.drawio).
The legacy filename is retained to avoid breaking existing references; Redis is
not part of the Moodle data path in Sprint 1.

## 2. Accepted architecture

```text
Internet
  -> Internet-facing ALB in public subnets A and B
    -> fixed Moodle EC2 A in private app subnet A
    -> fixed Moodle EC2 B in private app subnet B

Moodle EC2 A/B
  -> RDS PostgreSQL Single-AZ ở private data subnet A
  -> Regional EFS through one mount target in each application AZ

Administrator / CI
  -> monitor-ai-01
    -> private Moodle EC2 instances through SSH ProxyJump

monitor-ai-01
  -> Prometheus, Grafana, Alertmanager and Blackbox Exporter
  -> Node Exporter and cAdvisor on both Moodle hosts
  -> PostgreSQL exporter using a read-only database account
```

## 3. Scope decisions

### Included in Sprint 1

- One VPC spanning two Availability Zones.
- Two public subnets for the internet-facing ALB.
- Two private application subnets for fixed Moodle EC2 instances.
- Two private data subnets for the RDS DB subnet group.
- One NAT Gateway per AZ so each private application subnet has independent
  outbound access.
- Two fixed Moodle EC2 instances, one per AZ.
- One private RDS PostgreSQL Single-AZ `db.t4g.micro` instance in Data A.
- One Regional EFS file system and one mount target per application AZ.
- One separate `monitor-ai-01` host for monitoring and bastion access.
- Terraform for AWS resources, Ansible for host configuration, and the existing
  release gate for application deployment.

### Explicitly excluded from Sprint 1

- Auto Scaling Group and automatic EC2 replacement.
- AWS Systems Manager Session Manager or Run Command.
- ElastiCache/Redis for Moodle sessions or application cache.
- Kubernetes/EKS.
- Public IP addresses on Moodle EC2 instances.
- PostgreSQL containers on Moodle EC2 instances.
- Automatic remediation by the AI Agent.

The AI Agent's existing internal Redis/Celery dependency is separate from the
Moodle architecture. It may remain on the monitoring host for Sprint 2, but it
must not be treated as Moodle shared state.

## 4. Resource naming

| Resource | Logical name | AWS Name tag |
|---|---|---|
| VPC | `main` | `moodle-staging-vpc` |
| Public subnet A | `public_a` | `moodle-staging-public-a` |
| Public subnet B | `public_b` | `moodle-staging-public-b` |
| App subnet A | `app_a` | `moodle-staging-app-a` |
| App subnet B | `app_b` | `moodle-staging-app-b` |
| Data subnet A | `data_a` | `moodle-staging-data-a` |
| Data subnet B | `data_b` | `moodle-staging-data-b` |
| Moodle EC2 A | `moodle_a` | `moodle-app-a` |
| Moodle EC2 B | `moodle_b` | `moodle-app-b` |
| Monitoring EC2 | `monitor` | `monitor-ai-01` |
| ALB | `moodle` | `moodle-staging-alb` |
| Target group | `moodle` | `moodle-staging-tg` |
| RDS PostgreSQL | `moodle` | `moodle-staging-postgres` |
| EFS | `moodledata` | `moodle-staging-efs` |

Required common tags:

```text
Project=moodle-aiops
Environment=staging
ManagedBy=Terraform
Owner=Infrastructure
ExperimentScope=thesis
```

## 5. Network plan

VPC CIDR: `10.10.0.0/16`

| Tier | AZ | CIDR | Public IP on launch | Route |
|---|---|---|---|---|
| Public | A | `10.10.1.0/24` | Yes | Internet Gateway |
| Public | B | `10.10.2.0/24` | Yes | Internet Gateway |
| Application | A | `10.10.11.0/24` | No | NAT Gateway A |
| Application | B | `10.10.12.0/24` | No | NAT Gateway B |
| Data | A | `10.10.21.0/24` | No | No default Internet route |
| Data | B | `10.10.22.0/24` | No | No default Internet route |
| Management | A | `10.10.31.0/24` | No; monitor uses explicit EIP | Internet Gateway |

The ALB spans public A and public B. `moodle-app-a` and its EFS mount target are
placed in AZ A; `moodle-app-b` and its EFS mount target are placed in AZ B. EFS
mount targets live in the data subnet of the corresponding AZ (matching the
diagram); clients reach them through the VPC local route. The RDS subnet group
contains data A and data B.

Migration defaults preserve project/environment names and EC2 subnet placement.
Optional legacy EC2/EIPs use moved blocks to preserve state identity when adding
count. New resources use the `moodle-staging` prefix. Existing monitoring stays
in public A by default; a new deployment can explicitly choose Management A.
Changing the subnet of an existing monitor replaces it. See the
[Moodle Operations Runbook](MOODLE_OPERATIONS_RUNBOOK.md) for reviewed input
choices.

## 6. Security-group contract

Security-group references are preferred over CIDR rules for internal traffic.

| Source | Destination | Protocol/port | Purpose |
|---|---|---:|---|
| Internet | ALB SG | TCP 80/443 | Moodle entry point |
| ALB SG | Moodle SG | TCP 8080 | Web traffic and health check |
| Admin CIDR | Monitor SG | TCP 22 | Restricted bastion access |
| Admin CIDR | Monitor SG | TCP 3000 | Restricted Grafana access |
| Monitor SG | Moodle SG | TCP 22 | Ansible/SSH through bastion |
| Monitor SG | Moodle SG | TCP 9100 | Node Exporter scrape |
| Monitor SG | Moodle SG | TCP 8088 | cAdvisor scrape |
| Moodle SG | RDS SG | TCP 5432 | Moodle database endpoint |
| Monitor SG | RDS SG | TCP 5432 | Read-only PostgreSQL exporter |
| Moodle SG | EFS SG | TCP 2049 | Shared `moodledata` mount |

Forbidden ingress:

- Internet to Moodle EC2, RDS or EFS.
- Internet to Prometheus, Alertmanager, exporters or the AI Agent API.
- Moodle EC2 directly to SSH/admin ports on the monitoring host.
- Any database ingress not originating from Moodle SG or the approved
  monitoring exporter identity.

## 7. Moodle state and deployment decisions

| Data | Location | Decision |
|---|---|---|
| Moodle application code | Immutable container image | Same pinned image on both EC2 instances |
| Moodle configuration | Generated from deployment secrets/config | Same logical configuration on both nodes |
| Business data | RDS PostgreSQL | No local database container |
| `moodledata` | EFS Regional | Mounted through an EFS Access Point |
| Web session | EFS-backed file session for Sprint 1 | Redis deferred; validate locking and latency |
| Plugin/theme code | Container image | No manual per-host modification |
| Cron | `moodle-app-a` only | Avoid duplicate scheduled execution in Sprint 1 |
| Logs | Container/host logs plus monitoring collection | No application log used as source of truth |

The deployment remains near-stateless, not fully stateless: active file sessions
and uploaded files depend on EFS. Redis may replace file sessions later if EFS
session locking or latency becomes a measured bottleneck.

## 8. Availability and experiment behavior

- The ALB distributes requests across two fixed EC2 targets.
- Losing one Moodle target must not make the ALB endpoint unavailable.
- No ASG will replace a stopped instance; this preserves a deterministic target
  for fault-injection and lets the agent's remediation be measured.
- RDS is deliberately Single-AZ for the short test environment. Database-level
  faults such as connection exhaustion remain visible, but database failover is
  not evaluated in this deployment.
- EFS is Regional and reachable through a mount target in each application AZ.
- The monitoring host is intentionally single-instance in Sprint 1. Monitoring
  failure affects observability, not the Moodle request path, and is recorded as
  a known limitation.
- Moodle cron is temporarily a single-node function. Web availability can
  continue when node A is down, but scheduled jobs may pause until recovery.

## 9. Access without Systems Manager

Moodle EC2 instances have no public IP. Human and CI access uses:

```text
workstation or CI runner -> monitor-ai-01 -> Moodle private IP
```

Ansible and SSH must use `ProxyJump`. Private keys remain on the workstation or
CI runner and must not be copied to `monitor-ai-01`.

The current Terraform provider reads the latest Amazon Linux AMI ID from a
public SSM Parameter Store path. This is only an AMI lookup and does not deploy
or authorize Systems Manager on the instances. It may be replaced with an
`aws_ami` data source if the project requires zero SSM API dependency.

## 10. Baseline sizing and cost controls

Initial staging defaults:

- Moodle EC2: `t3.small`, 30 GiB encrypted gp3 each (release/rollback headroom).
- Monitoring EC2: `t3.small`, 30 GiB encrypted gp3.
- RDS PostgreSQL: version 16.10, Single-AZ `db.t4g.micro` in Data A; 20 GiB
  encrypted gp3 storage.
- EFS: General Purpose performance mode with lifecycle policy enabled.
- Two NAT Gateways for AZ independence.

Database/storage configuration, credentials and runtime acceptance are maintained
in [Moodle Database and Shared Storage](MOODLE_DATABASE_STORAGE.md).

Instance and database sizes remain Terraform variables. CPU, memory, database
connections and p95 latency from the two-hour baseline determine whether sizing
must change before the benchmark.

Cost alarms and daily resource review are required because RDS and two
NAT Gateways are the main continuous-cost items.

## 11. TLS and health checks

- Preferred listener: HTTPS 443 using ACM; HTTP 80 redirects to HTTPS.
- If a domain and certificate are not available on the first deployment day,
  HTTP is permitted only for the staging smoke test and must be documented.
- ALB target health endpoint: `/healthz` on port 8080.
- `/healthz` is a fast readiness check; the independent synthetic transaction
  performs Moodle login/read/write and verifies the full DB/EFS path.

## 12. Design acceptance checklist

- [x] Final target architecture is documented.
- [x] Multi-AZ network CIDRs are assigned without overlap.
- [x] Resource names and mandatory tags are defined.
- [x] Security-group flows and forbidden paths are defined.
- [x] Moodle code, database, files, sessions and cron ownership are defined.
- [x] ASG, Moodle Redis and Systems Manager execution are explicitly deferred.
- [x] Monitoring location and its single-instance limitation are documented.
- [x] Design preparation does not create or change AWS resources.

## 13. Network and security implementation

Implementation details are maintained in [Moodle Network and Security](MOODLE_NETWORK_SECURITY.md).
The network and security implementation follows these steps:

1. Add the second public subnet and both app/data subnet pairs.
2. Add Internet Gateway/public route associations and one NAT Gateway per AZ.
3. Add private application routes and isolated data route tables.
4. Replace public service ingress with ALB, Moodle, RDS PostgreSQL, EFS and monitor
   security-group contracts.
5. Run `terraform fmt`, `terraform validate` and inspect a saved plan.
6. Do not apply a plan that destroys the current payment/demo infrastructure.
