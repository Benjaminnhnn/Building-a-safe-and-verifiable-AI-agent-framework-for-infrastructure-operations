import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import main
from core.coordination.tool_auto_discovery import detect_ci_toolchain, verify_github_signature


client = TestClient(main.app)


def _workflow_patch() -> str:
    return """
@@ -10,6 +10,12 @@ jobs:
       - run: pytest
+      - run: trivy image ghcr.io/example/app:${{ github.sha }}
+      - run: terraform validate
+      - run: ansible-lint ansible/playbooks/site.yml
"""


def test_detect_ci_toolchain_from_github_workflow_patch() -> None:
    detection = detect_ci_toolchain(
        [
            {
                "filename": ".github/workflows/ci.yml",
                "patch": _workflow_patch(),
            }
        ]
    )

    assert detection == {
        "discovered_tools": ["trivy", "terraform_validate", "ansible_lint"],
        "changed_files": [".github/workflows/ci.yml"],
        "matched_commands": [
            ".github/workflows/ci.yml:ansible_lint:- run: ansible-lint ansible/playbooks/site.yml",
            ".github/workflows/ci.yml:terraform_validate:- run: terraform validate",
            ".github/workflows/ci.yml:trivy:- run: trivy image ghcr.io/example/app:${{ github.sha }}",
        ],
        "discovery_signature": detection["discovery_signature"],
    }


def test_detect_ci_toolchain_ignores_old_patterns_in_diff_context() -> None:
    detection = detect_ci_toolchain(
        [
            {
                "filename": ".github/workflows/ci.yml",
                "patch": """
@@ -10,8 +10,9 @@ jobs:
       - run: trivy image ghcr.io/example/app:${{ github.sha }}
       - run: terraform validate
       - run: ansible-lint ansible/playbooks/site.yml
+      - run: pytest tests
""",
            }
        ]
    )

    assert detection is None


def test_verify_github_signature() -> None:
    body = b'{"zen":"keep it logically crisp"}'
    secret = "demo-secret"
    import hmac
    import hashlib

    signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(secret, body, signature) is True
    assert verify_github_signature(secret, body, "sha256=bad") is False
    assert verify_github_signature(None, body, signature) is False


def test_github_webhook_auto_registers_ci_quality_gate_without_publishing() -> None:
    with TemporaryDirectory() as tmp:
        workflow_dir = os.path.join(tmp, "workflow")
        kb_dir = os.path.join(tmp, "knowledge_base")
        os.makedirs(kb_dir)

        payload = {
            "after": "abc1234567890",
            "repository": {"full_name": "example/aws-hybrid"},
            "sender": {"login": "demo-admin"},
        }

        with (
            patch.dict(os.environ, {"RUNBOOK_WORKFLOW_DIR": workflow_dir, "KNOWLEDGE_BASE_DIR": kb_dir, "GITHUB_WEBHOOK_AUTH_OPTIONAL": "true"}),
            patch.object(main, "GITHUB_WEBHOOK_SECRET", None),
            patch.object(
                main,
                "changed_files_from_github_event",
                return_value=[{"filename": ".github/workflows/ci.yml", "patch": _workflow_patch()}],
            ),
            patch.object(main.review_tool_change_task, "delay") as delay,
        ):
            response = client.post("/github/webhook", headers={"X-GitHub-Event": "push"}, json=payload)

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "registered"
        assert body["tool_name"] == "ci_security_iac_quality_gate"
        assert body["review_status"] == "queued"
        assert body["discovered_tools"] == ["trivy", "terraform_validate", "ansible_lint"]
        delay.assert_called_once()
        assert not os.path.exists(os.path.join(kb_dir, "published"))


def test_github_webhook_dedupes_same_toolchain_signature_on_new_sha() -> None:
    with TemporaryDirectory() as tmp:
        workflow_dir = os.path.join(tmp, "workflow")
        kb_dir = os.path.join(tmp, "knowledge_base")
        os.makedirs(kb_dir)

        payload = {
            "after": "abc1234567890",
            "repository": {"full_name": "example/aws-hybrid"},
            "sender": {"login": "demo-admin"},
        }
        files = [{"filename": ".github/workflows/ci.yml", "patch": _workflow_patch()}]

        with (
            patch.dict(os.environ, {"RUNBOOK_WORKFLOW_DIR": workflow_dir, "KNOWLEDGE_BASE_DIR": kb_dir, "GITHUB_WEBHOOK_AUTH_OPTIONAL": "true"}),
            patch.object(main, "GITHUB_WEBHOOK_SECRET", None),
            patch.object(main, "changed_files_from_github_event", return_value=files),
            patch.object(main.review_tool_change_task, "delay") as delay,
        ):
            first = client.post("/github/webhook", headers={"X-GitHub-Event": "push"}, json=payload)
            second_payload = {**payload, "after": "def9876543210"}
            second = client.post("/github/webhook", headers={"X-GitHub-Event": "push"}, json=second_payload)

        assert first.status_code == 202
        assert first.json()["status"] == "registered"
        assert second.status_code == 202
        assert second.json()["status"] == "unchanged"
        assert second.json()["review_status"] == "not_queued"
        delay.assert_called_once()
