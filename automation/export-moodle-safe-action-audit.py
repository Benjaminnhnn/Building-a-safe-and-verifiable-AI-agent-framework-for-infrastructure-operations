#!/usr/bin/env python3
"""Export a consistent, read-only snapshot of the monitor's safe-action audit DB."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable


_REMOTE_SNAPSHOT = r"""
import sqlite3,sys
source=sqlite3.connect('file:/app/data/safe_action_audit.sqlite3?mode=ro',uri=True)
target=sqlite3.connect(':memory:')
source.backup(target)
sys.stdout.buffer.write(target.serialize())
target.close()
source.close()
""".strip()


def export_snapshot(
    output: Path,
    ssh_config: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> Path:
    if not ssh_config.is_file():
        raise ValueError(f"SSH configuration not found: {ssh_config}")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    remote_command = "sudo docker exec moodle-ai-agent python -c " + shlex.quote(_REMOTE_SNAPSHOT)
    result = run(
        ["ssh", "-T", "-F", str(ssh_config), "monitor-ai-01", remote_command],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"remote audit snapshot failed (ssh exit {result.returncode})")
    if not result.stdout.startswith(b"SQLite format 3\x00"):
        raise ValueError("remote command did not return a SQLite database snapshot")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(result.stdout)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-config", type=Path, default=Path("terraform/.artifacts/moodle-ssh.config"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        path = export_snapshot(args.output, args.ssh_config)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"audit export failed: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
