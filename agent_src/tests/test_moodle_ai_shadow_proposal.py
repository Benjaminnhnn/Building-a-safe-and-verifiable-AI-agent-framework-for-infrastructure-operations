from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.moodle.ai_shadow_proposal import evaluate_model_proposal
from core.moodle import ai_shadow_proposal
from core.safety.safe_action_audit import SafeActionAuditStore
from test_moodle_safety_shadow import prepared_candidate


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(self.answer))


def test_model_generated_intent_reaches_actual_gate_only_as_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOPS_AI_SHADOW_PROPOSALS_ENABLED", "true")
    prepared, key = prepared_candidate()
    fake = FakeModel({"action_id": "flush_all_iptables_rules", "target_scope": "staging_moodle_nodes",
                      "rationale": "try to restore connectivity"})
    db = tmp_path / "audit.sqlite3"
    result = evaluate_model_proposal(prepared, public_key=key, actors={"operator"}, audit_db=str(db), client=fake)
    assert result["source"] == "model_generated"
    assert result["gate_decision"] == "DENY"
    assert result["dispatched"] is False
    assert result["risk_adjudication"] == "pending_independent_review"
    assert "flush_all_iptables_rules" not in fake.calls[0]["contents"]
    with SafeActionAuditStore(db, read_only=True) as audit:
        events = audit.list_for_incident(prepared["incident_id"])
        assert audit.verify_chain(prepared["incident_id"])
    assert events[0]["event_type"] == "model_proposal_shadow_gate"
    assert events[0]["payload"]["response_sha256"] == result["response_sha256"]


def test_model_shadow_requires_explicit_enablement_and_valid_json(tmp_path, monkeypatch):
    prepared, key = prepared_candidate()
    fake = FakeModel({"action_id": "test", "target_scope": "scope", "rationale": "reason"})
    with pytest.raises(ValueError, match="disabled"):
        evaluate_model_proposal(prepared, public_key=key, actors={"operator"},
                                audit_db=str(tmp_path / "audit.sqlite3"), client=fake)
    monkeypatch.setenv("AIOPS_AI_SHADOW_PROPOSALS_ENABLED", "true")
    fake.answer = {"command": "rm -rf /"}
    with pytest.raises(ValueError):
        evaluate_model_proposal(prepared, public_key=key, actors={"operator"},
                                audit_db=str(tmp_path / "audit.sqlite3"), client=fake)


def test_model_client_uses_sdk_compatible_bounded_http_options(tmp_path, monkeypatch):
    prepared, key = prepared_candidate()
    monkeypatch.setenv("AIOPS_AI_SHADOW_PROPOSALS_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-only-key")
    fake = FakeModel({"action_id": "unknown_action", "target_scope": "staging_moodle_nodes",
                      "rationale": "inspect candidate"})
    options = []
    def create_client(**kwargs):
        options.append(kwargs["http_options"])
        return fake
    monkeypatch.setattr(ai_shadow_proposal.genai, "Client", create_client)
    result = evaluate_model_proposal(prepared, public_key=key, actors={"operator"},
                                     audit_db=str(tmp_path / "audit.sqlite3"))
    assert result["gate_decision"] == "DENY"
    assert options == [{"timeout": 10000}]
    assert fake.calls[0]["config"].max_output_tokens == 1024
