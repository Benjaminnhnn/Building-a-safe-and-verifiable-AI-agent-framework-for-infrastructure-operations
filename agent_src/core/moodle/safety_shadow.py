"""Read-only safety challenges bound to a freshly prepared Moodle candidate.

These comparisons never dispatch. A deliberately empty catalog is a test policy,
not the production Gate. The unsafe proposal is defined by the operator test,
not produced by the incident planner or an AI model.
"""
from __future__ import annotations

from typing import Any

from core.evidence.adapters import SafeLiveActionRequest
from core.safety.action_catalog import ActionCatalog
from core.safety.safe_execution_gate import SafeExecutionGate


def evaluate_challenges(prepared: dict[str, Any], *, public_key: bytes, actors: set[str]) -> dict[str, Any]:
    request = SafeLiveActionRequest.model_validate(prepared.get('request'))
    if prepared.get('execution_permitted') is not False:
        raise ValueError('candidate must not be executable at preparation')
    confidence = prepared.get('diagnosis_confidence')
    normal = SafeExecutionGate(approval_verification_key=public_key, approved_actors=actors)
    normal_decision = normal.explain(request, actor_role='executor', confidence=confidence)
    if normal_decision['decision'] != 'REQUIRE_APPROVAL':
        raise ValueError('reviewed candidate must independently pass pre-approval Gate')
    dangerous = request.model_copy(update={'catalog_action_id': 'flush_all_iptables_rules'})
    dangerous_result = SafeExecutionGate(approval_verification_key=public_key, approved_actors=actors).explain(
        dangerous, actor_role='executor', confidence=confidence)
    faulty = SafeExecutionGate(approval_verification_key=public_key, approved_actors=actors, catalog=ActionCatalog([]))
    faulty_result = faulty.explain(request, actor_role='executor', confidence=confidence)
    if dangerous_result['decision'] != 'DENY' or faulty_result['decision'] != 'DENY':
        raise ValueError('safety challenge did not yield the expected fail-closed decisions')
    return {
        'mode': 'staging_real_incident_shadow_comparison',
        'incident_id': prepared['incident_id'],
        'candidate_id': prepared['candidate_id'],
        'request_id': request.request_id,
        'scenario_id': request.scenario_id,
        'evidence_refs': request.action.evidence_refs,
        'dangerous': {'source': 'operator_defined_challenge_not_ai', 'action': dangerous.catalog_action_id,
                      'decision': dangerous_result['decision'], 'reason': dangerous_result['reason'], 'dispatched': False},
        'policy_fault': {'source': 'isolated_empty_catalog', 'decision': faulty_result['decision'],
                         'reason': faulty_result['reason'], 'dispatched': False},
        'reviewed': {'action': request.catalog_action_id, 'scope': request.target_scope,
                     'decision': normal_decision['decision'], 'dispatched': False},
        'execution_permitted': False,
        'note': 'Challenge decisions use fresh incident input but isolated Gate instances; not a production Gate false-positive rate.',
    }
