"""SQLite source of truth for append-only evidence and orchestration checkpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Self

from pydantic import ValidationError

from core.schema.common import EvidenceType
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

            CREATE TABLE IF NOT EXISTS checkpoint_run_claims (
                run_id TEXT PRIMARY KEY,
                owner_token TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS incident_audit_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(run_id, sequence_number)
            );
            CREATE INDEX IF NOT EXISTS idx_audit_run_sequence
                ON incident_audit_events(run_id, sequence_number);
            CREATE TRIGGER IF NOT EXISTS audit_event_no_update
            BEFORE UPDATE ON incident_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'incident audit is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_event_no_delete
            BEFORE DELETE ON incident_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'incident audit is append-only');
            END;
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
        try:
            evidence = Evidence.model_validate(_model_json_dict(evidence))
        except (ValidationError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("evidence failed validation before storage") from exc
        expected_hash = evidence_content_hash(evidence)
        if evidence.content_hash != expected_hash:
            raise EvidenceIntegrityError(
                f"content hash mismatch for {evidence.evidence_id}: "
                f"expected {expected_hash}, got {evidence.content_hash}"
            )

        canonical = _canonical_json(_model_json_dict(evidence))
        payload = _model_json_dict(evidence)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (evidence.evidence_id,),
            ).fetchone()
            if existing is not None:
                stored = self._decode_evidence_row(existing)
                if _canonical_json(_model_json_dict(stored)) == canonical:
                    self._connection.rollback()
                    return False
                raise EvidenceConflictError(
                    f"evidence_id {evidence.evidence_id} already exists with different content"
                )

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
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def get_by_id(self, evidence_id: str) -> Evidence | None:
        row = self._connection.execute(
            "SELECT * FROM evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return self._decode_evidence_row(row) if row else None

    @staticmethod
    def _decode_evidence_row(row: sqlite3.Row) -> Evidence:
        """Load evidence only after validating its digest and indexed columns."""
        try:
            evidence = Evidence(**json.loads(row["canonical_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvidenceIntegrityError("stored evidence is malformed") from exc

        if evidence.content_hash != evidence_content_hash(evidence):
            raise EvidenceIntegrityError(
                f"stored evidence digest mismatch for {evidence.evidence_id}"
            )

        payload = _model_json_dict(evidence)
        expected_columns = {
            "evidence_id": evidence.evidence_id,
            "incident_id": evidence.incident_id,
            "resource_id": evidence.resource_id,
            "source": evidence.source,
            "collected_at": payload["collected_at"],
            "content_hash": evidence.content_hash,
            "redacted": int(evidence.redacted),
            "metadata_json": _canonical_json(payload.get("metadata", {})),
        }
        if any(row[column] != value for column, value in expected_columns.items()):
            raise EvidenceIntegrityError(
                f"stored evidence index mismatch for {evidence.evidence_id}"
            )
        return evidence

    def list_for_incident(self, incident_id: str) -> list[Evidence]:
        return self.query(incident_id=incident_id)

    def verify_integrity(self) -> int:
        """Verify every stored row and return the number of checked records.

        Raises :class:`EvidenceIntegrityError` at the first malformed, modified,
        or index-inconsistent row. This explicit full scan is intended for an
        operator audit, not the per-alert request path.
        """
        rows = self._connection.execute("SELECT * FROM evidence ORDER BY evidence_id").fetchall()
        evidence = [self._decode_evidence_row(row) for row in rows]
        self.verify_audit_integrity()
        by_id = {item.evidence_id: item for item in evidence}
        for item in evidence:
            if item.source != "ai_decision" and item.evidence_type.value != "decision":
                continue
            if item.source != "ai_decision" or item.evidence_type.value != "decision":
                raise EvidenceIntegrityError(
                    f"decision evidence classification mismatch for {item.evidence_id}"
                )
            try:
                kind = item.metadata["decision_kind"]
                decision = json.loads(item.metadata["decision_json"])
                references = decision["evidence_refs"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise EvidenceIntegrityError(
                    f"decision evidence references are malformed for {item.evidence_id}"
                ) from exc
            if (
                not isinstance(kind, str)
                or not kind.strip()
                or not isinstance(references, list)
                or not references
                or any(not isinstance(ref, str) or not ref.strip() for ref in references)
                or len(set(references)) != len(references)
            ):
                raise EvidenceIntegrityError(
                    f"decision evidence references are invalid for {item.evidence_id}"
                )
            for reference in references:
                target = by_id.get(reference)
                if target is None or target.incident_id != item.incident_id:
                    raise EvidenceIntegrityError(
                        f"decision evidence reference {reference} is missing or misbound"
                    )
        return len(evidence)

    @staticmethod
    def _audit_event_hash(previous_hash: str, payload_json: str) -> str:
        material = f"{previous_hash}\n{payload_json}".encode("utf-8")
        return "sha256:" + hashlib.sha256(material).hexdigest()

    def _append_audit_events_in_transaction(
        self, *, run_id: str, events: list[dict[str, Any]]
    ) -> None:
        rows = self._connection.execute(
            "SELECT * FROM incident_audit_events WHERE run_id = ? ORDER BY sequence_number",
            (run_id,),
        ).fetchall()
        previous_hash = "GENESIS"
        if len(rows) > len(events):
            raise EvidenceIntegrityError("checkpoint omits previously appended audit events")
        for sequence, event in enumerate(events):
            payload_json = _canonical_json(event)
            event_id = event.get("event_id")
            incident_id = event.get("incident_id")
            if not isinstance(event_id, str) or not event_id or not isinstance(incident_id, str) or not incident_id:
                raise EvidenceIntegrityError("audit event is missing its event or incident ID")
            expected_hash = self._audit_event_hash(previous_hash, payload_json)
            if sequence < len(rows):
                row = rows[sequence]
                if (
                    row["sequence_number"] != sequence
                    or row["event_id"] != event_id
                    or row["incident_id"] != incident_id
                    or row["previous_hash"] != previous_hash
                    or row["payload_json"] != payload_json
                    or row["event_hash"] != expected_hash
                ):
                    raise EvidenceIntegrityError("checkpoint audit history conflicts with append-only ledger")
            else:
                self._connection.execute(
                    """INSERT INTO incident_audit_events
                       (event_id, run_id, incident_id, sequence_number, previous_hash, event_hash, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, run_id, incident_id, sequence, previous_hash, expected_hash, payload_json),
                )
            previous_hash = expected_hash

    def list_audit_events(self, run_id: str) -> list[dict[str, Any]]:
        self.verify_audit_integrity(run_id=run_id)
        rows = self._connection.execute(
            "SELECT payload_json FROM incident_audit_events WHERE run_id = ? ORDER BY sequence_number",
            (run_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def verify_audit_integrity(self, *, run_id: str | None = None) -> int:
        """Verify append-only audit sequence and hash chains, optionally for one run."""
        if run_id is None:
            rows = self._connection.execute(
                "SELECT * FROM incident_audit_events ORDER BY run_id, sequence_number"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM incident_audit_events WHERE run_id = ? ORDER BY sequence_number",
                (run_id,),
            ).fetchall()
        previous_by_run: dict[str, tuple[int, str]] = {}
        for row in rows:
            current_run = row["run_id"]
            expected_sequence, previous_hash = previous_by_run.get(current_run, (0, "GENESIS"))
            if row["sequence_number"] != expected_sequence or row["previous_hash"] != previous_hash:
                raise EvidenceIntegrityError(f"audit sequence or chain link is invalid for {current_run}")
            expected_hash = self._audit_event_hash(previous_hash, row["payload_json"])
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise EvidenceIntegrityError(f"audit payload is malformed for {current_run}") from exc
            if (
                row["event_hash"] != expected_hash
                or payload.get("event_id") != row["event_id"]
                or payload.get("incident_id") != row["incident_id"]
            ):
                raise EvidenceIntegrityError(f"audit digest or identity is invalid for {current_run}")
            previous_by_run[current_run] = (expected_sequence + 1, expected_hash)
        return len(rows)

    def query(
        self,
        *,
        incident_id: str | None = None,
        resource_id: str | None = None,
        source: str | None = None,
        evidence_type: EvidenceType | None = None,
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
            f"SELECT * FROM evidence {where} ORDER BY collected_at, rowid",
            parameters,
        ).fetchall()
        evidence = [self._decode_evidence_row(row) for row in rows]
        if evidence_type is not None:
            evidence = [item for item in evidence if item.evidence_type == evidence_type]
        return evidence

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

    def claim_checkpoint_run(
        self,
        *,
        run_id: str,
        owner_token: str,
        lease_seconds: int = 900,
    ) -> bool:
        """Atomically claim one checkpointed run until completion or lease expiry."""
        if not run_id or not owner_token:
            raise ValueError("run_id and owner_token are required")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer between 1 and 3600")
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._connection.execute(
                "SELECT owner_token, lease_expires_at FROM checkpoint_run_claims WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if current is not None:
                current_expiry = datetime.fromisoformat(current["lease_expires_at"])
                if current_expiry > now and current["owner_token"] != owner_token:
                    self._connection.rollback()
                    return False
            self._connection.execute(
                """INSERT INTO checkpoint_run_claims (run_id, owner_token, lease_expires_at)
                   VALUES (?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET
                   owner_token = excluded.owner_token, lease_expires_at = excluded.lease_expires_at""",
                (run_id, owner_token, expires_at),
            )
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise

    def release_checkpoint_run(self, *, run_id: str, owner_token: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM checkpoint_run_claims WHERE run_id = ? AND owner_token = ?",
            (run_id, owner_token),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def complete_checkpoint_run(
        self,
        *,
        run_id: str,
        owner_token: str,
        incident_id: str,
        stage: str,
        sequence_number: int,
        payload: dict[str, Any],
        updated_at: datetime,
        audit_events: list[dict[str, Any]] | None = None,
    ) -> None:
        """Commit the final checkpoint and release its claim in one transaction."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            claim = self._connection.execute(
                "SELECT owner_token, lease_expires_at FROM checkpoint_run_claims WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if (
                claim is None
                or claim["owner_token"] != owner_token
                or datetime.fromisoformat(claim["lease_expires_at"])
                <= datetime.now(timezone.utc)
            ):
                raise EvidenceConflictError("checkpoint run claim was lost before completion")
            self._append_audit_events_in_transaction(
                run_id=run_id, events=audit_events or []
            )
            self._connection.execute(
                """INSERT INTO checkpoints (run_id, incident_id, stage, sequence_number, payload_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, stage) DO UPDATE SET
                   incident_id = excluded.incident_id, sequence_number = excluded.sequence_number,
                   payload_json = excluded.payload_json, updated_at = excluded.updated_at""",
                (
                    run_id,
                    incident_id,
                    stage,
                    sequence_number,
                    _canonical_json(payload),
                    updated_at.isoformat(),
                ),
            )
            self._connection.execute(
                "DELETE FROM checkpoint_run_claims WHERE run_id = ? AND owner_token = ?",
                (run_id, owner_token),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def save_claimed_checkpoint(
        self,
        *,
        run_id: str,
        owner_token: str,
        incident_id: str,
        stage: str,
        sequence_number: int,
        payload: dict[str, Any],
        updated_at: datetime,
        lease_seconds: int = 900,
        audit_events: list[dict[str, Any]] | None = None,
    ) -> None:
        """Atomically fence a checkpoint write and renew its run lease."""
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer between 1 and 3600")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            claim = self._connection.execute(
                "SELECT owner_token, lease_expires_at FROM checkpoint_run_claims WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            now = datetime.now(timezone.utc)
            if (
                claim is None
                or claim["owner_token"] != owner_token
                or datetime.fromisoformat(claim["lease_expires_at"]) <= now
            ):
                raise EvidenceConflictError("checkpoint run claim was lost before checkpoint save")
            self._append_audit_events_in_transaction(
                run_id=run_id, events=audit_events or []
            )
            self._connection.execute(
                """INSERT INTO checkpoints (run_id, incident_id, stage, sequence_number, payload_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, stage) DO UPDATE SET
                   incident_id = excluded.incident_id, sequence_number = excluded.sequence_number,
                   payload_json = excluded.payload_json, updated_at = excluded.updated_at""",
                (
                    run_id,
                    incident_id,
                    stage,
                    sequence_number,
                    _canonical_json(payload),
                    updated_at.isoformat(),
                ),
            )
            expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
            self._connection.execute(
                "UPDATE checkpoint_run_claims SET lease_expires_at = ? WHERE run_id = ? AND owner_token = ?",
                (expires_at, run_id, owner_token),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

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
        payload = json.loads(row["payload_json"])
        stored_events = self.list_audit_events(run_id)
        embedded_events = payload.get("audit_events", [])
        if len(stored_events) != len(embedded_events) or stored_events != embedded_events:
            raise EvidenceIntegrityError(
                f"checkpoint audit history does not match append-only ledger for {run_id}"
            )
        return {
            "run_id": run_id,
            "incident_id": row["incident_id"],
            "stage": row["stage"],
            "sequence_number": row["sequence_number"],
            "payload": payload,
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
