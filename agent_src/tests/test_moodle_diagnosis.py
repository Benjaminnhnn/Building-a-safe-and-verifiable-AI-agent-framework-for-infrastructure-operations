from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.moodle_diagnosis import diagnose_moodle_alert
from core.evidence_collectors import METRIC_QUERIES_BY_ALERT
from core.moodle_diagnosis import (
    _ALL_QUERY_IDS,
    _DIRECT_NODE_RULES,
    _DIRECT_PROBE_RULES,
    _DIRECT_THRESHOLD_RULES,
)
import yaml


def _evidence(query_id: str, value: float, evidence_id: str, *, instance=None):
    return SimpleNamespace(
        source="prometheus",
        evidence_id=evidence_id,
        collected_at=datetime.now(timezone.utc),
        metadata={"payload_json": json.dumps({
            "query_id": query_id,
            "sample_value": value,
            "labels": {"instance": instance} if instance else {},
        })},
    )


def _complete_observations(*, synthetic=0, rds=1, public=1, efs=1):
    return [
        _evidence("moodle_synthetic_success", synthetic, "ev-synthetic"),
        _evidence("moodle_rds_tcp_probe", rds, "ev-rds"),
        _evidence("moodle_public_probe", public, "ev-public"),
        _evidence("moodle_efs_mount", efs, "ev-efs"),
    ]


def test_every_configured_moodle_alert_has_a_fixed_query_and_diagnosis_contract():
    config_path = Path(__file__).resolve().parents[2] / "ansible" / "config" / "moodle_alert_rules.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    alerts = {
        rule["alert"]
        for group in config["groups"]
        for rule in group["rules"]
    }
    diagnosis_alerts = (
        set(_DIRECT_NODE_RULES)
        | set(_DIRECT_PROBE_RULES)
        | set(_DIRECT_THRESHOLD_RULES)
        | {"MoodleSyntheticTransactionFailed"}
    )

    assert alerts == set(METRIC_QUERIES_BY_ALERT)
    assert alerts == diagnosis_alerts
    rules_by_name = {
        rule["alert"]: rule
        for group in config["groups"]
        for rule in group["rules"]
    }
    memory_rule = " ".join(rules_by_name["MoodleNodeMemoryPressure"]["expr"].split())
    memory_query = " ".join(METRIC_QUERIES_BY_ALERT["MoodleNodeMemoryPressure"][0][1].split())
    assert memory_rule == f"{memory_query} > 33554432"
    configured_query_ids = {
        query_id
        for queries in METRIC_QUERIES_BY_ALERT.values()
        for query_id, _ in queries
    }
    assert configured_query_ids <= set(_ALL_QUERY_IDS)


