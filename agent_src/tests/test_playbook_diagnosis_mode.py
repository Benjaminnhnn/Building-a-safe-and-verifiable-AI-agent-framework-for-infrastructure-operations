"""The deployed Agent runs hybrid diagnosis without any key in the playbook."""

from __future__ import annotations

import re
from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[2] / "ansible" / "playbooks" / "configure-moodle-monitoring.yml"


def _text() -> str:
    return PLAYBOOK.read_text(encoding="utf-8")


def test_agent_and_worker_both_run_the_overridable_hybrid_mode() -> None:
    # The Celery worker runs the pipeline, so it needs the mode as much as the API.
    lines = re.findall(r"^\s+AIOPS_DIAGNOSIS_MODE: (.+)$", _text(), flags=re.MULTILINE)
    assert lines == ['"{{ moodle_diagnosis_mode | default(\'hybrid\') }}"'] * 2


def test_unsupported_modes_fail_the_deployment() -> None:
    text = _text()
    assert "moodle_diagnosis_mode | default('hybrid') in ['rules', 'hybrid', 'llm']" in text
    assert "llm_unvalidated" not in re.search(r"in \['rules'[^\]]*\]", text).group(0)


def test_no_model_credential_is_written_into_the_playbook() -> None:
    assert not re.search(r"GEMINI_API_KEY|AIza[0-9A-Za-z_-]{20,}", _text())
