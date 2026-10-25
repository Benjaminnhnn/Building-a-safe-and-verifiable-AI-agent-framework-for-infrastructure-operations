"""Closed Moodle cause vocabulary and the signals that must support each cause.

The vocabulary is derived from the reviewed rule tables in
``core.moodle.diagnosis`` so an LLM can only name a cause the planner, catalog
and verifier already understand. Each cause lists the Prometheus signals a
citation must cover and the predicate the stored sample must satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass

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


CAUSE_VOCABULARY: dict[str, CauseDefinition] = _build_vocabulary()
