# Moodle to RDS connectivity

This runbook applies to the Moodle staging topology: two Moodle EC2 nodes, an
Application Load Balancer, RDS PostgreSQL, and Prometheus probes. Use it for
`MoodleRdsTcpProbeFailed`, `MoodleNodeRdsTcpFailed`, or
`MoodleNodeRdsTcpLatencyHigh`.

## Evidence to collect

- Compare the public synthetic transaction with the direct node-to-RDS TCP
  probe. A failed application transaction alone does not identify a database
  network fault.
- Check both Moodle nodes and confirm which node emitted the alert.
- Review the current RDS endpoint and port from deployed runtime configuration
  without exposing credentials.
- Check RDS availability and connection metrics, node probe timestamps, and
  the relevant security-group path.
- Preserve timestamps, source names, resource IDs, and content hashes. Redact
  credentials and connection strings.

## Interpretation and safety

- TCP success with high latency is distinct from a failed TCP connection.
- A synthetic-transaction failure can also result from application or storage
  problems; correlate it with independent database and web probes.
- Do not open RDS ingress broadly, disable TLS, rotate the database master
  credential, or claim recovery from a green health endpoint alone.
- Any configuration change must use the reviewed staging procedure and be
  followed by database TLS, Moodle synthetic-transaction, and independent
  communication-contract checks. Security-group changes require the
  infrastructure operator.
