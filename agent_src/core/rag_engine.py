import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

STANDARD_RUNBOOKS_COLLECTION = "standard_runbooks"
INCIDENT_MEMORY_COLLECTION = "incident_memory"
LEGACY_COLLECTION = "ops_runbooks"
RUNBOOK_CHUNK_CHARS = int(os.getenv("RAG_RUNBOOK_CHUNK_CHARS", "1200"))
RAG_MAX_DISTANCE = float(os.getenv("RAG_MAX_DISTANCE", "1.2"))
RUNBOOK_ALERT_NAMES = {
    "runbook_nginx.md": ("WebEndpointDown", "FrontendAPIProxyDown"),
    "runbook_postgresql.md": ("PostgreSQLDown", "PaymentAPIEndpointDown"),
    "runbook_redis.md": ("RedisDown", "RedisBrokerDown"),
    "runbook_docker.md": ("DockerContainerDown",),
}


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


def _source_provenance(source: str, content: str, *, source_path: str | None = None) -> dict[str, str]:
    """Stable source identity plus source and ingestion timestamps for RAG metadata."""
    source_time = datetime.now(timezone.utc)
    if source_path and os.path.exists(source_path):
        source_time = datetime.fromtimestamp(os.path.getmtime(source_path), timezone.utc)
    return {
        "source": source,
        "source_observed_at": source_time.isoformat(),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


class RAGEngine:
    def __init__(self, db_path: str = "./vector_db"):
        self.db_path = db_path
        self.client = chromadb.PersistentClient(path=db_path)
        self._embedding_fn = embedding_functions.DefaultEmbeddingFunction()
        self.standard_runbooks = self.client.get_or_create_collection(
            name=STANDARD_RUNBOOKS_COLLECTION,
            embedding_function=self._embedding_fn,  # type: ignore[arg-type]
        )
        self.incident_memory = self.client.get_or_create_collection(
            name=INCIDENT_MEMORY_COLLECTION,
            embedding_function=self._embedding_fn,  # type: ignore[arg-type]
        )
        self._migrate_legacy_collection()
        self._ingest_initial_data()

    def _migrate_legacy_collection(self) -> None:
        """Copy dynamic legacy memory into the new incident collection."""
        try:
            legacy = self.client.get_collection(
                name=LEGACY_COLLECTION,
                embedding_function=self._embedding_fn,  # type: ignore[arg-type]
            )
        except Exception:
            return

        try:
            data = legacy.get(include=["documents", "metadatas"])
            ids = data.get("ids") or []
            documents = data.get("documents") or []
            metadatas = data.get("metadatas") or []
            migrated_count = 0
            for doc_id, document, metadata in zip(ids, documents, metadatas):
                metadata = metadata or {}
                source = str(metadata.get("source", "legacy_runbook"))
                if source not in {"incident_history", "admin_feedback"}:
                    continue
                migrated_metadata = dict(metadata)
                migrated_metadata["migrated_from"] = LEGACY_COLLECTION
                migrated_metadata.setdefault("document_type", source)
                self.incident_memory.upsert(
                    ids=[f"legacy::{doc_id}"],
                    documents=[document],
                    metadatas=[migrated_metadata],
                )
                migrated_count += 1
            if migrated_count:
                logger.info("Migrated %s legacy RAG documents from %s", migrated_count, LEGACY_COLLECTION)
        except Exception as exc:
            logger.warning("Unable to migrate legacy RAG collection: %s", exc)

    def _ingest_initial_data(self) -> None:
        kb_path = os.path.join(os.path.dirname(__file__), "..", "config", "knowledge_base")
        if not os.path.exists(kb_path):
            logger.warning("KB path not found: %s", kb_path)
            return

        total_chunks = 0
        for filename in sorted(os.listdir(kb_path)):
            if not filename.endswith(".md"):
                continue

            file_path = os.path.join(kb_path, filename)
            with open(file_path, "r", encoding="utf-8") as file:
                content = file.read()

            try:
                self.standard_runbooks.delete(where={"source_file": filename})
            except Exception:
                pass

            chunks = _chunk_markdown(content)
            if not chunks:
                continue

            ids = []
            documents = []
            metadatas = []
            provenance = _source_provenance(filename, content, source_path=file_path)
            alert_names = RUNBOOK_ALERT_NAMES.get(filename, ("Unknown",))
            for alert_name in alert_names:
                for index, (heading, chunk) in enumerate(chunks):
                    digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:12]
                    ids.append(f"runbook::{filename}::{alert_name}::{index:03d}::{digest}")
                    documents.append(chunk)
                    metadatas.append(
                        {
                            **provenance,
                            "source_file": filename,
                            "document_type": "standard_runbook",
                            "alert_name": alert_name,
                            "chunk_index": index,
                            "heading": heading,
                        }
                    )

            self.standard_runbooks.upsert(ids=ids, documents=documents, metadatas=metadatas)
            total_chunks += len(ids)

        logger.info("Loaded %s runbook chunks from %s", total_chunks, kb_path)

    def ingest_runbook_file(self, file_path: str) -> None:
        """Add or refresh one runbook file, including admin-published drafts."""
        if not os.path.exists(file_path):
            logger.warning("Runbook file not found: %s", file_path)
            return

        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read()

        source = os.path.basename(file_path)
        kb_root = os.path.join(os.path.dirname(__file__), "..", "config", "knowledge_base")
        source_file = os.path.relpath(file_path, kb_root)
        document_type = "published_runbook" if "published" in file_path.split(os.sep) else "standard_runbook"

        try:
            self.standard_runbooks.delete(where={"source_file": source_file})
        except Exception:
            pass

        ids = []
        documents = []
        metadatas = []
        provenance = _source_provenance(source_file, content, source_path=file_path)
        alert_names = RUNBOOK_ALERT_NAMES.get(source, ("Unknown",))
        for alert_name in alert_names:
            for index, (heading, chunk) in enumerate(_chunk_markdown(content)):
                digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:12]
                ids.append(f"runbook::{source_file}::{alert_name}::{index:03d}::{digest}")
                documents.append(chunk)
                metadatas.append(
                    {
                        **provenance,
                        "source_file": source_file,
                        "document_type": document_type,
                        "alert_name": alert_name,
                        "chunk_index": index,
                        "heading": heading,
                    }
                )

        if ids:
            self.standard_runbooks.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def save_incident(
        self,
        alert_name: str,
        description: str,
        ai_analysis: str,
        resolution: str,
        outcome: str,
    ) -> None:
        timestamp = datetime.now()
        doc_id = f"incident_{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_{alert_name}"
        document = (
            f"# Incident: {alert_name}\n"
            f"Mô tả: {description}\n\n"
            f"## AI Phân tích: {ai_analysis}\n\n"
            f"## Hành động đã thực hiện: {resolution}\n\n"
            f"## Kết quả: {outcome}\n"
        )
        self.incident_memory.upsert(
            ids=[doc_id],
            documents=[document],
            metadatas=[
                {
                    "source": "incident_history",
                    "document_type": "incident_history",
                    "alert_name": alert_name,
                    "timestamp": timestamp.isoformat(),
                    **_source_provenance("incident_history", document),
                    "outcome": outcome,
                }
            ],
        )
        logger.info("Saved incident '%s' into incident memory (outcome=%s)", alert_name, outcome)

    def save_admin_solution(
        self,
        incident_id: str,
        alert_name: str,
        incident_details: str,
        admin_feedback: str,
        reviewed_solution: str,
        review_status: str,
    ) -> None:
        timestamp = datetime.now()
        doc_id = f"admin_feedback_{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_{incident_id}"
        document = (
            f"# Admin Feedback: {alert_name}\n"
            f"Incident ID: {incident_id}\n\n"
            f"## Incident context\n{incident_details}\n\n"
            f"## Admin suggestion\n{admin_feedback}\n\n"
            f"## Agent-reviewed solution\n{reviewed_solution}\n\n"
            f"## Review status\n{review_status}\n"
        )
        self.incident_memory.upsert(
            ids=[doc_id],
            documents=[document],
            metadatas=[
                {
                    "source": "admin_feedback",
                    "document_type": "admin_feedback",
                    "alert_name": alert_name,
                    "incident_id": incident_id,
                    "timestamp": timestamp.isoformat(),
                    **_source_provenance("admin_feedback", document),
                    "review_status": review_status,
                }
            ],
        )
        logger.info("Saved admin feedback for incident '%s' into incident memory", incident_id)

    @staticmethod
    def _keyword_score(document: str, keywords: set[str]) -> int:
        lower_document = document.lower()
        return sum(1 for keyword in keywords if keyword in lower_document)

    def _retrieve(
        self,
        collection: Any,
        query_text: str,
        n_results: int = 3,
        alert_name: str | None = None,
    ) -> str:
        try:
            total_count = collection.count()
            if total_count == 0:
                return ""

            results = collection.query(
                query_texts=[query_text],
                n_results=min(n_results, total_count),
                where={"alert_name": alert_name} if alert_name else None,
                include=["documents", "metadatas", "distances"],
            )
            documents = (results.get("documents") or [[]])[0]
            metadatas = (results.get("metadatas") or [[]])[0]
            distances = (results.get("distances") or [[]])[0]
            if not documents:
                return ""

            keywords = set(re.findall(r"\w+", query_text.lower()))
            candidates = []
            for index, (document, metadata) in enumerate(zip(documents, metadatas)):
                distance = distances[index] if index < len(distances) else 0.0
                if distance is not None and float(distance) > RAG_MAX_DISTANCE:
                    continue
                candidates.append((document, metadata, float(distance or 0.0)))

            ranked = sorted(
                candidates,
                key=lambda item: (-self._keyword_score(item[0], keywords), item[2]),
            )
            rendered = []
            for document, metadata, _distance in ranked:
                metadata = metadata or {}
                source = metadata.get("source_file") or metadata.get("source") or "unknown"
                source_time = metadata.get("source_observed_at") or metadata.get("timestamp") or "unknown"
                source_hash = metadata.get("source_sha256") or "unknown"
                rendered.append(f"[Nguồn: {source}; thời điểm: {source_time}; SHA-256: {source_hash}]\n{document}")
            return "\n\n---\n\n".join(rendered)
        except Exception as exc:
            logger.error("RAG retrieval failed: %s", exc)
            return ""

    def query_knowledge(self, alert_description: str, alert_name: str | None = None) -> str:
        runbook_text = self._retrieve(self.standard_runbooks, alert_description, alert_name=alert_name)
        memory_text = self._retrieve(self.incident_memory, alert_description, alert_name=alert_name)
        sections = []
        if runbook_text:
            sections.append(f"## Quy trình chuẩn từ runbook\n{runbook_text}")
        if memory_text:
            sections.append(f"## Kinh nghiệm từ incident và feedback trước đây\n{memory_text}")
        return "\n\n".join(sections)

    def query_runbook(self, alert_description: str) -> str:
        """Backward-compatible alias for callers using the old method name."""
        return self.query_knowledge(alert_description)


_rag_instance = None


def get_rag_instance() -> RAGEngine | None:
    global _rag_instance
    if _rag_instance is None:
        try:
            db_path = os.getenv("VECTOR_DB_PATH", "./vector_db")
            _rag_instance = RAGEngine(db_path=db_path)
            logger.info("RAG Engine initialized successfully at %s", db_path)
        except Exception as exc:
            logger.error("Failed to initialize RAG Engine: %s", exc)
            return None
    return _rag_instance
