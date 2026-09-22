"""Tests for the Safety Policy Engine."""

import json
import pathlib

from core.safety_engine import SafetyPolicyEngine
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionDecision, ActionType, Environment
from core.schema.diagnosis import DiagnosisResult, RootCauseHypothesis
from core.schema.scenario import ScenarioGroundTruth


def _make_action(**overrides) -> TypedAction:
    defaults = {
        "action_id": "act-test-001",
        "incident_id": "inc-test-001",
        "action_type": ActionType.START_CONTAINER,
        "target_resource_id": "postgres-db",
        "environment": Environment.STAGING,
        "reason": "test reason",
        "evidence_refs": ["ev-001", "ev-002", "ev-003"],
        "parameters": {"scenario_id": "DB-01"},
        "preconditions": [],
        "expected_outcome": "service recovers",
        "reversible": True,
        "rollback_plan": RollbackPlan(available=True, method="stop container"),
    }
    defaults.update(overrides)
    return TypedAction(**defaults)


def _make_diagnosis(confidence: float = 0.9) -> DiagnosisResult:
    return DiagnosisResult(
        diagnosis_id="diag-test",
        incident_id="inc-test-001",
        top_hypothesis=RootCauseHypothesis(
            hypothesis_id="hyp-test",
            root_cause="PostgreSQL stopped",
            confidence=confidence,
            affected_resources=["postgres-db"],
            supporting_evidence_refs=["ev-001"],
            reasoning_summary="test",
        ),
        evidence_refs=["ev-001", "ev-002", "ev-003"],
        confidence=confidence,
        ready_for_planning=True,
    )


def _load_scenario(scenario_id: str = "DB-01") -> ScenarioGroundTruth:
    gt_dir = pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "ground_truth"
    for f in gt_dir.glob("*.json"):
        data = json.loads(f.read_text())
        if data.get("scenario_id") == scenario_id:
            return ScenarioGroundTruth(**data)
    raise FileNotFoundError(f"No ground truth for {scenario_id}")


class TestSafetyPolicyEngine:
    def setup_method(self):
        self.engine = SafetyPolicyEngine()
        self.scenario = _load_scenario("DB-01")

    def test_allow_staging_with_evidence_and_rollback(self):
        action = _make_action()
        diagnosis = _make_diagnosis(confidence=0.9)
        result = self.engine.evaluate(action, self.scenario, diagnosis=diagnosis)
        assert result.decision == ActionDecision.ALLOW

    def test_deny_no_evidence(self):
        action = _make_action()
        # Use object mutation to simulate cleared evidence after construction
        # because TypedAction enforces min_length=1 on evidence_refs
        object.__setattr__(action, 'evidence_refs', [])
        result = self.engine.evaluate(action, self.scenario)
        assert result.decision == ActionDecision.DENY
        assert "no supporting evidence" in result.reasons[0]

    def test_deny_forbidden_action(self):
        action = _make_action(action_type=ActionType.BLOCKED_UNRESTRICTED_SHELL)
        scenario_data = self.scenario.model_dump()
        scenario_data["forbidden_actions"].append("blocked_unrestricted_shell")
        scenario = ScenarioGroundTruth(**scenario_data)
        result = self.engine.evaluate(action, scenario)
        assert result.decision == ActionDecision.DENY

    def test_deny_low_confidence(self):
        action = _make_action()
        diagnosis = _make_diagnosis(confidence=0.5)
        result = self.engine.evaluate(action, self.scenario, diagnosis=diagnosis)
        assert result.decision == ActionDecision.DENY
        assert "confidence" in result.reasons[0]

    def test_require_approval_medium_confidence(self):
        action = _make_action()
        diagnosis = _make_diagnosis(confidence=0.72)
        result = self.engine.evaluate(action, self.scenario, diagnosis=diagnosis)
        assert result.decision == ActionDecision.REQUIRE_APPROVAL

    def test_require_approval_production(self):
        action = _make_action(environment=Environment.PRODUCTION)
        diagnosis = _make_diagnosis(confidence=0.9)
        result = self.engine.evaluate(action, self.scenario, diagnosis=diagnosis)
        assert result.decision == ActionDecision.REQUIRE_APPROVAL

    def test_require_approval_high_blast_radius(self):
        action = _make_action()
        diagnosis = _make_diagnosis(confidence=0.9)
        result = self.engine.evaluate(action, self.scenario, diagnosis=diagnosis, blast_radius="high")
        assert result.decision == ActionDecision.REQUIRE_APPROVAL

    def test_deny_wrong_role(self):
        action = _make_action()
        result = self.engine.evaluate(action, self.scenario, actor="observer")
        assert result.decision == ActionDecision.DENY
        assert "permission" in result.reasons[0]

    def test_read_only_always_allowed(self):
        action = _make_action(action_type=ActionType.READ_HEALTH)
        result = self.engine.evaluate(action, self.scenario, actor="observer")
        assert result.decision == ActionDecision.ALLOW

    def test_require_approval_no_rollback(self):
        action = _make_action(
            rollback_plan=RollbackPlan(available=False),
            reversible=False,
        )
        diagnosis = _make_diagnosis(confidence=0.9)
        result = self.engine.evaluate(action, self.scenario, diagnosis=diagnosis)
        assert result.decision == ActionDecision.REQUIRE_APPROVAL
