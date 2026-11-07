from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.evidence.adapters import SafeLiveActionRequest
from core.moodle.safety_shadow import evaluate_challenges
from core.moodle.safe_action_notify import format_safety_challenges
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment


def prepared_candidate():
    action = TypedAction(
        action_id='action-test', incident_id='incident-test', action_type=ActionType.RUN_ANSIBLE_PLAYBOOK,
        target_resource_id='moodle-app', environment=Environment.STAGING, reason='diagnosis',
        evidence_refs=['one', 'two', 'three'], expected_outcome='restore service', reversible=True,
        requires_approval=True,
        rollback_plan=RollbackPlan(available=True, rollback_action_id='reconcile_net01_safe_baseline', method='reset'),
    )
    request = SafeLiveActionRequest(
        request_id='request-test', idempotency_key='idempotency-test', scenario_id='NET-01',
        catalog_action_id='recreate_moodle_web_from_reviewed_compose', target_scope='staging_moodle_nodes',
        action=action, timeout_seconds=120,
    )
    key = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    prepared = {'request': request.model_dump(mode='json'), 'diagnosis_confidence': .72,
                'incident_id': request.action.incident_id, 'candidate_id': 'candidate-test',
                'execution_permitted': False}
    return prepared, key


def test_actual_gate_challenges_never_dispatch_and_report_policy_comparison():
    prepared, key = prepared_candidate()
    result = evaluate_challenges(prepared, public_key=key, actors={'operator'})
    assert result['dangerous']['decision'] == 'DENY'
    assert result['dangerous']['source'] == 'operator_defined_challenge_not_ai'
    assert result['policy_fault']['decision'] == 'DENY'
    assert result['reviewed']['decision'] == 'REQUIRE_APPROVAL'
    assert result['execution_permitted'] is False
    assert all(not result[name]['dispatched'] for name in ('dangerous', 'policy_fault', 'reviewed'))
    message = format_safety_challenges(result)
    assert 'Gate đối chứng riêng' in message and 'không phải AI' in message
    assert 'không thực thi' in message and 'Operator duyệt' in message


def test_challenge_requires_nonexecutable_fresh_candidate_that_passes_gate():
    prepared, key = prepared_candidate()
    prepared['execution_permitted'] = True
    with pytest.raises(ValueError):
        evaluate_challenges(prepared, public_key=key, actors={'operator'})
    prepared['execution_permitted'] = False
    prepared['request']['action']['evidence_refs'] = ['one']
    with pytest.raises(ValueError):
        evaluate_challenges(prepared, public_key=key, actors={'operator'})
