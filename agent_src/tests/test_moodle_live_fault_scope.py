"""Safety boundary tests for staging Moodle fault injection and reset."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
INJECTOR = REPO_ROOT / "automation" / "moodle-fault-inject.sh"
RESET = REPO_ROOT / "automation" / "moodle-fault-reset.sh"
LIVE_SCENARIOS = {"DB-01", "DB-02", "DB-03", "RES-01", "RES-02", "RES-03", "NET-01", "NET-02", "NET-03", "CON-01", "CON-02", "CON-03", "SEC-02", "SEC-03"}
OFFLINE_SCENARIOS = {
    "SEC-01",
}


def _bash_command(script: Path) -> list[str]:
    if os.name != "nt":
        return ["bash", str(script)]
    git_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
    bash = git_root / "bin" / "bash.exe"
    cygpath = git_root / "usr" / "bin" / "cygpath.exe"
    if bash.is_file() and cygpath.is_file():
        converted = subprocess.run(
            [str(cygpath), "-u", str(script)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return [str(bash), converted]
    return ["bash", str(script)]


def _run_script(script: Path, scenario: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("MOODLE_FAULT_CONFIRM", None)
    return subprocess.run(
        [*_bash_command(script), scenario],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


@pytest.mark.parametrize("scenario", sorted(OFFLINE_SCENARIOS))
def test_unreviewed_scenarios_are_refused_before_live_access(scenario: str) -> None:
    result = _run_script(INJECTOR, scenario)

    assert result.returncode == 77
    assert "no reviewed injector/reset pair is available" in result.stderr
    assert "Set MOODLE_FAULT_CONFIRM=staging" not in result.stderr


@pytest.mark.parametrize("scenario", sorted(LIVE_SCENARIOS))
def test_reviewed_scenarios_still_require_explicit_staging_confirmation(
    scenario: str,
) -> None:
    result = _run_script(INJECTOR, scenario)

    assert result.returncode == 77
    assert "Set MOODLE_FAULT_CONFIRM=staging" in result.stderr


@pytest.mark.parametrize("scenario", sorted(OFFLINE_SCENARIOS))
def test_unreviewed_reset_is_refused_before_live_access(scenario: str) -> None:
    result = _run_script(RESET, scenario)

    assert result.returncode == 77
    assert "injector/reset pair has not been reviewed" in result.stderr


def test_obsolete_unsafe_fault_implementations_are_absent() -> None:
    source = INJECTOR.read_text(encoding="utf-8") + RESET.read_text(encoding="utf-8")
    for obsolete in (
        "sudo tc qdisc add dev eth0 root",
        "MOODLE_REVERSEPROXY",
        "disk-fill-res-03.dat",
        "sh -c 'tail -f /dev/null'",
        "sudo iptables -I INPUT 1 -p tcp --dport 5432",
    ):
        assert obsolete not in source
