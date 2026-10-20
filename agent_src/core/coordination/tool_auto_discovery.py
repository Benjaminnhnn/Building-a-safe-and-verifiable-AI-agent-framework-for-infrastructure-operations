import hmac
import hashlib
import json
import os
import re
from typing import Any

import requests

from core.rag.runbook_registry import get_current_tool_revision, save_tool_revision
from core.tasks import review_tool_change_task


WATCHED_FILE_PATTERNS = (
    re.compile(r"^\.github/workflows/[^/]+\.(ya?ml)$"),
    re.compile(r"^ansible/.+\.(ya?ml)$"),
    re.compile(r"^terraform/.+\.tf$"),
    re.compile(r"(^|/)Dockerfile$"),
    re.compile(r"(^|/)Makefile$"),
)

DISCOVERY_PATTERNS = {
    "trivy": re.compile(r"\btrivy\b", re.IGNORECASE),
    "terraform_validate": re.compile(r"\bterraform\s+validate\b", re.IGNORECASE),
    "ansible_lint": re.compile(r"\bansible-lint\b", re.IGNORECASE),
}

TOOL_DISPLAY_NAMES = {
    "trivy": "Trivy",
    "terraform_validate": "terraform validate",
    "ansible_lint": "ansible-lint",
}


def verify_github_signature(secret: str | None, body: bytes, signature: str | None) -> bool:
    """Validate GitHub's X-Hub-Signature-256 header.
    
    Fail-closed: requires valid secret and signature unless explicitly configured for testing.
    """
    if not secret:
        # shortcut: fail-closed in production unless explicitly opted in for local testing
        return os.getenv("GITHUB_WEBHOOK_AUTH_OPTIONAL", "false").lower() in {"1", "true", "yes"}
    if not signature or not signature.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_watched_file(filename: str) -> bool:
    return any(pattern.search(filename) for pattern in WATCHED_FILE_PATTERNS)


def _github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aws-hybrid-ai-agent-tool-discovery",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request_json(url: str, token: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    response = requests.get(url, headers=_github_headers(token), timeout=10)
    response.raise_for_status()
    return response.json()


def _files_from_push(payload: dict[str, Any], token: str | None) -> list[dict[str, Any]]:
    repository = payload.get("repository") or {}
    full_name = repository.get("full_name")
    before = payload.get("before")
    after = payload.get("after")

    if full_name and before and after:
        compare_url = f"https://api.github.com/repos/{full_name}/compare/{before}...{after}"
        data = _request_json(compare_url, token)
        if isinstance(data, dict):
            return data.get("files") or []

    # Test/fallback payloads may provide files directly.
    return payload.get("files") or []


def _files_from_pull_request(payload: dict[str, Any], token: str | None) -> list[dict[str, Any]]:
    pull_request = payload.get("pull_request") or {}
    files_url = pull_request.get("url")
    if files_url:
        data = _request_json(f"{files_url}/files", token)
        if isinstance(data, list):
            return data

    return payload.get("files") or []


def changed_files_from_github_event(event: str, payload: dict[str, Any], token: str | None = None) -> list[dict[str, Any]]:
    if event == "push":
        return _files_from_push(payload, token)
    if event == "pull_request":
        return _files_from_pull_request(payload, token)
    return payload.get("files") or []


