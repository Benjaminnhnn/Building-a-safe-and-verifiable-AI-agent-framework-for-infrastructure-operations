"""Controlled staging preparation tests; no Operator signature or Executor."""

import tempfile
import unittest
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.evidence.adapters import SafeLiveActionRequest
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.evidence.evidence_collectors import EvidenceDraft
from core.moodle.aihs_contract import DecisionContext, MappingDefinition, load_mapping
from core.moodle.aihs_staging_handoff import prepare_aihs_staging_handoff
from core.moodle.alert_integration import process_moodle_alert
from core.safety.safe_action_audit import SafeActionAuditStore
from core.safety.safe_execution_gate import SafeExecutionGate
from core.safety.safe_executor_client import action_digest
from core.schema.action import RollbackPlan, TypedAction
from core.schema.common import ActionType, Environment, ResourceType
from core.schema.evidence import Evidence


ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
OUTPUT = {"hostname": "synthetic-server", "timestamp": 12345, "action": ["MSR"]}


class AIHSStagingHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence_db = Path(self.temp.name) / "evidence.sqlite3"
        self.audit_db = Path(self.temp.name) / "audit.sqlite3"
        self.mapping = load_mapping(ROOT / "evaluation/aihs/a3-action-mapping.json")
        self.context = DecisionContext(
            incident_id="inc-staging-fixture", scenario_id="CON-01", environment="staging",
            trusted_target_scope="moodle-app-b", trusted_target_resource_id="moodle-app",
            trusted_target_resource_type=ResourceType.APPLICATION,
            evidence_refs=["ev-a", "ev-b", "ev-c"], predicted_at=AT,
            model_version="synthetic-test-model", data_version="synthetic-fixture-v1",
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
                    summary="synthetic fixture", content_hash="pending",
                )
                store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))

    def live_fixture_mapping(self):
        raw = self.mapping.model_dump(mode="json")
        raw["review_status"] = "reviewed_for_live"
        raw["mapping_version"] = "test-only-live-binding-v1"
        rule = next(item for item in raw["actions"] if item["aihs_action"] == "MSR")
        rule.update({
            "status": "live_reviewed", "catalog_action_id": "start_reviewed_compose_service",
            "scenario_id": "CON-01", "target_scope": "moodle-app-b",
            "target_resource_id": "moodle-app", "target_resource_type": "application",
            "required_role": "executor", "review_reference": "TEST-ONLY-NOT-APPROVED",
            "reason": "Synthetic binding fixture only; no domain review was performed.",
        })
        return MappingDefinition.model_validate(raw)

    def prepared_fixture(self):
        request = SafeLiveActionRequest(
            request_id="req-fixture", idempotency_key="idem-fixture",
            catalog_action_id="start_reviewed_compose_service", scenario_id="CON-01",
            target_scope="moodle-app-b",
            action=TypedAction(
                action_id="act-fixture", incident_id=self.context.incident_id,
                action_type=ActionType.START_CONTAINER, target_resource_id="moodle-app",
                environment=Environment.STAGING,
                reason="synthetic planner fixture", evidence_refs=list(self.context.evidence_refs),
                expected_outcome="Moodle web container starts", reversible=True,
                rollback_plan=RollbackPlan(
                    available=True, rollback_action_id="reconcile_con01_safe_baseline",
                    method="Re-apply reviewed baseline after partial execution.",
                ),
                requires_approval=True,
            ),
        )
        gate = SafeExecutionGate(approval_verification_key=self.public_key, approved_actors={"operator"})
        preview = gate.explain(request, actor_role="executor", confidence=0.9)
        self.assertEqual(preview["decision"], "REQUIRE_APPROVAL")
        return {
            "status": "approval_required", "gate_preview": preview,
            "incident_id": self.context.incident_id, "candidate_id": "candidate-fixture",
            "request": request.model_dump(mode="json"), "diagnosis_confidence": 0.9,
            "gate_action_sha256": gate.action_hash(request),
            "executor_action_sha256": action_digest(
                request.scenario_id, request.catalog_action_id,
                request.target_scope, request.idempotency_key,
            ),
            "execution_permitted": False, "resolution_eligible": False,
        }

    def prepare(self, *, mapping=None, output=OUTPUT, context=None):
        return prepare_aihs_staging_handoff(
            output, context=context or self.context, evidence_db=self.evidence_db,
            mapping=mapping or self.mapping, candidate_id="candidate-fixture",
            gate_public_key=self.public_key, gate_actors={"operator"},
            audit_db=self.audit_db, now=AT + timedelta(seconds=1),
        )

    def test_repository_mapping_blocks_without_planner_or_dispatch(self):
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff") as planner:
            result = self.prepare()
        planner.assert_not_called()
        self.assertEqual((result["status"], result["reason"]), ("BLOCKED", "unmapped_action"))
        self.assertFalse(result["execution_permitted"] or result["dispatched"])
        with SafeActionAuditStore(self.audit_db, read_only=True) as audit:
            self.assertTrue(audit.verify_chain(self.context.incident_id))

    def test_shadow_review_alone_cannot_prepare_staging(self):
        raw = self.live_fixture_mapping().model_dump(mode="json")
        raw["review_status"] = "reviewed_for_shadow"
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff") as planner:
            result = self.prepare(mapping=MappingDefinition.model_validate(raw))
        planner.assert_not_called()
        self.assertEqual(result["reason"], "mapping_not_reviewed_for_live")

    def test_test_only_live_binding_waits_for_operator(self):
        fake = self.prepared_fixture()
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff", return_value=fake) as planner:
            result = self.prepare(mapping=self.live_fixture_mapping())
        planner.assert_called_once()
        self.assertEqual(result["status"], "AWAITING_OPERATOR_APPROVAL")
        self.assertEqual(result["operator_handoff"]["request"], fake["request"])
        self.assertFalse(result["execution_permitted"] or result["dispatched"] or result["resolution_eligible"])
        with SafeActionAuditStore(self.audit_db, read_only=True) as audit:
            self.assertTrue(audit.verify_chain(self.context.incident_id))

    def test_planner_mismatch_and_bad_digest_block(self):
        fake = self.prepared_fixture()
        fake["request"]["target_scope"] = "staging_moodle_nodes"
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff", return_value=fake):
            wrong_scope = self.prepare(mapping=self.live_fixture_mapping())
        self.assertEqual(wrong_scope["reason"], "planner_candidate_mismatch")
        fake = self.prepared_fixture()
        fake["executor_action_sha256"] = "0" * 64
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff", return_value=fake):
            wrong_digest = self.prepare(mapping=self.live_fixture_mapping())
        self.assertEqual(wrong_digest["reason"], "planner_candidate_mismatch")
        fake = self.prepared_fixture()
        fake["candidate_id"] = "different-candidate"
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff", return_value=fake):
            wrong_candidate = self.prepare(mapping=self.live_fixture_mapping())
        self.assertEqual(wrong_candidate["reason"], "planner_candidate_mismatch")

    def test_missing_candidate_or_stale_prediction_blocks(self):
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff", side_effect=ValueError("missing")):
            missing = self.prepare(mapping=self.live_fixture_mapping())
        self.assertEqual(missing["reason"], "trusted_handoff_unavailable")
        stale = self.context.model_copy(update={"predicted_at": AT - timedelta(seconds=300)})
        with patch("core.moodle.aihs_staging_handoff.prepare_moodle_live_handoff") as planner:
            result = self.prepare(mapping=self.live_fixture_mapping(), context=stale)
        planner.assert_not_called()
        self.assertEqual(result["reason"], "stale_prediction")

    def test_live_mapping_requires_review_reference(self):
        raw = self.live_fixture_mapping().model_dump(mode="json")
        entry = next(item for item in raw["actions"] if item["aihs_action"] == "MSR")
        entry["review_reference"] = None
        with self.assertRaises(ValueError):
            MappingDefinition.model_validate(raw)

    def test_offline_real_planner_and_handoff_prepare_without_executor(self):
        """Exercise actual incident planner and Gate preview with synthetic probes."""
        samples = {
            "moodle_fault_con01_web_stopped": [(0.0, "moodle-app-a"), (1.0, "moodle-app-b")],
            "moodle_node_web_running": [(1.0, "moodle-app-a"), (0.0, "moodle-app-b")],
            "moodle_node_up": [(1.0, "moodle-app-a"), (1.0, "moodle-app-b")],
            "moodle_cadvisor_up": [(1.0, "moodle-app-a"), (1.0, "moodle-app-b")],
        }

        def collect(_self, query_id, _expression, resource_id):
            return [
                EvidenceDraft(
                    "prometheus", resource_id, "metric_sample", datetime.now(timezone.utc),
                    {"query_id": query_id, "sample_value": value, "labels": {"instance": instance}},
                )
                for value, instance in samples.get(query_id, [])
            ]

        alert = {
            "status": "firing", "fingerprint": "fp-aihs-staging-fixture",
            "labels": {"alertname": "MoodleWebContainerMissing", "service": "moodle", "environment": "staging"},
        }
        environment = {
            "AIOPS_UNIFIED_CORE_MODE": "investigate",
            "AIOPS_EVIDENCE_DB": str(self.evidence_db),
            "PROMETHEUS_URL": "http://prometheus:9090",
            "AIOPS_APPROVAL_MODE": "operator",
        }
        with patch.dict(os.environ, environment), patch(
            "core.evidence.evidence_collectors.PrometheusEvidenceCollector._collect_query", new=collect,
        ):
            report = process_moodle_alert(alert)
            with SQLiteEvidenceStore(self.evidence_db) as store:
                candidate = next(item for item in store.query(
                    incident_id=report["incident_id"], source="ai_decision"
                ) if item.metadata.get("decision_kind") == "remediation_candidate")
                candidate_data = json.loads(candidate.metadata["decision_json"])
            context = self.context.model_copy(update={
                "incident_id": report["incident_id"],
                "evidence_refs": candidate_data["evidence_refs"],
                "predicted_at": datetime.now(timezone.utc),
            })
            result = prepare_aihs_staging_handoff(
                OUTPUT, context=context, evidence_db=self.evidence_db,
                mapping=self.live_fixture_mapping(), candidate_id=candidate.evidence_id,
                gate_public_key=self.public_key, gate_actors={"operator"},
                audit_db=self.audit_db,
            )
        self.assertEqual(result["status"], "AWAITING_OPERATOR_APPROVAL")
        self.assertEqual(result["gate_preview_decision"], "REQUIRE_APPROVAL")
        self.assertFalse(result["execution_permitted"] or result["dispatched"])


if __name__ == "__main__":
    unittest.main()
