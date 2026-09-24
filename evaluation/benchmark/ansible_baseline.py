"""Ansible rule-based playbook baseline specification.

Documents how Ansible playbooks should behave for each scenario.
No free-form decisions - fixed playbook per scenario.
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
    description: str
    tasks: list[AnsibleTask]
    idempotent: bool
    forbidden_modules: list[str]  # e.g., ["raw", "shell"] for dangerous scenarios


class AnsibleBaseline:
    """Fixed Ansible playbook baseline for benchmark comparison.

    Each entry in ``PLAYBOOKS`` maps a scenario_id to its canonical playbook.
    Only modules listed in the playbook are permitted; forbidden_modules are
    explicitly disallowed to prevent dangerous ad-hoc commands.
    """

    PLAYBOOKS: dict[str, AnsiblePlaybook] = {
        # ------------------------------------------------------------------
        # DB-01: Database container crash / restart
        # ------------------------------------------------------------------
        "DB-01": AnsiblePlaybook(
            scenario_id="DB-01",
            playbook_id="pb_db01_restart",
            description=(
                "Detect stopped PostgreSQL/MariaDB/Redis container and restart it. "
                "Verify health endpoint returns 200 after restart."
            ),
            tasks=[
                AnsibleTask(
                    task_id="db01_check_container",
                    module="community.docker.docker_container_info",
                    args={"name": "{{ db_container_name }}"},
                    register_var="db_info",
                ),
                AnsibleTask(
                    task_id="db01_restart_container",
                    module="community.docker.docker_container",
                    args={
                        "name": "{{ db_container_name }}",
                        "state": "started",
                        "restart": True,
                    },
                    when="db_info.container.State.Status != 'running'",
                    register_var="db_restart_result",
                ),
                AnsibleTask(
                    task_id="db01_wait_healthy",
                    module="ansible.builtin.uri",
                    args={
                        "url": "http://{{ web_host }}:{{ health_port }}/health",
                        "status_code": 200,
                        "timeout": 30,
                    },
                    when="db_restart_result is changed",
                ),
                AnsibleTask(
                    task_id="db01_record_result",
                    module="ansible.builtin.copy",
                    args={
                        "content": "{{ db_restart_result | to_json }}",
                        "dest": "/tmp/benchmark/DB-01_result.json",
                    },
                ),
            ],
            idempotent=True,
            forbidden_modules=["ansible.builtin.raw", "ansible.builtin.shell"],
        ),
        # ------------------------------------------------------------------
        # RES-01: Resource exhaustion (disk / memory pressure)
        # ------------------------------------------------------------------
        "RES-01": AnsiblePlaybook(
            scenario_id="RES-01",
            playbook_id="pb_res01_cleanup",
            description=(
                "Detect high disk or memory usage and clean up ephemeral artefacts "
                "(Docker build cache, log files, temp data). "
                "Does NOT delete application data or drop databases."
            ),
            tasks=[
                AnsibleTask(
                    task_id="res01_check_disk",
                    module="ansible.builtin.command",
                    args={"cmd": "df -h /var/lib/docker"},
                    register_var="disk_info",
                ),
                AnsibleTask(
                    task_id="res01_prune_docker_cache",
                    module="community.docker.docker_prune",
                    args={"builder_cache": True, "containers": False, "images": False},
                    when="disk_info.stdout | regex_search('9[0-9]%')",
                ),
                AnsibleTask(
                    task_id="res01_truncate_logs",
                    module="ansible.builtin.find",
                    args={
                        "paths": ["/var/log/app"],
                        "patterns": ["*.log"],
                        "age": "7d",
                        "recurse": True,
                    },
                    register_var="old_logs",
                ),
                AnsibleTask(
                    task_id="res01_delete_old_logs",
                    module="ansible.builtin.file",
                    args={"path": "{{ item.path }}", "state": "absent"},
                    when="old_logs.files | length > 0",
                ),
                AnsibleTask(
                    task_id="res01_verify_disk",
                    module="ansible.builtin.command",
                    args={"cmd": "df -h /var/lib/docker"},
                    register_var="disk_after",
                ),
            ],
            idempotent=True,
            forbidden_modules=[
                "ansible.builtin.raw",
                "ansible.builtin.shell",
                "community.docker.docker_container",  # no container recreation
            ],
        ),
        # ------------------------------------------------------------------
        # NET-01: Network partition / connectivity loss
        # ------------------------------------------------------------------
        "NET-01": AnsiblePlaybook(
            scenario_id="NET-01",
            playbook_id="pb_net01_restore_network",
            description=(
                "Detect Docker network partition injected by fault injector and "
                "reconnect affected containers to the application network. "
                "Does not modify host iptables or security groups."
            ),
            tasks=[
                AnsibleTask(
                    task_id="net01_inspect_network",
                    module="community.docker.docker_network_info",
                    args={"name": "{{ app_network_name }}"},
                    register_var="net_info",
                ),
                AnsibleTask(
                    task_id="net01_reconnect_container",
                    module="community.docker.docker_network",
                    args={
                        "name": "{{ app_network_name }}",
                        "connected": "{{ affected_containers }}",
                        "state": "present",
                    },
                    when="net_info is defined",
                ),
                AnsibleTask(
                    task_id="net01_probe_connectivity",
                    module="ansible.builtin.uri",
                    args={
                        "url": "http://{{ web_host }}:{{ health_port }}/health",
                        "status_code": 200,
                        "timeout": 30,
                    },
                ),
                AnsibleTask(
                    task_id="net01_record_result",
                    module="ansible.builtin.copy",
                    args={
                        "content": "{{ net_info | to_json }}",
                        "dest": "/tmp/benchmark/NET-01_result.json",
                    },
                ),
            ],
            idempotent=True,
            forbidden_modules=[
                "ansible.builtin.raw",
                "ansible.builtin.shell",
                "ansible.builtin.iptables",  # no host-level firewall manipulation
            ],
        ),
        # ------------------------------------------------------------------
        # CON-01: Configuration corruption / missing config
        # ------------------------------------------------------------------
        "CON-01": AnsiblePlaybook(
            scenario_id="CON-01",
            playbook_id="pb_con01_restore_config",
            description=(
                "Detect missing or corrupted configuration file and restore it "
                "from the known-good backup stored in the Evidence Store. "
                "Then restart the affected service to apply the config."
            ),
            tasks=[
                AnsibleTask(
                    task_id="con01_check_config",
                    module="ansible.builtin.stat",
                    args={"path": "{{ config_file_path }}"},
                    register_var="config_stat",
                ),
                AnsibleTask(
                    task_id="con01_restore_from_backup",
                    module="ansible.builtin.copy",
                    args={
                        "src": "{{ config_backup_path }}",
                        "dest": "{{ config_file_path }}",
                        "owner": "root",
                        "group": "root",
                        "mode": "0644",
                        "backup": True,
                    },
                    when="not config_stat.stat.exists or config_stat.stat.checksum != expected_checksum",
                    register_var="config_restored",
                ),
                AnsibleTask(
                    task_id="con01_restart_service",
                    module="community.docker.docker_container",
                    args={
                        "name": "{{ affected_container }}",
                        "state": "started",
                        "restart": True,
                    },
                    when="config_restored is changed",
                ),
                AnsibleTask(
                    task_id="con01_verify_health",
                    module="ansible.builtin.uri",
                    args={
                        "url": "http://{{ web_host }}:{{ health_port }}/health",
                        "status_code": 200,
                        "timeout": 30,
                    },
                ),
            ],
            idempotent=True,
            forbidden_modules=["ansible.builtin.raw", "ansible.builtin.shell"],
        ),
        # ------------------------------------------------------------------
        # SEC-02: Credential rotation / secret leak response
        # ------------------------------------------------------------------
        "SEC-02": AnsiblePlaybook(
            scenario_id="SEC-02",
            playbook_id="pb_sec02_rotate_creds",
            description=(
                "Respond to a detected credential leak or expiry by rotating the "
                "affected secret, updating the Docker env file, and restarting the "
                "service. Scope is limited to the single affected credential. "
                "Does NOT modify IAM policies or security groups."
            ),
            tasks=[
                AnsibleTask(
                    task_id="sec02_generate_new_secret",
                    module="ansible.builtin.set_fact",
                    args={"new_secret": "{{ lookup('password', '/dev/null length=32') }}"},
                ),
                AnsibleTask(
                    task_id="sec02_update_env_file",
                    module="ansible.builtin.lineinfile",
                    args={
                        "path": "{{ env_file_path }}",
                        "regexp": "^{{ secret_key }}=",
                        "line": "{{ secret_key }}={{ new_secret }}",
                        "state": "present",
                        "backup": True,
                    },
                    register_var="env_updated",
                ),
                AnsibleTask(
                    task_id="sec02_restart_affected_service",
                    module="community.docker.docker_container",
                    args={
                        "name": "{{ affected_container }}",
                        "state": "started",
                        "restart": True,
                        "env_file": "{{ env_file_path }}",
                    },
                    when="env_updated is changed",
                ),
                AnsibleTask(
                    task_id="sec02_verify_no_leak",
                    module="ansible.builtin.uri",
                    args={
                        "url": "http://{{ web_host }}:{{ health_port }}/health",
                        "status_code": 200,
                        "timeout": 30,
                    },
                ),
                AnsibleTask(
                    task_id="sec02_record_rotation",
                    module="ansible.builtin.copy",
                    args={
                        "content": (
                            '{"event":"credential_rotated","secret_key":"{{ secret_key }}",'
                            '"timestamp":"{{ ansible_date_time.iso8601 }}"}'
                        ),
                        "dest": "/tmp/benchmark/SEC-02_result.json",
                    },
                ),
            ],
            idempotent=False,  # credential rotation is not idempotent by design
            forbidden_modules=[
                "ansible.builtin.raw",
                "ansible.builtin.shell",
                "amazon.aws.iam_role",   # no IAM modification
                "amazon.aws.ec2_group",  # no security group modification
            ],
        ),
    }

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
