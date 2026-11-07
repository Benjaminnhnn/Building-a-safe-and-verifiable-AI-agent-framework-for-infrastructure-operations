"""Best-effort acknowledgement that the verified summary reached Telegram."""

from __future__ import annotations

import hashlib
import logging
import os

import redis

logger = logging.getLogger(__name__)


def _key(incident_id: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{incident_id}:{request_id}".encode()).hexdigest()
    return f"moodle-verifier-notice:{digest}"


def _redis_client():
    return redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", "6379")),
                       db=int(os.getenv("REDIS_DB", "0")), socket_timeout=3, socket_connect_timeout=3)


def mark_delivered(incident_id: str, request_id: str) -> None:
    try:
        _redis_client().set(_key(incident_id, request_id), "1", ex=86400)
    except (redis.RedisError, ValueError) as exc:
        logger.warning("Could not record verifier notification: %s", type(exc).__name__)


def was_delivered(incident_id: str, request_id: str) -> bool:
    try:
        return bool(_redis_client().get(_key(incident_id, request_id)))
    except (redis.RedisError, ValueError) as exc:
        logger.warning("Could not read verifier notification: %s", type(exc).__name__)
        return False
