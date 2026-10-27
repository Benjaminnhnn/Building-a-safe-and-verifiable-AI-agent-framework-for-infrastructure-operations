"""Closed Moodle cause vocabulary and the signals that must support each cause.

The vocabulary is derived from the reviewed rule tables in
``core.moodle.diagnosis`` so an LLM can only name a cause the planner, catalog
and verifier already understand. Each cause lists the Prometheus signals a
citation must cover and the predicate the stored sample must satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.moodle import diagnosis as rules


@dataclass(frozen=True)
class SignalRequirement:
    """One query that must be cited, and the predicate its stored samples satisfy.

    ``predicate`` is one of ``any_eq``, ``all_eq``, ``any_gt`` or ``any_ge``;
    ``any_*`` holds when at least one cited sample satisfies it, ``all_eq``
    when every cited sample does.
    """

    query_id: str
    predicate: str
    value: float

    def holds(self, values: list[float]) -> bool:
        if not values:
            return False
        if self.predicate == "any_eq":
            return any(value == self.value for value in values)
        if self.predicate == "all_eq":
            return all(value == self.value for value in values)
        if self.predicate == "any_gt":
            return any(value > self.value for value in values)
        if self.predicate == "any_ge":
            return any(value >= self.value for value in values)
        return False

    def describe(self) -> str:
        operator = {"any_eq": "==", "all_eq": "== (every sample)", "any_gt": ">", "any_ge": ">="}
        return f"{self.query_id} {operator.get(self.predicate, '?')} {self.value:g}"


@dataclass(frozen=True)
class CauseDefinition:
    cause_code: str
    description: str
    requirements: tuple[SignalRequirement, ...]
    status: str = "hypothesis"
    # Disambiguation hint shown to the model; never shown to operators as the diagnosis.
    guidance: str = ""
    # True for causes that exist only in the LLM tier: no rule table produces them and
    # the planner has no mapping for them, so they can never yield an action candidate.
    llm_only: bool = False


def _build_vocabulary() -> dict[str, CauseDefinition]:
    causes: dict[str, CauseDefinition] = {}
    probe_descriptions = {
        "rds_path_probe_failed": "Monitor-to-RDS TCP probe is failing; Moodle-node reachability is unconfirmed",
        "public_entry_probe_failed": "Monitor-to-public-ALB Moodle probe is failing; the fault domain is not isolated",
        "efs_mount_probe_failed": "Moodle EFS mount is unavailable on the probed node set",
        "rds_ingress_audit_unavailable": "The read-only RDS security-group audit is failing; SEC-01 exposure detection is blind",
    }
    for query_id, cause_code in rules._DIRECT_PROBE_RULES.values():
        causes[cause_code] = CauseDefinition(
            cause_code,
            probe_descriptions[cause_code],
            (SignalRequirement(query_id, "any_eq", 0.0),),
        )
    for query_id, cause_code, hypothesis, _reason in rules._DIRECT_NODE_RULES.values():
        causes[cause_code] = CauseDefinition(
            cause_code, hypothesis, (SignalRequirement(query_id, "any_eq", 0.0),)
        )
    for table in (rules._DIRECT_THRESHOLD_RULES, rules._BUSINESS_ALERT_RULES, rules._SECURITY_ALERT_RULES):
        for query_id, threshold, cause_code, hypothesis, _node_scoped, inclusive in table.values():
            causes[cause_code] = CauseDefinition(
                cause_code,
                hypothesis,
                (SignalRequirement(query_id, "any_ge" if inclusive else "any_gt", float(threshold)),),
            )
    causes["application_path_not_isolated"] = CauseDefinition(
        "application_path_not_isolated",
        "Moodle application transaction path is failing; internal cause is not isolated",
        (
            SignalRequirement("moodle_synthetic_success", "any_eq", 0.0),
            SignalRequirement("moodle_rds_tcp_probe", "all_eq", 1.0),
            SignalRequirement("moodle_public_probe", "all_eq", 1.0),
            SignalRequirement("moodle_efs_mount", "all_eq", 1.0),
        ),
    )
    causes["upgrade_in_progress_human_escalation"] = CauseDefinition(
        "upgrade_in_progress_human_escalation",
        "Moodle upgrade actively in progress; human-only escalation required",
        (
            SignalRequirement("moodle_cli_maintenance_mode_enabled", "any_ge", 1.0),
            SignalRequirement("moodle_upgrade_in_progress", "any_ge", 1.0),
        ),
        status="escalated_human_only",
    )
    return causes


# Reviewed guidance for rule-derived causes that are easy to confuse.
_RULE_CAUSE_GUIDANCE = {
    "application_path_not_isolated": (
        "Use only when the synthetic transaction fails and the RDS, public and EFS probes all pass. "
        "If any dependency probe also fails, prefer abstaining or that dependency's cause."
    ),
    "moodle_adhoc_task_queue_stuck": (
        "Requires overdue tasks; the cron container being stopped is what makes the infrastructure restart relevant."
    ),
    "efs_mount_probe_failed": "EFS mount is a per-node signal; one failing node is enough.",
    "web_container_not_running": "Per-node signal; a healthy peer node does not contradict a failing node.",
    "node_cpu_pressure": "Per-node signal; one node above the threshold is enough while the peer stays healthy.",
}

# Causes that only the LLM tier can name. Each one is a reviewed, evidence-only description
# that the rule tables do not emit. They must never be added to the planner catalog.
_LLM_ONLY_CAUSES: tuple[CauseDefinition, ...] = (
    CauseDefinition(
        "adhoc_backlog_with_cron_running",
        "Moodle ad-hoc tasks are overdue while no node reports the cron container stopped; "
        "slow or failing tasks are suspected and need human review of the task logs",
        (
            SignalRequirement("moodle_adhoc_tasks_overdue_count", "any_ge", 1.0),
            SignalRequirement("moodle_cron_container_stopped", "all_eq", 0.0),
        ),
        guidance=(
            "Pick this instead of moodle_adhoc_task_queue_stuck when cron is not stopped on any node. "
            "A restart does not apply; this is a human-review diagnosis."
        ),
        llm_only=True,
    ),
    CauseDefinition(
        "node_telemetry_and_web_container_down",
        "Node exporter, cAdvisor and the web-container probe each report a failure; a host-level outage "
        "is suspected but not proven, and the signals may come from different nodes",
        (
            SignalRequirement("moodle_node_up", "any_eq", 0.0),
            SignalRequirement("moodle_cadvisor_up", "any_eq", 0.0),
            SignalRequirement("moodle_node_web_running", "any_eq", 0.0),
        ),
        guidance=(
            "Pick this instead of web_container_not_running only when scrape telemetry is also down; "
            "a stopped container with healthy telemetry is web_container_not_running."
        ),
        llm_only=True,
    ),
)


def _finalize(causes: dict[str, CauseDefinition]) -> dict[str, CauseDefinition]:
    for code, guidance in _RULE_CAUSE_GUIDANCE.items():
        if code in causes:
            causes[code] = replace(causes[code], guidance=guidance)
    for definition in _LLM_ONLY_CAUSES:
        if definition.cause_code in causes:
            raise ValueError(f"LLM-only cause {definition.cause_code} collides with a rule cause")
        causes[definition.cause_code] = definition
    return causes


CAUSE_VOCABULARY: dict[str, CauseDefinition] = _finalize(_build_vocabulary())
LLM_ONLY_CAUSE_CODES: frozenset[str] = frozenset(item.cause_code for item in _LLM_ONLY_CAUSES)
