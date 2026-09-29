# Sprint 3 Baseline

Status: frozen pending the Sprint 3 integration work.

This manifest identifies the reviewed repository state from which Sprint 3
starts. It is deliberately separate from AWS runtime evidence: no Terraform
apply, Ansible run, secret staging, or Moodle deployment occurs when this
baseline is created.

## Immutable reference

| Field | Value |
| --- | --- |
| Baseline quality-gate commit | `aeb6676d0afc9bfa1e83e5edab75e95b86785002` |
| Quality-gate subject | `chore: complete Moodle Sprint 3 quality gate` |
| Baseline tag | `sprint3-baseline-20260927` |
| Branch at freeze | `develop` |
| Scope | Moodle staging delivery, monitoring/AI Agent source, Terraform and Ansible definitions |

The annotated Git tag is the release reference. The tag must point to the
commit containing this manifest; the quality-gate commit above is the tested
implementation immediately preceding this administrative baseline record.

## Gates already satisfied

The following checks were run against the quality-gate implementation:

1. `terraform fmt -check -recursive`, `terraform validate`, and `terraform test`
   completed successfully (19 Terraform tests passed).
2. Agent critical Ruff checks and pytest passed: 277 tests passed.
3. Moodle and unified AI control-plane replay completed with all 15 Moodle
   ground-truth scenarios in non-mutating verification mode.
4. Moodle and AI Agent Docker build paths, secret-file handling, PHP syntax,
   and Compose configuration were validated.
5. Git index contains no Terraform state, SSH keys, secret artifacts, or
   ChromaDB runtime database. `agent_src/vector_db/` remains local ignored
   runtime data rather than release source.

## Freeze rules

- New Sprint 3 work uses a feature branch and is merged through a reviewed PR.
- Every change must keep the CI quality gates in `.github/workflows/ci.yml`
  green before merge.
- Terraform and Ansible remain explicit, reviewed operator actions; CI/CD must
  not acquire authority to apply or destroy AWS resources.
- Secrets, Terraform state, `.artifacts`, SSH keys, and runtime vector data
  remain outside Git.
- Any change to an action adapter must preserve the allow-list, dry-run,
  idempotency, timeout, and evidence requirements defined for Sprint 3.

## Sprint 3 exit evidence

Sprint 3 can be closed only when the contract mapping, evidence collectors,
safe infrastructure adapters, and all Moodle scenario replays are recorded in
their corresponding runbooks and pass in CI. The S3.3 adapter/executor boundary
and its explicit staging drill procedure are documented in
`docs/SPRINT3_S3_3_SAFE_EXECUTOR.md`. Live infrastructure testing is a separate,
explicitly approved operation with its own runtime evidence; do not close S3.3
until all five first-slice reset drills and final off-switch verification pass.
