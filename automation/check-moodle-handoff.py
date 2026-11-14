#!/usr/bin/env python3
"""Read-only portability check; never prints credentials or private paths."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check(root: Path, profile: str) -> dict:
    findings = []
    local_private_path = None

    def record(name: str, status: str, detail: str):
        findings.append({"name": name, "status": status, "detail": detail})

    tfvars = root / "terraform/deployment.tfvars"
    if not tfvars.is_file():
        record("operator_tfvars", "BLOCKED", "Create deployment.tfvars from the example on this workstation.")
    else:
        data = tfvars.read_text()
        for field in ("public_key_path", "private_key_path"):
            match = re.search(rf'^\s*{field}\s*=\s*"([^"]+)"', data, re.MULTILINE)
            if not match or not Path(match.group(1)).expanduser().is_file():
                record(field, "BLOCKED", "Configure an existing local key file; do not copy a private key through Git.")
            else:
                if field == "private_key_path":
                    local_private_path = Path(match.group(1)).expanduser().resolve()
                record(field, "OK", "Local key file exists; ownership and SSH access still require verification.")
        if "REPLACE_WITH_YOUR_PUBLIC_IP" in data:
            record("admin_cidr", "BLOCKED", "Set this workstation's public /32 in deployment.tfvars.")
        else:
            record("admin_cidr", "CHECK", "Confirm the configured /32 matches this workstation before planning.")

    state_path = root / "terraform/terraform.tfstate"
    if not state_path.is_file():
        record("terraform_state", "BLOCKED", "No local state. Locate the authoritative state before import, plan or apply.")
        state_has_resources = False
    else:
        try:
            state = json.loads(state_path.read_text())
            state_has_resources = bool(state.get("resources")) and bool(state.get("outputs"))
            record("terraform_state", "OK" if state_has_resources else "BLOCKED",
                   "Local state has resources and outputs; compare with AWS." if state_has_resources else
                   "Local state is empty or lacks outputs; generated host files may be stale. Compare with AWS.")
        except (OSError, ValueError):
            state_has_resources = False
            record("terraform_state", "BLOCKED", "Local state is unreadable; locate authoritative state.")

    for relative in ("terraform/.artifacts/moodle-ssh.config", "terraform/.artifacts/moodle-inventory.yml"):
        path = root / relative
        if not path.is_file():
            record(relative, "BLOCKED", "Regenerate from verified Terraform outputs on the receiving workstation.")
            continue
        content = path.read_text()
        if not state_has_resources:
            record(relative, "STALE", "Present but no matching local state; do not use for SSH or Ansible.")
        elif relative.endswith("moodle-inventory.yml") and (str(root) not in content or
                                                              local_private_path is None or
                                                              str(local_private_path) not in content):
            record(relative, "STALE", "Inventory has a different repository or key path; regenerate locally.")
        elif relative.endswith("moodle-ssh.config") and (local_private_path is None or
                                                           str(local_private_path) not in content):
            record(relative, "STALE", "SSH config has a different key path; regenerate locally.")
        else:
            record(relative, "CHECK", "Generated file exists; confirm host keys, key path and AWS resources before use.")

    for relative in ("release/.env", "terraform/.artifacts/moodle-agent-image",
                     "terraform/.artifacts/safe-executor/agent-hmac.key",
                     "terraform/.artifacts/safe-executor/safe-action-api-token",
                     "terraform/.artifacts/safe-executor/approval-ed25519.pub",
                     "terraform/.artifacts/safe-executor/verifier-ed25519.pub"):
        record(relative, "OK" if (root / relative).is_file() else "BLOCKED",
               "Local-only file exists; validate its configuration and do not commit it." if (root / relative).is_file()
               else "Missing local-only runtime artifact; provision it on the receiving workstation.")

    mapping = root / "evaluation/aihs/a3-action-mapping.json"
    if mapping.is_file():
        actions = json.loads(mapping.read_text()).get("actions", [])
        live_count = sum(a.get("status") == "live_reviewed" for a in actions)
        record("aihs_live_mapping", "OK" if live_count else "BLOCKED",
               f"{live_count} reviewed live action mappings; AIHS live arm requires at least one reviewed mapping.")
    else:
        record("aihs_live_mapping", "BLOCKED", "AIHS mapping file is absent.")

    source_manifest = root / "evaluation/aihs/source-manifest.json"
    if source_manifest.is_file():
        source = json.loads(source_manifest.read_text())["source"]["archive"]
        archive = root / source["path"]
        if not archive.is_file():
            record("aihs_source_archive", "BLOCKED", "Download the pinned public prototype archive and verify its checksum.")
        elif hashlib.sha256(archive.read_bytes()).hexdigest() != source["sha256"]:
            record("aihs_source_archive", "BLOCKED", "Prototype archive checksum mismatch; do not run it.")
        else:
            record("aihs_source_archive", "OK", "Pinned public prototype archive checksum matches.")
    else:
        record("aihs_source_archive", "BLOCKED", "AIHS source manifest is absent.")

    for name in ("MOODLE_OPERATOR_SIGNING_KEY_FILE", "MOODLE_VERIFIER_SIGNING_KEY_FILE"):
        value = os.environ.get(name, "")
        record(name, "OK" if value and Path(value).expanduser().is_file() else "BLOCKED",
               "Signing key file is present locally; match its public key before any drill." if value and Path(value).expanduser().is_file()
               else "Set this variable to the local private key file; never commit the key.")

    try:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                               check=True, timeout=5).stdout.splitlines()
        record("git_handoff", "BLOCKED" if dirty else "OK",
               f"{len(dirty)} uncommitted or untracked entries; clone/pull will not transfer them." if dirty
               else "Working tree clean; confirm the teammate has the same commit SHA.")
    except (OSError, subprocess.SubprocessError):
        record("git_handoff", "BLOCKED", "Could not inspect Git working tree.")

    try:
        profiles = subprocess.run(["aws", "configure", "list-profiles"], capture_output=True, text=True,
                                  check=True, timeout=5).stdout.splitlines()
        record("aws_profile", "CHECK" if profile in profiles else "BLOCKED",
               "Profile exists; identity, MFA, permissions and AWS resource state remain unverified." if profile in profiles
               else "Configure the target-account profile locally; never send access keys through Git/chat.")
    except (OSError, subprocess.SubprocessError):
        record("aws_profile", "BLOCKED", "AWS CLI missing or profile list unavailable.")

    return {"schema_version": 1, "mode": "read_only_local_handoff", "aws_live_state_verified": False,
            "ready_for_live_drill": False, "findings": findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE", "target-account"))
    args = parser.parse_args()
    result = check(args.root.resolve(), args.aws_profile)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
