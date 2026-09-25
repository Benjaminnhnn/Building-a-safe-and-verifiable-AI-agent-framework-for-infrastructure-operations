from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.agents import AgentContractError, DiagnosisAgent, ObserverAgent, PlannerAgent
from core.dependency_graph import DependencyGraph
from core.evidence_store import evidence_content_hash
from core.schema.common import Environment, IncidentStatus, ResourceType
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource
from core.schema.scenario import ScenarioGroundTruth


def _resources_for(scenario: ScenarioGroundTruth) -> list[Resource]:
    return [
        Resource(
            resource_id=resource_id,
            name=resource_id,
            type=ResourceType.APPLICATION,
            environment=Environment.STAGING,
            owner_role="infrastructure_engineer",
        )
        for resource_id in sorted(set(scenario.affected_resources))
    ]


def _evidence(incident_id: str, scenario: ScenarioGroundTruth) -> list[Evidence]:
    items: list[Evidence] = []
    for index, signal in enumerate(scenario.observed_signals[:3], start=1):
        item = Evidence(
            evidence_id=f"ev-{scenario.scenario_id.lower()}-{index}",
            incident_id=incident_id,
            resource_id=scenario.affected_resources[min(index - 1, len(scenario.affected_resources) - 1)],
            source="fixture",
            summary=signal,
            content_hash="pending",
            metadata={"scenario_id": scenario.scenario_id},
        )
        item.content_hash = evidence_content_hash(item)
        items.append(item)
    return items


def _scenario_paths() -> list[Path]:
    prefixes = ("DB-", "RES-", "NET-", "CON-", "SEC-")
    return sorted(
        path
        for path in Path("evaluation/ground_truth").glob("*.json")
        if path.name.startswith(prefixes)
    )


def test_observer_compresses_duplicate_burst_to_one_incident() -> None:
    alerts = [
        {
            "status": "firing",
            "fingerprint": "same-fingerprint",
            "labels": {
                "alertname": "PostgreSQLDown",
                "severity": "critical",
                "service": "postgres-db",
                "environment": "staging",
            },
            "annotations": {"summary": "database unavailable"},
        }
        for _ in range(100)
    ]
    resource = Resource(
        resource_id="postgres-db",
        name="PostgreSQL",
        type=ResourceType.DATABASE,
        environment=Environment.STAGING,
        owner_role="infrastructure_engineer",
    )
    results = ObserverAgent().observe({"status": "firing", "alerts": alerts}, [resource])
    assert len(results) == 1
    assert len(results[0].normalized_events) == 100
    assert results[0].incident.affected_resources == ["postgres-db"]
    assert results[0].evidence_requests[0].source == "prometheus"


def test_observer_does_not_invent_unknown_resource_mapping() -> None:
    payload = {
        "alerts": [
            {
                "fingerprint": "unknown",
                "labels": {"alertname": "Unknown", "service": "not-in-inventory"},
            }
        ]
    }
    with pytest.raises(AgentContractError, match="no known resource"):
        ObserverAgent().observe(payload, [])


def test_diagnosis_with_missing_evidence_is_not_ready() -> None:
    scenario = ScenarioGroundTruth(**json.loads(_scenario_paths()[0].read_text(encoding="utf-8")))
    incident = Incident(
        incident_id="inc-1",
        fingerprint="fp-1",
        title=scenario.scenario_name,
        status=IncidentStatus.TRIAGED,
        severity="critical",
        affected_resources=scenario.affected_resources,
    )
    graph = DependencyGraph(_resources_for(scenario))
    result = DiagnosisAgent().diagnose(incident, [], graph, {scenario.scenario_id: scenario})
    assert result.ready_for_planning is False
    assert result.top_hypothesis is None
    assert result.missing_evidence


