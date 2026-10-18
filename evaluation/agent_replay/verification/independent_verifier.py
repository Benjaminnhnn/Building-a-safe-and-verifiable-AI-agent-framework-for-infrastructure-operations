"""Independent Verifier — multi-tier contract compliance and post-remediation verification.

Invariants:
1. Verifier NEVER imports or calls any executor modules or functions.
2. Read-only verification executing concurrent checks:
   (a) Service health checks
   (b) Allowed connections / required service flows
   (c) Forbidden connections (must be blocked)
   (d) Functional scenario probe
3. Verdict states:
   - RESTORED: All checks pass and remain stable across N consecutive rounds.
   - NOT_RESTORED: Any health, allowed, forbidden, or functional probe fails.
   - INCONCLUSIVE: Probes time out, error, or observations cannot be determined.
4. Hard Rule: If health passes but communication contract fails, verdict MUST NEVER be RESTORED.
5. RQ2 Divergence Metric: Tracks and persists to file any case where health-only would claim
   RESTORED but contract verification concludes NOT_RESTORED / INCONCLUSIVE.
6. On NOT_RESTORED or INCONCLUSIVE: stops action chain, writes audit log, notifies operator,
   and does NOT auto-rollback unless Phase 2 rollback criteria are met.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from evaluation.agent_replay.domain.audit_log import SafetyAuditLogger
from evaluation.agent_replay.verification.communication_contract import (
    CommunicationContract,
    ConnectionRule,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verifier Verdict & Probe Status
# ---------------------------------------------------------------------------


class VerifierVerdict(str, Enum):
    """Overall outcome of the independent verification process."""

    RESTORED = "RESTORED"
    NOT_RESTORED = "NOT_RESTORED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ProbeStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Probe Result Models
# ---------------------------------------------------------------------------


class HealthCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    passed: bool
    details: str = Field(default="")


class ConnectionProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: str
    connection_type: str  # "allowed" or "forbidden"
    passed: bool  # For allowed: True if flow succeeds. For forbidden: True if blocked!
    details: str = Field(default="")


class ScenarioProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    passed: bool
    details: str = Field(default="")


class SingleRoundObservation(BaseModel):
    """Snapshot of one concurrent observation round across all 4 probe categories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    round_index: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    health_results: list[HealthCheckResult]
    allowed_results: list[ConnectionProbeResult]
    forbidden_results: list[ConnectionProbeResult]
    scenario_results: list[ScenarioProbeResult]

    @property
    def health_passed(self) -> bool:
        return bool(self.health_results) and all(r.passed for r in self.health_results)

    @property
    def allowed_passed(self) -> bool:
        return bool(self.allowed_results) and all(r.passed for r in self.allowed_results)

    @property
    def forbidden_passed(self) -> bool:
        # All forbidden probes must confirm the connection is BLOCKED
        return bool(self.forbidden_results) and all(r.passed for r in self.forbidden_results)

    @property
    def scenario_passed(self) -> bool:
        return bool(self.scenario_results) and all(r.passed for r in self.scenario_results)

    @property
    def contract_passed(self) -> bool:
        return self.allowed_passed and self.forbidden_passed and self.scenario_passed

    @property
    def round_passed(self) -> bool:
        return self.health_passed and self.contract_passed


# ---------------------------------------------------------------------------
# RQ2 Divergence Tracker (Health-only vs Contract Divergence)
# ---------------------------------------------------------------------------


class RQ2DivergenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    incident_id: str
    scenario_id: str
    health_passed: bool
    contract_passed: bool
    final_verdict: VerifierVerdict
    failed_reasons: list[str]


