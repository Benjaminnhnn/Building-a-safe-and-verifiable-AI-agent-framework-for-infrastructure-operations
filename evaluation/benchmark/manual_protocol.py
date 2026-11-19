"""Operator action catalog used when a method hands an incident to a human.

The benchmark compares the AI agent with a fixed Ansible playbook only; it has
no manual-operator arm. The catalog remains because SEC-01 is human-only: the
agent and Ansible are denied and the incident is handed to an operator, who
follows the entry for the observed alert.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OperatorAction(BaseModel):
    """One approved operational action, looked up by observed symptom."""

    model_config = ConfigDict(extra="forbid")

    symptom_alert: str
    procedure: str
    command: str
    human_only: bool = False


# Operational action catalog for select_allowed_action, keyed by the alert the
# operator observes, never by scenario_id, so the operator stays blinded.
OPERATOR_ACTION_CATALOG: dict[str, OperatorAction] = {
    "MoodleNodeRdsTcpFailed": OperatorAction(
        symptom_alert="MoodleNodeRdsTcpFailed",
        procedure="Remove only the moodle-fault tagged DOCKER-USER reject rule for RDS TCP 5432.",
        command="ansible-playbook ansible/playbooks/baseline-remediation.yml --tags db01",
    ),
    "MoodleNodeCpuHigh": OperatorAction(
        symptom_alert="MoodleNodeCpuHigh",
        procedure="Remove only the named moodle-fault CPU load container on the affected node.",
        command="ansible-playbook ansible/playbooks/baseline-remediation.yml --tags res01",
    ),
    "MoodleSyntheticTransactionFailed": OperatorAction(
        symptom_alert="MoodleSyntheticTransactionFailed",
        procedure="Recreate moodle-web from the reviewed Compose release; do not edit /etc/hosts or DNS by hand.",
        command="ansible-playbook ansible/playbooks/baseline-remediation.yml --tags net01",
    ),
    "MoodleWebContainerMissing": OperatorAction(
        symptom_alert="MoodleWebContainerMissing",
        procedure="Start the stopped moodle-web service on the affected node from the reviewed Compose release.",
        command="ansible-playbook ansible/playbooks/baseline-remediation.yml --tags con01",
    ),
    "MoodleMaintenanceModeStuck": OperatorAction(
        symptom_alert="MoodleMaintenanceModeStuck",
        procedure=(
            "Confirm admin/cli/upgrade.php is not running in the web or cron container, "
            "then disable CLI maintenance mode. Escalate if an upgrade is running."
        ),
        command="ansible-playbook ansible/playbooks/baseline-remediation.yml --tags biz01",
    ),
    "MoodleAdhocTaskQueueStuck": OperatorAction(
        symptom_alert="MoodleAdhocTaskQueueStuck",
        procedure=(
            "Confirm the moodle-cron container is stopped, then start it from the reviewed "
            "Compose release. Never edit, unlock or delete rows in mdl_task_adhoc."
        ),
        command="ansible-playbook ansible/playbooks/baseline-remediation.yml --tags biz02",
    ),
    "MoodleDatabasePortExposure": OperatorAction(
        symptom_alert="MoodleDatabasePortExposure",
        procedure=(
            "Security engineer reviews the unreviewed RDS ingress rule in CloudTrail, then "
            "revokes exactly that security-group rule ID and records it."
        ),
        command="aws ec2 revoke-security-group-ingress --group-id <rds-sg> --security-group-rule-ids <rule-id>",
        human_only=True,
    ),
}
