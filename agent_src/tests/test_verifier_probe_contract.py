"""Live verifier contracts must equal the checks the probe script emits.

``automation/moodle-independent-verifier.py`` rejects a sample whose check ids
differ from the contract, so a renamed check makes every verification fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS

PROBE = Path(__file__).resolve().parents[2] / "automation" / "moodle-scenario-verify.sh"
COMMON_CHECKS = {"common_synthetic_transaction", "scenario_alert_resolved", "internet_to_postgresql_blocked"}


def _emitted_checks(scenario_id: str) -> set[str]:
    script = PROBE.read_text(encoding="utf-8")
    body = script.split('case "$scenario" in\n  DB-01)\n    for host', 1)[1]
    block = re.search(rf"^  {re.escape(scenario_id)}\)\n(.*?)^    ;;", "  DB-01)\n    for host" + body, re.MULTILINE | re.DOTALL)
    assert block is not None, f"{scenario_id} has no probe block"
    text = block.group(1)
    hosts = re.findall(r"for host in ([a-z0-9 -]+); do", text)
    checks: set[str] = set()
    for check_id in re.findall(r'run_check "?([A-Za-z0-9_${}-]+)"?', text):
        if "${host}" in check_id:
            checks |= {check_id.replace("${host}", host) for host in hosts[0].split()}
        else:
            checks.add(check_id)
    return checks | COMMON_CHECKS


@pytest.mark.parametrize("scenario_id", sorted(MOODLE_LIVE_CONTRACTS))
def test_contract_equals_emitted_probe_checks(scenario_id: str) -> None:
    contract = MOODLE_LIVE_CONTRACTS[scenario_id]
    required = set(contract["allowed"]) | set(contract["forbidden"]) | set(contract["related"])
    assert required == _emitted_checks(scenario_id)


def test_sec01_has_no_agent_verification_contract() -> None:
    assert "SEC-01" not in MOODLE_LIVE_CONTRACTS
