# Moodle data storage and node pressure

This runbook applies to `MoodleEfsMountMissing`, `MoodleScratchEnospc`,
`MoodleNodeCpuHigh`, and `MoodleNodeMemoryPressure` in the staging Moodle
environment.

## Evidence to collect

- Identify the affected node and distinguish the shared Moodle EFS mount from
  the isolated synthetic-fault scratch filesystem.
- Compare node-exporter capacity metrics with the EFS mount probe and Moodle
  synthetic transaction. Preserve observation time and resource identity.
- For memory alerts, confirm the node's available-memory decrease against fresh
  samples. A rapid drop is a capacity signal, not proof of application failure
  or a safe remediation.
- Collect only bounded process/container summaries. Do not copy Moodle data,
  learner records, or secret-bearing environment output into incident evidence.

## Interpretation and safety

- A full isolated scratch filesystem is not evidence that the shared Moodle
  data volume is full.
- Do not delete Moodle data, prune Docker volumes, resize or detach storage, or
  kill arbitrary processes as an automatic response.
- Storage and host-capacity changes require an infrastructure operator and a
  fresh baseline. Verify the EFS mount and Moodle synthetic transaction after
  any approved repair.
