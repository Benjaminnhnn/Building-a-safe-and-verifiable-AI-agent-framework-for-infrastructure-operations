"""Conservative evidence-derived diagnosis for Moodle Prometheus alerts.

This module consumes stored Prometheus observations only. It does not read the
scenario catalog, infer remediation, execute actions, or resolve incidents.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

_REQUIRED_QUERY_IDS = (
    "moodle_synthetic_success",
    "moodle_rds_tcp_probe",
    "moodle_public_probe",
    "moodle_efs_mount",
)
_ALL_QUERY_IDS = (
    *_REQUIRED_QUERY_IDS,
    "moodle_node_up",
    "moodle_cadvisor_up",
    "moodle_node_rds_tcp_success",
    "moodle_synthetic_last_run_age_seconds",
    "moodle_node_cpu_busy_percent",
    "moodle_node_memory_available_drop_bytes",
    "moodle_web_container_last_seen_age_seconds",
    "moodle_node_scratch_enospc",
    "moodle_node_web_restart_count_2m",
    "moodle_node_probe_age_seconds",
    "moodle_node_rds_tcp_duration_seconds",
    "moodle_node_apache_router_enabled",
    "moodle_node_router_fallback_success",
    "moodle_node_reverse_proxy_enabled",
)
_MAX_SAMPLE_AGE_SECONDS = 120
_MAX_FUTURE_SKEW_SECONDS = 30
_DIRECT_PROBE_RULES = {
    "MoodleRdsTcpProbeFailed": ("moodle_rds_tcp_probe", "rds_path_probe_failed"),
    "MoodlePublicProbeFailed": ("moodle_public_probe", "public_entry_probe_failed"),
    "MoodleEfsMountMissing": ("moodle_efs_mount", "efs_mount_probe_failed"),
}
_DIRECT_NODE_RULES = {
    "MoodleNodeRdsTcpFailed": (
        "moodle_node_rds_tcp_success",
        "node_rds_path_probe_failed",
        "One or more Moodle nodes cannot establish the monitored TCP path to RDS",
        "Fresh node-local probes confirm failed Moodle-to-RDS paths; database health and causality are not proven",
    ),
    "MoodleNodeExporterDown": (
        "moodle_node_up",
        "node_exporter_scrape_unavailable",
        "Node exporter metrics are unavailable for one or more Moodle nodes; host failure is unconfirmed",
        "Fresh Prometheus scrape status shows an unavailable node exporter; the host itself may still be healthy",
    ),
    "MoodleCAdvisorDown": (
        "moodle_cadvisor_up",
        "cadvisor_scrape_unavailable",
        "Container metrics are unavailable for one or more Moodle nodes; container state is unconfirmed",
        "Fresh Prometheus scrape status shows unavailable cAdvisor telemetry; application container state is not proven",
    ),
    "MoodleApacheRouterMissing": (
        "moodle_node_apache_router_enabled",
        "apache_router_signal_missing",
        "The web container is running but its Apache router-enabled probe reports disabled",
        "The current node-local probes confirm Apache is running while its configured router signal is disabled",
    ),
    "MoodleNodeRouterFallbackFailed": (
        "moodle_node_router_fallback_success",
        "node_router_fallback_failed",
        "The web container is running but its node-local router fallback probe fails",
        "The current node-local probes confirm a failed router fallback while the web container is running",
    ),
}
_DIRECT_THRESHOLD_RULES = {
    "MoodleSyntheticTransactionStale": (
        "moodle_synthetic_last_run_age_seconds",
        180.0,
        "synthetic_transaction_source_stale",
        "Synthetic transaction telemetry has not refreshed within its configured 180-second threshold",
        False,
        False,
    ),
    "MoodleNodeCpuHigh": (
        "moodle_node_cpu_busy_percent",
        70.0,
        "node_cpu_pressure",
        "Moodle node CPU busy percentage exceeds the configured 70 percent alert threshold",
        True,
        False,
    ),
    "MoodleNodeMemoryPressure": (
        "moodle_node_memory_available_drop_bytes",
        33554432.0,
        "node_available_memory_drop",
        "Node available memory dropped by more than 32 MiB in the last minute",
        True,
        False,
    ),
    "MoodleWebContainerMissing": (
        "moodle_web_container_last_seen_age_seconds",
        30.0,
        "web_container_observation_stale",
        "The Moodle web container has not been observed recently by cAdvisor; container absence is not independently proven",
        True,
        False,
    ),
    "MoodleScratchEnospc": (
        "moodle_node_scratch_enospc",
        0.5,
        "isolated_fault_scratch_full",
        "The isolated fault scratch filesystem reports ENOSPC; Moodle data storage is not implicated by this signal",
        True,
        False,
    ),
    "MoodleWebContainerRestarting": (
        "moodle_node_web_restart_count_2m",
        2.0,
        "web_container_restarting",
        "The Moodle web container restarted at least twice in the two-minute alert window; the cause is not isolated",
        True,
        True,
    ),
    "MoodleNodeProbeStale": (
        "moodle_node_probe_age_seconds",
        120.0,
        "node_probe_stale",
        "Node-local probe telemetry has not refreshed within the configured 120-second threshold",
        True,
        False,
    ),
    "MoodleNodeRdsTcpLatencyHigh": (
        "moodle_node_rds_tcp_duration_seconds",
        0.2,
        "node_rds_tcp_latency_high",
        "A Moodle node's successful RDS TCP probe exceeds the configured 0.2-second latency threshold",
        True,
        False,
    ),
    "MoodleTrustedProxyConfigDrift": (
        "moodle_node_reverse_proxy_enabled",
        0.5,
        "trusted_proxy_setting_flagged_for_review",
        "The alert rule flags enabled reverse-proxy trust; compare the node setting to the approved staging baseline",
        True,
        False,
    ),
}


def diagnose_moodle_alert(alert_name: str, evidence: list[Any]) -> dict[str, Any]:
    """Return a bounded hypothesis, or explicit insufficient/conflicting evidence."""
    direct_rule = _DIRECT_PROBE_RULES.get(alert_name)
    direct_node_rule = _DIRECT_NODE_RULES.get(alert_name)
    threshold_rule = _DIRECT_THRESHOLD_RULES.get(alert_name)
    if alert_name != "MoodleSyntheticTransactionFailed" and direct_rule is None and direct_node_rule is None and threshold_rule is None:
        return {
            "status": "insufficient_evidence",
            "reason": "no evidence-derived decision rule is approved for this alert",
            "evidence_refs": [],
            "hypothesis": None,
            "confidence": 0.0,
            "action_candidate": None,
        }

    samples: dict[str, list[tuple[float, str, datetime, str | None]]] = {
        query_id: [] for query_id in _ALL_QUERY_IDS
    }
    for item in evidence:
        if getattr(item, "source", None) != "prometheus":
            continue
        metadata = getattr(item, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        try:
            payload = json.loads(metadata.get("payload_json", "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        query_id = payload.get("query_id")
        value = payload.get("sample_value")
        labels = payload.get("labels", {})
        instance = labels.get("instance") if isinstance(labels, dict) else None
        collected_at = getattr(item, "collected_at", None)
        if (
            query_id in samples
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and isinstance(collected_at, datetime)
            and collected_at.tzinfo is not None
            and collected_at.utcoffset() is not None
            and isinstance(getattr(item, "evidence_id", None), str)
            and bool(item.evidence_id.strip())
        ):
            samples[query_id].append(
                (float(value), item.evidence_id, collected_at, str(instance) if instance else None)
            )

    required_queries = (
        _REQUIRED_QUERY_IDS
        if alert_name == "MoodleSyntheticTransactionFailed"
        else (direct_rule or direct_node_rule or threshold_rule)[0:1]
    )
    missing = [query_id for query_id in required_queries if not samples[query_id]]
    if missing:
        return {
            "status": "insufficient_evidence",
            "reason": "required fresh Prometheus observations are missing",
            "missing_queries": missing,
            "evidence_refs": sorted({ref for values in samples.values() for _, ref, _, _ in values}),
            "hypothesis": None,
            "confidence": 0.0,
            "action_candidate": None,
        }

    timestamps = [
        timestamp.astimezone(timezone.utc)
        for query_id in required_queries
        for _, _, timestamp, _ in samples[query_id]
    ]
    now = datetime.now(timezone.utc)
    ages = [(now - timestamp).total_seconds() for timestamp in timestamps]
    if any(age > _MAX_SAMPLE_AGE_SECONDS or age < -_MAX_FUTURE_SKEW_SECONDS for age in ages):
        return {
            "status": "insufficient_evidence",
            "reason": "Prometheus observations are stale or have timestamps too far in the future",
            "evidence_refs": sorted({ref for values in samples.values() for _, ref, _, _ in values}),
            "hypothesis": None,
            "confidence": 0.0,
            "action_candidate": None,
        }
    spread = (max(timestamps) - min(timestamps)).total_seconds()
    if spread > 60:
        return {
            "status": "insufficient_evidence",
            "reason": "Prometheus observations do not belong to one 60-second evidence window",
            "evidence_refs": sorted({ref for values in samples.values() for _, ref, _, _ in values}),
            "hypothesis": None,
            "confidence": 0.0,
            "action_candidate": None,
        }

    states = {
        query_id: {value for value, _, _, _ in samples[query_id]}
        for query_id in required_queries
    }
    efs_by_instance: dict[str | None, set[float]] = defaultdict(set)
    for value, _, _, instance in samples.get("moodle_efs_mount", []):
        efs_by_instance[instance].add(value)
    contradictory = any(
        len(values) != 1
        for query_id, values in states.items()
        if query_id != "moodle_efs_mount"
        and not (direct_node_rule and query_id == direct_node_rule[0])
        and not (threshold_rule and threshold_rule[4] and query_id == threshold_rule[0])
    ) or any(len(values) != 1 for values in efs_by_instance.values())
    if (
        "moodle_efs_mount" in states
        and len(states["moodle_efs_mount"]) > 1
        and None in efs_by_instance
    ):
        contradictory = True
    if contradictory:
        return {
            "status": "conflicting_evidence",
            "reason": "Prometheus returned contradictory health samples for one query",
            "evidence_refs": sorted({ref for values in samples.values() for _, ref, _, _ in values}),
            "hypothesis": None,
            "confidence": 0.0,
            "action_candidate": None,
        }

    status = {query_id: next(iter(values)) for query_id, values in states.items()}
    if "moodle_efs_mount" in states and 0.0 in states["moodle_efs_mount"]:
        # This is a per-node metric: mixed values identify an unhealthy node,
        # not contradictory samples, when each value has a node identity.
        status["moodle_efs_mount"] = 0.0
    refs_by_query = {
        query_id: sorted({ref for _, ref, _, _ in values})
        for query_id, values in samples.items()
    }
    failed_efs_nodes = sorted({
        instance for value, _, _, instance in samples.get("moodle_efs_mount", [])
        if value == 0.0 and instance is not None
    })
    all_refs = sorted({ref for refs in refs_by_query.values() for ref in refs})
    if direct_rule is not None:
        query_id, cause_code = direct_rule
        if status[query_id] != 0:
            return {
                "status": "conflicting_evidence",
                "reason": "the firing probe alert conflicts with the current Prometheus sample",
                "evidence_refs": refs_by_query[query_id],
                "hypothesis": None,
                "confidence": 0.0,
                "action_candidate": None,
            }
        hypotheses = {
            "rds_path_probe_failed": "Monitor-to-RDS TCP probe is failing; Moodle-node reachability is unconfirmed",
            "public_entry_probe_failed": "Monitor-to-public-ALB Moodle probe is failing; the fault domain is not isolated",
            "efs_mount_probe_failed": "Moodle EFS mount is unavailable on the probed node set",
        }
        return {
            "status": "hypothesis",
            "cause_code": cause_code,
            "reason": "the firing alert is confirmed by its fresh Prometheus probe; causality is not proven",
            "evidence_refs": refs_by_query[query_id],
            "hypothesis": hypotheses[cause_code],
            "confidence": 0.65,
            **({"affected_nodes": failed_efs_nodes} if cause_code == "efs_mount_probe_failed" and failed_efs_nodes else {}),
            "action_candidate": None,
        }

    if threshold_rule is not None:
        query_id, threshold, cause_code, hypothesis, node_scoped, inclusive = threshold_rule
        by_node: dict[str | None, set[float]] = defaultdict(set)
        for value, _, _, instance in samples[query_id]:
            by_node[instance].add(value)
        if node_scoped and any(
            instance is None or len(values) != 1
            for instance, values in by_node.items()
        ):
            return {
                "status": "conflicting_evidence",
                "reason": "per-node alert samples are missing instance identity or conflict for one node",
                "evidence_refs": refs_by_query[query_id],
                "hypothesis": None,
                "confidence": 0.0,
                "action_candidate": None,
            }
        values = [value for group in by_node.values() for value in group]
        if not node_scoped and len(set(values)) != 1:
            return {
                "status": "conflicting_evidence",
                "reason": "the alert query returned conflicting scalar threshold samples",
                "evidence_refs": refs_by_query[query_id],
                "hypothesis": None,
                "confidence": 0.0,
                "action_candidate": None,
            }
        matching = [
            value for value in values
            if (value >= threshold if inclusive else value > threshold)
        ]
        if not matching:
            return {
                "status": "conflicting_evidence",
                "reason": "the firing threshold alert conflicts with the current metric sample",
                "evidence_refs": refs_by_query[query_id],
                "hypothesis": None,
                "confidence": 0.0,
                "action_candidate": None,
            }
        result = {
            "status": "hypothesis",
            "cause_code": cause_code,
            "reason": f"fresh Prometheus sample confirms value above alert threshold {threshold:g}",
            "evidence_refs": refs_by_query[query_id],
            "hypothesis": hypothesis,
            "confidence": 0.6,
            "action_candidate": None,
        }
        if node_scoped:
            result["affected_nodes"] = sorted(
                instance for instance, group in by_node.items()
                if instance is not None and any(
                    value >= threshold if inclusive else value > threshold
                    for value in group
                )
            )
        return result

    if direct_node_rule is not None:
        query_id, cause_code, hypothesis, reason = direct_node_rule
        node_samples = samples[query_id]
        by_node: dict[str | None, set[float]] = defaultdict(set)
        for value, _, _, instance in node_samples:
            by_node[instance].add(value)
        if any(instance is None or len(values) != 1 for instance, values in by_node.items()):
            return {
                "status": "conflicting_evidence",
                "reason": "node-local RDS probe samples are missing instance identity or conflict for one node",
                "evidence_refs": refs_by_query[query_id],
                "hypothesis": None,
                "confidence": 0.0,
                "action_candidate": None,
            }
        failed_nodes = sorted(instance for instance, values in by_node.items() if 0.0 in values)
        if not failed_nodes:
            return {
                "status": "conflicting_evidence",
                "reason": "the firing node-local RDS alert conflicts with current node probes",
                "evidence_refs": refs_by_query[query_id],
                "hypothesis": None,
                "confidence": 0.0,
                "action_candidate": None,
            }
        return {
            "status": "hypothesis",
            "cause_code": cause_code,
            "reason": reason,
            "evidence_refs": refs_by_query[query_id],
            "hypothesis": hypothesis,
            "affected_nodes": failed_nodes,
            "confidence": 0.65,
            "action_candidate": None,
        }

    if status["moodle_synthetic_success"] != 0:
        return {
            "status": "conflicting_evidence",
            "reason": "the firing synthetic-failure alert conflicts with the current synthetic metric",
            "evidence_refs": all_refs,
            "hypothesis": None,
            "confidence": 0.0,
            "action_candidate": None,
        }

    failed_dependencies = [
        (query_id, label)
        for query_id, label in (
            ("moodle_rds_tcp_probe", "Monitor-to-RDS TCP probe is failing; Moodle-node reachability is unconfirmed"),
            ("moodle_public_probe", "Monitor-to-public-ALB Moodle probe is failing; the fault domain is not isolated"),
            ("moodle_efs_mount", "Moodle EFS mount is unavailable on the probed node set"),
        )
        if status[query_id] == 0
    ]
    if len(failed_dependencies) == 1:
        query_id, hypothesis = failed_dependencies[0]
        supporting_refs = sorted(set(refs_by_query["moodle_synthetic_success"] + refs_by_query[query_id]))
        cause_code = {
            "moodle_rds_tcp_probe": "rds_path_probe_failed",
            "moodle_public_probe": "public_entry_probe_failed",
            "moodle_efs_mount": "efs_mount_probe_failed",
        }[query_id]
        result = {
            "status": "hypothesis",
            "cause_code": cause_code,
            "reason": "one independent dependency probe is failing alongside the synthetic transaction; causality is not proven",
            "evidence_refs": supporting_refs,
            "hypothesis": hypothesis,
            "confidence": 0.65,
            **({"affected_nodes": failed_efs_nodes} if query_id == "moodle_efs_mount" and failed_efs_nodes else {}),
        }
    elif len(failed_dependencies) > 1:
        result = {
            "status": "insufficient_evidence",
            "reason": "multiple dependency probes are failing; causal order is not established",
            "evidence_refs": all_refs,
            "hypothesis": None,
            "confidence": 0.0,
        }
    else:
        result = {
            "status": "hypothesis",
            "cause_code": "application_path_not_isolated",
            "reason": "dependency probes pass while the synthetic transaction fails",
            "evidence_refs": sorted(set(refs_by_query["moodle_synthetic_success"] + [
                ref for query_id in _REQUIRED_QUERY_IDS[1:] for ref in refs_by_query[query_id]
            ])),
            "hypothesis": "Moodle application transaction path is failing; internal cause is not isolated",
            "confidence": 0.55,
        }
    result["action_candidate"] = None
    return result
