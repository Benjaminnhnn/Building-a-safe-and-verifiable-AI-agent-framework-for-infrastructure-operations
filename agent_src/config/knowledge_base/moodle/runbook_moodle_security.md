# Moodle database exposure and proxy trust

This runbook applies to `MoodleDatabasePortExposure`,
`MoodleTrustedProxyMisconfigured`, and `MoodleTrustedProxyConfigDrift`.

## Evidence to collect

- For database exposure, inspect the effective RDS security-group ingress and
  confirm that only reviewed application-node sources can reach PostgreSQL.
  Preserve the rule identifiers and source ranges; do not copy credentials.
- For proxy trust alerts, compare the running Moodle configuration with the
  approved load-balancer and trusted-proxy baseline.
- Check the public probe, Moodle synthetic transaction, and database probe to
  distinguish proxy rejection from an application or database outage.

## Interpretation and safety

- Never broaden database ingress to the internet or an unrestricted CIDR.
- Security-group remediation is HUMAN_ONLY and belongs to the infrastructure
  operator. The AI agent may gather read-only evidence and hand off findings.
- Do not trust client-supplied forwarding headers or expand the trusted-proxy
  list to silence a failed public probe.
- After an approved change, repeat the exposure probe, Moodle transaction, and
  independent communication-contract verification before reporting recovery.
