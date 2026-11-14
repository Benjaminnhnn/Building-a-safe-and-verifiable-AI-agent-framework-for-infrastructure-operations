"""B2.1 freeze tests: actual data blocks; synthetic cohort checks split rules."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation/aihs"
sys.path.insert(0, str(EVAL))
from b2_freeze_dataset import freeze  # noqa: E402


class AIHSFreezeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.schema = EVAL / "a2-record.schema.json"

    def synthetic_cohort(self):
        template = json.loads((EVAL / "a2-export/audit-records.jsonl").read_text().splitlines()[0])
        rows, reviews = [], []
        for index in range(12):
            row = copy.deepcopy(template)
            decision = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index * 10)
            as_text = lambda value: value.isoformat().replace("+00:00", "Z")
            row.update({
                "record_id": f"a2-{index+1:04d}",
                "incident_group_id": f"g{index+1:04d}",
                "session_group_id": f"s{index+1:04d}",
                "scenario_code": "CON-01", "training_eligible": True,
                "outcome": "success" if index % 2 == 0 else "failure",
                "exclusion_reasons": [],
            })
            row["provenance"] = {
                "evidence_mode": "synthetic", "source_sha256": "a" * 64,
                "historical_code_version": "fixture-v1",
            }
            row["timeline"] = {
                "detected_at": as_text(decision - timedelta(minutes=2)),
                "decision_at": as_text(decision),
                "executed_at": as_text(decision + timedelta(minutes=1)),
                "verified_at": as_text(decision + timedelta(minutes=2)),
            }
            row["pre_decision"] = {
                "snapshot_at": as_text(decision - timedelta(minutes=1)),
                "feature_status": "verified_predecision",
                "evidence_ref_hashes": [hashlib.sha256(f"evidence-{index}".encode()).hexdigest()],
                "features": {"cpu_busy_percent": float(index + 1)},
            }
            row["proposal"] = {"source": "aihs", "action_id": "NOP", "model_version": "fixture-v1"}
            row["post_decision"] = {
                "gate_decision": "ALLOW", "operator_approved": True,
                "executed_action_id": "NOP", "receipt_status": "executed",
                "verifier_verdict": "ready_for_runtime_acceptance" if index % 2 == 0 else "failed",
                "runtime_status": "resolved" if index % 2 == 0 else "failed",
            }
            rows.append(row)
            reviews.append({
                "record_id": row["record_id"], "outcome": row["outcome"],
                "executed_action_id": "NOP", "reviewer_id": "fixture-reviewer",
                "evidence_sha256": hashlib.sha256(f"label-{index}".encode()).hexdigest(),
                "reviewed_at": as_text(decision + timedelta(minutes=3)),
            })
        return rows, reviews

    def write_cohort(self, rows, reviews):
        records = self.work / "records.jsonl"
        labels = self.work / "label-review.jsonl"
        source = self.work / "source-manifest.json"
        records.write_text("".join(json.dumps(row) + "\n" for row in rows))
        labels.write_text("".join(json.dumps(row) + "\n" for row in reviews))
        source.write_text(json.dumps({
            "audit_records_sha256": hashlib.sha256(records.read_bytes()).hexdigest(),
            "candidate_records": len(rows), "source_files": [],
        }))
        return records, labels, source

    def test_real_a2_data_blocks_without_fake_test_split(self):
        result = freeze(
            records=EVAL / "a2-export/audit-records.jsonl",
            source_manifest=EVAL / "a2-export/manifest.json",
            schema_path=self.schema, label_review=None,
            output_root=self.work / "frozen", manifest_out=self.work / "manifest.json",
        )
        self.assertEqual((result["status"], result["selected_records"]), ("BLOCKED", 0))
        self.assertIsNone(result["splits"])
        self.assertFalse((self.work / "frozen").exists())

    def test_synthetic_fixture_freezes_disjoint_time_ordered_splits(self):
        records, labels, source = self.write_cohort(*self.synthetic_cohort())
        kwargs = dict(
            records=records, source_manifest=source, schema_path=self.schema,
            label_review=labels, output_root=self.work / "frozen",
            manifest_out=self.work / "manifest.json", allowed_mode="synthetic",
        )
        result = freeze(**kwargs)
        self.assertEqual(result["status"], "READY")
        self.assertEqual([result["splits"][key]["groups"] for key in ("train", "validation", "test")], [8, 2, 2])
        frozen = self.work / "frozen" / result["freeze_id"]
        all_ids = []
        for name in ("train", "validation", "test"):
            feature_text = (frozen / f"{name}-features.jsonl").read_text()
            self.assertNotIn("scenario_code", feature_text)
            self.assertNotIn("outcome", feature_text)
            all_ids += [json.loads(line)["record_id"] for line in feature_text.splitlines()]
            self.assertTrue((frozen / f"{name}-labels.jsonl").is_file())
        self.assertEqual(len(all_ids), len(set(all_ids)))
        with self.assertRaisesRegex(ValueError, "immutable"):
            freeze(**kwargs)

    def test_answer_bearing_feature_is_rejected(self):
        rows, reviews = self.synthetic_cohort()
        rows[0]["pre_decision"]["features"]["scenario_marker"] = 1
        records, labels, source = self.write_cohort(rows, reviews)
        with self.assertRaisesRegex(ValueError, "answer-bearing feature"):
            freeze(records=records, source_manifest=source, schema_path=self.schema,
                   label_review=labels, output_root=self.work / "frozen",
                   manifest_out=self.work / "manifest.json", allowed_mode="synthetic")

    def test_one_outcome_class_does_not_create_test_set(self):
        rows, reviews = self.synthetic_cohort()
        for row, review in zip(rows, reviews):
            row["outcome"] = review["outcome"] = "success"
            row["post_decision"]["verifier_verdict"] = "ready_for_runtime_acceptance"
            row["post_decision"]["runtime_status"] = "resolved"
        records, labels, source = self.write_cohort(rows, reviews)
        result = freeze(records=records, source_manifest=source, schema_path=self.schema,
                        label_review=labels, output_root=self.work / "frozen",
                        manifest_out=self.work / "manifest.json", allowed_mode="synthetic")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("only_one_observed_outcome_class", result["reasons"])
        self.assertFalse((self.work / "frozen").exists())

    def test_label_review_before_verifier_is_rejected(self):
        rows, reviews = self.synthetic_cohort()
        reviews[0]["reviewed_at"] = rows[0]["timeline"]["executed_at"]
        records, labels, source = self.write_cohort(rows, reviews)
        with self.assertRaisesRegex(ValueError, "after independent verification"):
            freeze(records=records, source_manifest=source, schema_path=self.schema,
                   label_review=labels, output_root=self.work / "frozen",
                   manifest_out=self.work / "manifest.json", allowed_mode="synthetic")


if __name__ == "__main__":
    unittest.main()
