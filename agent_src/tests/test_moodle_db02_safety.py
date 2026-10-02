"""Static guardrails for the reviewed, shared-RDS DB-02 drill."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
HELPER = (REPO / "automation" / "moodle-db02-role-quota.sh").read_text(encoding="utf-8")
INJECT = (REPO / "automation" / "moodle-fault-inject.sh").read_text(encoding="utf-8")
RESET = (REPO / "automation" / "moodle-fault-reset.sh").read_text(encoding="utf-8")


def test_db02_preflight_requires_exact_account_snapshot_and_prior_limit() -> None:
    assert "583845872420" in HELPER
    assert "moodle-sprint6-baseline-20261001" in HELPER
    assert "[[ \"$old_limit\" == -1 ]]" in HELPER
    assert "DB-02 ownership fixture" in HELPER


def test_db02_only_changes_app_role_and_terminates_tagged_sessions() -> None:
    assert "ALTER ROLE moodle_app CONNECTION LIMIT 1" in HELPER
    assert "ALTER ROLE moodle_app CONNECTION LIMIT $old_limit" in HELPER
    assert "usename='moodle_app' AND application_name='moodle-fault-DB-02'" in HELPER
    assert "max_connections" not in HELPER
    assert "pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='moodle_app' AND application_name='moodle-fault-DB-02'" in HELPER


def test_db02_fixture_sessions_are_bounded_and_reset_uses_helper() -> None:
    assert "SELECT pg_sleep(420)" in INJECT
    assert "application_name=moodle-fault-DB-02" in INJECT
    assert 'moodle-db02-role-quota.sh" prepare' in INJECT
    assert 'moodle-db02-role-quota.sh" set-one' in INJECT
    assert 'moodle-db02-role-quota.sh" restore' in RESET
