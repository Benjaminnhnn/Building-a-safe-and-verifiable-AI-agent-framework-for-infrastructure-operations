import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def workflow_root() -> Path:
    default_root = Path(__file__).resolve().parents[2] / "config" / "runbook_workflow"
    return Path(os.getenv("RUNBOOK_WORKFLOW_DIR", str(default_root)))


def knowledge_base_root() -> Path:
    default_root = Path(__file__).resolve().parents[2] / "config" / "knowledge_base"
    return Path(os.getenv("KNOWLEDGE_BASE_DIR", str(default_root)))


def _tool_dir(tool_name: str) -> Path:
    return workflow_root() / "tool_registry" / slugify(tool_name)


def _draft_dir() -> Path:
    return workflow_root() / "runbook_drafts"


def _draft_path(draft_id: str) -> Path:
    if not draft_id or not re.match(r"^[a-zA-Z0-9_\-]+$", draft_id):
        raise ValueError(f"Invalid draft_id format: {draft_id}")
    root = _draft_dir().resolve()
    target = (root / f"{draft_id}.json").resolve()
    if not target.is_relative_to(root):
        raise ValueError("Path traversal attempt detected")
    return target


def _audit_path() -> Path:
    return workflow_root() / "audit" / "runbook_workflow.jsonl"


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "item"


def now_iso() -> str:
    return datetime.now(VN_TZ).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_audit(event: str, actor: str, payload: dict[str, Any]) -> None:
    audit = {
        "event": event,
        "actor": actor,
        "payload": payload,
        "timestamp": now_iso(),
    }
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n")


def validate_tool_metadata(metadata: dict[str, Any]) -> None:
    required = ("name", "version", "description", "risk_level")
    missing = [key for key in required if not str(metadata.get(key, "")).strip()]
    if missing:
        raise ValueError(f"Missing required tool metadata fields: {', '.join(missing)}")

    risk_level = metadata["risk_level"]
    if risk_level not in {"read_only", "remediation", "destructive"}:
        raise ValueError("risk_level must be one of: read_only, remediation, destructive")

    for key in ("related_services", "runbook_tags", "inputs", "outputs"):
        value = metadata.get(key, [])
        if value is not None and not isinstance(value, list):
            raise ValueError(f"{key} must be a list")


def save_tool_revision(metadata: dict[str, Any], actor: str = "admin") -> dict[str, Any]:
    validate_tool_metadata(metadata)

    tool_name = metadata["name"]
    revision_id = metadata.get("revision_id") or f"rev-{datetime.now(VN_TZ).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    previous = get_current_tool_revision(tool_name)
    revision = {
        **metadata,
        "revision_id": revision_id,
        "status": "registered",
        "actor": actor,
        "created_at": now_iso(),
        "previous_revision_id": previous.get("revision_id") if previous else None,
    }

    root = _tool_dir(tool_name)
    _write_json(root / "revisions" / f"{revision_id}.json", revision)
    _write_json(root / "current.json", {"tool_name": tool_name, "revision_id": revision_id, "updated_at": now_iso()})
    append_audit("tool_revision_registered", actor, {"tool_name": tool_name, "revision_id": revision_id})
    return revision


def get_tool_revision(tool_name: str, revision_id: str) -> dict[str, Any]:
    return _read_json(_tool_dir(tool_name) / "revisions" / f"{revision_id}.json")


def get_current_tool_revision(tool_name: str) -> dict[str, Any] | None:
    current_path = _tool_dir(tool_name) / "current.json"
    if not current_path.exists():
        return None
    current = _read_json(current_path)
    return get_tool_revision(tool_name, current["revision_id"])


def list_tool_revisions() -> list[dict[str, Any]]:
    registry_root = workflow_root() / "tool_registry"
    if not registry_root.exists():
        return []
    revisions = []
    for current_path in registry_root.glob("*/current.json"):
        current = _read_json(current_path)
        revisions.append(get_tool_revision(current["tool_name"], current["revision_id"]))
    return sorted(revisions, key=lambda item: item.get("name", ""))


def find_related_runbooks(metadata: dict[str, Any]) -> list[dict[str, str]]:
    kb_root = knowledge_base_root()
    if not kb_root.exists():
        return []

    keywords = set()
    for key in ("name", "description"):
        keywords.update(re.findall(r"\w+", str(metadata.get(key, "")).lower()))
    for key in ("related_services", "runbook_tags"):
        for value in metadata.get(key, []) or []:
            keywords.update(re.findall(r"\w+", str(value).lower()))

    candidates = []
    for path in kb_root.rglob("*.md"):
        if "published" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        haystack = f"{path.name}\n{text}".lower()
        score = sum(1 for keyword in keywords if keyword and keyword in haystack)
        if score > 0:
            candidates.append({"path": str(path), "name": path.stem, "score": str(score)})

    candidates.sort(key=lambda item: int(item["score"]), reverse=True)
    return candidates[:3]


def classify_tool_change(revision: dict[str, Any]) -> str:
    if not revision.get("previous_revision_id"):
        return "new_tool"
    if revision.get("risk_level") in {"remediation", "destructive"}:
        return "risk_level_or_behavior_change"
    return "metadata_or_interface_change"


