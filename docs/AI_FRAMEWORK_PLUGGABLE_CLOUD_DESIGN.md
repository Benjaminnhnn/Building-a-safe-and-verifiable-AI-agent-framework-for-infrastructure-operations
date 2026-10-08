# Plug-and-play cloud profile design

## Target outcome

Keep incident handling independent of the cloud: Monitor -> Detect -> Collect
Evidence -> Diagnose -> Plan -> Safety Gate -> Execute -> Verify -> Resolve or
Rollback -> Audit. Switching the deployment target changes profile data and
the provider adapter, not the incident contracts or the safety rules.

## Profile contract

Each deployment profile should provide one validated `TargetProfile` with:

- `target`: `floci` or `aws`.
- `environment`: local or staging/production.
- `terraform_root` and a profile-specific state location.
- `metrics_url`, `alertmanager_url`, and the workload health endpoints.
- `resource_inventory` mapping logical Moodle services to provider resource IDs.
- `executor_adapter` and `verifier_endpoint`.
- `evidence_class`, so local Floci runs cannot be recorded as AWS acceptance.

The AI policy, incident schema, diagnosis, and verifier consume logical resource
IDs and typed actions. A provider adapter translates an approved action to the
selected backend. Model output never supplies shell commands, provider API
arguments, credentials, or resource IDs outside the reviewed inventory.

## Provider boundary

| Concern | Floci profile | AWS profile |
| --- | --- | --- |
| Terraform | `terraform/local-floci/`, fake credentials, endpoint restricted to loopback | `terraform/`, standard AWS provider and IAM role/profile |
| Workload runtime | Local Docker Compose Moodle replicas and support services | EC2/Ansible and the Moodle deployment workflow |
| Data plane | Docker-backed PostgreSQL behind Floci RDS compatibility; local Docker volume for Moodle data | AWS RDS, EFS and EC2 data plane |
| Remediation adapter | AUTH-01 allowlisted Docker actuator only | Reviewed Safe Executor/SSM action slices only |
| Evidence | `runtime`, `local-auth-lab` | AWS staging/live evidence, separately verified |

Terraform states remain separate. A profile switch must never reuse the other
profile's state, credentials, resource identifiers, or evidence labels. Floci
must reject non-loopback endpoints. AWS credentials should come from a named
profile or instance role; AWS apply remains an explicit operator action.

## Current implementation boundary

The repository already has separate Floci and AWS Terraform roots, environment
variables for Prometheus and executor endpoints, typed infrastructure adapter
contracts, and a fixed local AUTH-01 actuator. The new
`automation/select-cloud-profile.ps1` routes Floci start/plan and AWS show/plan
to the correct roots. Its AWS path rejects `AWS_ENDPOINT_URL`, requires a
named profile, and does not apply resources. It is an infrastructure profile
selector, not proof that live remediation is interchangeable: AUTH-01 is local
Docker-specific, while the AWS Terraform root provisions a different workload
shape. Do not describe the current lab as an AWS deployment or as full
provider plug-and-play.

The next step for full runtime plug-and-play is a common contract at deployment startup:
load one profile, validate its endpoint and credential source, produce a
redacted effective configuration, then pass the same logical inventory and
evidence label to monitoring, agent, executor, and verifier. Provider-specific
adapters should advertise capabilities and fail closed when an action is not
implemented for that target. Roll out additional AWS actions only as separately
reviewed slices with their own rollback and independent verification.

## Grafana dashboard

The local AUTH lab provisions Grafana with a Prometheus datasource and a
read-only dashboard. Dashboard queries use the same stable probe and agent
metric names intended for future profiles, so the dashboard can follow AWS
after its datasource points to the AWS Prometheus endpoint. Metrics are for
operator visibility; verifier output and immutable incident evidence remain the
authority for resolution claims.
