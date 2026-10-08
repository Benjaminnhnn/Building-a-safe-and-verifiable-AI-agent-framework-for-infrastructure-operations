from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from core.agents import (
    AgentContractError,
    DiagnosisAgent,
    ObserverAgent,
    PlannerAgent,
    UnsupportedRemediationError,
)
from core.dependency_graph import DependencyGraph
from core.evidence_store import evidence_content_hash
from core.schema.common import Environment, IncidentStatus, ResourceType
from core.schema.evidence import Evidence
from core.schema.incident import Incident
from core.schema.resource import Resource
from core.schema.scenario import ScenarioGroundTruth


def _resources_for(scenario: ScenarioGroundTruth) -> list[Resource]:
    data = json.loads(
        Path("agent_src/config/moodle_resource_inventory.json").read_text(
            encoding="utf-8"
        )
    )
    return [Resource(**item) for item in data["resources"]]


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
        item = item.model_copy(update={"content_hash": evidence_content_hash(item)})
        items.append(item)
    return items


def _seal_evidence(items: list[Evidence]) -> list[Evidence]:
    return [
        item.model_copy(update={"content_hash": evidence_content_hash(item)})
        for item in items
    ]


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
    result = DiagnosisAgent().diagnose(incident, [], graph)
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
        DiagnosisAgent().diagnose(incident, items, graph)

    incident.affected_resources = ["resource-not-in-graph"]
    with pytest.raises(AgentContractError, match="unknown affected resources"):
        DiagnosisAgent().diagnose(incident, [], graph)

    incident.affected_resources = scenario.affected_resources
    mutated = _evidence(incident.incident_id, scenario)
    mutated[0] = mutated[0].model_copy(update={"summary": "forged signal"})
    with pytest.raises(AgentContractError, match="content digest does not match"):
        DiagnosisAgent().diagnose(incident, mutated, graph)


def test_diagnosis_separates_graph_derived_potential_impact_from_observed_resources() -> None:
    from core.agents import DIAGNOSIS_RULES

    incident = Incident(
        incident_id="inc-db-impact",
        fingerprint="fp-db-impact",
        title="Database connectivity alert",
        severity="critical",
        affected_resources=["postgres-db"],
    )
    graph = DependencyGraph([
        Resource(
            resource_id="postgres-db",
            name="PostgreSQL",
            type=ResourceType.DATABASE,
            environment=Environment.STAGING,
            owner_role="infrastructure_engineer",
        ),
        Resource(
            resource_id="moodle-app",
            name="Moodle application",
            type=ResourceType.APPLICATION,
            environment=Environment.STAGING,
            owner_role="infrastructure_engineer",
            depends_on=["postgres-db"],
        ),
    ])
    rule = DIAGNOSIS_RULES[0]
    items = _seal_evidence([
        Evidence(
            evidence_id=f"ev-impact-{index}",
            incident_id=incident.incident_id,
            resource_id="postgres-db",
            source="prometheus",
            summary=signal,
            content_hash="pending",
            metadata={"signal": signal},
        )
        for index, signal in enumerate(sorted(rule.required_signals), start=1)
    ])

    result = DiagnosisAgent().diagnose(incident, items, graph)

    assert result.top_hypothesis is not None
    assert result.top_hypothesis.affected_resources == ["postgres-db"]
    assert result.top_hypothesis.potentially_affected_resources == ["moodle-app"]
    assert "potential downstream impact: moodle-app" in result.top_hypothesis.reasoning_summary


def test_planner_rejects_catalog_target_unrelated_to_incident_resources() -> None:
    from core.agents import DIAGNOSIS_RULES_BY_ID

    incident = Incident(
        incident_id="inc-unrelated-plan",
        fingerprint="fp-unrelated-plan",
        title="Moodle database error",
        severity="critical",
        affected_resources=["moodle-app"],
    )
    graph = DependencyGraph([
        Resource(
            resource_id=resource_id,
            name=resource_id,
            type=ResourceType.DATABASE if resource_id == "postgres-db" else ResourceType.APPLICATION,
            environment=Environment.STAGING,
            owner_role="infrastructure_engineer",
        )
        for resource_id in ("moodle-app", "postgres-db")
    ])
    rule = DIAGNOSIS_RULES_BY_ID["postgres_stopped"]
    evidence = _seal_evidence([
        Evidence(
            evidence_id=f"ev-unrelated-{index}",
            incident_id=incident.incident_id,
            resource_id="moodle-app",
            source="prometheus",
            summary=signal,
            content_hash="pending",
            metadata={"signal": signal},
        )
        for index, signal in enumerate(sorted(rule.required_signals), start=1)
    ])
    diagnosis = DiagnosisAgent().diagnose(incident, evidence, graph)

    assert diagnosis.top_hypothesis is not None
    with pytest.raises(AgentContractError, match="outside the incident dependency neighborhood"):
        PlannerAgent().plan(incident, diagnosis, graph, evidence_items=evidence)


