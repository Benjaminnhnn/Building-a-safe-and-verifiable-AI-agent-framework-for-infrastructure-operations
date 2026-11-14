"""B2.2 checks: unavailable empirical metrics remain null and source-bound."""

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation/aihs"
sys.path.insert(0, str(EVAL))
from b2_measure_attempt import measure  # noqa: E402


class AIHSMeasurementAttemptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "measurement.json"
        self.kwargs = dict(
            freeze_path=EVAL / "b2-freeze-manifest-2026-11-14.json",
            audit_path=EVAL / "a2-export/audit-records.jsonl",
            audit_manifest_path=EVAL / "a2-export/manifest.json",
            mapping_path=EVAL / "a3-action-mapping.json",
            shadow_path=EVAL / "b1-shadow-replay-2026-11-14.json",
            output_path=self.output,
        )

    def test_no_test_or_labels_never_becomes_zero_percent_rate(self):
        result = measure(**self.kwargs)
        self.assertEqual(result["status"], "NOT_MEASURABLE")
        self.assertEqual(result["independent_test_records"], 0)
        self.assertEqual(result["aihs_moodle_proposals"], 0)
        self.assertIsNone(result["model_metrics"]["top1_all"])
        self.assertIsNone(result["safety_gate_metrics"]["false_block_rate"])
        self.assertIsNone(result["safety_gate_metrics"]["unsafe_block_rate"])
        self.assertEqual(result["safety_gate_metrics"]["safe_labeled"], 0)
        self.assertEqual(result["descriptive_only"]["historical_non_aihs_audit"]["gemini_shadow"]["gate_decisions"], {"DENY": 2})
        self.assertEqual(result["descriptive_only"]["synthetic_fixture_gate_decisions"], {"ALLOW": 1, "NOT_EVALUATED": 2})

    def test_audit_checksum_mismatch_fails_before_reporting(self):
        changed = Path(self.temp.name) / "changed-audit.jsonl"
        changed.write_bytes(self.kwargs["audit_path"].read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            measure(**{**self.kwargs, "audit_path": changed})
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
