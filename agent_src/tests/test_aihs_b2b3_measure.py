import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("b2b3_measure", ROOT / "evaluation/aihs/b2b3_measure.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class B2B3MeasurementTests(unittest.TestCase):
    def test_historical_and_paired_case_keep_distinct_statuses(self):
        result = MODULE.measure()
        self.assertEqual(result["historical_staging"]["observed_resolved_runs"], 8)
        self.assertEqual(result["historical_staging"]["detect_to_resolved_seconds"]["median"], 521.5)
        pair = result["verifier_paired_case"]
        self.assertEqual(pair["health_only_http_status"], 200)
        self.assertEqual(pair["verifier_verdict"], "collateral_damage")
        self.assertEqual(pair["runtime_status_after_collateral"], "verifying")
        self.assertIsNone(pair["population_false_recovery_rate"])
        self.assertEqual(result["new_live_staging"]["new_runs"], 0)
        self.assertIsNone(result["new_live_staging"]["comparative_rto_seconds"])

    def test_corrupted_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = json.loads(MODULE.MANIFEST.read_text())
            manifest_path = root / MODULE.MANIFEST.relative_to(ROOT)
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps(manifest))
            first = manifest["source_files"][0]["path"]
            source = root / first
            source.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / first, source)
            source.write_bytes(source.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                MODULE.measure(root)


if __name__ == "__main__":
    unittest.main()
