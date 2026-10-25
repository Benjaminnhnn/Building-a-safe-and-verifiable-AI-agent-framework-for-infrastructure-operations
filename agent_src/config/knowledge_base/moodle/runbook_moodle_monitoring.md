# Moodle monitoring signal quality

This runbook applies to `MoodleNodeExporterDown`, `MoodleCAdvisorDown`,
`MoodleNodeProbeStale`, `MoodleSyntheticTransactionStale`,
`MoodleAdhocQueueProbeFailed`, and `MoodleRdsIngressAuditFailed`.

## Evidence to collect

- Check Prometheus target health and scrape timestamps for the named exporter
  or node-local probe.
- Compare monitoring freshness with the independent public Moodle probe and
  the synthetic-transaction last-run timestamp.
- Verify that the monitoring host and Alertmanager are reachable before
  treating missing telemetry as application recovery or failure.
- Keep this alert distinct from an observed Moodle failure: stale telemetry
  means the relevant state is unknown until an independent probe succeeds.
- For `MoodleAdhocQueueProbeFailed`, run the node-local probe service by hand
  and check that the web container can bootstrap Moodle from the CLI.
- For `MoodleRdsIngressAuditFailed`, check that monitor-ai-01 has the
  read-only audit instance role and the `AiopsRdsSecurityGroupId` and
  `AiopsRdsReviewedSourceGroups` tags, and that instance metadata tags are
  enabled. While it fires, SEC-01 exposure cannot be detected.

## Interpretation and safety

- Missing or stale monitoring data must not be converted into a healthy verdict
  or a remediation success claim.
- Do not restart Moodle solely to address an exporter or stale-probe alert.
- Restore telemetry through the reviewed monitoring-host procedure, then
  collect fresh Moodle, database, and storage observations before resolving an
  incident.
