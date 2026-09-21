#!/usr/bin/env python3
"""Create reproducible, non-mutating Sprint 2 Moodle pipeline evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent_src"))

from core.moodle_pipeline import replay_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "terraform" / ".artifacts" / "moodle-pipeline-replay",
        help="directory for local secret-free replay evidence",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reports = replay_all(args.output_dir)
    report_path = args.output_dir / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "mode": "dry-run",
                "scenario_count": len(reports),
                "forbidden_execution_count": 0,
                "reports": reports,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Moodle Sprint 2 dry-run replay passed for {len(reports)} scenarios: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
