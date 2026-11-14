"""Offline AIHS shadow tests; no cloud, approval, or Executor calls."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.evidence.adapters import SafeActionRequest
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.moodle.aihs_contract import DecisionContext, MappingDefinition, load_mapping
from core.moodle.aihs_shadow import evaluate_aihs_shadow
from core.safety.safe_action_audit import SafeActionAuditStore
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment, ResourceType
from core.schema.evidence import Evidence


ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
OUTPUT = {"hostname": "untrusted-host", "timestamp": 12345, "action": ["NOP"]}


class AIHSShadowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence_db = Path(self.temp.name) / "evidence.sqlite3"
        self.audit_db = Path(self.temp.name) / "audit.sqlite3"
        self.context = DecisionContext(
            incident_id="inc-shadow-test", scenario_id="CON-01", environment="staging",
            trusted_target_scope="moodle-app-b", trusted_target_resource_id="moodle-app",
            trusted_target_resource_type=ResourceType.APPLICATION,
            evidence_refs=["ev-a", "ev-b", "ev-c"], predicted_at=AT,
            model_version="aihs-public-prototype-as-is-v1", data_version="synthetic-fixture-v1",
        )
        self.mapping = load_mapping(ROOT / "evaluation/aihs/a3-action-mapping.json")
        raw = self.mapping.model_dump(mode="json")
        raw["review_status"] = "reviewed_for_shadow"
        raw["mapping_version"] = "test-only-observation-v1"
        nop = next(item for item in raw["actions"] if item["aihs_action"] == "NOP")
        nop.update({
            "status": "shadow_reviewed", "catalog_action_id": "wait_for_alb_target_health",
            "scenario_id": "CON-01", "target_scope": "moodle-app-b",
            "target_resource_id": "moodle-app", "target_resource_type": "application",
            "required_role": "verifier", "reason": "Synthetic test fixture, not an approved mapping.",
        })
        self.test_mapping = MappingDefinition.model_validate(raw)
        self.request = SafeActionRequest(
            request_id="req-shadow", idempotency_key="idem-shadow",
            catalog_action_id="wait_for_alb_target_health", scenario_id="CON-01",
            target_scope="moodle-app-b",
            action=TypedAction(
                action_id="act-shadow", incident_id=self.context.incident_id,
                action_type=ActionType.READ_HEALTH, target_resource_id="moodle-app",
                environment=Environment.STAGING, reason="reviewed synthetic observation",
                evidence_refs=list(self.context.evidence_refs),
                expected_outcome="ALB target state observed", reversible=True,
                rollback_plan=RollbackPlan(available=False),
            ),
        )
        key = Ed25519PrivateKey.generate()
        self.public_key = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        with SQLiteEvidenceStore(self.evidence_db) as store:
            for ref in self.context.evidence_refs:
                item = Evidence(
                    evidence_id=ref, incident_id=self.context.incident_id,
                    resource_id="moodle-app", source="prometheus",
                    observed_at=AT - timedelta(seconds=10),
                    collected_at=AT - timedelta(seconds=10),
                    summary="synthetic numeric observation", content_hash="pending",
                )
                store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))

    def run_shadow(self, output=OUTPUT, *, mapping=None, request=None, confidence=0.9, context=None):
        return evaluate_aihs_shadow(
            output, context=context or self.context, evidence_db=self.evidence_db,
            mapping=mapping or self.mapping, audit_db=self.audit_db,
            gate_public_key=self.public_key, gate_actors={"operator-test"},
            reviewed_request=request, trusted_confidence=confidence,
            now=AT + timedelta(seconds=1),
        )

    def test_unreviewed_mapping_audits_human_review_without_gate(self):
        result = self.run_shadow(request=self.request)
        self.assertEqual(result["disposition"], "HUMAN_REVIEW")
        self.assertEqual(result["adapter_reason"], "unmapped_action")
        self.assertIsNone(result["gate_decision"])
        self.assertFalse(result["execution_permitted"] or result["dispatched"])
        with SafeActionAuditStore(self.audit_db, read_only=True) as audit:
            self.assertTrue(audit.verify_chain(self.context.incident_id))
            events = audit.list_for_incident(self.context.incident_id)
            self.assertEqual(len(events), 1)
            self.assertNotIn("untrusted-host", str(events))

    def test_test_only_mapping_evaluates_real_gate_without_dispatch(self):
        result = self.run_shadow(mapping=self.test_mapping, request=self.request)
        self.assertEqual((result["adapter_status"], result["gate_decision"]), ("SHADOW_CANDIDATE", "ALLOW"))
        self.assertEqual(result["disposition"], "GATE_SHADOW_ONLY")
        self.assertFalse(result["execution_permitted"] or result["dispatched"] or result["resolution_eligible"])
        with SafeActionAuditStore(self.audit_db, read_only=True) as audit:
            self.assertTrue(audit.verify_chain(self.context.incident_id))
            self.assertEqual(audit.list_for_incident(self.context.incident_id)[0]["payload"]["gate_decision"], "ALLOW")

    def test_gate_denies_low_confidence(self):
        result = self.run_shadow(mapping=self.test_mapping, request=self.request, confidence=0.0)
        self.assertEqual(result["gate_decision"], "DENY")
        self.assertFalse(result["dispatched"])

    def test_missing_template_or_confidence_stays_human_review(self):
        no_template = self.run_shadow(mapping=self.test_mapping)
        no_confidence = self.run_shadow(mapping=self.test_mapping, request=self.request, confidence=None)
        self.assertEqual(no_template["reason"], "missing_reviewed_request")
        self.assertEqual(no_confidence["reason"], "missing_trusted_confidence")
        self.assertIsNone(no_template["gate_decision"])
        self.assertIsNone(no_confidence["gate_decision"])

    def test_mismatched_request_never_reaches_gate(self):
        bad = self.request.model_copy(update={"target_scope": "staging_moodle_nodes"})
        result = self.run_shadow(mapping=self.test_mapping, request=bad)
        self.assertEqual(result["reason"], "reviewed_request_mismatch")
        self.assertIsNone(result["gate_decision"])

    def test_unknown_action_and_stale_evidence_fail_closed(self):
        unknown = self.run_shadow({**OUTPUT, "action": ["DROP_DATABASE"]}, request=self.request)
        stale = self.run_shadow(
            mapping=self.test_mapping, request=self.request,
            context=self.context.model_copy(update={"predicted_at": AT - timedelta(seconds=300)}),
        )
        self.assertEqual(unknown["adapter_reason"], "unknown_aihs_action")
        self.assertEqual(stale["adapter_reason"], "stale_prediction")
        self.assertIsNone(unknown["gate_decision"])
        self.assertIsNone(stale["gate_decision"])

    def test_model_failure_and_missing_evidence_fail_closed(self):
        failed_model = self.run_shadow(None, mapping=self.test_mapping, request=self.request)
        missing = self.context.model_copy(update={"evidence_refs": ["ev-a", "ev-b", "ev-missing"]})
        missing_evidence = self.run_shadow(
            mapping=self.test_mapping, request=self.request, context=missing,
        )
        self.assertEqual(failed_model["adapter_reason"], "model_unavailable")
        self.assertEqual(missing_evidence["adapter_reason"], "missing_evidence")
        self.assertIsNone(failed_model["gate_decision"])
        self.assertIsNone(missing_evidence["gate_decision"])


if __name__ == "__main__":
    unittest.main()
