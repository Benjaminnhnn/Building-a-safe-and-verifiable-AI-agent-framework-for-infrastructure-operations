"""Single source of truth for the AIHS-on-Moodle predictor action space.

The predictor classes are the safe Moodle catalog actions plus ``call_human``
and the observe-only ``wait_and_recheck`` rung (the NOP equivalent: it never
mutates anything). The cost ladder orders them from cheapest to most
expensive; actions with the same cost share a rank. Nothing outside this
module may define a competing list: the lab executor, the behavior policy, the
runner and (later) the Controller fallback all import from here.

Catalog keys (scenario, action, scope) mirror CATALOG in
automation/moodle-safe-executor-api.py. SEC-01, DB-02, BIZ-01 and every
``reconcile_*`` action are deliberately absent (human-only, AWS-config change,
site-wide maintenance toggle, and reset-only respectively).
"""

from __future__ import annotations

from dataclasses import dataclass

CALL_HUMAN = "call_human"
WAIT_AND_RECHECK = "wait_and_recheck"


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    scenario: str | None      # catalog scenario id; None for non-catalog rungs
    scope: str | None         # catalog target scope
    nodes: tuple[str, ...]    # node aliases the executor touches, in order
    cost_rank: int            # lower is cheaper; equal rank = equal cost
    mutates: bool


# Cheapest -> most expensive. Equal ranks are intentional (see PLANNING 11.1).
_SPECS = (
    ActionSpec(WAIT_AND_RECHECK, None, None, (), 0, False),
    ActionSpec("restart_moodle_cron_service", "BIZ-02", "staging_moodle_app", ("a",), 1, True),
    ActionSpec("start_reviewed_compose_service", "CON-01", "moodle-app-b", ("b",), 2, True),
    ActionSpec("recreate_moodle_web_from_reviewed_compose", "NET-01", "staging_moodle_nodes", ("a", "b"), 3, True),
    ActionSpec("remove_named_cpu_load_container", "RES-01", "moodle-app-b", ("b",), 4, True),
    ActionSpec("remove_scoped_db_reject", "DB-01", "staging_moodle_nodes", ("a", "b"), 4, True),
    ActionSpec("restore_fixture_directory_mode", "SEC-02", "moodledata_synthetic_fixture_directory", ("a",), 4, True),
    ActionSpec(CALL_HUMAN, None, None, (), 5, False),
)

ACTION_SPECS: dict[str, ActionSpec] = {spec.action_id: spec for spec in _SPECS}
# Order of ``COST_LADDER`` is the cheapest-to-most-expensive order; ties keep
# the declaration order above so the sequence is deterministic.
COST_LADDER: tuple[str, ...] = tuple(spec.action_id for spec in sorted(_SPECS, key=lambda s: s.cost_rank))
CATALOG_ACTIONS: tuple[str, ...] = tuple(s.action_id for s in _SPECS if s.scenario)
PREDICTOR_CLASSES: tuple[str, ...] = tuple(spec.action_id for spec in _SPECS)

# Fault scenario -> the catalog action that repairs it. Used ONLY by the lab
# behavior policy to pick the "matching" action; it is not a model feature.
MATCHING_ACTION: dict[str, str] = {s.scenario: s.action_id for s in _SPECS if s.scenario}


def spec(action_id: str) -> ActionSpec:
    """Fail closed: unknown actions raise instead of defaulting."""
    try:
        return ACTION_SPECS[action_id]
    except KeyError:
        raise ValueError(f"action outside the reviewed action space: {action_id!r}") from None


def cost_rank(action_id: str) -> int:
    return spec(action_id).cost_rank


def is_cheaper_or_equal(left: str, right: str) -> bool:
    return cost_rank(left) <= cost_rank(right)
