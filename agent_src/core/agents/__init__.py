"""Runtime role agents: Observer, Diagnosis, Planner, Execution and Verification.

Each agent consumes and produces typed messages that share ``incident_id``,
evidence IDs and ``action_id``; every stage is recorded as a hash-chained
``StageAuditEvent`` in the incident's evidence ledger.
"""
