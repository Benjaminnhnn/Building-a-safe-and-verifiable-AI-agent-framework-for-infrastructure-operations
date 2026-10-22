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

## Missing Okta sign-in option

`MoodleSsoLoginOptionMissing` fires when the Moodle login page renders but no
OAuth 2 sign-in link is present, while the login-page probe still passes. The
fault is in the identity configuration, not in the web, database, or storage
layers.

- Collect read-only evidence: the SSO-option probe, the login-page probe, the
  ALB target health, and whether local form login still works.
- Likely causes: the Okta OAuth 2 issuer is disabled or hidden from the login
  page, the OAuth 2 authentication plugin is disabled, or the issuer lacks a
  required endpoint.
- Remediation is HUMAN_ONLY: an operator re-enables or repairs the issuer in
  Moodle site administration. The agent must not change identity settings.
- After the change, confirm that the sign-in link redirects to the Okta
  authorize endpoint over the stability window and that a test user can sign in.
