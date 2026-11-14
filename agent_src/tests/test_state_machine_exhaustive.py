"""Exhaustive model check of the incident state machine.

The other state-machine tests enumerate chosen paths. This module instead
discovers the transition graph by *calling* the machine for every
(state, requested state, actor) pair, reaching each state only through the
machine itself, and then proves safety properties over every path in the
discovered graph:

P1  ``transition()`` never produces RESOLVED or VERIFIED_DRY_RUN, for any actor.
P2  ``resolve_verified()`` with an eligible result succeeds only from VERIFYING.
P3  Every path from OPEN to RESOLVED passes GATED, then EXECUTED, then VERIFYING,
    and the last edge is taken by the independent verifier with an attestation.
P4  RESOLVED, VERIFIED_DRY_RUN and FAILED are absorbing.
P5  A rejected transition leaves the incident unchanged (no partial mutation).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

import pytest
from core.schema.common import IncidentStatus
from core.schema.incident import Incident
from core.schema.verification import ProbeResult, StabilityObservation, VerificationResult
from core.verification.state_machine import IncidentStateMachine, StateTransitionError

ACTORS = ("planner", "executor", "orchestrator", "operator", "independent_verifier", "")
TERMINAL = {IncidentStatus.RESOLVED, IncidentStatus.VERIFIED_DRY_RUN, IncidentStatus.FAILED}
VERIFIER_STATES = {IncidentStatus.RESOLVED, IncidentStatus.VERIFIED_DRY_RUN}
RESOLVE = "resolve_verified"


def _incident() -> Incident:
    return Incident(
        incident_id="inc-model-check",
        fingerprint="fp-model-check",
        title="model check",
        severity="critical",
        affected_resources=["moodle-app"],
    )


def _verification(*, simulated: bool) -> VerificationResult:
    now = datetime.now(timezone.utc)
    return VerificationResult(
        verification_id="verify-model-check",
        incident_id="inc-model-check",
        health_passed=True,
        communication_contract_passed=True,
        stability_seconds=120,
        allowed_probes=[ProbeResult(name="moodle", passed=True, simulated=simulated, details="ok")],
        forbidden_probes=[ProbeResult(name="public_db", passed=True, simulated=simulated, details="blocked")],
        related_probes=[ProbeResult(name="monitoring", passed=True, simulated=simulated, details="ok")],
        stability_observations=[
            StabilityObservation(observed_at=now - timedelta(seconds=s), healthy=True, simulated=simulated)
            for s in (120, 60, 0)
        ],
        verdict="resolved",
        resolution_eligible=True,
        simulated=simulated,
    )


def _snapshot(incident: Incident) -> tuple:
    return (
        incident.status,
        incident.resolved_by_verifier,
        incident.verifier_attestation_ref,
        incident.updated_at,
    )


def _replay(path: list[tuple]) -> Incident:
    """Rebuild an incident in a given state using only machine calls."""
    incident = _incident()
    machine = IncidentStateMachine()
    for kind, target, actor in path:
        if kind == RESOLVE:
            machine.resolve_verified(incident, _verification(simulated=target), reason="replay")
        else:
            machine.transition(incident, target, actor=actor, reason="replay")
    return incident


def _attempt(path: list[tuple], step: tuple) -> tuple[Incident, bool, tuple, object]:
    incident = _replay(path)
    before = _snapshot(incident)
    kind, target, actor = step
    machine = IncidentStateMachine()
    try:
        if kind == RESOLVE:
            event = machine.resolve_verified(incident, _verification(simulated=target), reason="probe")
        else:
            event = machine.transition(incident, target, actor=actor, reason="probe")
    except StateTransitionError:
        return incident, False, before, None
    return incident, True, before, event


def _candidate_steps() -> list[tuple]:
    steps = [("transition", target, actor) for target in IncidentStatus for actor in ACTORS]
    steps += [(RESOLVE, False, "independent_verifier"), (RESOLVE, True, "independent_verifier")]
    return steps


def _explore():
    """BFS over states reached by real machine calls; returns edges and witnesses."""
    paths: dict[IncidentStatus, list[tuple]] = {IncidentStatus.OPEN: []}
    edges: set[tuple] = set()  # (from, to, kind, actor, attestation_ref)
    rejections: list[tuple] = []
    queue = deque([IncidentStatus.OPEN])
    while queue:
        state = queue.popleft()
        for step in _candidate_steps():
            incident, accepted, before, event = _attempt(paths[state], step)
            if not accepted:
                rejections.append((state, step, before, _snapshot(incident)))
                continue
            kind, _, actor = step
            new_state = incident.status
            edges.add((state, new_state, kind, event.actor if event else actor,
                       incident.verifier_attestation_ref))
            if new_state not in paths:
                paths[new_state] = [*paths[state], step]
                queue.append(new_state)
    return paths, edges, rejections


PATHS, EDGES, REJECTIONS = _explore()


def test_every_status_is_reachable_through_the_machine() -> None:
    assert set(PATHS) == set(IncidentStatus)


def test_p1_plain_transition_never_reaches_a_verifier_terminal_state() -> None:
    offending = [e for e in EDGES if e[2] == "transition" and e[1] in VERIFIER_STATES]
    assert offending == []


def test_p2_eligible_verifier_result_is_accepted_only_from_verifying() -> None:
    sources = {e[0] for e in EDGES if e[2] == RESOLVE}
    assert sources == {IncidentStatus.VERIFYING}


def test_p3_every_path_to_resolved_is_gated_executed_and_verified() -> None:
    graph: dict[IncidentStatus, list[tuple]] = {}
    for edge in EDGES:
        graph.setdefault(edge[0], []).append(edge)

    # Product-graph search: (state, saw_gated, saw_executed_after_gated, saw_verifying_after_executed).
    start = (IncidentStatus.OPEN, False, False, False)
    seen = {start}
    queue = deque([start])
    resolved_entries = []
    while queue:
        state, gated, executed, verifying = queue.popleft()
        for _, target, kind, actor, attestation in graph.get(state, []):
            g = gated or target == IncidentStatus.GATED
            x = executed or (g and target == IncidentStatus.EXECUTED)
            v = verifying or (x and target == IncidentStatus.VERIFYING)
            if target == IncidentStatus.RESOLVED:
                resolved_entries.append((g, x, v, kind, actor, attestation))
            node = (target, g, x, v)
            if node not in seen:
                seen.add(node)
                queue.append(node)

    assert resolved_entries, "RESOLVED must stay reachable for a valid verifier result"
    for gated, executed, verifying, kind, actor, attestation in resolved_entries:
        assert (gated, executed, verifying) == (True, True, True)
        assert kind == RESOLVE
        assert actor == "independent_verifier"
        assert attestation == "verify-model-check"


def test_simulated_verification_never_reaches_resolved() -> None:
    simulated_edges = [e for e in EDGES if e[2] == RESOLVE and e[1] == IncidentStatus.RESOLVED]
    path = PATHS[IncidentStatus.VERIFYING]
    incident, accepted, _, _ = _attempt(path, (RESOLVE, True, "independent_verifier"))
    assert accepted and incident.status == IncidentStatus.VERIFIED_DRY_RUN
    assert incident.resolved_by_verifier is False
    assert simulated_edges and all(e[4] == "verify-model-check" for e in simulated_edges)


@pytest.mark.parametrize("terminal", sorted(TERMINAL, key=lambda s: s.value))
def test_p4_terminal_states_are_absorbing(terminal: IncidentStatus) -> None:
    assert [e for e in EDGES if e[0] == terminal] == []


def test_p5_rejected_transitions_do_not_mutate_the_incident() -> None:
    assert REJECTIONS, "the model check must exercise rejected transitions"
    mutated = [(state, step) for state, step, before, after in REJECTIONS if before != after]
    assert mutated == []