def test_diagnosis_rejects_foreign_or_unknown_evidence() -> None:
    scenario = ScenarioGroundTruth(**json.loads(_scenario_paths()[0].read_text(encoding="utf-8")))
    incident = Incident(
        incident_id="inc-1",
        fingerprint="fp-1",
        title=scenario.scenario_name,
        severity="critical",
        affected_resources=scenario.affected_resources,
    )
    graph = DependencyGraph(_resources_for(scenario))
    items = _evidence("inc-other", scenario)
    with pytest.raises(AgentContractError, match="another incident"):
        DiagnosisAgent().diagnose(incident, items, graph, {scenario.scenario_id: scenario})


def test_five_scenarios_run_deterministically_through_agents() -> None:
    paths = _scenario_paths()
    assert len(paths) >= 5
    for path in paths[:5]:
        scenario = ScenarioGroundTruth(**json.loads(path.read_text(encoding="utf-8")))
        incident = Incident(
            incident_id=f"inc-{scenario.scenario_id.lower()}",
            fingerprint=f"fp-{scenario.scenario_id.lower()}",
            title=scenario.scenario_name,
            status=IncidentStatus.TRIAGED,
            severity="critical",
            affected_resources=scenario.affected_resources,
        )
        graph = DependencyGraph(_resources_for(scenario))
        items = _evidence(incident.incident_id, scenario)
        catalog = {scenario.scenario_id: scenario}
        first = DiagnosisAgent().diagnose(incident, items, graph, catalog)
        second = DiagnosisAgent().diagnose(incident, items, graph, catalog)
        assert first == second
        action = PlannerAgent().plan(incident, first, scenario, graph)
        assert action.evidence_refs == first.evidence_refs
        assert action.target_resource_id in graph.resources
        assert "command" not in action.parameters


def test_planner_rejects_not_ready_and_ignores_ground_truth_answers() -> None:
    scenario = ScenarioGroundTruth(**json.loads(_scenario_paths()[0].read_text(encoding="utf-8")))
    incident = Incident(
        incident_id="inc-1",
        fingerprint="fp-1",
        title=scenario.scenario_name,
        severity="critical",
        affected_resources=scenario.affected_resources,
    )
    graph = DependencyGraph(_resources_for(scenario))
    not_ready = DiagnosisAgent().diagnose(incident, [], graph, {scenario.scenario_id: scenario})
    with pytest.raises(AgentContractError, match="not ready"):
        PlannerAgent().plan(incident, not_ready, scenario, graph)

    ready = DiagnosisAgent().diagnose(
        incident,
        _evidence(incident.incident_id, scenario),
        graph,
        {scenario.scenario_id: scenario},
    )
    first = PlannerAgent().plan(incident, ready, scenario, graph)
    scenario.expected_root_cause["cause"] = "oracle answer must not be used"
    scenario.allowed_remediation[0]["action"] = "raw_shell"
    scenario.allowed_actions.append("raw_shell")
    second = PlannerAgent().plan(incident, ready, scenario, graph)
    assert second == first
    assert second.action_type.value != "raw_shell"


def test_diagnosis_ignores_scenario_id_and_expected_root_cause() -> None:
    scenario = ScenarioGroundTruth(**json.loads(_scenario_paths()[0].read_text(encoding="utf-8")))
    incident = Incident(
        incident_id="inc-oracle",
        fingerprint="fp-oracle",
        title="Observed incident",
        severity="critical",
        affected_resources=scenario.affected_resources,
    )
    graph = DependencyGraph(_resources_for(scenario))
    items = _evidence(incident.incident_id, scenario)
    first = DiagnosisAgent().diagnose(incident, items, graph, {})

    scenario.expected_root_cause["cause"] = "fabricated oracle value"
    for item in items:
        item.metadata["scenario_id"] = "WRONG-99"
    second = DiagnosisAgent().diagnose(
        incident,
        items,
        graph,
        {"WRONG-99": scenario},
    )

    assert second == first
    assert second.top_hypothesis is not None
    assert second.top_hypothesis.root_cause != "fabricated oracle value"
