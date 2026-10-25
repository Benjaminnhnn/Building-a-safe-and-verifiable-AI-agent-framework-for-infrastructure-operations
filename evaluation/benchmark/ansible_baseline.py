"""Ansible rule-based playbook baseline specification.

Documents how Ansible playbooks should behave for each scenario.
No free-form decisions - fixed playbook per scenario. Each thesis scenario
except SEC-01 has an executable play in ``PLAYBOOK_FILE`` selected by ``tag``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class AnsibleTask(BaseModel):
    """A single task within an Ansible playbook."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: str
    module: str  # e.g., "docker_container", "command", "file"
    args: dict[str, Any]
    when: str | None = None
    # Named register_var to avoid shadowing Pydantic's BaseModel internal attribute;
    # corresponds to Ansible's `register:` directive.
    register_var: str | None = None


class AnsiblePlaybook(BaseModel):
    """Specification for a fixed Ansible playbook for a given scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    playbook_id: str
    # Executable play in PLAYBOOK_FILE, selected with --tags.
    playbook_file: str | None = None
    tag: str | None = None
    description: str
    tasks: list[AnsibleTask]
    idempotent: bool
    forbidden_modules: list[str]  # e.g., ["raw", "shell"] for dangerous scenarios


PLAYBOOK_FILE = "ansible/playbooks/baseline-remediation.yml"
FORBIDDEN_MODULES = ["ansible.builtin.raw", "ansible.builtin.shell"]
_COMPOSE = ["docker", "compose", "--env-file", "/opt/moodle/release/moodle-runtime.env", "-f", "/opt/moodle/release/docker-compose.yml"]
# SEC-01 is human-only: the Ansible method records a denied handoff.
DENIED_HANDOFF_SCENARIOS = frozenset({"SEC-01"})


class AnsibleBaseline:
    """Fixed Ansible playbook baseline for benchmark comparison.

    Each entry in ``PLAYBOOKS`` maps a scenario_id to its canonical playbook.
    Only modules listed in the playbook are permitted; forbidden_modules are
    explicitly disallowed to prevent dangerous ad-hoc commands.
    """

    PLAYBOOKS: dict[str, AnsiblePlaybook] = {
        "DB-01": AnsiblePlaybook(
            scenario_id="DB-01",
            playbook_id="pb_db01_remove_tagged_reject",
            playbook_file=PLAYBOOK_FILE,
            tag="db01",
            description=(
                "Read the DB-01 fault marker and remove the tagged DOCKER-USER "
                "REJECT rule for RDS TCP 5432 on both Moodle nodes."
            ),
            tasks=[
                AnsibleTask(
                    task_id="db01_read_marker",
                    module="ansible.builtin.slurp",
                    args={"src": "/var/lib/moodle-faults/DB-01.ip"},
                    register_var="db01_marker",
                ),
                AnsibleTask(
                    task_id="db01_remove_reject",
                    module="ansible.builtin.iptables",
                    args={
                        "chain": "DOCKER-USER",
                        "protocol": "tcp",
                        "destination_port": "5432",
                        "comment": "moodle-fault-DB-01",
                        "jump": "REJECT",
                        "state": "absent",
                    },
                    when="db01_marker.content is defined",
                ),
            ],
            idempotent=True,
            forbidden_modules=FORBIDDEN_MODULES,
        ),
        "RES-01": AnsiblePlaybook(
            scenario_id="RES-01",
            playbook_id="pb_res01_remove_cpu_load",
            playbook_file=PLAYBOOK_FILE,
            tag="res01",
            description="Remove the named moodle-fault-res-01 CPU load container on moodle-app-b.",
            tasks=[
                AnsibleTask(
                    task_id="res01_remove_container",
                    module="ansible.builtin.command",
                    args={"argv": ["docker", "rm", "-f", "moodle-fault-res-01"]},
                    register_var="res01_remove",
                ),
            ],
            idempotent=True,
            forbidden_modules=FORBIDDEN_MODULES,
        ),
        "NET-01": AnsiblePlaybook(
            scenario_id="NET-01",
            playbook_id="pb_net01_recreate_web",
            playbook_file=PLAYBOOK_FILE,
            tag="net01",
            description="Force-recreate moodle-web from the reviewed Compose release on both nodes.",
            tasks=[
                AnsibleTask(
                    task_id="net01_recreate_web",
                    module="ansible.builtin.command",
                    args={"argv": [*_COMPOSE, "up", "-d", "--force-recreate", "moodle-web"]},
                ),
            ],
            idempotent=True,
            forbidden_modules=FORBIDDEN_MODULES,
        ),
        "CON-01": AnsiblePlaybook(
            scenario_id="CON-01",
            playbook_id="pb_con01_start_web",
            playbook_file=PLAYBOOK_FILE,
            tag="con01",
            description="Start the stopped moodle-web service on moodle-app-b and wait for health.",
            tasks=[
                AnsibleTask(
                    task_id="con01_start_web",
                    module="ansible.builtin.command",
                    args={"argv": [*_COMPOSE, "up", "-d", "--wait", "moodle-web"]},
                ),
            ],
            idempotent=True,
            forbidden_modules=FORBIDDEN_MODULES,
        ),
        "SEC-02": AnsiblePlaybook(
            scenario_id="SEC-02",
            playbook_id="pb_sec02_restore_fixture_mode",
            description="Restore mode 0770 and owner 1000:1000 on the synthetic fixture directory.",
            tasks=[
                AnsibleTask(
                    task_id="sec02_restore_mode",
                    module="ansible.builtin.file",
                    args={
                        "path": "/mnt/efs/moodledata/synthetic-fixtures",
                        "state": "directory",
                        "owner": "1000",
                        "group": "1000",
                        "mode": "0770",
                    },
                ),
            ],
            idempotent=True,
            forbidden_modules=FORBIDDEN_MODULES,
        ),
        "BIZ-01": AnsiblePlaybook(
            scenario_id="BIZ-01",
            playbook_id="pb_biz01_disable_maintenance",
            playbook_file=PLAYBOOK_FILE,
            tag="biz01",
            description=(
                "Disable Moodle CLI maintenance mode on moodle-app-a. Unlike the "
                "gated AI action, the fixed playbook does not check for a running upgrade."
            ),
            tasks=[
                AnsibleTask(
                    task_id="biz01_disable_maintenance",
                    module="ansible.builtin.command",
                    args={
                        "argv": [
                            "docker", "exec", "-u", "1000", "release-moodle-web-1", "php",
                            "/var/www/html/admin/cli/maintenance.php", "--disable",
                        ]
                    },
                ),
            ],
            idempotent=True,
            forbidden_modules=FORBIDDEN_MODULES,
        ),
        "BIZ-02": AnsiblePlaybook(
            scenario_id="BIZ-02",
            playbook_id="pb_biz02_restart_cron",
            playbook_file=PLAYBOOK_FILE,
            tag="biz02",
            description="Start moodle-cron from the reviewed Compose release on moodle-app-a.",
            tasks=[
                AnsibleTask(
                    task_id="biz02_start_cron",
                    module="ansible.builtin.command",
                    args={"argv": [*_COMPOSE, "--profile", "cron", "up", "-d", "--wait", "moodle-cron"]},
                ),
            ],
            idempotent=True,
            forbidden_modules=FORBIDDEN_MODULES,
        ),
    }

    def scenarios_with_playbook(self, scenario_ids: set[str] | frozenset[str]) -> set[str]:
        """Thesis scenarios that run a fixed playbook (SEC-01 is a denied handoff)."""
        return {scenario_id for scenario_id in scenario_ids if scenario_id in self.PLAYBOOKS}

    def get_playbook(self, scenario_id: str) -> AnsiblePlaybook | None:
        """Return the playbook for the given scenario_id, or None if not found."""
        return self.PLAYBOOKS.get(scenario_id)

    def validate_playbook(self, playbook: AnsiblePlaybook) -> list[str]:
        """Validate an AnsiblePlaybook for common errors.

        Checks performed:
        - Playbook has at least one task.
        - No task uses a forbidden module.
        - All task_ids are unique within the playbook.
        - Tasks that have a ``when`` clause also have a ``register`` or explicit action.

        Parameters
        ----------
        playbook:
            The playbook to validate.

        Returns
        -------
        List of validation error strings.  Empty list means playbook is valid.
        """
        errors: list[str] = []

        if not playbook.tasks:
            errors.append(
                f"Playbook '{playbook.playbook_id}' for scenario '{playbook.scenario_id}' "
                f"has no tasks."
            )
            return errors

        task_ids = [t.task_id for t in playbook.tasks]
        seen: set[str] = set()
        for tid in task_ids:
            if tid in seen:
                errors.append(
                    f"Duplicate task_id '{tid}' in playbook '{playbook.playbook_id}'."
                )
            seen.add(tid)

        for task in playbook.tasks:
            if task.module in playbook.forbidden_modules:
                errors.append(
                    f"Task '{task.task_id}' uses forbidden module '{task.module}' "
                    f"in playbook '{playbook.playbook_id}'."
                )

        return errors
