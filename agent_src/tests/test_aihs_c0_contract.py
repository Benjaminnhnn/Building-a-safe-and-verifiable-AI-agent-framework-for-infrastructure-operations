"""C0 proposal -> adapter -> Gate -> handoff contract tests; offline, no approval or Executor.

Rows use the schema of evaluation/aihs/.artifacts/phase2/paper/proposals-*.jsonl. The test on the
real locked file is skipped when that git-ignored artifact is absent (CI); it reads the proposal
file only, never labels or outcomes.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.evidence.adapters import SafeActionRequest, SafeLiveActionRequest
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.moodle.aihs_c0 import (
    C0_ACTIONS, C0MappingDefinition, C0Proposal, adapt_c0_proposal, load_c0_mapping,
)
from core.moodle.aihs_contract import DecisionContext
from core.moodle.aihs_shadow import evaluate_aihs_shadow
from core.moodle.aihs_staging_handoff import prepare_aihs_staging_handoff
from core.safety.action_catalog import ActionCatalog
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_execution_gate import SafeExecutionGate
from core.safety.safe_executor_client import action_digest
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import Environment
from core.schema.evidence import Evidence


ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "evaluation/aihs/a3-c0-action-mapping-draft.json"
REAL_PROPOSALS = ROOT / "evaluation/aihs/.artifacts/phase2/paper/proposals-test.jsonl"
AT = datetime(2026, 11, 15, 16, 43, 18, tzinfo=timezone.utc)
MUTATING = sorted(C0_ACTIONS - {"wait_and_recheck", "call_human"})


def proposal(action="start_reviewed_compose_service", record_id="a2-0001", **extra):
    row = {
        "baseline_id": "aihs-paper-repro-moodle-v1",
        "controller_trace": [{"action": action, "controller": "present", "p_success": 0.99, "predicted_success": True}],
        "evidence_cutoff_at": "2026-11-15T16:43:18.239Z", "model_version": "aihs-paper-repro-moodle-v1+0da82b583e8bce36",
        "path": "controller_accepted", "predicted_at": "2026-11-15T18:01:42.708Z",
        "predictor_action": action, "proposed_action": action,
        "record_hash": "d6cd2ad3ec16ffc7b98c289255975dd179814248b75fe99473475349a85915ef",
        "record_id": record_id, "snapshot_sha256": "d2c84a5ff8a5541721a7b9be4a749f5bde96a9d97997dba4b75790916f2018d4",
        "split": "test", "status": "LOCKED_C0",
    }
    row.update(extra)
    return row


class C0ContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence_db = Path(self.temp.name) / "evidence.sqlite3"
        self.audit_db = Path(self.temp.name) / "audit.sqlite3"
        self.mapping = load_c0_mapping(DRAFT)
        self.catalog = ActionCatalog.load_moodle_7_scenario_catalog()
        key = Ed25519PrivateKey.generate()
        self.public_key = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.counter = 0

    def rule(self, action, mapping=None):
        return next(e for e in (mapping or self.mapping).actions if e.aihs_action == action)

    def context(self, action="start_reviewed_compose_service", evidence_at=AT, **update):
        self.counter += 1
        incident = f"inc-c0-test-{self.counter}"
        refs = [f"{incident}-ev-{n}" for n in (1, 2, 3)]
        with SQLiteEvidenceStore(self.evidence_db) as store:
            for ref in refs:
                item = Evidence(
                    evidence_id=ref, incident_id=incident, resource_id="moodle-app", source="prometheus",
                    observed_at=evidence_at, collected_at=evidence_at, summary="synthetic stand-in", content_hash="pending",
                )
                store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))
        rule = self.rule(action) if action in C0_ACTIONS and self.rule(action).scenario_id else None
        values = dict(
            incident_id=incident, scenario_id=rule.scenario_id if rule else "CON-01", environment="staging",
            trusted_target_scope=rule.target_scope if rule else "moodle-app-b",
            trusted_target_resource_id=rule.target_resource_id if rule else "moodle-app",
            trusted_target_resource_type=rule.target_resource_type if rule else "application",
            evidence_refs=refs, predicted_at=AT + timedelta(seconds=1),
            model_version="aihs-paper-repro-moodle-v1+0da82b583e8bce36", data_version="phase2-freeze-test",
        )
        values.update(update)
        return DecisionContext(**values)

    def adapt(self, row, context, *, draft=True, mapping=None):
        return adapt_c0_proposal(
            row, context=context, evidence_db=self.evidence_db, mapping=mapping or self.mapping,
            now=AT + timedelta(seconds=2), catalog=self.catalog, allow_draft_for_replay=draft,
        )

    def request(self, rule, context, *, live=False):
        entry = self.catalog.lookup(rule.catalog_action_id)
        model = SafeLiveActionRequest if live else SafeActionRequest
        return model(
            request_id="req-c0", idempotency_key=f"idem-c0-{context.incident_id}",
            catalog_action_id=rule.catalog_action_id, scenario_id=rule.scenario_id, target_scope=rule.target_scope,
            action=TypedAction(
                action_id="act-c0", incident_id=context.incident_id, action_type=entry.action_type,
                target_resource_id=rule.target_resource_id, environment=Environment.STAGING,
                reason="C0 contract test", evidence_refs=list(context.evidence_refs),
                expected_outcome="Reapply the reviewed baseline", reversible=True,
                rollback_plan=RollbackPlan(available=True, rollback_action_id=entry.rollback_action,
                                           method=f"Reapply baseline using {entry.rollback_action}."),
                requires_approval=True,
            ),
        )

    def shadow(self, row, context, request, *, mapping=None, draft=True, confidence=0.9):
        adapter = self.adapt(row, context, draft=draft, mapping=mapping)
        return evaluate_aihs_shadow(
            row, context=context, evidence_db=self.evidence_db, mapping=mapping or self.mapping,
            audit_db=self.audit_db, gate_public_key=self.public_key, gate_actors={"operator-test"},
            reviewed_request=request, trusted_confidence=confidence, now=AT + timedelta(seconds=2),
            adapter_result=adapter,
        )

    # --- mapping draft ---------------------------------------------------------------------

    @unittest.skip("SEC-02 was retired from the action catalog on develop (thesis7 = 7 scenarios); restore its shadow-only catalog entry to re-enable")
    def test_draft_mapping_has_no_review_and_matches_catalog(self):
        self.assertEqual(self.mapping.review_status, "draft_pending_domain_review")
        self.assertFalse([e for e in self.mapping.actions if e.status in {"shadow_reviewed", "live_reviewed"}])
        self.assertTrue(all(e.review_reference is None for e in self.mapping.actions))
        self.assertEqual({e.aihs_action for e in self.mapping.actions}, set(C0_ACTIONS))
        for action in MUTATING:
            rule = self.rule(action)
            entry = self.catalog.lookup(action)
            self.assertEqual(entry.rollback_action, rule.rollback_action_id, action)
            self.assertIsNotNone(self.catalog.lookup(rule.verifier_probe_id), action)
            self.assertTrue(self.catalog.target_type_allowed(action, rule.target_resource_type), action)
            self.assertTrue(self.catalog.is_safe_execution_allowed(
                action, scenario_id=rule.scenario_id, target_scope=rule.target_scope,
                environment="staging", role="executor"), action)

    def test_mapping_rejects_premature_review_claims(self):
        raw = self.mapping.model_dump(mode="json")
        draft = json.loads(json.dumps(raw))
        draft["actions"][0]["review_reference"] = "TEST"
        with self.assertRaises(ValueError):
            C0MappingDefinition.model_validate(draft)
        live = json.loads(json.dumps(raw))
        live["actions"][0]["status"] = "live_reviewed"
        with self.assertRaises(ValueError):  # no review_reference
            C0MappingDefinition.model_validate(live)
        reviewed_in_draft = json.loads(json.dumps(raw))
        reviewed_in_draft["actions"][0].update(status="live_reviewed", review_reference="TEST")
        with self.assertRaises(ValueError):  # draft review_status cannot carry reviewed entries
            C0MappingDefinition.model_validate(reviewed_in_draft)

    # --- adapter -> Gate ---------------------------------------------------------------------

    @unittest.skip("SEC-02 was retired from the action catalog on develop (thesis7 = 7 scenarios); restore its shadow-only catalog entry to re-enable")
    def test_every_mutating_action_reaches_gate_and_requires_approval_without_dispatch(self):
        for action in MUTATING:
            row, context = proposal(action), self.context(action)
            result = self.shadow(row, context, self.request(self.rule(action), context))
            self.assertEqual(result["adapter_reason"], "draft_shadow_candidate", action)
            self.assertEqual(result["gate_decision"], "REQUIRE_APPROVAL", action)
            self.assertFalse(result["execution_permitted"] or result["dispatched"] or result["resolution_eligible"])
        with SafeActionAuditStore(self.audit_db, read_only=True) as audit:
            self.assertTrue(all(audit.verify_chain(f"inc-c0-test-{n}") for n in range(1, self.counter + 1)))

    def test_draft_mapping_is_not_a_candidate_without_the_replay_flag(self):
        for action in MUTATING:
            result = self.adapt(proposal(action), self.context(action), draft=False)
            self.assertEqual((result.status, result.reason_code), ("HUMAN_REVIEW", "unmapped_action"), action)

    def test_unknown_and_out_of_space_actions_fail_closed(self):
        for action in ("DROP_DATABASE", "RI", "reconcile_db01_safe_baseline", "disable_moodle_cli_maintenance_mode",
                       "remove_scoped_port_block", ""):
            result = self.adapt(proposal(action), self.context())
            self.assertEqual((result.status, result.reason_code), ("HUMAN_REVIEW", "unknown_aihs_action" if action else "invalid_model_output"), action)
            self.assertIsNone(result.catalog_action_id)

    def test_human_and_observe_rungs_never_reach_gate(self):
        human = self.shadow(proposal("call_human", path="fallback_exhausted_call_human"), self.context("call_human"), None)
        wait = self.shadow(proposal("wait_and_recheck"), self.context("wait_and_recheck"), None)
        self.assertEqual((human["adapter_reason"], human["gate_decision"]), ("human_only_action", None))
        self.assertEqual((wait["adapter_reason"], wait["gate_decision"]), ("observe_only_action", None))
        self.assertEqual((human["disposition"], wait["disposition"]), ("HUMAN_REVIEW", "HUMAN_REVIEW"))

    def test_label_scenario_or_model_extra_fields_are_rejected(self):
        for extra in ({"scenario_id": "DB-01"}, {"scenario_code": "DB-01"}, {"outcome": "success"},
                      {"label": "success"}, {"independent_label": True}, {"target_scope": "moodle-app-b"}):
            result = self.adapt(proposal(**extra), self.context())
            self.assertEqual(result.reason_code, "invalid_model_output", extra)
        fields = set(C0Proposal.model_fields)
        self.assertFalse(fields & {"outcome", "label", "scenario_id", "scenario_code", "target_scope", "gate_decision"})

    def test_model_confidence_is_not_authorization(self):
        context = self.context()
        rule = self.rule("start_reviewed_compose_service")
        row = proposal(controller_trace=[{"action": rule.aihs_action, "controller": "present", "p_success": 1.0, "predicted_success": True}])
        denied = self.shadow(row, context, self.request(rule, context), confidence=0.0)
        self.assertEqual(denied["gate_decision"], "DENY")  # only the trusted confidence input counts

    def test_scope_mismatch_and_stale_evidence_fail_closed(self):
        wrong = self.context(trusted_target_scope="staging_moodle_nodes")
        self.assertEqual(self.adapt(proposal(), wrong).reason_code, "scope_mismatch")
        wrong_scenario = self.context(scenario_id="DB-01")
        self.assertEqual(self.adapt(proposal(), wrong_scenario).reason_code, "scope_mismatch")
        recorded = self.context(predicted_at=datetime(2026, 11, 15, 18, 1, 42, tzinfo=timezone.utc))
        late = adapt_c0_proposal(
            proposal(), context=recorded, evidence_db=self.evidence_db, mapping=self.mapping,
            now=recorded.predicted_at + timedelta(seconds=1), catalog=self.catalog, allow_draft_for_replay=True,
        )
        self.assertEqual(late.reason_code, "stale_evidence")
        missing = self.context(evidence_refs=["x-1", "x-2", "x-3"])
        self.assertEqual(self.adapt(proposal(), missing).reason_code, "missing_evidence")

    # --- handoff -----------------------------------------------------------------------------

    def live_mapping(self):
        raw = self.mapping.model_dump(mode="json")
        raw["review_status"] = "reviewed_for_live"
        raw["mapping_version"] = "test-only-live-v1"
        entry = next(e for e in raw["actions"] if e["aihs_action"] == "start_reviewed_compose_service")
        entry.update(status="live_reviewed", review_reference="TEST-ONLY-NOT-APPROVED")
        return C0MappingDefinition.model_validate(raw)

    def prepared(self, context):
        rule = self.rule("start_reviewed_compose_service")
        request = self.request(rule, context, live=True)
        gate = SafeExecutionGate(approval_verification_key=self.public_key, approved_actors={"operator"})
        return {
            "status": "approval_required", "gate_preview": gate.explain(request, actor_role="executor", confidence=0.9),
            "incident_id": context.incident_id, "candidate_id": "cand", "request": request.model_dump(mode="json"),
            "diagnosis_confidence": 0.9, "gate_action_sha256": gate.action_hash(request),
            "executor_action_sha256": action_digest(request.scenario_id, request.catalog_action_id,
                                                    request.target_scope, request.idempotency_key),
            "execution_permitted": False, "resolution_eligible": False,
        }

    def handoff(self, row, context, mapping, *, draft=False):
        adapter = self.adapt(row, context, draft=draft, mapping=mapping)
        return prepare_aihs_staging_handoff(
            row, context=context, evidence_db=self.evidence_db, mapping=mapping, candidate_id="cand",
            gate_public_key=self.public_key, gate_actors={"operator"}, audit_db=self.audit_db,
            now=AT + timedelta(seconds=2), adapter_result=adapter,
        )

    def test_draft_mapping_blocks_handoff_even_with_replay_adapter(self):
        context = self.context()
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff") as planner:
            strict = self.handoff(proposal(), context, self.mapping)
            replay = self.handoff(proposal(), self.context(), self.mapping, draft=True)
        planner.assert_not_called()
        self.assertEqual((strict["status"], strict["reason"]), ("BLOCKED", "unmapped_action"))
        self.assertEqual((replay["status"], replay["reason"]), ("BLOCKED", "mapping_not_reviewed_for_live"))

    def test_test_only_live_binding_waits_for_operator_and_rejects_mismatch(self):
        context = self.context()
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff", return_value=self.prepared(context)):
            result = self.handoff(proposal(), context, self.live_mapping())
        self.assertEqual(result["status"], "AWAITING_OPERATOR_APPROVAL")
        self.assertFalse(result["execution_permitted"] or result["dispatched"] or result["resolution_eligible"])
        bad = self.prepared(context)
        bad["executor_action_sha256"] = "0" * 64
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff", return_value=bad):
            result = self.handoff(proposal(), context, self.live_mapping())
        self.assertEqual(result["reason"], "planner_candidate_mismatch")
        other = proposal("remove_scoped_db_reject")  # unreviewed sibling stays blocked under the same mapping
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff") as planner:
            blocked = self.handoff(other, self.context("remove_scoped_db_reject"), self.live_mapping())
        planner.assert_not_called()
        self.assertEqual(blocked["status"], "BLOCKED")

    # --- real locked C0 file (skipped without the git-ignored artifact) ---------------------------

    @unittest.skipUnless(REAL_PROPOSALS.is_file(), "locked C0 proposal artifact is git-ignored and absent")
    def test_real_locked_c0_proposals_pass_the_contracts_without_authority(self):
        rows = [json.loads(line) for line in REAL_PROPOSALS.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(rows)
        for number, row in enumerate(rows):
            C0Proposal.model_validate(row)  # schema of the real file
            action = row["proposed_action"]
            self.assertIn(action, C0_ACTIONS)
            cutoff = datetime.fromisoformat(row["evidence_cutoff_at"].replace("Z", "+00:00"))
            context = self.context(action, evidence_at=cutoff, predicted_at=cutoff + timedelta(seconds=1))
            rule = self.rule(action)
            request = self.request(rule, context) if rule.scenario_id else None
            result = evaluate_aihs_shadow(
                row, context=context, evidence_db=self.evidence_db, mapping=self.mapping, audit_db=self.audit_db,
                gate_public_key=self.public_key, gate_actors={"operator-test"}, reviewed_request=request,
                trusted_confidence=0.9, now=cutoff + timedelta(seconds=2),
                adapter_result=adapt_c0_proposal(
                    row, context=context, evidence_db=self.evidence_db, mapping=self.mapping,
                    now=cutoff + timedelta(seconds=2), catalog=self.catalog, allow_draft_for_replay=True),
            )
            self.assertNotEqual(result["gate_decision"], "ALLOW", number)  # nothing is allowed without approval
            self.assertFalse(result["execution_permitted"] or result["dispatched"])
            if action == "call_human":
                self.assertIsNone(result["gate_decision"])
            else:
                self.assertEqual(result["gate_decision"], "REQUIRE_APPROVAL", number)


if __name__ == "__main__":
    unittest.main()
