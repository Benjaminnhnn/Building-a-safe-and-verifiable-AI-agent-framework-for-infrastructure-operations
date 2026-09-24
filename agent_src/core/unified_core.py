"""Unified incident contracts, append-only evidence, and deterministic agent stages.

These components are deliberately provider-neutral and safe for fixture replay. They
do not execute infrastructure actions; executable actions remain behind the existing
Moodle safety gate and adapter boundary.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IncidentState(str, Enum):
    OPEN = "OPEN"
    OBSERVED = "OBSERVED"
    DIAGNOSED = "DIAGNOSED"
    PLANNED = "PLANNED"
    GATED = "GATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    VERIFIED_DRY_RUN = "VERIFIED_DRY_RUN"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


TRANSITIONS: dict[IncidentState, set[IncidentState]] = {
    IncidentState.OPEN: {IncidentState.OBSERVED, IncidentState.ESCALATED, IncidentState.FAILED},
    IncidentState.OBSERVED: {IncidentState.DIAGNOSED, IncidentState.ESCALATED, IncidentState.FAILED},
    IncidentState.DIAGNOSED: {IncidentState.PLANNED, IncidentState.ESCALATED, IncidentState.FAILED},
    IncidentState.PLANNED: {IncidentState.GATED, IncidentState.ESCALATED, IncidentState.FAILED},
    IncidentState.GATED: {IncidentState.AWAITING_APPROVAL, IncidentState.EXECUTING, IncidentState.VERIFIED_DRY_RUN, IncidentState.ESCALATED, IncidentState.FAILED},
    IncidentState.AWAITING_APPROVAL: {IncidentState.EXECUTING, IncidentState.ESCALATED, IncidentState.FAILED},
    IncidentState.EXECUTING: {IncidentState.VERIFYING, IncidentState.FAILED, IncidentState.ESCALATED},
    IncidentState.VERIFYING: {IncidentState.RESOLVED, IncidentState.FAILED, IncidentState.ESCALATED},
    IncidentState.VERIFIED_DRY_RUN: set(),
    IncidentState.RESOLVED: set(),
    IncidentState.ESCALATED: set(),
    IncidentState.FAILED: set(),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Resource(Contract):
    resource_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    service: str = Field(min_length=1)
    capabilities: set[str] = Field(default_factory=set)
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)


class Evidence(Contract):
    evidence_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    observed_at: datetime
    resource_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class Incident(Contract):
    incident_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    resource_ids: list[str] = Field(min_length=1)
    state: IncidentState = IncidentState.OPEN
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TypedAction(Contract):
    action_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    target_resource_id: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    lifecycle: Literal["PROPOSED", "GATED", "APPROVED", "RUNNING", "SUCCEEDED", "FAILED", "ROLLED_BACK"] = "PROPOSED"
    rollback_action: str | None = None


ACTION_TRANSITIONS: dict[str, set[str]] = {
    "PROPOSED": {"GATED", "FAILED"},
    "GATED": {"APPROVED", "FAILED"},
    "APPROVED": {"RUNNING", "FAILED"},
    "RUNNING": {"SUCCEEDED", "FAILED", "ROLLED_BACK"},
    "SUCCEEDED": set(),
    "FAILED": {"ROLLED_BACK"},
    "ROLLED_BACK": set(),
}


def transition_action(action: TypedAction, lifecycle: str) -> TypedAction:
    if lifecycle not in ACTION_TRANSITIONS.get(action.lifecycle, set()):
        raise ValueError(f"invalid action lifecycle transition: {action.lifecycle} -> {lifecycle}")
    return action.model_copy(update={"lifecycle": lifecycle})


class AgentMessage(Contract):
    message_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    stage: Literal["observer", "diagnosis", "planner", "gate", "execution", "verification"]
    status: Literal["ok", "retry", "escalate", "failed"]
    input_refs: list[str] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def transition(incident: Incident, new_state: IncidentState, *, actor: str) -> Incident:
    if actor == "verifier" and new_state != IncidentState.RESOLVED:
        raise ValueError("verifier authority is reserved for the RESOLVED transition")
    if new_state == IncidentState.RESOLVED and actor != "verifier":
        raise ValueError("only the independent verifier may set RESOLVED")
    if new_state not in TRANSITIONS[incident.state]:
        raise ValueError(f"invalid incident transition: {incident.state.value} -> {new_state.value}")
    return incident.model_copy(update={"state": new_state})


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


class SQLiteEvidenceStore:
    """Append-only SQLite evidence ledger with indexed queries and dependency edges."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                    source TEXT NOT NULL, observed_at TEXT NOT NULL, resource_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload_json TEXT NOT NULL, sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS evidence_incident_time ON evidence(incident_id, observed_at);
                CREATE INDEX IF NOT EXISTS evidence_resource_time ON evidence(resource_id, observed_at);
                CREATE TABLE IF NOT EXISTS dependency_edges (
                    source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
                    PRIMARY KEY(source_id, target_id, relation)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    incident_id TEXT NOT NULL, stage TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL, attempt INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(incident_id, stage)
                );
                CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence
                    BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence
                    BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def append(self, *, incident_id: str, source: str, resource_id: str, kind: str, payload: dict[str, Any], observed_at: datetime | None = None) -> Evidence:
        timestamp = observed_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        body = {"incident_id": incident_id, "source": source, "observed_at": timestamp.isoformat(), "resource_id": resource_id, "kind": kind, "payload": payload}
        digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
        record = Evidence(evidence_id=f"ev-{digest[:24]}", sha256=digest, **body)
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (record.evidence_id, incident_id, source, timestamp.isoformat(), resource_id, kind, _canonical(payload), digest))
        return record

    def query(self, *, incident_id: str | None = None, resource_id: str | None = None, kind: str | None = None, limit: int = 100) -> list[Evidence]:
        clauses, params = [], []
        for column, value in (("incident_id", incident_id), ("resource_id", resource_id), ("kind", kind)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        sql = "SELECT * FROM evidence" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY observed_at, evidence_id LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [Evidence(evidence_id=r["evidence_id"], incident_id=r["incident_id"], source=r["source"], observed_at=r["observed_at"], resource_id=r["resource_id"], kind=r["kind"], payload=json.loads(r["payload_json"]), sha256=r["sha256"]) for r in rows]

    def add_dependency(self, source_id: str, target_id: str, relation: str = "depends_on") -> None:
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO dependency_edges VALUES (?, ?, ?)", (source_id, target_id, relation))

    def dependencies(self, resource_id: str) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute("SELECT source_id, target_id, relation FROM dependency_edges WHERE source_id = ? OR target_id = ? ORDER BY source_id, target_id", (resource_id, resource_id)).fetchall()
        return [dict(row) for row in rows]

    def record_checkpoint(self, *, incident_id: str, stage: str, idempotency_key: str, status: str, attempt: int, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(incident_id, stage) DO UPDATE SET status=excluded.status, attempt=excluded.attempt, payload_json=excluded.payload_json, updated_at=excluded.updated_at", (incident_id, stage, idempotency_key, status, attempt, _canonical(payload), datetime.now(timezone.utc).isoformat()))

    def get_checkpoint(self, incident_id: str, stage: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM checkpoints WHERE incident_id=? AND stage=?", (incident_id, stage)).fetchone()
        if not row:
            return None
        return {**dict(row), "payload": json.loads(row["payload_json"])}

    @staticmethod
    def rag_provenance(*, source: str, observed_at: datetime, content: str, resource_id: str, source_type: str) -> dict[str, str]:
        if observed_at.tzinfo is None:
            raise ValueError("RAG source time must include timezone")
        return {"source": source, "source_type": source_type, "observed_at": observed_at.isoformat(), "resource_id": resource_id, "sha256": hashlib.sha256(content.encode()).hexdigest()}


class ObserverAgent:
    def __init__(self, store: SQLiteEvidenceStore, window_seconds: int = 300):
        self.store, self.window_seconds = store, window_seconds

    def observe(self, event: dict[str, Any], *, resource_id: str) -> dict[str, Any]:
        fingerprint = str(event.get("fingerprint") or event.get("event_id") or "")
        if not fingerprint:
            raise ValueError("event requires fingerprint or event_id")
        known = self.store.query(resource_id=resource_id, kind="alert_observation", limit=1000)
        duplicates = [e for e in known if e.payload.get("fingerprint") == fingerprint]
        now = datetime.now(timezone.utc)
        within_window = [e for e in duplicates if abs((now - e.observed_at).total_seconds()) <= self.window_seconds]
        if within_window:
            return {"group_id": within_window[0].payload["group_id"], "duplicate": True, "suppressed_count": len(within_window) + 1, "evidence_refs": [e.evidence_id for e in within_window]}
        group_id = "grp-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        evidence = self.store.append(incident_id=str(event.get("incident_id") or group_id), source=str(event.get("source") or "alertmanager"), resource_id=resource_id, kind="alert_observation", payload={"fingerprint": fingerprint, "group_id": group_id, "event_type": event.get("event_type"), "severity": event.get("severity")})
        return {"group_id": group_id, "duplicate": False, "suppressed_count": 0, "evidence_refs": [evidence.evidence_id]}


class DiagnosisAgent:
    def diagnose(self, *, signals: list[str], hypotheses: list[dict[str, Any]], evidence_refs: list[str]) -> dict[str, Any]:
        if not evidence_refs:
            return {"status": "escalate", "reason": "no evidence references", "hypotheses": []}
        signal_set = {s.casefold() for s in signals}
        ranked = []
        for hypothesis in hypotheses:
            matched = [s for s in hypothesis.get("signals", []) if str(s).casefold() in signal_set]
            score = len(matched) / max(1, len(hypothesis.get("signals", [])))
            ranked.append({"root_cause": hypothesis.get("root_cause"), "confidence": round(score, 4), "matched_signals": matched, "evidence_refs": list(dict.fromkeys(evidence_refs))})
        ranked.sort(key=lambda h: (-h["confidence"], str(h["root_cause"])))
        return {"status": "ok" if ranked and ranked[0]["confidence"] > 0 else "escalate", "top1": ranked[0] if ranked else None, "hypotheses": ranked}


class PlannerAgent:
    def plan(self, *, incident_id: str, environment: str, resource_id: str, allowed_actions: list[dict[str, str]], evidence_refs: list[str]) -> list[TypedAction]:
        if not evidence_refs:
            raise ValueError("planner requires evidence-backed diagnosis")
        actions = []
        for item in allowed_actions:
            semantic = item["action"]
            action_id = "act-" + hashlib.sha256(f"{incident_id}:{semantic}:{item['target']}".encode()).hexdigest()[:20]
            actions.append(TypedAction(action_id=action_id, incident_id=incident_id, action=semantic, target_resource_id=item["target"], environment=environment, evidence_ids=list(dict.fromkeys(evidence_refs)), idempotency_key=action_id))
        return actions


class SequentialOrchestrator:
    """Stage runner with persistent checkpoints, bounded retry, and escalation."""

    STAGES = ("observer", "diagnosis", "planner", "gate", "execution", "verification")

    def __init__(self, store: SQLiteEvidenceStore, *, max_attempts: int = 2):
        self.store, self.max_attempts = store, max(1, min(max_attempts, 5))

    def run(self, *, incident_id: str, handlers: dict[str, Any], context: dict[str, Any]) -> list[AgentMessage]:
        messages: list[AgentMessage] = []
        for stage in self.STAGES:
            handler = handlers.get(stage)
            if handler is None:
                message = AgentMessage(message_id=f"{incident_id}:{stage}:missing", incident_id=incident_id, stage=stage, status="escalate", output={"reason": "stage handler unavailable"})
                messages.append(message)
                break
            prior = self.store.get_checkpoint(incident_id, stage)
            if prior and prior["status"] == "ok":
                message = AgentMessage.model_validate(prior["payload"])
                messages.append(message)
                context[stage] = message.output
                continue
            result = None
            for attempt in range(1, self.max_attempts + 1):
                try:
                    result = handler(context)
                    status = "ok"
                    break
                except Exception as exc:  # fail closed; details are summarized, not persisted
                    status = "retry" if attempt < self.max_attempts else "escalate"
                    result = {"reason": type(exc).__name__}
                    self.store.record_checkpoint(incident_id=incident_id, stage=stage, idempotency_key=f"{incident_id}:{stage}", status=status, attempt=attempt, payload={"error_type": type(exc).__name__})
            message = AgentMessage(message_id=f"{incident_id}:{stage}", incident_id=incident_id, stage=stage, status=status, input_refs=list(context.get("evidence_refs", [])), output=result or {})
            self.store.record_checkpoint(incident_id=incident_id, stage=stage, idempotency_key=f"{incident_id}:{stage}", status=status, attempt=attempt, payload=message.model_dump(mode="json"))
            messages.append(message)
            if status != "ok":
                break
            context[stage] = message.output
        return messages