def test_diagnosis_rejects_known_evidence_from_unrelated_resource() -> None:
    from core.agents import DIAGNOSIS_RULES_BY_ID

    incident = Incident(
        incident_id="inc-unrelated-evidence",
        fingerprint="fp-unrelated-evidence",
        title="Moodle database error",
        severity="critical",
        affected_resources=["moodle-app"],
    )
    graph = DependencyGraph([
        Resource(
            resource_id=resource_id,
            name=resource_id,
            type=ResourceType.DATABASE if resource_id == "postgres-db" else ResourceType.APPLICATION,
            environment=Environment.STAGING,
            owner_role="infrastructure_engineer",
            depends_on=["postgres-db"] if resource_id == "moodle-app" else [],
        )
        for resource_id in ("moodle-app", "postgres-db", "billing-api")
    ])
    rule = DIAGNOSIS_RULES_BY_ID["postgres_stopped"]
    items = _seal_evidence([
        Evidence(
            evidence_id=f"ev-unrelated-evidence-{index}",
            incident_id=incident.incident_id,
            resource_id="billing-api",
            source="prometheus",
            summary=signal,
            content_hash="pending",
            metadata={"signal": signal},
        )
        for index, signal in enumerate(sorted(rule.required_signals), start=1)
    ])

    with pytest.raises(AgentContractError, match="outside the incident dependency neighborhood"):
        DiagnosisAgent().diagnose(incident, items, graph)


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
        first = DiagnosisAgent().diagnose(incident, items, graph)
        second = DiagnosisAgent().diagnose(incident, items, graph)
        assert first == second
        if first.top_hypothesis and first.top_hypothesis.cause_code == "postgres_stopped":
            with pytest.raises(UnsupportedRemediationError, match="no catalog action"):
                PlannerAgent().plan(incident, first, graph, evidence_items=items)
            continue
        action = PlannerAgent().plan(incident, first, graph, evidence_items=items)
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
    not_ready = DiagnosisAgent().diagnose(incident, [], graph)
    with pytest.raises(AgentContractError, match="not ready"):
        PlannerAgent().plan(incident, not_ready, graph, evidence_items=[])

    evidence = _evidence(incident.incident_id, scenario)
    ready = DiagnosisAgent().diagnose(
        incident,
        evidence,
        graph,
    )
    first = PlannerAgent().plan(incident, ready, graph, evidence_items=evidence)
    tampered = ready.model_copy(update={
        "top_hypothesis": ready.top_hypothesis.model_copy(update={"cause_code": "untrusted-cause"})
    })
    with pytest.raises(AgentContractError, match="does not match the validated incident evidence"):
        PlannerAgent().plan(incident, tampered, graph, evidence_items=evidence)
    scenario.expected_root_cause["cause"] = "oracle answer must not be used"
    scenario.allowed_remediation[0]["action"] = "raw_shell"
    scenario.allowed_actions.append("raw_shell")
    second = PlannerAgent().plan(incident, ready, graph, evidence_items=evidence)
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
    first = DiagnosisAgent().diagnose(incident, items, graph)

    scenario.expected_root_cause["cause"] = "fabricated oracle value"
    for item in items:
        item = item.model_copy(update={"metadata": item.metadata | {"scenario_id": "WRONG-99"}})
    second = DiagnosisAgent().diagnose(incident, items, graph)

    assert second == first
    assert second.top_hypothesis is not None
    assert second.top_hypothesis.root_cause != "fabricated oracle value"


def test_decision_agent_contracts_do_not_accept_ground_truth_objects() -> None:
    assert "scenarios" not in inspect.signature(DiagnosisAgent.diagnose).parameters
    assert "scenario" not in inspect.signature(PlannerAgent.plan).parameters
