"""Bounded LLM clients for evidence reasoning.

Every call is a single, tool-free, temperature-0 completion with a hard
timeout and a capped output length. Clients never receive credentials,
raw logs, or action catalogs, and their output is untrusted until the
citation validator accepts it.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_OUTPUT_TOKENS = 1024

# A small shared pool bounds concurrent model calls; a timed-out call is
# abandoned rather than awaited.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="llm-diagnosis")


@dataclass(frozen=True)
class LLMCompletion:
    text: str
    model: str
    latency_seconds: float
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None

    def usage(self) -> dict[str, Any]:
        return {
            "llm_call_count": 1,
            "model": self.model,
            "latency_seconds": round(self.latency_seconds, 4),
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
        }


class DiagnosisLLM(Protocol):
    model: str

    def complete(self, *, system_instruction: str, prompt: str) -> LLMCompletion:
        ...


class GeminiDiagnosisClient:
    """Gemini client using the pinned ``google-genai`` package."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    def _generate(self, system_instruction: str, prompt: str) -> Any:
        from google.genai import types

        return self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.0,
                max_output_tokens=self.max_output_tokens,
                response_mime_type="application/json",
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=0
                ),
            ),
        )

    def complete(self, *, system_instruction: str, prompt: str) -> LLMCompletion:
        from core.metrics import GEMINI_CALL_LATENCY_SECONDS

        started = time.perf_counter()
        outcome = "error"
        try:
            future = _EXECUTOR.submit(self._generate, system_instruction, prompt)
            response = future.result(timeout=self.timeout_seconds)
            usage = getattr(response, "usage_metadata", None)
            outcome = "success"
            return LLMCompletion(
                text=str(getattr(response, "text", "") or ""),
                model=self.model,
                latency_seconds=time.perf_counter() - started,
                prompt_tokens=getattr(usage, "prompt_token_count", None),
                output_tokens=getattr(usage, "candidates_token_count", None),
            )
        except FutureTimeout:
            return LLMCompletion("", self.model, time.perf_counter() - started, error="timeout")
        except Exception as exc:  # provider errors must not break the pipeline
            return LLMCompletion("", self.model, time.perf_counter() - started, error=type(exc).__name__)
        finally:
            GEMINI_CALL_LATENCY_SECONDS.labels(operation="evidence_diagnosis", outcome=outcome).observe(
                time.perf_counter() - started
            )


class ScriptedDiagnosisLLM:
    """Deterministic client for tests and offline replay; responses are fixtures, not model output."""

    def __init__(self, responder: Callable[[str], str] | str, *, model: str = "scripted") -> None:
        self._responder = responder
        self.model = model
        self.calls: list[str] = []

    def complete(self, *, system_instruction: str, prompt: str) -> LLMCompletion:
        self.calls.append(prompt)
        text = self._responder(prompt) if callable(self._responder) else self._responder
        return LLMCompletion(text=text, model=self.model, latency_seconds=0.0)


def valid_env_value(value: str | None) -> str | None:
    """Return a stripped env value, treating template placeholders as unset."""
    if not value or not value.strip():
        return None
    value = value.strip()
    if any(marker in value for marker in ("your_", "change_me", "_here")):
        return None
    return value


def build_default_client() -> DiagnosisLLM | None:
    """Return the configured Gemini client, or None when no key is present."""
    api_key = valid_env_value(os.getenv("GEMINI_API_KEY"))
    if not api_key:
        return None
    model = (
        valid_env_value(os.getenv("AIOPS_LLM_DIAGNOSIS_MODEL"))
        or valid_env_value(os.getenv("GEMINI_MODEL"))
        or "gemini-2.5-flash"
    )
    try:
        timeout = float(os.getenv("AIOPS_LLM_DIAGNOSIS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = min(max(timeout, 1.0), 60.0)
    return GeminiDiagnosisClient(api_key=api_key, model=model, timeout_seconds=timeout)