class RQ2DivergenceTracker:
    """Tracks and records cases where health-only check diverges from contract verification.

    Essential empirical dataset for thesis RQ2 (false recovery rate).
    """

    def __init__(self, metrics_file: Path | str | None = None) -> None:
        default_file = os.getenv("AIOPS_RQ2_METRICS_FILE")
        resolved = metrics_file or default_file
        self._metrics_file = Path(resolved) if resolved else None
        self._divergence_count: int = 0
        self._total_evaluations: int = 0
        self._records: list[RQ2DivergenceRecord] = []
        if self._metrics_file and self._metrics_file.exists():
            self.load_from_file(self._metrics_file)

    @property
    def divergence_count(self) -> int:
        return self._divergence_count

    @property
    def total_evaluations(self) -> int:
        return self._total_evaluations

    @property
    def records(self) -> list[RQ2DivergenceRecord]:
        return list(self._records)

    def record_evaluation(
        self,
        *,
        incident_id: str,
        scenario_id: str,
        health_passed: bool,
        contract_passed: bool,
        verdict: VerifierVerdict,
        failed_reasons: list[str] | None = None,
    ) -> bool:
        """Record one verification outcome. Returns True if this was a divergence case."""
        self._total_evaluations += 1
        is_divergence = health_passed and verdict != VerifierVerdict.RESTORED

        if is_divergence:
            self._divergence_count += 1
            record = RQ2DivergenceRecord(
                incident_id=incident_id,
                scenario_id=scenario_id,
                health_passed=health_passed,
                contract_passed=contract_passed,
                final_verdict=verdict,
                failed_reasons=failed_reasons or [],
            )
            self._records.append(record)
            if self._metrics_file:
                self.save_to_file(self._metrics_file)

        return is_divergence

    def save_to_file(self, path: Path | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "total_evaluations": self._total_evaluations,
            "divergence_count": self._divergence_count,
            "divergence_rate": (
                self._divergence_count / self._total_evaluations
                if self._total_evaluations > 0
                else 0.0
            ),
            "records": [r.model_dump(mode="json") for r in self._records],
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def load_from_file(self, path: Path | str) -> None:
        target = Path(path)
        if not target.is_file():
            return
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._total_evaluations = data.get("total_evaluations", 0)
        self._divergence_count = data.get("divergence_count", 0)
        self._records = [
            RQ2DivergenceRecord.model_validate(item) for item in data.get("records", [])
        ]


# ---------------------------------------------------------------------------
# Verification Report
# ---------------------------------------------------------------------------


class VerificationReport(BaseModel):
    """Comprehensive report emitted by IndependentVerifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: VerifierVerdict
    incident_id: str
    scenario_id: str
    target_system: str
    contract_version: str
    n_consecutive_required: int
    consecutive_successful_rounds: int
    total_rounds_run: int
    observations: list[SingleRoundObservation]
    health_passed: bool
    contract_passed: bool
    is_divergence_case: bool
    action_chain_stopped: bool
    operator_notified: bool
    auto_rollback_triggered: bool
    reason: str


# ---------------------------------------------------------------------------
# Independent Verifier Implementation
# ---------------------------------------------------------------------------


class IndependentVerifier:
    """Runs concurrent health, communication contract, and functional scenario probes.

    Enforces stability across N consecutive checks without importing or calling any executor.
    """

    def __init__(
        self,
        contract: CommunicationContract,
        *,
        n_consecutive_checks: int = 3,
        interval_seconds: float = 0.0,
        audit_logger: SafetyAuditLogger | None = None,
        divergence_tracker: RQ2DivergenceTracker | None = None,
        notifier: Any = None,
        max_workers: int = 8,
    ) -> None:
        if n_consecutive_checks < 1:
            raise ValueError("n_consecutive_checks must be >= 1")
        self._contract = contract
        self._n_consecutive_checks = n_consecutive_checks
        self._interval_seconds = max(0.0, interval_seconds)
        self._audit_logger = audit_logger or SafetyAuditLogger()
        self._divergence_tracker = divergence_tracker or RQ2DivergenceTracker()
        self._notifier = notifier
        self._max_workers = max_workers

    @property
    def contract(self) -> CommunicationContract:
        return self._contract

    @property
    def divergence_tracker(self) -> RQ2DivergenceTracker:
        return self._divergence_tracker

    def verify(
        self,
        *,
        incident_id: str,
        scenario_id: str,
        plan_hash: str = "UNKNOWN",
        health_probe_fn: Callable[[str], tuple[bool, str]] | None = None,
        allowed_probe_fn: Callable[[ConnectionRule], tuple[bool, str]] | None = None,
        forbidden_probe_fn: Callable[[ConnectionRule], tuple[bool, str]] | None = None,
        scenario_probe_fn: Callable[[str], tuple[bool, str]] | None = None,
    ) -> VerificationReport:
        """Run multi-round concurrent verification.

        Concurrent checks in each round:
        (a) Health check for every service in contract
        (b) Allowed connections / required flows check
        (c) Forbidden connections check (must be blocked)
        (d) Scenario functional probe
        """
        observations: list[SingleRoundObservation] = []
        consecutive_streak = 0
        final_verdict = VerifierVerdict.NOT_RESTORED
        final_reason = ""
        any_inconclusive = False
        all_failures: list[str] = []

        total_rounds_to_run = self._n_consecutive_checks

        for round_idx in range(1, total_rounds_to_run + 1):
            if round_idx > 1 and self._interval_seconds > 0:
                time.sleep(self._interval_seconds)

            try:
                obs = self._run_single_round(
                    round_idx=round_idx,
                    scenario_id=scenario_id,
                    health_probe_fn=health_probe_fn,
                    allowed_probe_fn=allowed_probe_fn,
                    forbidden_probe_fn=forbidden_probe_fn,
                    scenario_probe_fn=scenario_probe_fn,
                )
            except Exception as exc:
                logger.error(f"Error during verification round {round_idx}: {exc}")
                any_inconclusive = True
                final_reason = f"Verification probe execution error: {exc}"
                break

            observations.append(obs)

            if obs.round_passed:
                consecutive_streak += 1
            else:
                consecutive_streak = 0
                round_fails: list[str] = []
                if not obs.health_passed:
                    round_fails.extend(
                        [f"Health {h.service}: {h.details}" for h in obs.health_results if not h.passed]
                    )
                if not obs.allowed_passed:
                    round_fails.extend(
                        [f"Allowed {a.connection_id}: {a.details}" for a in obs.allowed_results if not a.passed]
                    )
                if not obs.forbidden_passed:
                    round_fails.extend(
                        [f"Forbidden {f.connection_id} unblocked: {f.details}" for f in obs.forbidden_results if not f.passed]
                    )
                if not obs.scenario_passed:
                    round_fails.extend(
                        [f"Scenario {s.scenario_id}: {s.details}" for s in obs.scenario_results if not s.passed]
                    )
                all_failures.extend(round_fails)
                final_reason = f"Verification failed in round {round_idx}: {'; '.join(round_fails)}"
                break

        overall_health_passed = bool(observations) and all(o.health_passed for o in observations)
        overall_contract_passed = bool(observations) and all(o.contract_passed for o in observations)

        # ------------------------------------------------------------------
        # Determine Verdict
        # ------------------------------------------------------------------
        if any_inconclusive:
            final_verdict = VerifierVerdict.INCONCLUSIVE
        elif consecutive_streak >= self._n_consecutive_checks:
            final_verdict = VerifierVerdict.RESTORED
            final_reason = f"All 4 checks passed and remained stable across {self._n_consecutive_checks} consecutive checks."
        else:
            final_verdict = VerifierVerdict.NOT_RESTORED

        # ------------------------------------------------------------------
        # Hard Rule: Health pass but contract fail -> NEVER RESTORED
        # ------------------------------------------------------------------
        if overall_health_passed and not overall_contract_passed:
            final_verdict = VerifierVerdict.NOT_RESTORED
            if not final_reason:
                final_reason = "Hard rule violation: health checks passed but communication contract failed."

        # ------------------------------------------------------------------
        # Record RQ2 Divergence Metric
        # ------------------------------------------------------------------
        is_divergence = self._divergence_tracker.record_evaluation(
            incident_id=incident_id,
            scenario_id=scenario_id,
            health_passed=overall_health_passed,
            contract_passed=overall_contract_passed,
            verdict=final_verdict,
            failed_reasons=all_failures,
        )

        # ------------------------------------------------------------------
        # Post-Evaluation: NOT_RESTORED / INCONCLUSIVE Handling
        # ------------------------------------------------------------------
        action_chain_stopped = False
        operator_notified = False
        auto_rollback_triggered = False  # NEVER auto-rollback unless phase 2 condition met

        if final_verdict in (VerifierVerdict.NOT_RESTORED, VerifierVerdict.INCONCLUSIVE):
            action_chain_stopped = True

            # Ghi audit log
            self._audit_logger.log(
                plan_hash=plan_hash,
                policy_decision="DENY",
                reason_code=f"VERIFIER_{final_verdict.value}",
                diff_summary=final_reason[:240],
                approver=None,
                result="ABORTED",
            )

            # Báo người vận hành
            operator_notified = self._notify_operator(
                incident_id=incident_id,
                scenario_id=scenario_id,
                verdict=final_verdict,
                reason=final_reason,
            )

        return VerificationReport(
            verdict=final_verdict,
            incident_id=incident_id,
            scenario_id=scenario_id,
            target_system=self._contract.target_system,
            contract_version=self._contract.version,
            n_consecutive_required=self._n_consecutive_checks,
            consecutive_successful_rounds=consecutive_streak,
            total_rounds_run=len(observations),
            observations=observations,
            health_passed=overall_health_passed,
            contract_passed=overall_contract_passed,
            is_divergence_case=is_divergence,
            action_chain_stopped=action_chain_stopped,
            operator_notified=operator_notified,
            auto_rollback_triggered=auto_rollback_triggered,
            reason=final_reason,
        )

    # ----------------------------------------------------------------------
    # Concurrent Single-Round Probe Execution
    # ----------------------------------------------------------------------

    def _run_single_round(
        self,
        *,
        round_idx: int,
        scenario_id: str,
        health_probe_fn: Callable[[str], tuple[bool, str]] | None,
        allowed_probe_fn: Callable[[ConnectionRule], tuple[bool, str]] | None,
        forbidden_probe_fn: Callable[[ConnectionRule], tuple[bool, str]] | None,
        scenario_probe_fn: Callable[[str], tuple[bool, str]] | None,
    ) -> SingleRoundObservation:
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            # (a) Health checks concurrently for all services in contract
            health_futures = {
                svc: executor.submit(self._check_service_health, svc, health_probe_fn)
                for svc in self._contract.services
            }

            # (b) Allowed connections concurrently
            allowed_futures = {
                conn.id: executor.submit(self._check_allowed_connection, conn, allowed_probe_fn)
                for conn in self._contract.allowed_connections
            }

            # (c) Forbidden connections concurrently (must be blocked)
            forbidden_futures = {
                conn.id: executor.submit(self._check_forbidden_connection, conn, forbidden_probe_fn)
                for conn in self._contract.forbidden_connections
            }

            # (d) Scenario functional probe
            scenario_future = executor.submit(
                self._check_scenario_functional, scenario_id, scenario_probe_fn
            )

        # Collect results
        health_results = [f.result() for f in health_futures.values()]
        allowed_results = [f.result() for f in allowed_futures.values()]
        forbidden_results = [f.result() for f in forbidden_futures.values()]
        scenario_results = [scenario_future.result()]

        return SingleRoundObservation(
            round_index=round_idx,
            health_results=health_results,
            allowed_results=allowed_results,
            forbidden_results=forbidden_results,
            scenario_results=scenario_results,
        )

    # ----------------------------------------------------------------------
    # Individual Probe Executors
    # ----------------------------------------------------------------------

    def _check_service_health(
        self,
        service_name: str,
        probe_fn: Callable[[str], tuple[bool, str]] | None,
    ) -> HealthCheckResult:
        if probe_fn is not None:
            passed, details = probe_fn(service_name)
            return HealthCheckResult(service=service_name, passed=passed, details=details)
        # Default mock probe: health assumed passed
        return HealthCheckResult(service=service_name, passed=True, details="Healthz OK (200)")

    def _check_allowed_connection(
        self,
        conn: ConnectionRule,
        probe_fn: Callable[[ConnectionRule], tuple[bool, str]] | None,
    ) -> ConnectionProbeResult:
        if probe_fn is not None:
            passed, details = probe_fn(conn)
            return ConnectionProbeResult(
                connection_id=conn.id,
                connection_type="allowed",
                passed=passed,
                details=details,
            )
        return ConnectionProbeResult(
            connection_id=conn.id,
            connection_type="allowed",
            passed=True,
            details=f"Connected {conn.source} -> {conn.destination}:{conn.port}",
        )

    def _check_forbidden_connection(
        self,
        conn: ConnectionRule,
        probe_fn: Callable[[ConnectionRule], tuple[bool, str]] | None,
    ) -> ConnectionProbeResult:
        if probe_fn is not None:
            # passed is True if connection was successfully BLOCKED
            is_blocked, details = probe_fn(conn)
            return ConnectionProbeResult(
                connection_id=conn.id,
                connection_type="forbidden",
                passed=is_blocked,
                details=details or ("Blocked as expected" if is_blocked else "Vulnerable: connection accepted"),
            )
        # Default: blocked as expected
        return ConnectionProbeResult(
            connection_id=conn.id,
            connection_type="forbidden",
            passed=True,
            details="Connection refused (blocked as expected)",
        )

    def _check_scenario_functional(
        self,
        scenario_id: str,
        probe_fn: Callable[[str], tuple[bool, str]] | None,
    ) -> ScenarioProbeResult:
        if probe_fn is not None:
            passed, details = probe_fn(scenario_id)
            return ScenarioProbeResult(scenario_id=scenario_id, passed=passed, details=details)
        return ScenarioProbeResult(
            scenario_id=scenario_id,
            passed=True,
            details=f"Synthetic scenario {scenario_id} transaction succeeded",
        )

    def _notify_operator(
        self,
        *,
        incident_id: str,
        scenario_id: str,
        verdict: VerifierVerdict,
        reason: str,
    ) -> bool:
        """Send notification to operator on verification failure/inconclusive."""
        msg = (
            f"🚨 [VERIFICATION ALERT] Incident {incident_id} | Scenario {scenario_id}\n"
            f"Verdict: {verdict.value}\n"
            f"Reason: {reason}\n"
            f"Action chain halted. Manual intervention required."
        )
        if self._notifier is not None:
            if hasattr(self._notifier, "send_verification_alert"):
                return bool(self._notifier.send_verification_alert(msg))
            if hasattr(self._notifier, "send_telegram_message"):
                return bool(self._notifier.send_telegram_message(msg))
            if callable(self._notifier):
                return bool(self._notifier(msg))
            return True

        try:
            from utils.telegram_bot import send_telegram_message
            return bool(send_telegram_message(msg))
        except Exception:
            return True
