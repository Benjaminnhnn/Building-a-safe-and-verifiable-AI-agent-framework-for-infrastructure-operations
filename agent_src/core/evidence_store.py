"""SQLite source of truth for append-only evidence and orchestration checkpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from core.schema.evidence import Evidence


class EvidenceConflictError(ValueError):
    pass


class EvidenceIntegrityError(ValueError):
    pass


def _model_json_dict(model: Any, *, exclude: set[str] | None = None) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", exclude=exclude or set())
    return json.loads(model.json(exclude=exclude or set()))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence_content_hash(evidence: Evidence) -> str:
    payload = _model_json_dict(evidence, exclude={"content_hash"})
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class SQLiteEvidenceStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                source TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                redacted INTEGER NOT NULL CHECK (redacted IN (0, 1)),
                metadata_json TEXT NOT NULL,
                canonical_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_evidence_incident
                ON evidence(incident_id, collected_at, evidence_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_resource
                ON evidence(resource_id, collected_at, evidence_id);
            CREATE INDEX IF NOT EXISTS idx_evidence_source
                ON evidence(source, collected_at, evidence_id);

            CREATE TRIGGER IF NOT EXISTS evidence_no_update
            BEFORE UPDATE ON evidence
            BEGIN
                SELECT RAISE(ABORT, 'evidence is append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS evidence_no_delete
            BEFORE DELETE ON evidence
            BEGIN
                SELECT RAISE(ABORT, 'evidence is append-only');
            END;

            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, stage)
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def append(self, evidence: Evidence) -> bool:
        expected_hash = evidence_content_hash(evidence)
        if evidence.content_hash != expected_hash:
            raise EvidenceIntegrityError(
                f"content hash mismatch for {evidence.evidence_id}: "
                f"expected {expected_hash}, got {evidence.content_hash}"
            )

        canonical = _canonical_json(_model_json_dict(evidence))
        existing = self._connection.execute(
            "SELECT canonical_json FROM evidence WHERE evidence_id = ?",
            (evidence.evidence_id,),
        ).fetchone()
        if existing is not None:
            if existing["canonical_json"] == canonical:
                return False
            raise EvidenceConflictError(
                f"evidence_id {evidence.evidence_id} already exists with different content"
            )

        payload = _model_json_dict(evidence)
        self._connection.execute(
            """
            INSERT INTO evidence (
                evidence_id, incident_id, resource_id, source, collected_at,
                content_hash, redacted, metadata_json, canonical_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_id,
                evidence.incident_id,
                evidence.resource_id,
                evidence.source,
                payload["collected_at"],
                evidence.content_hash,
                int(evidence.redacted),
                _canonical_json(payload.get("metadata", {})),
                canonical,
            ),
        )
        self._connection.commit()
        return True

    def get_by_id(self, evidence_id: str) -> Evidence | None:
        row = self._connection.execute(
            "SELECT canonical_json FROM evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return Evidence(**json.loads(row["canonical_json"])) if row else None

    def list_for_incident(self, incident_id: str) -> list[Evidence]:
        return self.query(incident_id=incident_id)

    def query(
        self,
        *,
        incident_id: str | None = None,
        resource_id: str | None = None,
        source: str | None = None,
        collected_from: datetime | None = None,
        collected_to: datetime | None = None,
    ) -> list[Evidence]:
        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (
            ("incident_id", incident_id),
            ("resource_id", resource_id),
            ("source", source),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if collected_from is not None:
            clauses.append("collected_at >= ?")
            parameters.append(collected_from.isoformat())
        if collected_to is not None:
            clauses.append("collected_at <= ?")
            parameters.append(collected_to.isoformat())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT canonical_json FROM evidence {where} ORDER BY collected_at, evidence_id",
            parameters,
        ).fetchall()
        return [Evidence(**json.loads(row["canonical_json"])) for row in rows]

    def save_checkpoint(
        self,
        *,
        run_id: str,
        incident_id: str,
        stage: str,
        sequence_number: int,
        payload: dict[str, Any],
        updated_at: datetime,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO checkpoints (
                run_id, incident_id, stage, sequence_number, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, stage) DO UPDATE SET
                incident_id = excluded.incident_id,
                sequence_number = excluded.sequence_number,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                incident_id,
                stage,
                sequence_number,
                _canonical_json(payload),
                updated_at.isoformat(),
            ),
        )
        self._connection.commit()

    def load_latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT incident_id, stage, sequence_number, payload_json, updated_at
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY sequence_number DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "run_id": run_id,
            "incident_id": row["incident_id"],
            "stage": row["stage"],
            "sequence_number": row["sequence_number"],
            "payload": json.loads(row["payload_json"]),
            "updated_at": row["updated_at"],
        }

    def list_checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT incident_id, stage, sequence_number, payload_json, updated_at
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY sequence_number
            """,
            (run_id,),
        ).fetchall()
        return [
            {
                "run_id": run_id,
                "incident_id": row["incident_id"],
                "stage": row["stage"],
                "sequence_number": row["sequence_number"],
                "payload": json.loads(row["payload_json"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
