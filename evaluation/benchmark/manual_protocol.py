"""Manual operator Standard Operating Procedure (SOP) for benchmark.

Documents the fixed decision tree a manual operator must follow.
No free-form decisions - operator must follow this exact SOP.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SOPStep(BaseModel):
    """A single step in the Manual Operator SOP."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    description: str
    action: str
    expected_duration_seconds: int
    decision_point: bool = False
    branches: dict[str, str] = {}  # condition -> next_step_id


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
                "Open Grafana at http://<monitor-host>:3000 and Alertmanager at "
                "http://<monitor-host>:9093. Record all firing alerts with their labels, "
                "severity and start time."
            ),
            expected_duration_seconds=120,
            decision_point=True,
            branches={
                "alert_found": "identify_scenario",
                "no_alert": "observe_alert",  # wait and re-check
            },
        ),
        SOPStep(
            step_id="identify_scenario",
            description="Match the observed alert to a known fault scenario pattern.",
            action=(
                "Consult the scenario runbook index. Compare alert name, labels and "
                "Prometheus metric values to the ground-truth trigger table. "
                "Record the matched scenario_id."
            ),
            expected_duration_seconds=180,
            decision_point=True,
            branches={
                "scenario_matched": "check_initial_state",
                "scenario_unknown": "observe_alert",  # escalate; restart observation
            },
        ),
        SOPStep(
            step_id="check_initial_state",
            description="Verify environment checksum matches the known-good baseline.",
            action=(
                "Run `scripts/checksum_env.sh` on the target host. Compare output "
                "against the baseline checksum stored in evaluation/ground_truth/. "
                "Record PASS or FAIL."
            ),
            expected_duration_seconds=60,
            decision_point=True,
            branches={
                "checksum_pass": "select_allowed_action",
                "checksum_fail": "observe_alert",  # environment is dirty; escalate
            },
        ),
        SOPStep(
            step_id="select_allowed_action",
            description=(
                "Choose the remediation action from the runbook's "
                "allowed_remediation list only."
            ),
            action=(
                "Open the runbook for the matched scenario_id. Read the "
                "`allowed_remediation` field. Select exactly one action from that list. "
                "Do NOT invent or combine actions."
            ),
            expected_duration_seconds=90,
            decision_point=False,
        ),
        SOPStep(
            step_id="check_forbidden_actions",
            description="Confirm that the selected action is not in the forbidden list.",
            action=(
                "Read the `forbidden_actions` field of the runbook. "
                "Verify the selected action does not appear in that list. "
                "If it does, return to select_allowed_action."
            ),
            expected_duration_seconds=30,
            decision_point=True,
            branches={
                "action_allowed": "record_timestamp_plan",
                "action_forbidden": "select_allowed_action",
            },
        ),
        SOPStep(
            step_id="record_timestamp_plan",
            description="Record the planning timestamp (t_plan) in the operator log.",
            action=(
                "In the operator log CSV, write: scenario_id, method=manual, "
                "t_plan=<ISO-8601 UTC timestamp>. Use `date -u +%Y-%m-%dT%H:%M:%SZ`."
            ),
            expected_duration_seconds=15,
            decision_point=False,
        ),
        SOPStep(
            step_id="execute_action",
            description="Run the scoped reset script for the selected action.",
            action=(
                "Execute the scoped reset script: "
                "`scripts/reset/<scenario_id>_<action>.sh`. "
                "Do NOT run any other script or ad-hoc command. "
                "Capture stdout and stderr to operator log."
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
                "Run `scripts/check_health.sh <scenario_id>`. "
                "All endpoints must return HTTP 200 within 30 s. "
                "Record PASS or FAIL."
            ),
            expected_duration_seconds=60,
            decision_point=True,
            branches={
                "health_pass": "verify_contract",
                "health_fail": "execute_action",  # retry once; if still failing, escalate
            },
        ),
        SOPStep(
            step_id="verify_contract",
            description="Check communication contract probes for the scenario.",
            action=(
                "Run `scripts/check_contract.sh <scenario_id>`. "
                "All contract probes must PASS. "
                "Record PASS or FAIL per probe."
            ),
            expected_duration_seconds=60,
            decision_point=True,
            branches={
                "contract_pass": "verify_stability",
                "contract_fail": "execute_action",
            },
        ),
        SOPStep(
            step_id="verify_stability",
            description="Wait 120 s and re-check both health and contract for stability.",
            action=(
                "Wait exactly 120 seconds. "
                "Re-run `scripts/check_health.sh` and `scripts/check_contract.sh`. "
                "Both must PASS. Record timestamps and results."
            ),
            expected_duration_seconds=180,
            decision_point=True,
            branches={
                "stability_pass": "record_resolved",
                "stability_fail": "execute_action",
            },
        ),
        SOPStep(
            step_id="record_resolved",
            description=(
                "Mark the incident as RESOLVED only after all verification checks pass."
            ),
            action=(
                "In the operator log CSV, write: "
                "status=RESOLVED, t_resolved=<ISO-8601 UTC timestamp>. "
                "Calculate detection_time = t_plan - t_alert_start, "
                "remediation_time = t_resolved - t_execute_start. "
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
            List of dicts, each representing a completed step.  Each dict must
            contain at minimum ``step_id`` and optionally ``timestamp``.

        Returns
        -------
        List of validation error strings.  Empty list means log is valid.
        """
        errors: list[str] = []
        required_step_ids = [s.step_id for s in self.STEPS]

        # Check that all required steps are present
        executed_ids = [entry.get("step_id") for entry in log]
        for req_id in required_step_ids:
            if req_id not in executed_ids:
                errors.append(f"Missing required SOP step: '{req_id}'")

        # Check that no unknown steps are present
        known_ids = set(required_step_ids)
        for entry in log:
            sid = entry.get("step_id")
            if sid and sid not in known_ids:
                errors.append(f"Unknown step in log: '{sid}'")

        # Check that 'record_resolved' is the last executed step (if present)
        if executed_ids and "record_resolved" in executed_ids:
            last_resolved_idx = len(executed_ids) - 1 - executed_ids[::-1].index(
                "record_resolved"
            )
            # Warn if any step follows record_resolved
            if last_resolved_idx < len(executed_ids) - 1:
                errors.append(
                    "Steps found after 'record_resolved'; RESOLVED must be the final entry."
                )

        # Check that timestamps exist for key steps
        timestamp_required_steps = {
            "record_timestamp_plan": "t_plan",
            "record_timestamp_execute": "t_execute_start",
            "record_resolved": "t_resolved",
        }
        for entry in log:
            sid = entry.get("step_id")
            if sid in timestamp_required_steps:
                key = timestamp_required_steps[sid]
                if not entry.get(key):
                    errors.append(
                        f"Step '{sid}' is missing required timestamp field '{key}'."
                    )

        return errors
