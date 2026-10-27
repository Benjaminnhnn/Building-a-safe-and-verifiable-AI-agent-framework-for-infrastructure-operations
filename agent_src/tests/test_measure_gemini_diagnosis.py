"""Guards and wiring of the real-Gemini measurement script, without calling the API."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.reasoning.llm_client import LLMCompletion, ScriptedDiagnosisLLM

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("measure_gemini", ROOT / "automation" / "measure-gemini-diagnosis.py")
assert spec and spec.loader
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


class _Client:
    """A non-scripted client whose answer never validates, so nothing is falsely diagnosed."""

    model = "fake-real-model"

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls = 0

    def complete(self, *, system_instruction: str, prompt: str) -> LLMCompletion:
        self.calls += 1
        return LLMCompletion("not json", self.model, 0.01, 10, 2, error=self.error)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(measure.CONFIRM_VARIABLE, raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _forbid_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_path: Any) -> None:
        raise AssertionError("the client must not be built before confirmation")

    monkeypatch.setattr(measure, "load_client", boom)


def test_nothing_is_sent_without_the_confirmation_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_client(monkeypatch)
    assert measure.main(["--repeats", "1"]) == 3


def test_planned_calls_above_the_cap_are_refused_before_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(measure.CONFIRM_VARIABLE, measure.CONFIRM_VALUE)
    _forbid_client(monkeypatch)
    assert measure.main(["--repeats", "1", "--max-calls", "5"]) == 64


@pytest.mark.parametrize("argv", [["--repeats", "0"], ["--repeats", "11"], ["--usd-per-mtok-in", "1"]])
def test_invalid_arguments_are_rejected(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_client(monkeypatch)
    assert measure.main(argv) == 64


def test_confirmed_run_without_a_usable_key_sends_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(measure.CONFIRM_VARIABLE, measure.CONFIRM_VALUE)
    placeholder = tmp_path / "key"
    placeholder.write_text("your_gemini_api_key_here\n", encoding="utf-8")
    assert measure.main(["--repeats", "1", "--api-key-file", str(placeholder)]) == 2
    assert measure.main(["--repeats", "1"]) == 2


def test_call_plan_matches_what_a_run_actually_sends() -> None:
    client = _Client()
    args = SimpleNamespace(repeats=1, usd_per_mtok_in=None, usd_per_mtok_out=None, skip_robustness=False)
    measure.measure(args, client)
    assert client.calls == measure.plan_calls(1, skip_robustness=False)


def test_scripted_client_is_never_reported_as_a_model_measurement() -> None:
    args = SimpleNamespace(repeats=1, usd_per_mtok_in=None, usd_per_mtok_out=None, skip_robustness=True)
    report = measure.measure(args, ScriptedDiagnosisLLM("{}"))
    assert report["simulated"] is True and report["valid_model_measurement"] is False
    assert "WARNING" in measure.render_summary(report)


def test_provider_errors_invalidate_the_measurement() -> None:
    args = SimpleNamespace(repeats=1, usd_per_mtok_in=None, usd_per_mtok_out=None, skip_robustness=True)
    report = measure.measure(args, _Client(error="timeout"))
    assert report["provider_errors"] > 0 and report["valid_model_measurement"] is False


def test_confirmed_run_writes_a_report_with_provenance_and_cost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(measure.CONFIRM_VARIABLE, measure.CONFIRM_VALUE)
    client = _Client()
    monkeypatch.setattr(measure, "load_client", lambda _path: client)
    code = measure.main([
        "--repeats", "2", "--skip-robustness", "--output-dir", str(tmp_path),
        "--usd-per-mtok-in", "0.5", "--usd-per-mtok-out", "2",
    ])
    assert code == 0
    (report_path,) = tmp_path.glob("gemini-diagnosis-*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["simulated"] is False and report["valid_model_measurement"] is True
    assert len(report["provenance"]["system_instruction_sha256"]) == 64
    assert report["accuracy"]["summary"]["llm"]["cost"]["estimated_usd"] is not None
    assert report["accuracy"]["summary"]["rules"]["cost"]["llm_call_count"] == 0
    assert "stability" in report["accuracy"]
