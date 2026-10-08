"""Manual operator Standard Operating Procedure (SOP) for benchmark.

Documents the fixed decision tree a manual operator must follow.
No free-form decisions - operator must follow this exact SOP.
"""

from __future__ import annotations

from datetime import datetime
import math

from pydantic import BaseModel, ConfigDict


class SOPStep(BaseModel):
    """A single step in the Manual Operator SOP."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    description: str
    action: str
    expected_duration_seconds: int
    decision_point: bool = False
    branches: dict[str, str] = {}  # condition -> next_step_id or "escalate"


class ManualSOP:
    """Fixed Standard Operating Procedure for the manual operator benchmark.

    The operator must follow every step in sequence without deviation.
    Decision points specify allowed branches; any unlisted condition is an error.
    """

    STEPS: list[SOPStep] = [
        SOPStep(
            step_id="observe_alert",
            description="Check Grafana dashboard and Alertmanager for active alerts.",
            action=(
                "Use the reviewed SSH tunnel to monitor-ai-01, then open local "
                "Grafana (:3000), Prometheus (:9090), and Alertmanager (:9093). "
                "Use only the blinded operator view: scenario_id/drill_id labels, "
                "fault-marker series, and evaluation files must not be visible. "
                "Record its alert name, operational labels, severity, and start time. "
                "The experiment controller retains the unmodified alert separately."
            ),
            expected_duration_seconds=120,
            decision_point=True,
            branches={
                "alert_found": "identify_scenario",
                "no_alert": "escalate",
            },
        ),
        SOPStep(
            step_id="identify_scenario",
            description="Record a diagnosis from operational evidence before treatment.",
            action=(
                "Record the alert labels, observed metrics, predicted root cause, and "
                "confidence using only the blinded operational alert/catalog. The "
                "experiment controller must record prediction_locked_at and retain the "
                "injected scenario mapping privately until every treatment decision and "
                "raw artifact is frozen. Do not write scenario_id in the operator log."
            ),
            expected_duration_seconds=180,
            decision_point=True,
            branches={
                "hypothesis_locked": "check_initial_state",
                "scenario_unknown": "escalate",
            },
        ),
        SOPStep(
            step_id="check_initial_state",
            description="Verify environment checksum matches the known-good baseline.",
            action=(
                "Run `AWS_PROFILE=target-account bash "
                "automation/moodle-environment-baseline.sh verify` and compare the "
                "environment with the common reviewed baseline. Record raw output and "
                "PASS or FAIL; do not inspect scenario-marker files or evaluation data."
            ),
            expected_duration_seconds=60,
            decision_point=True,
            branches={
                "checksum_pass": "select_allowed_action",
                "checksum_fail": "escalate",
            },
        ),
        SOPStep(
            step_id="select_allowed_action",
            description="Choose one approved action from the operational action catalog.",
            action=(
                "Using the observed symptoms and approved operational action catalog, "
                "select exactly one in-scope action. Do not consult evaluation "
                "ground-truth, expected-root-cause, or expected-remediation files. "
                "Record the selected action and rationale before execution."
            ),
            expected_duration_seconds=90,
            decision_point=False,
        ),
        SOPStep(
            step_id="check_forbidden_actions",
            description="Confirm that the selected action is not in the forbidden list.",
            action=(
                "Check the selected action against the separately maintained "
                "operational denylist. If it is denied or the target/scope is unclear, "
                "stop and escalate; do not try alternate actions."
            ),
            expected_duration_seconds=30,
            decision_point=True,
            branches={
                "action_allowed": "record_timestamp_plan",
                "action_forbidden": "escalate",
            },
        ),
        SOPStep(
            step_id="record_timestamp_plan",
            description="Record the planning timestamp (t_plan) in the operator log.",
            action=(
                "In the operator log CSV, write the blinded run_id, method=manual, "
                "t_plan=<ISO-8601 UTC timestamp>, predicted_root_cause, and selected "
                "action. The controller joins run_id to scenario_id only after the "
                "treatment record and raw evidence are frozen."
            ),
            expected_duration_seconds=15,
            decision_point=False,
        ),
        SOPStep(
            step_id="execute_action",
            description="Apply the selected, approved remediation action once.",
            action=(
                "Perform only the single remediation selected from the operational "
                "runbook and approved for this staging scope. Capture the exact "
                "command/playbook, actor, start/end timestamps, stdout/stderr, and "
                "result. Never use the experiment fault-reset script as remediation: "
                "the trial operator runs that only after evidence collection to restore "
                "the baseline. SEC-01 is HUMAN_ONLY and requires the reviewed exact "
                "security-group rule-ID procedure; no other method may mutate it."
            ),
            expected_duration_seconds=120,
            decision_point=False,
        ),
        SOPStep(
            step_id="record_timestamp_execute",
            description=(
                "Record t_execute_start and t_execute_end timestamps in the operator log."
            ),
            action=(
                "In the operator log CSV, add: "
                "t_execute_start=<timestamp before script>, "
                "t_execute_end=<timestamp after script>."
            ),
            expected_duration_seconds=15,
            decision_point=False,
        ),
        SOPStep(
            step_id="verify_health",
            description="Check health endpoints for all affected services.",
            action=(
                "Run `AWS_PROFILE=target-account bash "
                "automation/moodle-environment-baseline.sh verify`. Record "
                "ALB, authenticated synthetic, RDS, EFS, cron, and monitoring "
                "results; a public HTTP 200 alone is insufficient."
            ),
            expected_duration_seconds=60,
            decision_point=True,
            branches={
                "health_pass": "verify_contract",
                "health_fail": "escalate",
            },
        ),
        SOPStep(
            step_id="verify_contract",
            description="Check communication contract probes for the scenario.",
            action=(
                "An independent verifier, not the treatment operator, runs the "
                "pre-registered allowed/forbidden/related probes for the privately "
                "held scenario contract and records the raw results. The operator may "
                "not select a contract by scenario_id or mark this result resolved."
            ),
            expected_duration_seconds=60,
            decision_point=True,
            branches={
                "contract_pass": "verify_stability",
                "contract_fail": "escalate",
            },
        ),
        SOPStep(
            step_id="verify_stability",
            description="Wait 120 s and re-check both health and contract for stability.",
            action=(
                "Observe at least 120 seconds of timestamped, healthy "
                "samples (no gap over 60 seconds); rerun the baseline and "
                "scenario verifier at the end. Record all observations, "
                "not only a start/end HTTP response."
            ),
            expected_duration_seconds=180,
            decision_point=True,
            branches={
                "stability_pass": "record_resolved",
                "stability_fail": "escalate",
            },
        ),
        SOPStep(
            step_id="record_resolved",
            description=(
                "The independent verifier records RESOLVED only after all contract and stability checks pass."
            ),
            action=(
                "The independent verifier writes status=RESOLVED, "
                "resolution_actor=independent_verifier, and "
                "t_resolved=<ISO-8601 UTC timestamp> in the run record. "
                "Calculate detection_time = t_detect - t_inject, "
                "remediation_time = t_resolved - t_execute_start. "
                "After all raw evidence and decisions are frozen, restore the "
                "known-good baseline with the reviewed scenario reset procedure and "
                "record its independent checksum separately from remediation. "
                "Submit the log row."
            ),
            expected_duration_seconds=30,
            decision_point=False,
        ),
    ]

    def get_step(self, step_id: str) -> SOPStep | None:
        """Return the SOPStep with the given step_id, or None if not found."""
        for step in self.STEPS:
            if step.step_id == step_id:
                return step
        return None

    def validate_operator_log(self, log: list[dict]) -> list[str]:
        """Validate an operator execution log against the SOP.

        Parameters
        ----------
        log:
            Ordered step records. Every record requires a timezone-aware ISO-8601
            ``timestamp``; decision steps require an ``outcome`` matching one of
            their documented branches. A run may end early at an escalation.

        Returns
        -------
        List of validation error strings.  Empty list means log is valid.
        """
        if not isinstance(log, list) or not log:
            return ["Operator log must be a non-empty list of step records."]

        errors: list[str] = []
        steps = {step.step_id: step for step in self.STEPS}
        previous_id: str | None = None
        previous_time: datetime | None = None
        previous_metric_time: datetime | None = None
        previous_metric_name: str | None = None
        seen: set[str] = set()
        resolved = False
        stability_pass_time: datetime | None = None
        prediction_locked_time: datetime | None = None
        evaluator_only_fields = {
            "scenario_id", "drill_id", "fault_marker_id", "expected_root_cause",
            "expected_remediation", "allowed_remediation", "ground_truth",
        }
        expected_timestamp_fields = {
            "record_timestamp_plan": ("t_plan",),
            "record_timestamp_execute": ("t_execute_start", "t_execute_end"),
            "verify_health": ("t_verify",),
            "record_resolved": ("t_resolved",),
        }

        for index, entry in enumerate(log):
            if not isinstance(entry, dict):
                errors.append(f"Log entry {index + 1} must be an object.")
                continue
            leaked_fields = self._find_fields(entry, evaluator_only_fields)
            if leaked_fields:
                errors.append(
                    f"Treatment log contains evaluator-only fields: {sorted(leaked_fields)}"
                )
            step_id = entry.get("step_id")
            step = steps.get(step_id) if isinstance(step_id, str) else None
            if step is None:
                errors.append(f"Unknown step in log: {step_id!r}")
                continue
            if step_id in seen:
                errors.append(f"Repeated SOP step is not allowed: '{step_id}'")
            seen.add(step_id)
            if resolved:
                errors.append("No step may follow 'record_resolved'.")

            event_time = self._parse_utc(entry.get("timestamp"))
            if event_time is None:
                errors.append(f"Step '{step_id}' requires a timezone-aware ISO-8601 timestamp.")
            elif previous_time is not None and event_time < previous_time:
                errors.append(f"Timestamp for '{step_id}' precedes the prior logged step.")
            if event_time is not None:
                previous_time = event_time

            if previous_id is None:
                if step_id != "observe_alert":
                    errors.append("The first logged step must be 'observe_alert'.")
            else:
                previous_step = steps[previous_id]
                previous_entry = log[index - 1]
                if previous_step.decision_point:
                    outcome = previous_entry.get("outcome") if isinstance(previous_entry, dict) else None
                    destination = previous_step.branches.get(outcome)
                    if destination is None:
                        errors.append(f"Decision step '{previous_id}' has an invalid or missing outcome.")
                    elif destination == "escalate":
                        errors.append(f"Step '{step_id}' follows an escalation at '{previous_id}'.")
                    elif destination != step_id:
                        errors.append(f"'{previous_id}' outcome requires '{destination}', got '{step_id}'.")
                else:
                    step_index = next(i for i, item in enumerate(self.STEPS) if item.step_id == previous_id)
                    expected_next = self.STEPS[step_index + 1].step_id if step_index + 1 < len(self.STEPS) else None
                    if step_id != expected_next:
                        errors.append(f"'{previous_id}' must be followed by '{expected_next}'.")

            if step.decision_point and entry.get("outcome") not in step.branches:
                errors.append(f"Decision step '{step_id}' requires a documented outcome.")

            if step_id == "identify_scenario":
                prediction = entry.get("predicted_root_cause")
                confidence = entry.get("confidence")
                locked_at = self._parse_utc(entry.get("prediction_locked_at"))
                if not isinstance(prediction, str) or not prediction.strip():
                    errors.append("Diagnosis must be recorded before treatment as 'predicted_root_cause'.")
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
                    errors.append("Diagnosis must include a numeric confidence between 0 and 1.")
                if locked_at is None or (event_time is not None and locked_at > event_time):
                    errors.append("Prediction must have a timezone-aware lock time no later than diagnosis logging.")
                if locked_at is not None:
                    prediction_locked_time = locked_at

            if step_id == "verify_contract":
                if entry.get("verifier_actor") != "independent_verifier":
                    errors.append("Communication-contract checks must be recorded by the independent verifier.")
                expected_verification = {
                    "contract_pass": "passed",
                    "contract_fail": "failed",
                }.get(entry.get("outcome"))
                if expected_verification and entry.get("verification_status") != expected_verification:
                    errors.append("Contract outcome and independent verification_status disagree.")

            if step_id == "verify_stability":
                stability = entry.get("stability_seconds")
                if (
                    not isinstance(stability, (int, float))
                    or isinstance(stability, bool)
                    or not math.isfinite(stability)
                    or stability < 120
                ):
                    errors.append("A passing verification requires at least 120 seconds of measured stability.")
                if entry.get("outcome") == "stability_pass" and event_time is not None:
                    stability_pass_time = event_time

            for field in expected_timestamp_fields.get(step_id, ()):
                field_time = self._parse_utc(entry.get(field))
                if field_time is None:
                    errors.append(f"Step '{step_id}' requires timezone-aware timestamp '{field}'.")
                elif previous_metric_time is not None and field_time < previous_metric_time:
                    errors.append(f"'{field}' precedes earlier measurement '{previous_metric_name}'.")
                if field_time is not None and event_time is not None and field_time > event_time:
                    errors.append(f"'{field}' occurs after the log event timestamp for '{step_id}'.")
                if (
                    step_id == "record_timestamp_plan"
                    and field == "t_plan"
                    and field_time is not None
                    and prediction_locked_time is not None
                    and field_time < prediction_locked_time
                ):
                    errors.append("'t_plan' precedes the locked diagnosis.")
                if (
                    step_id == "record_resolved"
                    and field == "t_resolved"
                    and field_time is not None
                    and stability_pass_time is not None
                    and field_time < stability_pass_time
                ):
                    errors.append("'t_resolved' precedes the passing stability verification.")
                if field_time is not None:
                    previous_metric_time = field_time
                    previous_metric_name = field

            if step_id == "record_resolved":
                if previous_id != "verify_stability" or log[index - 1].get("outcome") != "stability_pass":
                    errors.append("RESOLVED requires a passing stability verification immediately before it.")
                if entry.get("status") != "RESOLVED" or entry.get("resolution_actor") != "independent_verifier":
                    errors.append("Only the independent verifier may record status=RESOLVED.")
                resolved = True
            previous_id = step_id

        if not seen.intersection({"observe_alert"}):
            errors.append("Log is missing the initial alert observation.")
        if log and isinstance(log[-1], dict):
            last_step_id = log[-1].get("step_id")
            last_step = steps.get(last_step_id) if isinstance(last_step_id, str) else None
            last_outcome = log[-1].get("outcome")
            if last_step_id != "record_resolved" and not (
                last_step is not None
                and last_step.decision_point
                and last_step.branches.get(last_outcome) == "escalate"
            ):
                errors.append("Operator log must end with RESOLVED or an escalation.")
        return errors

    @classmethod
    def _find_fields(cls, value: object, forbidden: set[str]) -> set[str]:
        if isinstance(value, dict):
            found = forbidden.intersection(value)
            for child in value.values():
                found.update(cls._find_fields(child, forbidden))
            return found
        if isinstance(value, list):
            found: set[str] = set()
            for child in value:
                found.update(cls._find_fields(child, forbidden))
            return found
        return set()

    @staticmethod
    def _parse_utc(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