def detect_ci_toolchain(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    watched = []
    discovered = set()
    matched_commands = []

    for item in files:
        filename = str(item.get("filename") or item.get("path") or "")
        if not filename or not is_watched_file(filename):
            continue

        watched.append(filename)
        patch = str(item.get("patch") or "")
        added_lines = []
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:].strip())

        # Fallback for synthetic tests or API payloads that provide full content instead of unified diff.
        if not added_lines and not patch.startswith("@@"):
            added_lines = [line.strip() for line in patch.splitlines() if line.strip()]

        for line in added_lines:
            content = f"{filename}\n{line}"
            for key, pattern in DISCOVERY_PATTERNS.items():
                if pattern.search(content):
                    discovered.add(key)
                    matched_commands.append(f"{filename}:{key}:{line}")

    if not discovered:
        return None

    ordered = [key for key in ("trivy", "terraform_validate", "ansible_lint") if key in discovered]
    signature_payload = {
        "discovered_tools": ordered,
        "changed_files": sorted(set(watched)),
        "matched_commands": sorted(set(matched_commands)),
    }
    return {
        "discovered_tools": ordered,
        "changed_files": sorted(set(watched)),
        "matched_commands": sorted(set(matched_commands)),
        "discovery_signature": hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16],
    }


def _short_sha(value: str | None) -> str:
    if not value:
        return "unknown"
    return value[:12]


def build_ci_quality_gate_metadata(payload: dict[str, Any], detection: dict[str, Any], actor: str) -> dict[str, Any]:
    repository = payload.get("repository") or {}
    sha = payload.get("after") or (payload.get("pull_request") or {}).get("head", {}).get("sha")
    tools = [TOOL_DISPLAY_NAMES[key] for key in detection["discovered_tools"]]
    files = detection["changed_files"]

    return {
        "name": "ci_security_iac_quality_gate",
        "version": f"github-{_short_sha(sha)}",
        "description": (
            "CI quality gate auto-discovered from GitHub workflow changes: "
            f"{', '.join(tools)} in {repository.get('full_name', 'repository')}."
        ),
        "risk_level": "read_only",
        "inputs": ["git_sha", "workflow_run_id", "changed_files"],
        "outputs": [
            "trivy_result",
            "terraform_validate_result",
            "ansible_lint_result",
            "recommendation",
        ],
        "related_services": ["github-actions", "container-image", "terraform", "ansible", "deployment"],
        "runbook_tags": ["ci", "security", "iac", "ansible", "quality-gate"],
        "enabled": True,
        "source": {
            "event": "github_webhook",
            "repository": repository.get("full_name"),
            "sha": sha,
            "changed_files": files,
            "discovered_tools": tools,
            "matched_commands": detection.get("matched_commands", []),
            "discovery_signature": detection.get("discovery_signature"),
        },
    }


def register_discovered_ci_toolchain(payload: dict[str, Any], detection: dict[str, Any], actor: str) -> dict[str, Any]:
    metadata = build_ci_quality_gate_metadata(payload, detection, actor)
    current = get_current_tool_revision(metadata["name"])
    current_signature = (current or {}).get("source", {}).get("discovery_signature")
    next_signature = metadata.get("source", {}).get("discovery_signature")
    if current and current_signature and current_signature == next_signature:
        return {
            "status": "unchanged",
            "tool_name": metadata["name"],
            "revision_id": current["revision_id"],
            "review_status": "not_queued",
        }
    current_source = (current or {}).get("source", {})
    next_source = metadata.get("source", {})
    if current and not current_signature and (
        current_source.get("discovered_tools") == next_source.get("discovered_tools")
        and current_source.get("changed_files") == next_source.get("changed_files")
    ):
        return {
            "status": "unchanged",
            "tool_name": metadata["name"],
            "revision_id": current["revision_id"],
            "review_status": "not_queued",
        }
    if current and current.get("version") == metadata["version"]:
        return {
            "status": "unchanged",
            "tool_name": metadata["name"],
            "revision_id": current["revision_id"],
            "review_status": "not_queued",
        }

    revision = save_tool_revision(metadata, actor=actor)
    review_status = "queued"
    try:
        review_tool_change_task.delay(revision["name"], revision["revision_id"])
    except Exception:
        review_status = "queue_unavailable"

    return {
        "status": "registered",
        "tool_name": revision["name"],
        "revision_id": revision["revision_id"],
        "review_status": review_status,
    }


def github_token() -> str | None:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_DISCOVERY_TOKEN")
    return token.strip() if token else None
