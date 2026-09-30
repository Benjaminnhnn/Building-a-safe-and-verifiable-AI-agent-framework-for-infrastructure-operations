# Sprint 3 Baseline

Baseline status: frozen as of 2026-09-27. Sprint 3 status: local acceptance
passed on 2026-09-29; PR CI and reviewed merge are still required for formal
closure. See the combined [Sprint 3–4 completion report](SPRINT3_SPRINT4_COMPLETION_REPORT.md).

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

The consolidated Sprint 3 stage results, 15/15 offline scenario replay, five
allowlisted staging drills and current read-only verification, executor
off-switch, Terraform `No changes` record, and formal closeout gate are in
[`SPRINT3_SPRINT4_COMPLETION_REPORT.md`](SPRINT3_SPRINT4_COMPLETION_REPORT.md).
The feature-branch changes still require GitHub CI and reviewed PR merge before
Sprint 3 is formally closed. Live testing remains explicitly approved and
separate from replay/CI.
