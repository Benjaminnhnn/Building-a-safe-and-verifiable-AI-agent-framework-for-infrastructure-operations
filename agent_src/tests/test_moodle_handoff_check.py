import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "automation/check-moodle-handoff.py"
SPEC = importlib.util.spec_from_file_location("check_moodle_handoff", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MoodleHandoffCheckTests(unittest.TestCase):
    def test_empty_state_marks_generated_host_files_stale_without_leaking_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "terraform/.artifacts").mkdir(parents=True)
            (root / "terraform/terraform.tfstate").write_text(json.dumps({"resources": [], "outputs": {}}))
            (root / "terraform/.artifacts/moodle-ssh.config").write_text("IdentityFile /secret/private-key\n")
            (root / "terraform/.artifacts/moodle-inventory.yml").write_text("ansible_ssh_private_key_file: /secret/private-key\n")
            result = MODULE.check(root, "target-account")
            findings = {item["name"]: item["status"] for item in result["findings"]}
            self.assertEqual(findings["terraform_state"], "BLOCKED")
            self.assertEqual(findings["terraform/.artifacts/moodle-ssh.config"], "STALE")
            self.assertEqual(findings["terraform/.artifacts/moodle-inventory.yml"], "STALE")
            self.assertNotIn("/secret/private-key", json.dumps(result))
            self.assertFalse(result["ready_for_live_drill"])

    def test_staging_guard_accepts_custom_hostname_and_rejects_wrong_alb(self):
        guard = SCRIPT.parent / "lib/moodle-fault-common.sh"
        command = f'source "{guard}"; validate_staging_application "$1"'
        application = {
            "alb_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:829162041762:loadbalancer/app/moodle-staging-alb/abcdef0123456789",
            "target_group_arn": "arn:aws:elasticloadbalancing:ap-southeast-1:829162041762:targetgroup/moodle-staging-tg/abcdef0123456789",
            "url": "https://moodle.example.org",
            "legacy_demo_enabled": False,
            "nodes": {"a": {"id": "i-abcdef0123"}, "b": {"id": "i-abcdef0124"}},
        }
        good = subprocess.run(["bash", "-c", command, "guard", json.dumps(application)], capture_output=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        application["alb_arn"] = application["alb_arn"].replace("moodle-staging-alb", "production-alb")
        bad = subprocess.run(["bash", "-c", command, "guard", json.dumps(application)], capture_output=True)
        self.assertEqual(bad.returncode, 77)

    def test_nonempty_state_still_rejects_other_workstation_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "terraform/.artifacts").mkdir(parents=True)
            key = root / "local-key"
            key.write_text("test fixture")
            (root / "terraform/deployment.tfvars").write_text(
                f'public_key_path = "{key}"\nprivate_key_path = "{key}"\nmy_ip_cidr = "192.0.2.1/32"\n')
            (root / "terraform/terraform.tfstate").write_text(
                json.dumps({"resources": [{"type": "fixture"}], "outputs": {"fixture": {"value": 1}}}))
            (root / "terraform/.artifacts/moodle-ssh.config").write_text('IdentityFile "/old/key"\n')
            (root / "terraform/.artifacts/moodle-inventory.yml").write_text(
                'ansible_ssh_common_args: "-F /old/repo/terraform/.artifacts/moodle-ssh.config"\n')
            findings = {item["name"]: item["status"] for item in MODULE.check(root, "target-account")["findings"]}
            self.assertEqual(findings["terraform_state"], "OK")
            self.assertEqual(findings["terraform/.artifacts/moodle-ssh.config"], "STALE")
            self.assertEqual(findings["terraform/.artifacts/moodle-inventory.yml"], "STALE")


if __name__ == "__main__":
    unittest.main()
