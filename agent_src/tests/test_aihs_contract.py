"""A3 contract tests; no model call, approval, or executor dispatch."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.moodle.aihs_contract import (
    DecisionContext, MappingDefinition, adapt_aihs_output, load_mapping,
)
from core.schema.common import ResourceType
from core.schema.evidence import Evidence


ROOT = Path(__file__).resolve().parents[2]
MAPPING = ROOT / "evaluation/aihs/a3-action-mapping.json"
AT = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
OUTPUT = {"hostname": "untrusted-server-name", "timestamp": 12345, "action": ["NOP"]}


def reviewed_test_mapping():
    """Test-only observation mapping; repository mapping remains unmapped."""
    raw = load_mapping(MAPPING).model_dump(mode="json")
    raw["review_status"] = "reviewed_for_shadow"
    raw["mapping_version"] = "test-only-observation-v1"
    entry = next(item for item in raw["actions"] if item["aihs_action"] == "NOP")
    entry.update({
        "status": "shadow_reviewed",
        "catalog_action_id": "wait_for_alb_target_health",
        "scenario_id": "CON-01",
        "target_scope": "moodle-app-b",
        "target_resource_id": "moodle-app-b",
        "target_resource_type": "application",
        "required_role": "verifier",
        "reason": "Test fixture for an observation-only shadow candidate.",
    })
    return MappingDefinition.model_validate(raw)


class AIHSContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "evidence.sqlite3"
        self.context = DecisionContext(
            incident_id="inc-test-001", scenario_id="CON-01", environment="staging",
            trusted_target_scope="moodle-app-b",
            trusted_target_resource_id="moodle-app-b",
            trusted_target_resource_type=ResourceType.APPLICATION,
            evidence_refs=["ev-1", "ev-2", "ev-3"], predicted_at=AT,
            model_version="aihs-public-prototype-as-is-v1",
            data_version="synthetic-source-sha256",
        )
        self.mapping = load_mapping(MAPPING)
        self.put_evidence()

    def put_evidence(self, *, incident_id="inc-test-001", age_seconds=10):
        with SQLiteEvidenceStore(self.db) as store:
            for ref in self.context.evidence_refs:
                item = Evidence(
                    evidence_id=ref, incident_id=incident_id, resource_id="moodle-app-b",
                    source="prometheus", observed_at=AT - timedelta(seconds=age_seconds),
                    collected_at=AT - timedelta(seconds=age_seconds),
                    summary="bounded numeric signal", content_hash="pending",
                )
                store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))

    def adapt(self, output=OUTPUT, *, mapping=None, context=None, db=None):
        return adapt_aihs_output(
            output, context=context or self.context, evidence_db=db or self.db,
            mapping=mapping or self.mapping, now=AT + timedelta(seconds=1),
        )

    def test_repository_mapping_fails_closed(self):
        self.assertEqual([x.status for x in self.mapping.actions].count("unmapped"), 5)
        result = self.adapt()
        self.assertEqual((result.status, result.reason_code), ("HUMAN_REVIEW", "unmapped_action"))
        self.assertFalse(result.execution_permitted)
        self.assertFalse(result.dispatched)
        self.assertIsNone(result.catalog_action_id)

    def test_test_only_observation_mapping_emits_non_executable_candidate(self):
        result = self.adapt(mapping=reviewed_test_mapping())
        self.assertEqual((result.status, result.reason_code), ("SHADOW_CANDIDATE", "shadow_candidate_ready"))
        self.assertEqual(result.catalog_action_id, "wait_for_alb_target_health")
        self.assertEqual(result.target_scope, "moodle-app-b")
        self.assertEqual(result.evidence_refs, self.context.evidence_refs)
        self.assertFalse(result.execution_permitted or result.dispatched)
        self.assertNotIn("command", type(result).model_fields)
        self.assertNotIn("approval", type(result).model_fields)

    def test_unknown_and_malformed_model_output(self):
        self.assertEqual(self.adapt({**OUTPUT, "action": ["DROP_DATABASE"]}).reason_code, "unknown_aihs_action")
        self.assertEqual(self.adapt({**OUTPUT, "command": "rm -rf /"}).reason_code, "invalid_model_output")
        self.assertEqual(self.adapt(None).reason_code, "model_unavailable")
        self.assertEqual(self.adapt({**OUTPUT, "action": ["RMA"]}).reason_code, "human_only_action")

    def test_missing_duplicate_and_cross_incident_evidence(self):
        missing = self.context.model_copy(update={"evidence_refs": ["ev-1", "ev-2", "ev-missing"]})
        duplicate = self.context.model_copy(update={"evidence_refs": ["ev-1", "ev-1", "ev-2"]})
        other = self.context.model_copy(update={"incident_id": "inc-other"})
        self.assertEqual(self.adapt(context=missing).reason_code, "missing_evidence")
        self.assertEqual(self.adapt(context=duplicate).reason_code, "missing_evidence")
        self.assertEqual(self.adapt(context=other).reason_code, "cross_incident_evidence")

    def test_stale_evidence_and_prediction(self):
        old = self.context.model_copy(update={"predicted_at": AT + timedelta(seconds=300)})
        self.assertEqual(self.adapt(context=old).reason_code, "stale_prediction")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "stale.sqlite3"
            with SQLiteEvidenceStore(database) as store:
                for ref in self.context.evidence_refs:
                    item = Evidence(
                        evidence_id=ref, incident_id=self.context.incident_id,
                        resource_id="moodle-app-b", source="prometheus",
                        observed_at=AT - timedelta(seconds=300),
                        collected_at=AT - timedelta(seconds=300),
                        summary="old signal", content_hash="pending",
                    )
                    store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))
            self.assertEqual(self.adapt(db=database).reason_code, "stale_evidence")

    def test_scope_and_catalog_checks(self):
        mapping = reviewed_test_mapping()
        wrong_scope = self.context.model_copy(update={"trusted_target_scope": "staging_moodle_nodes"})
        self.assertEqual(self.adapt(mapping=mapping, context=wrong_scope).reason_code, "scope_mismatch")
        wrong_environment = self.context.model_copy(update={"environment": "production"})
        self.assertEqual(self.adapt(mapping=mapping, context=wrong_environment).reason_code, "scope_mismatch")
        raw = mapping.model_dump(mode="json")
        raw["actions"][0]["catalog_action_id"] = "restart_moodle_container"
        unreviewed = MappingDefinition.model_validate(raw)
        self.assertEqual(self.adapt(mapping=unreviewed).reason_code, "catalog_not_allowed")

    def test_ground_truth_cannot_be_used_as_decision_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "invalid-source.sqlite3"
            with SQLiteEvidenceStore(database) as store:
                for ref in self.context.evidence_refs:
                    item = Evidence(
                        evidence_id=ref, incident_id=self.context.incident_id,
                        resource_id="moodle-app-b", source="ground_truth",
                        observed_at=AT - timedelta(seconds=10),
                        collected_at=AT - timedelta(seconds=10),
                        summary="post-decision fixture", content_hash="pending",
                    )
                    store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))
            self.assertEqual(self.adapt(db=database).reason_code, "invalid_evidence")


if __name__ == "__main__":
    unittest.main()