def render_runbook_draft(revision: dict[str, Any], related_runbooks: list[dict[str, str]]) -> tuple[str, str]:
    tool_name = revision["name"]
    version = revision["version"]
    services = ", ".join(revision.get("related_services") or ["general"])
    tags = ", ".join(revision.get("runbook_tags") or [])
    related = "\n".join(f"- {item['name']} ({item['path']})" for item in related_runbooks) or "- None found"
    risk_note = {
        "read_only": "Tool is read-only and can be used for diagnostics.",
        "remediation": "Tool can change system state. Admin approval is required before execution.",
        "destructive": "Tool is destructive. Do not execute automatically.",
    }[revision["risk_level"]]
    slug = f"{slugify((revision.get('runbook_tags') or revision.get('related_services') or [tool_name])[0])}-tooling"
    content = f"""# Runbook Draft: {tool_name} v{version}

## Purpose
Use this runbook when incidents involve: {services}.

## Tool
- Name: `{tool_name}`
- Version: `{version}`
- Risk level: `{revision['risk_level']}`
- Description: {revision['description']}
- Tags: {tags or 'none'}

## Related Existing Runbooks
{related}

## Safety Rules
- {risk_note}
- Set a timeout before calling the tool.
- Record the tool output in the incident context.
- Do not overwrite existing runbooks; publish this draft as a new version only after admin approval.

## Diagnostic Flow
1. Confirm the alert is still firing.
2. Run `{tool_name}` with the documented inputs.
3. Compare the output with the alert threshold.
4. If the tool is read-only, use the result to guide remediation.
5. If the tool is remediation or destructive, ask admin approval before execution.

## Stop Conditions
- Stop if the tool times out.
- Stop if required inputs are missing.
- Stop if output is ambiguous or contradicts Prometheus metrics.

## Rollback
- Keep the previous published runbook version active until admin approval.
- If the published version is wrong, switch `current.json` back to the previous version.
"""
    return slug, content


def create_runbook_draft(tool_name: str, revision_id: str, actor: str = "agent") -> dict[str, Any]:
    revision = get_tool_revision(tool_name, revision_id)
    related_runbooks = find_related_runbooks(revision)
    runbook_slug, content = render_runbook_draft(revision, related_runbooks)
    draft_id = f"draft-{datetime.now(VN_TZ).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    draft = {
        "draft_id": draft_id,
        "status": "pending_approval",
        "tool_name": tool_name,
        "tool_revision_id": revision_id,
        "change_type": classify_tool_change(revision),
        "runbook_slug": runbook_slug,
        "related_runbooks": related_runbooks,
        "content": content,
        "created_at": now_iso(),
        "created_by": actor,
    }
    _write_json(_draft_path(draft_id), draft)
    append_audit("runbook_draft_created", actor, {"draft_id": draft_id, "tool_name": tool_name, "revision_id": revision_id})
    return draft


def get_runbook_draft(draft_id: str) -> dict[str, Any]:
    return _read_json(_draft_path(draft_id))


def list_runbook_drafts(status: str | None = None) -> list[dict[str, Any]]:
    root = _draft_dir()
    if not root.exists():
        return []
    drafts = [_read_json(path) for path in root.glob("*.json")]
    if status:
        drafts = [draft for draft in drafts if draft.get("status") == status]
    return sorted(drafts, key=lambda item: item.get("created_at", ""), reverse=True)


def update_draft_status(draft_id: str, status: str, actor: str, reason: str | None = None) -> dict[str, Any]:
    draft = get_runbook_draft(draft_id)
    draft["status"] = status
    draft["updated_at"] = now_iso()
    draft["updated_by"] = actor
    if reason:
        draft["reason"] = reason
    _write_json(_draft_path(draft_id), draft)
    append_audit("runbook_draft_status_changed", actor, {"draft_id": draft_id, "status": status, "reason": reason})
    return draft


def publish_runbook_draft(draft_id: str, actor: str = "admin") -> dict[str, Any]:
    draft = get_runbook_draft(draft_id)
    if draft.get("status") not in {"pending_approval", "approved"}:
        raise ValueError(f"Draft {draft_id} cannot be published from status {draft.get('status')}")

    slug = draft["runbook_slug"]
    published_root = knowledge_base_root() / "published" / slug
    published_root.mkdir(parents=True, exist_ok=True)
    version = f"v{datetime.now(VN_TZ).strftime('%Y%m%d%H%M%S')}"
    runbook_path = published_root / f"{version}.md"
    current_path = published_root / "current.json"
    previous = _read_json(current_path) if current_path.exists() else None

    runbook_path.write_text(draft["content"], encoding="utf-8")
    _write_json(
        current_path,
        {
            "runbook_slug": slug,
            "current_version": version,
            "current_path": str(runbook_path),
            "draft_id": draft_id,
            "previous_version": previous.get("current_version") if previous else None,
            "published_by": actor,
            "published_at": now_iso(),
        },
    )

    draft["status"] = "published"
    draft["published_version"] = version
    draft["published_path"] = str(runbook_path)
    draft["updated_at"] = now_iso()
    draft["updated_by"] = actor
    _write_json(_draft_dir() / f"{draft_id}.json", draft)
    append_audit("runbook_published", actor, {"draft_id": draft_id, "runbook_slug": slug, "version": version})
    return draft