def test_prometheus_alerts_do_not_expose_experiment_identity():
    config_path = Path(__file__).resolve().parents[2] / "ansible" / "config" / "moodle_alert_rules.yml"
    config_text = yaml.safe_dump(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    assert "scenario_id" not in config_text
    assert "drill_id" not in config_text
    assert "moodle_fault_scenario_active" not in config_text
    assert "moodle-fault-res-02" not in config_text


def test_memory_alert_threshold_matches_live_injector_strictly():
    injector = Path(__file__).resolve().parents[2] / "automation" / "moodle-fault-inject.sh"
    assert 'test "$((before - after))" -gt 32768' in injector.read_text(encoding="utf-8")


def test_synthetic_failure_is_attributed_to_single_failing_dependency():
    result = diagnose_moodle_alert(
        "MoodleSyntheticTransactionFailed",
        _complete_observations(rds=0),
    )

    assert result["status"] == "hypothesis"
    assert result["hypothesis"] == "Monitor-to-RDS TCP probe is failing; Moodle-node reachability is unconfirmed"
    assert result["evidence_refs"] == ["ev-rds", "ev-synthetic"]
    assert result["action_candidate"] is None


def test_synthetic_failure_with_multiple_failed_dependencies_escalates():
    result = diagnose_moodle_alert(
        "MoodleSyntheticTransactionFailed",
        _complete_observations(rds=0, public=0),
    )

    assert result["status"] == "insufficient_evidence"
    assert result["hypothesis"] is None
    assert result["action_candidate"] is None


def test_missing_or_conflicting_metric_evidence_never_produces_hypothesis():
    missing = diagnose_moodle_alert(
        "MoodleSyntheticTransactionFailed",
        _complete_observations()[:-1],
    )
    conflict = diagnose_moodle_alert(
        "MoodleSyntheticTransactionFailed",
        _complete_observations() + [_evidence("moodle_public_probe", 0, "ev-public-2")],
    )

    assert missing["status"] == "insufficient_evidence"
    assert "moodle_efs_mount" in missing["missing_queries"]
    assert conflict["status"] == "conflicting_evidence"
    assert conflict["hypothesis"] is None


def test_malformed_evidence_metadata_and_non_object_payload_fail_closed():
    malformed = SimpleNamespace(
        source="prometheus",
        evidence_id="ev-malformed",
        collected_at=datetime.now(timezone.utc),
        metadata={"payload_json": "[]"},
    )
    non_object_metadata = SimpleNamespace(
        source="prometheus",
        evidence_id="ev-bad-metadata",
        collected_at=datetime.now(timezone.utc),
        metadata=None,
    )

    result = diagnose_moodle_alert(
        "MoodleRdsTcpProbeFailed", [malformed, non_object_metadata]
    )

    assert result["status"] == "insufficient_evidence"
    assert result["hypothesis"] is None
    assert result["action_candidate"] is None


def test_metric_samples_outside_one_minute_window_are_not_combined():
    evidence = _complete_observations(rds=0)
    evidence[-1].collected_at = evidence[0].collected_at + timedelta(seconds=61)

    result = diagnose_moodle_alert("MoodleSyntheticTransactionFailed", evidence)

    assert result["status"] == "insufficient_evidence"
    assert result["hypothesis"] is None


def test_old_or_future_prometheus_samples_do_not_create_hypothesis():
    old = _complete_observations(rds=0)
    old_time = datetime.now(timezone.utc) - timedelta(seconds=121)
    for item in old:
        item.collected_at = old_time
    stale = diagnose_moodle_alert("MoodleSyntheticTransactionFailed", old)

    future = _complete_observations(rds=0)
    future_time = datetime.now(timezone.utc) + timedelta(seconds=31)
    for item in future:
        item.collected_at = future_time
    skewed = diagnose_moodle_alert("MoodleSyntheticTransactionFailed", future)

    assert stale["status"] == "insufficient_evidence"
    assert skewed["status"] == "insufficient_evidence"
    assert stale["hypothesis"] is None
    assert skewed["hypothesis"] is None


def test_unreviewed_alert_has_no_diagnosis_rule():
    for alert_name in ("MoodleNodeProbeUnknown",):
        result = diagnose_moodle_alert(alert_name, _complete_observations())
        assert result["status"] == "insufficient_evidence"
        assert result["hypothesis"] is None
        assert result["action_candidate"] is None


def test_direct_dependency_alerts_require_their_own_fresh_failing_probe():
    cases = [
        ("MoodleRdsTcpProbeFailed", "moodle_rds_tcp_probe", "rds_path_probe_failed"),
        ("MoodlePublicProbeFailed", "moodle_public_probe", "public_entry_probe_failed"),
        ("MoodleEfsMountMissing", "moodle_efs_mount", "efs_mount_probe_failed"),
    ]
    for alert_name, query_id, cause_code in cases:
        result = diagnose_moodle_alert(alert_name, [_evidence(query_id, 0, f"ev-{query_id}")])
        assert result["status"] == "hypothesis"
        assert result["cause_code"] == cause_code
        assert result["evidence_refs"] == [f"ev-{query_id}"]
        assert result["action_candidate"] is None


def test_direct_dependency_alerts_escalate_when_probe_is_missing_green_or_conflicting():
    missing = diagnose_moodle_alert("MoodleRdsTcpProbeFailed", [])
    green = diagnose_moodle_alert(
        "MoodleRdsTcpProbeFailed", [_evidence("moodle_rds_tcp_probe", 1, "ev-rds")]
    )
    conflicting = diagnose_moodle_alert(
        "MoodlePublicProbeFailed",
        [
            _evidence("moodle_public_probe", 0, "ev-public-down"),
            _evidence("moodle_public_probe", 1, "ev-public-up"),
        ],
    )
    assert missing["status"] == "insufficient_evidence"
    assert green["status"] == "conflicting_evidence"
    assert conflicting["status"] == "conflicting_evidence"
    assert all(item["action_candidate"] is None for item in (missing, green, conflicting))


def test_efs_per_node_values_identify_single_failed_moodle_node():
    result = diagnose_moodle_alert(
        "MoodleEfsMountMissing",
        [
            _evidence("moodle_efs_mount", 1, "ev-efs-a", instance="moodle-app-a"),
            _evidence("moodle_efs_mount", 0, "ev-efs-b", instance="moodle-app-b"),
        ],
    )

    assert result["status"] == "hypothesis"
    assert result["cause_code"] == "efs_mount_probe_failed"
    assert result["affected_nodes"] == ["moodle-app-b"]


def test_efs_conflicting_values_for_same_node_still_escalate():
    result = diagnose_moodle_alert(
        "MoodleEfsMountMissing",
        [
            _evidence("moodle_efs_mount", 1, "ev-efs-a-healthy", instance="moodle-app-a"),
            _evidence("moodle_efs_mount", 0, "ev-efs-a-failed", instance="moodle-app-a"),
        ],
    )

    assert result["status"] == "conflicting_evidence"
    assert result["hypothesis"] is None


def test_node_rds_alert_isolated_to_failed_instance_and_identifies_affected_nodes():
    result = diagnose_moodle_alert(
        "MoodleNodeRdsTcpFailed",
        [
            _evidence("moodle_node_rds_tcp_success", 1, "ev-rds-a", instance="moodle-app-a"),
            _evidence("moodle_node_rds_tcp_success", 0, "ev-rds-b", instance="moodle-app-b"),
        ],
    )

    assert result["status"] == "hypothesis"
    assert result["cause_code"] == "node_rds_path_probe_failed"
    assert result["affected_nodes"] == ["moodle-app-b"]
    assert result["action_candidate"] is None


def test_node_rds_alert_fails_closed_for_missing_identity_or_conflicting_samples():
    missing_identity = diagnose_moodle_alert(
        "MoodleNodeRdsTcpFailed",
        [_evidence("moodle_node_rds_tcp_success", 0, "ev-rds", instance=None)],
    )
    conflict = diagnose_moodle_alert(
        "MoodleNodeRdsTcpFailed",
        [
            _evidence("moodle_node_rds_tcp_success", 0, "ev-rds-down", instance="moodle-app-a"),
            _evidence("moodle_node_rds_tcp_success", 1, "ev-rds-up", instance="moodle-app-a"),
        ],
    )
    green = diagnose_moodle_alert(
        "MoodleNodeRdsTcpFailed",
        [_evidence("moodle_node_rds_tcp_success", 1, "ev-rds-green", instance="moodle-app-a")],
    )

    assert missing_identity["status"] == "conflicting_evidence"
    assert conflict["status"] == "conflicting_evidence"
    assert green["status"] == "conflicting_evidence"
    assert all(item["action_candidate"] is None for item in (missing_identity, conflict, green))


def test_synthetic_stale_alert_requires_sample_above_rule_threshold():
    stale = diagnose_moodle_alert(
        "MoodleSyntheticTransactionStale",
        [_evidence("moodle_synthetic_last_run_age_seconds", 181, "ev-stale")],
    )
    fresh = diagnose_moodle_alert(
        "MoodleSyntheticTransactionStale",
        [_evidence("moodle_synthetic_last_run_age_seconds", 90, "ev-fresh")],
    )

    assert stale["status"] == "hypothesis"
    assert stale["cause_code"] == "synthetic_transaction_source_stale"
    assert stale["action_candidate"] is None
    assert fresh["status"] == "conflicting_evidence"


def test_node_threshold_alerts_attribute_only_confirmed_affected_instances():
    cpu = diagnose_moodle_alert(
        "MoodleNodeCpuHigh",
        [
            _evidence("moodle_node_cpu_busy_percent", 42.0, "ev-cpu-a", instance="moodle-app-a"),
            _evidence("moodle_node_cpu_busy_percent", 82.0, "ev-cpu-b", instance="moodle-app-b"),
        ],
    )
    container = diagnose_moodle_alert(
        "MoodleWebContainerMissing",
        [_evidence("moodle_web_container_last_seen_age_seconds", 45.0, "ev-container", instance="moodle-app-c")],
    )

    assert cpu["status"] == "hypothesis"
    assert cpu["affected_nodes"] == ["moodle-app-b"]
    assert cpu["action_candidate"] is None
    assert container["status"] == "hypothesis"
    assert container["affected_nodes"] == ["moodle-app-c"]
    assert container["action_candidate"] is None


def test_memory_pressure_uses_generic_container_limit_ratio_not_fixture_identity():
    result = diagnose_moodle_alert(
        "MoodleNodeMemoryPressure",
        [
            _evidence("moodle_node_memory_available_drop_bytes", 20 * 1024 * 1024, "ev-memory-a", instance="moodle-app-a"),
            _evidence("moodle_node_memory_available_drop_bytes", 40 * 1024 * 1024, "ev-memory-b", instance="moodle-app-b"),
        ],
    )

    assert result["status"] == "hypothesis"
    assert result["cause_code"] == "node_available_memory_drop"
    assert result["affected_nodes"] == ["moodle-app-b"]
    assert result["evidence_refs"] == ["ev-memory-a", "ev-memory-b"]
    assert result["action_candidate"] is None


def test_per_node_threshold_alerts_reject_missing_or_conflicting_identity():
    no_instance = diagnose_moodle_alert(
        "MoodleNodeCpuHigh",
        [_evidence("moodle_node_cpu_busy_percent", 80.0, "ev-cpu-no-instance")],
    )
    conflict = diagnose_moodle_alert(
        "MoodleNodeCpuHigh",
        [
            _evidence("moodle_node_cpu_busy_percent", 80.0, "ev-cpu-high", instance="moodle-app-a"),
            _evidence("moodle_node_cpu_busy_percent", 20.0, "ev-cpu-low", instance="moodle-app-a"),
        ],
    )

    assert no_instance["status"] == "conflicting_evidence"
    assert conflict["status"] == "conflicting_evidence"
    assert no_instance["action_candidate"] is None
    assert conflict["action_candidate"] is None


def test_exporter_and_cadvisor_alerts_only_report_missing_node_telemetry():
    cases = [
        ("MoodleNodeExporterDown", "moodle_node_up", "node_exporter_scrape_unavailable"),
        ("MoodleCAdvisorDown", "moodle_cadvisor_up", "cadvisor_scrape_unavailable"),
    ]
    for alert_name, query_id, cause_code in cases:
        result = diagnose_moodle_alert(
            alert_name,
            [
                _evidence(query_id, 1, f"ev-{query_id}-healthy", instance="moodle-app-a"),
                _evidence(query_id, 0, f"ev-{query_id}-down", instance="moodle-app-b"),
            ],
        )
        assert result["status"] == "hypothesis"
        assert result["cause_code"] == cause_code
        assert result["affected_nodes"] == ["moodle-app-b"]
        assert result["action_candidate"] is None


def test_node_probe_stale_restart_and_scratch_alerts_are_node_scoped():
    cases = [
        ("MoodleScratchEnospc", "moodle_node_scratch_enospc", 1.0, "isolated_fault_scratch_full"),
        ("MoodleWebContainerRestarting", "moodle_node_web_restart_count_2m", 2.0, "web_container_restarting"),
        ("MoodleNodeProbeStale", "moodle_node_probe_age_seconds", 121.0, "node_probe_stale"),
    ]
    for alert_name, query_id, value, cause_code in cases:
        result = diagnose_moodle_alert(
            alert_name,
            [_evidence(query_id, value, f"ev-{query_id}", instance="moodle-app-a")],
        )
        assert result["status"] == "hypothesis"
        assert result["cause_code"] == cause_code
        assert result["affected_nodes"] == ["moodle-app-a"]
        assert result["action_candidate"] is None


def test_remaining_node_alerts_produce_bounded_node_specific_hypotheses():
    cases = [
        ("MoodleNodeRdsTcpLatencyHigh", "moodle_node_rds_tcp_duration_seconds", 0.25, "node_rds_tcp_latency_high"),
        ("MoodleTrustedProxyConfigDrift", "moodle_node_reverse_proxy_enabled", 1.0, "trusted_proxy_setting_flagged_for_review"),
    ]
    for alert_name, query_id, value, cause_code in cases:
        result = diagnose_moodle_alert(
            alert_name,
            [_evidence(query_id, value, f"ev-{query_id}", instance="moodle-app-a")],
        )
        assert result["status"] == "hypothesis"
        assert result["cause_code"] == cause_code
        assert result["affected_nodes"] == ["moodle-app-a"]
        assert result["action_candidate"] is None


def test_node_router_alerts_need_current_running_web_node_and_failed_router_probe():
    for alert_name, query_id, cause_code in (
        ("MoodleApacheRouterMissing", "moodle_node_apache_router_enabled", "apache_router_signal_missing"),
        ("MoodleNodeRouterFallbackFailed", "moodle_node_router_fallback_success", "node_router_fallback_failed"),
    ):
        result = diagnose_moodle_alert(
            alert_name,
            [_evidence(query_id, 0, f"ev-{query_id}", instance="moodle-app-a")],
        )
        assert result["status"] == "hypothesis"
        assert result["cause_code"] == cause_code
        assert result["affected_nodes"] == ["moodle-app-a"]
        assert result["action_candidate"] is None


def test_synthetic_alert_accepts_efs_failure_isolated_to_one_labeled_node():
    evidence = _complete_observations()
    evidence[-1].metadata["payload_json"] = json.dumps({
        "query_id": "moodle_efs_mount",
        "sample_value": 1,
        "labels": {"instance": "moodle-app-a"},
    })
    evidence.append(_evidence("moodle_efs_mount", 0, "ev-efs-b", instance="moodle-app-b"))

    result = diagnose_moodle_alert("MoodleSyntheticTransactionFailed", evidence)

    assert result["status"] == "hypothesis"
    assert result["cause_code"] == "efs_mount_probe_failed"
    assert result["affected_nodes"] == ["moodle-app-b"]
