"""Node-local probes are read-only and emit parseable, secret-free metrics."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "ansible/files/moodle-monitoring/moodle-node-probes.sh"
PLAYBOOK = REPO / "ansible/playbooks/configure-moodle-monitoring.yml"


def _cygpath(path: str | Path) -> str:
    git_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
    cygpath = git_root / "usr" / "bin" / "cygpath.exe"
    return subprocess.run(
        [str(cygpath), "-u", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _bash_command(script: Path) -> list[str]:
    if os.name != "nt":
        return ["bash", str(script)]
    git_root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
    bash = git_root / "bin" / "bash.exe"
    cygpath = git_root / "usr" / "bin" / "cygpath.exe"
    if bash.is_file() and cygpath.is_file():
        return [str(bash), _cygpath(script)]
    return ["bash", str(script)]


def _run(output_dir: Path, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    output_path = str(output_dir)
    if os.name == "nt" and output_dir.is_absolute():
        output_path = _cygpath(output_dir)
    env.update({
        "MOODLE_NODE_PROBE_OUTPUT_DIR": output_path,
        "MOODLE_NODE_PROBE_CONTAINER": "moodle-probe-test-missing",
    })
    env.update(extra_env or {})
    fake_bin = env.pop("MOODLE_TEST_FAKE_BIN", None)
    command = _bash_command(SCRIPT)
    if fake_bin:
        if os.name == "nt":
            env["MOODLE_TEST_FAKE_BIN"] = _cygpath(fake_bin)
            command = [
                *command[:1],
                "-c",
                'export PATH="$MOODLE_TEST_FAKE_BIN:$PATH"; exec "$1"',
                "moodle-node-probe-test",
                command[1],
            ]
        else:
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return subprocess.run(command, env=env, text=True, capture_output=True, check=False, timeout=20)


def test_missing_container_emits_zero_not_fake_recovery(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    metrics = (tmp_path / "moodle_node_probes.prom").read_text(encoding="utf-8")
    assert "moodle_node_web_running 0\n" in metrics
    assert "moodle_node_rds_tcp_success 0\n" in metrics
    assert "moodle_node_router_fallback_success 0\n" in metrics
    assert "moodle_node_reverse_proxy_enabled 0\n" in metrics
    assert "moodle_node_scratch_enospc 0\n" in metrics
    assert "MOODLE_DB_PASSWORD" not in metrics


def test_relative_output_path_is_rejected(tmp_path: Path) -> None:
    result = _run(tmp_path, extra_env={"MOODLE_NODE_PROBE_OUTPUT_DIR": "relative/path"})
    assert result.returncode == 64


def test_probe_timer_has_first_activation_after_deploy() -> None:
    source = PLAYBOOK.read_text(encoding="utf-8")
    timer = source.split("dest: /etc/systemd/system/moodle-node-probes.timer", 1)[1].split("dest:", 1)[0]
    assert "OnActiveSec=5s" in timer
    assert "OnUnitActiveSec=30s" in timer
    assert "state: restarted" in source.split("name: Enable node-local Moodle probe timer", 1)[1].split("- name:", 1)[0]


def test_positive_container_probes_emit_expected_values(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *RestartCount*) echo 3 ;;\n"
        "  *.State.Running*) echo true ;;\n"
        "  *'php -r'*) echo 0.012345 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\necho 'The requested resource could not be found. Please verify the URI'\n",
        encoding="utf-8",
        newline="\n",
    )
    curl.chmod(0o755)
    result = _run(tmp_path, extra_env={
        "MOODLE_TEST_FAKE_BIN": str(fake_bin),
        "MOODLE_NODE_PROBE_CONTAINER": "release-moodle-web-1",
    })
    assert result.returncode == 0, result.stderr
    metrics = (tmp_path / "moodle_node_probes.prom").read_text(encoding="utf-8")
    assert "moodle_node_web_running 1\n" in metrics
    assert "moodle_node_apache_router_enabled 1\n" in metrics
    assert "moodle_node_router_fallback_success 1\n" in metrics
    assert "moodle_node_reverse_proxy_enabled 1\n" in metrics
    assert "moodle_node_rds_tcp_success 1\n" in metrics
    assert "moodle_node_rds_tcp_duration_seconds 0.012345\n" in metrics
    assert "moodle_node_web_restart_count 3\n" in metrics
