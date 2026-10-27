"""Runbook chunking, stable chunk identity, and a deterministic retriever.

This module has no ChromaDB dependency so the diagnosis layer and offline
evaluation can use authored runbooks with the same chunk boundaries and
provenance as the vector store.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

RUNBOOK_CHUNK_CHARS = int(os.getenv("RAG_RUNBOOK_CHUNK_CHARS", "1200"))
RUNBOOK_ALERT_NAMES = {
    "moodle/runbook_moodle_connectivity.md": (
        "MoodleRdsTcpProbeFailed", "MoodleNodeRdsTcpFailed", "MoodleNodeRdsTcpLatencyHigh",
        "MoodleDatabaseEndpointInvalid", "MoodleDatabasePortBlocked", "MoodleDatabaseLatencyHigh",
        "MoodleSyntheticTransactionFailed",
    ),
    "moodle/runbook_moodle_service.md": (
        "MoodlePublicProbeFailed", "MoodleWebContainerMissing", "MoodleWebContainerRestarting",
        "MoodleApacheRouterMissing", "MoodleNodeRouterFallbackFailed", "MoodleTrustedProxyConfigDrift",
        "MoodleProxyUnavailable", "MoodleContainerCrashLoop", "MoodleSyntheticTransactionFailed",
    ),
    "moodle/runbook_moodle_storage_resources.md": (
        "MoodleEfsMountMissing", "MoodleScratchEnospc", "MoodleNodeCpuHigh", "MoodleNodeMemoryPressure",
        "MoodleDataVolumePressure", "MoodleSyntheticTransactionFailed",
    ),
    "moodle/runbook_moodle_monitoring.md": (
        "MoodleNodeExporterDown", "MoodleCAdvisorDown", "MoodleNodeProbeStale", "MoodleSyntheticTransactionStale",
        "MoodleAdhocQueueProbeFailed", "MoodleRdsIngressAuditFailed",
    ),
    "moodle/runbook_moodle_security.md": (
        "MoodleDatabasePortExposure", "MoodleTrustedProxyMisconfigured", "MoodleTrustedProxyConfigDrift",
        "MoodleRdsIngressAuditFailed",
    ),
    "moodle/runbook_moodle_platform_operations.md": (
        "MoodleMaintenanceModeStuck", "MoodleAdhocTaskQueueStuck",
    ),
}
DEFAULT_KNOWLEDGE_BASE = Path(__file__).resolve().parents[2] / "config" / "knowledge_base"


def _chunk_markdown(content: str, max_chars: int = RUNBOOK_CHUNK_CHARS) -> list[tuple[str, str]]:
    """Split Markdown into heading-aware chunks with stable, bounded sizes."""
    sections = re.split(r"(?=^#{1,6}\s+)", content.strip(), flags=re.MULTILINE)
    chunks: list[tuple[str, str]] = []

    for section in sections:
        section = section.strip()
        if not section:
            continue

        first_line = section.splitlines()[0].strip()
        heading = first_line.lstrip("#").strip() if first_line.startswith("#") else "Overview"
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section) if part.strip()]
        current = ""

        for paragraph in paragraphs:
            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if current and len(candidate) > max_chars:
                chunks.append((heading, current))
                current = paragraph
            else:
                current = candidate

            while len(current) > max_chars:
                chunks.append((heading, current[:max_chars].rstrip()))
                current = current[max_chars:].lstrip()

        if current:
            chunks.append((heading, current))

    return chunks


def runbook_chunk_id(source_file: str, chunk_index: int, text: str) -> str:
    """Stable identity of one runbook chunk revision."""
    digest = hashlib.sha256(f"{source_file}:{chunk_index}:{text}".encode("utf-8")).hexdigest()[:16]
    return f"rb-{digest}"


def chunk_record(
    *, source_file: str, chunk_index: int, heading: str, text: str, source_sha256: str
) -> dict[str, Any]:
    return {
        "chunk_id": runbook_chunk_id(source_file, chunk_index, text),
        "source_file": source_file,
        "heading": heading,
        "chunk_index": chunk_index,
        "source_sha256": source_sha256,
        "chunk_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


class StaticRunbookRetriever:
    """Deterministic alert-scoped keyword retriever over authored runbooks."""

    def __init__(self, knowledge_base: str | Path = DEFAULT_KNOWLEDGE_BASE) -> None:
        self._chunks_by_alert: dict[str, list[dict[str, Any]]] = {}
        root = Path(knowledge_base)
        for source_file, alert_names in RUNBOOK_ALERT_NAMES.items():
            path = root / source_file
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            source_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            records = [
                chunk_record(
                    source_file=source_file,
                    chunk_index=index,
                    heading=heading,
                    text=text,
                    source_sha256=source_sha256,
                )
                for index, (heading, text) in enumerate(_chunk_markdown(content))
            ]
            for alert_name in alert_names:
                self._chunks_by_alert.setdefault(alert_name, []).extend(records)

    def retrieve(self, alert_name: str, query_text: str = "", *, limit: int = 3) -> list[dict[str, Any]]:
        keywords = {
            token for token in re.findall(r"[a-z0-9]+", f"{alert_name} {query_text}".lower()) if len(token) > 2
        }
        # Split CamelCase alert names so "MoodleNodeCpuHigh" matches "cpu".
        keywords |= {token.lower() for token in re.findall(r"[A-Z][a-z0-9]+", alert_name)}
        candidates = self._chunks_by_alert.get(alert_name, [])
        ranked = sorted(
            candidates,
            key=lambda item: (
                -len(keywords & set(re.findall(r"[a-z0-9]+", item["text"].lower()))),
                item["source_file"],
                item["chunk_index"],
            ),
        )
        # An alert such as MoodleSyntheticTransactionFailed maps to several runbooks.
        # Take the best chunk of each source first so one runbook cannot fill every
        # slot, then fill the remaining slots by overall rank.
        selected: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for item in ranked:
            if item["source_file"] not in seen_sources:
                seen_sources.add(item["source_file"])
                selected.append(item)
        selected.extend(item for item in ranked if item not in selected)
        return [dict(item) for item in selected[:limit]]


_STATIC_RETRIEVER: StaticRunbookRetriever | None = None


def get_static_retriever() -> StaticRunbookRetriever:
    global _STATIC_RETRIEVER
    if _STATIC_RETRIEVER is None:
        _STATIC_RETRIEVER = StaticRunbookRetriever()
    return _STATIC_RETRIEVER
