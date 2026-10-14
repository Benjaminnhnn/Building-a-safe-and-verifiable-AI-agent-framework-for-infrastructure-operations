from pathlib import Path


MOODLE_ALERT_RULES = (
    Path(__file__).resolve().parents[2] / "ansible" / "config" / "moodle_alert_rules.yml"
)


def test_moodle_web_missing_alert_uses_node_probe_even_when_cadvisor_series_disappears() -> None:
    rules = MOODLE_ALERT_RULES.read_text(encoding="utf-8")
    web_rule = rules.split("- alert: MoodleWebContainerMissing", 1)[1].split(
        "- alert: MoodleNodeRdsTcpFailed", 1
    )[0]

    assert 'moodle_node_web_running{job="moodle_node"} == 0' in web_rule
    # Runtime alerts must not carry evaluation scenario labels.
    assert "moodle_fault_scenario_active" not in web_rule
    assert "container_last_seen" not in web_rule
