# Moodle web service and routing

This runbook applies to Moodle staging web availability and routing alerts:
`MoodlePublicProbeFailed`, `MoodleWebContainerMissing`,
`MoodleWebContainerRestarting`, `MoodleApacheRouterMissing`,
`MoodleNodeRouterFallbackFailed`, and `MoodleTrustedProxyConfigDrift`.

## Evidence to collect

- Compare the public ALB probe with node-local web-container health and the
  target-health state for both application nodes.
- Check container state, recent restart count, and bounded log excerpts for
  the affected node. Keep secrets and learner data out of collected output.
- Check Apache router and fallback probes independently from the public probe.
- For trusted-proxy drift, compare runtime configuration with the reviewed
  staging baseline. Do not infer a safe proxy list from a request header.
- Confirm PostgreSQL and EFS signals remain healthy before attributing a web
  outage to the web container.

## Interpretation and safety

- One unhealthy target may reduce capacity while the second target still
  serves requests. Do not stop or deregister the healthy node.
- A running container does not prove the Moodle route or ALB target is healthy.
- Use only the reviewed Moodle Compose release and scoped node procedure for
  recovery. Do not build an image on an EC2 host or substitute a mutable image
  tag.
- Recovery requires both target-health checks, the Moodle synthetic
  transaction, and independent communication-contract verification.
