# Moodle data storage and node pressure

This runbook applies to `MoodleEfsMountMissing`, `MoodleNodeCpuHigh`, and
`MoodleNodeMemoryPressure` in the staging Moodle environment.

## Evidence to collect

- Identify the affected node and the state of the shared Moodle EFS mount.
- Compare node-exporter capacity metrics with the EFS mount probe and Moodle
  synthetic transaction. Preserve observation time and resource identity.
- For memory alerts, confirm the node's available-memory decrease against fresh
  samples. A rapid drop is a capacity signal, not proof of application failure
  or a safe remediation.
- Collect only bounded process/container summaries. Do not copy Moodle data,
  learner records, or secret-bearing environment output into incident evidence.

## Interpretation and safety

- Do not delete Moodle data, prune Docker volumes, resize or detach storage, or
  kill arbitrary processes as an automatic response.
- Storage and host-capacity changes require an infrastructure operator and a
  fresh baseline. Verify the EFS mount and Moodle synthetic transaction after
  any approved repair.
