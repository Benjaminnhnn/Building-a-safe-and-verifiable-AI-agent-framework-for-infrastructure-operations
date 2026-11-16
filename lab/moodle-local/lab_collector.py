"""Pre-decision evidence collector for the local Moodle lab.

Captures metrics, firing alerts and redacted logs into a snapshot BEFORE any proposal/behavior decision exists, with
`detected_at`, `evidence_cutoff_at`, capture times and SHA-256 of every evidence part. Features are derived only from the
snapshot. `check_leakage` and `assert_cutoff` enforce PLANNING section 6: no scenario markers, answer-bearing run ids,
ground truth, signatures or post-decision fields in features or raw evidence, and `evidence_cutoff_at <= predicted_at`.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from lab_env import (CRON, DB, LB, WEB, docker, iso_ms, parse_iso, prom_query, sha256_bytes, canonical_json, utc_now)

SNAPSHOT_SCHEMA = "aihs-lab-snapshot-v1"
INSTANCES = {"moodle-app-a": "a", "moodle-app-b": "b"}

# (feature name, PromQL, label whose value suffixes the feature, expected label values)
NODE_METRICS = [
    "moodle_node_web_running", "moodle_node_web_restart_count", "moodle_node_apache_router_enabled",
    "moodle_node_router_fallback_success", "moodle_node_reverse_proxy_enabled", "moodle_node_rds_tcp_success",
    "moodle_node_rds_tcp_duration_seconds", "moodle_node_db_dns_success", "moodle_node_db_dns_matches_expected",
    "moodle_node_cpu_percent", "moodle_node_cron_running", "moodle_cron_container_stopped",
    "moodle_adhoc_queue_probe_success", "moodle_adhoc_tasks_overdue_count", "moodle_node_cron_heartbeat_age_seconds",
    "moodle_node_fixture_dir_exists", "moodle_node_fixture_dir_mode", "moodle_efs_mount_available",
]
_NODE = ("a", "b")
_PROBES = ("healthz", "login")
_SERVERS = ("node-a", "node-b")
QUERIES: list[tuple[str, str, str, tuple[str, ...]]] = (
    [(m, f'{m}{{job="moodle_node"}}', "instance", _NODE) for m in NODE_METRICS]
    + [
        ("node_probe_age_seconds", 'time() - moodle_node_probe_timestamp_seconds{job="moodle_node"}', "instance", _NODE),
        ("node_cpu_percent_max_30s", 'max_over_time(moodle_node_cpu_percent{job="moodle_node"}[30s])', "instance", _NODE),
        ("node_rds_tcp_success_min_30s", 'min_over_time(moodle_node_rds_tcp_success{job="moodle_node"}[30s])', "instance", _NODE),
        ("synthetic_success", "moodle_synthetic_success", "", ("",)),
        ("synthetic_success_ratio_60s", "avg_over_time(moodle_synthetic_success[60s])", "", ("",)),
        ("synthetic_latency_seconds", "moodle_synthetic_latency_seconds", "", ("",)),
        ("synthetic_age_seconds", "time() - moodle_synthetic_last_run_timestamp_seconds", "", ("",)),
        ("blackbox_public_probe_success", 'probe_success{job="blackbox_moodle_public"}', "probe", _PROBES),
        ("blackbox_public_probe_duration_seconds", 'probe_duration_seconds{job="blackbox_moodle_public"}', "probe", _PROBES),
        ("blackbox_db_probe_success", 'probe_success{job="blackbox_moodle_rds"}', "", ("",)),
        ("lb_server_up", 'haproxy_server_status{state="UP"}', "server", _SERVERS),
        ("lb_active_servers", "haproxy_backend_active_servers", "", ("",)),
        ("lb_5xx_30s", 'sum by (server) (increase(haproxy_server_http_responses_total{code="5xx"}[30s]))', "server", _SERVERS),
        ("lb_check_failures_60s", "sum by (server) (increase(haproxy_server_check_failures_total[60s]))", "server", _SERVERS),
    ]
)

_RULES = Path(__file__).resolve().parent / "images/prometheus/rules.yml"


def feature_alert_name(alert: str) -> str:
    """Alert class used in a feature name; avoids the substring 'action' that the freeze tool forbids ('Transaction')."""
    return alert.replace("Transaction", "Txn")


def known_alert_names() -> list[str]:
    try:
        return sorted(set(re.findall(r"^\s*- alert: (\w+)", _RULES.read_text(), re.M)))
    except OSError:
        return []


# ---- redaction ---------------------------------------------------------------------------------------------------
_REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "<redacted:key>"),
    (re.compile(r"(?i)\b(bearer|token|sesskey|password|passwd|secret|authorization|signature)\b(\s*[=:]\s*)(?:(?:bearer|basic)\s+)?\S+"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "<redacted:jwt>"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<redacted:aws-key>"),
    (re.compile(r"arn:aws:[^\s\"']+"), "<redacted:arn>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<redacted:email>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<redacted:uuid>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<redacted:hex>"),
    (re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}"), "<redacted:blob>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b"), "<redacted:mac>"),
]


def redact_text(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# ---- leakage rules -----------------------------------------------------------------------------------------------
# Same forbidden-name regex as evaluation/aihs/b2_freeze_dataset.py, plus lab-specific answer-bearing markers.
FORBIDDEN_FEATURE_NAME = re.compile(
    r"scenario|ground.?truth|outcome|verifier|gate|approval|receipt|action|incident|run.?id|fault|answer|label|post.?decision"
    r"|signature|idempot|episode|marker|injected|reset|baseline|variant|transient|exploration|behavior|policy",
    re.IGNORECASE)
# Strings that must never appear in raw evidence text or feature names: scenario codes, fixture/rule tags,
# executor/approval material, opaque run identifiers.
FORBIDDEN_EVIDENCE = re.compile(
    r"\b(?:DB|NET|RES|CON|SEC|BIZ)-0\d\b|moodle-fault|moodle_fault|fault-res|ground[_ -]?truth|idempotency|"
    r"action_sha256|approval|\bep-[0-9a-f]{8,}\b|run[_-]?id|catalog_action",
    re.IGNORECASE)
ALLOWED_SNAPSHOT_KEYS = {"schema", "snapshot_id", "detected_at", "evidence_cutoff_at", "capture_started_at",
                         "capture_finished_at", "collector_code_sha256", "parts", "evidence_bundle_sha256", "features_sha256"}


class LeakageError(ValueError):
    pass


def check_leakage(features: dict, raw_texts: dict[str, str], snapshot_index: dict | None = None,
                  extra_forbidden: list[str] | None = None) -> None:
    """Raise LeakageError unless features and raw evidence are free of post-decision / answer-bearing content."""
    for name, value in features.items():
        if FORBIDDEN_FEATURE_NAME.search(name) or FORBIDDEN_EVIDENCE.search(name):
            raise LeakageError(f"answer-bearing feature name: {name}")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise LeakageError(f"non-numeric feature value: {name}")
        if isinstance(value, float) and not math.isfinite(value):
            raise LeakageError(f"non-finite feature value: {name}")
    for part, text in raw_texts.items():
        match = FORBIDDEN_EVIDENCE.search(text)
        if match:
            raise LeakageError(f"forbidden marker {match.group(0)!r} in raw evidence part {part}")
        for token in extra_forbidden or []:
            if token and token in text:
                raise LeakageError(f"run-specific identifier present in raw evidence part {part}")
    if snapshot_index is not None and set(snapshot_index) - ALLOWED_SNAPSHOT_KEYS:
        raise LeakageError(f"unexpected snapshot fields: {sorted(set(snapshot_index) - ALLOWED_SNAPSHOT_KEYS)}")


def assert_cutoff(snapshot_index: dict, predicted_at: str) -> None:
    """Enforce detected_at <= evidence_cutoff_at <= predicted_at (all UTC ISO timestamps)."""
    detected = parse_iso(snapshot_index["detected_at"])
    cutoff = parse_iso(snapshot_index["evidence_cutoff_at"])
    finished = parse_iso(snapshot_index["capture_finished_at"])
    predicted = parse_iso(predicted_at)
    if not (detected <= cutoff and finished <= cutoff and cutoff <= predicted):
        raise LeakageError("evidence_cutoff_at must be >= detection and <= predicted_at")


# ---- collection ---------------------------------------------------------------------------------------------------
LOG_SOURCES = {"web_a": WEB["a"], "web_b": WEB["b"], "cron_a": CRON["a"], "lb": LB, "db": DB}
LOG_PATTERNS = {
    "db_connect_errors": re.compile(r"could not (?:connect|translate)|connection refused|connection to server|dml_connection|SQLSTATE|no route to host", re.I),
    "permission_denied": re.compile(r"permission denied|not writable|unable to create|cannot create|efs_|make_writable", re.I),
    "http_5xx": re.compile(r'" 5\d\d |\b5\d\d [0-9]+ -- |HTTP/1\.[01]" 5\d\d', re.I),
    "error_lines": re.compile(r"\b(?:error|fatal|exception|fail(?:ed|ure)?|denied)\b", re.I),
    "cron_runs": re.compile(r"cron|task", re.I),
}


def default_log_reader(container: str, since_seconds: int = 120, tail: int = 400) -> str:
    result = docker("logs", "--since", f"{since_seconds}s", "--tail", str(tail), container, check=False, timeout=20)
    return (result.stdout or "") + (result.stderr or "")


def _short(label_value: str) -> str:
    return INSTANCES.get(label_value, label_value)


def build_features(samples: dict[str, dict[str, float | None]], alerts: list[dict], logs: dict[str, str]) -> dict:
    features: dict[str, float | None] = {}
    for name, _promql, _label, expected in QUERIES:
        got = samples.get(name, {})
        for value in expected:
            key = f"{name}__{value}" if value else name
            features[key] = got.get(value)
    firing = {a["alertname"] for a in alerts}
    for alert in known_alert_names():
        features[f"alert_firing__{feature_alert_name(alert)}"] = 1.0 if alert in firing else 0.0
    features["alert_firing_count"] = float(len(alerts))
    for source, text in logs.items():
        lines = [line for line in text.splitlines() if line.strip()]
        features[f"log_lines__{source}"] = float(len(lines))
        for pattern_name, pattern in LOG_PATTERNS.items():
            features[f"log_{pattern_name}__{source}"] = float(sum(1 for line in lines if pattern.search(line)))
    return features


class Collector:
    def __init__(self, out_root: Path, *, query=prom_query, log_reader=default_log_reader, clock=utc_now):
        self.out_root = out_root
        self.query = query
        self.log_reader = log_reader
        self.clock = clock

    def _samples(self) -> tuple[dict, list[dict], list[dict]]:
        samples: dict[str, dict[str, float | None]] = {}
        raw = []
        for name, promql, label, _expected in QUERIES:
            bucket = samples.setdefault(name, {})
            try:
                result = self.query(promql)
            except Exception:
                result = []
            for item in result:
                metric = item["metric"]
                if any(k.startswith("moodle_fault") for k in (metric.get("__name__", ""),)):
                    continue  # defensive: staging marker series must never be collected
                key = _short(metric.get(label, "")) if label else ""
                try:
                    value = float(item["value"][1])
                except (ValueError, KeyError, IndexError):
                    continue
                if not math.isfinite(value):
                    continue
                bucket[key] = value
                raw.append({"feature": name, "label": key, "value": value})
        alerts = []
        try:
            for item in self.query('ALERTS{alertstate=~"firing|pending"}'):
                m = item["metric"]
                alerts.append({"alertname": m.get("alertname", ""), "instance": m.get("instance", ""), "state": m.get("alertstate", "")})
        except Exception:
            pass
        firing = [a for a in alerts if a["state"] == "firing"]
        return samples, sorted(alerts, key=lambda a: (a["alertname"], a["instance"], a["state"])), sorted(firing, key=lambda a: (a["alertname"], a["instance"]))

    def collect(self, snapshot_id: str, detected_at: str, *, extra_forbidden: list[str] | None = None) -> dict:
        """Write the snapshot directory and return its index. Must be called before any proposal exists."""
        directory = self.out_root / snapshot_id
        (directory / "logs").mkdir(parents=True, exist_ok=True)
        started = self.clock()
        samples, all_alerts, firing = self._samples()
        logs = {}
        for source, container in LOG_SOURCES.items():
            try:
                logs[source] = redact_text(self.log_reader(container))
            except Exception:
                logs[source] = ""
        features = build_features(samples, firing, logs)
        finished = self.clock()
        cutoff = iso_ms(finished)
        metrics_bytes = canonical_json({"samples": samples})
        alerts_bytes = canonical_json({"alerts": all_alerts})
        features_bytes = canonical_json(features)
        parts = {"metrics.json": sha256_bytes(metrics_bytes), "alerts.json": sha256_bytes(alerts_bytes)}
        (directory / "metrics.json").write_bytes(metrics_bytes)
        (directory / "alerts.json").write_bytes(alerts_bytes)
        raw_texts = {"metrics.json": metrics_bytes.decode(), "alerts.json": alerts_bytes.decode()}
        for source, text in logs.items():
            data = text.encode()
            (directory / "logs" / f"{source}.log").write_bytes(data)
            parts[f"logs/{source}.log"] = sha256_bytes(data)
            raw_texts[f"logs/{source}.log"] = text
        check_leakage(features, raw_texts, extra_forbidden=extra_forbidden)
        (directory / "features.json").write_bytes(features_bytes)
        bundle = sha256_bytes("\n".join(f"{k}:{v}" for k, v in sorted(parts.items())).encode())
        index = {
            "schema": SNAPSHOT_SCHEMA, "snapshot_id": snapshot_id,
            "detected_at": detected_at, "evidence_cutoff_at": cutoff,
            "capture_started_at": iso_ms(started), "capture_finished_at": cutoff,
            "collector_code_sha256": sha256_bytes(Path(__file__).read_bytes()),
            "parts": parts, "evidence_bundle_sha256": bundle, "features_sha256": sha256_bytes(features_bytes),
        }
        check_leakage({}, {}, snapshot_index=index)
        if parse_iso(detected_at) > finished:
            raise LeakageError("detected_at is after the capture finished")
        (directory / "snapshot.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        return index
