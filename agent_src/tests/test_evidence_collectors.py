from __future__ import annotations

import json
import time
from datetime import timezone

import pytest

from core.evidence_collectors import (
    METRIC_QUERIES_BY_ALERT,
    PrometheusEvidenceCollector,
    alert_context_evidence,
    sanitize_log_excerpt,
    static_inventory_evidence,
)


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Client:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        return _Response(self.body)


def _prometheus_body(*, sample_time=None, labels=None):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": labels or {"job": "moodle_node", "password": "leak"},
                    "value": [sample_time or time.time(), "0"],
                }
            ],
        },
    }


def test_prometheus_collector_uses_fixed_query_and_sanitizes_labels() -> None:
    client = _Client(_prometheus_body(labels={"job": "moodle_node", "instance": "moodle-app-a", "token": "secret"}))
    collector = PrometheusEvidenceCollector("http://prometheus:9090", client=client)

    batch = collector.collect("MoodleNodeExporterDown", resource_id="moodle-app")

    assert batch.errors == []
    assert len(batch.evidence) == 1
    evidence = batch.evidence[0]
    assert evidence.source == "prometheus"
    assert evidence.kind == "metric_sample"
    assert evidence.observed_at.tzinfo == timezone.utc
    assert evidence.payload["query_id"] == "moodle_node_up"
    assert evidence.payload["labels"] == {"job": "moodle_node", "instance": "moodle-app-a"}
    assert client.calls[0][1]["query"] == METRIC_QUERIES_BY_ALERT["MoodleNodeExporterDown"][0][1]


def test_prometheus_collector_rejects_unknown_alert_without_querying() -> None:
    client = _Client(_prometheus_body())
    collector = PrometheusEvidenceCollector("http://prometheus:9090", client=client)

    batch = collector.collect('x"} or vector(1)', resource_id="moodle-app")

    assert batch.evidence == []
    assert batch.errors == ["unsupported_alert_metric_contract"]
    assert client.calls == []


def test_prometheus_collector_rejects_stale_samples() -> None:
    client = _Client(_prometheus_body(sample_time=time.time() - 600))
    collector = PrometheusEvidenceCollector("http://prometheus:9090", client=client)

    batch = collector.collect("MoodleNodeExporterDown", resource_id="moodle-app")

    assert batch.evidence == []
    assert batch.errors == ["moodle_node_up:ValueError"]


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "http://user:password@prometheus:9090", "http://prometheus:9090?token=x"],
)
def test_prometheus_url_rejects_non_http_or_credentials(url: str) -> None:
    with pytest.raises(ValueError):
        PrometheusEvidenceCollector(url)


def test_log_excerpt_is_redacted_and_bounded() -> None:
    excerpt = "password=hunter2 Bearer abc.def AKIA1234567890ABCDEF " + ("x" * 1200)

    sanitized = sanitize_log_excerpt(excerpt, max_chars=128)

    assert len(sanitized) == 128
    assert "hunter2" not in sanitized
    assert "abc.def" not in sanitized
    assert "AKIA1234567890ABCDEF" not in sanitized


def test_log_watcher_annotation_becomes_redacted_log_evidence() -> None:
    evidence = alert_context_evidence(
        {
            "status": "firing",
            "labels": {"alertname": "LogFileErrorDetected", "source_file": "/private/path.log"},
            "annotations": {"description": "authorization=Bearer secret-value"},
        },
        resource_id="ai-agent",
        incident_id="incident-1",
    )

    assert evidence.source == "log_watcher"
    assert evidence.kind == "sanitized_log_excerpt"
    assert evidence.payload["context"] == "authorization=[REDACTED]"
    assert "/private/path.log" not in json.dumps(evidence.payload)


def test_static_inventory_emits_config_and_topology_hashes() -> None:
    batch = static_inventory_evidence("moodle-app")

    assert batch.errors == []
    assert {item.kind for item in batch.evidence} == {
        "static_config_snapshot",
        "topology_snapshot",
    }
    assert all(item.payload["inventory_sha256"] for item in batch.evidence)
    assert all(item.observed_at.tzinfo == timezone.utc for item in batch.evidence)
    topology = next(item for item in batch.evidence if item.kind == "topology_snapshot")
    assert topology.payload["depends_on"] == ["moodledata-volume", "postgres-db"]
