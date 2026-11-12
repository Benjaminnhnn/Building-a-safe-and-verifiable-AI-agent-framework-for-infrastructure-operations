"""The ALB recovery probe must follow the Terraform topology, not a fixed count.

The old probe passed when exactly two targets were healthy: with a third node
down it still passed (false VERIFIED), and with three healthy nodes it never
passed. These tests run the real shell function with a stubbed ``aws``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[2] / "automation" / "moodle-scenario-verify.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None or shutil.which("jq") is None, reason="needs bash and jq")


def _function_source() -> str:
    script = PROBE.read_text(encoding="utf-8")
    match = re.search(r"^check_all_expected_alb_targets_healthy\(\) \{\n.*?^\}\n", script, re.MULTILINE | re.DOTALL)
    assert match is not None, "topology-relative ALB probe is missing"
    return match.group(0)


def _health(*targets: tuple[str, str]) -> str:
    return json.dumps({"TargetHealthDescriptions": [
        {"Target": {"Id": target_id, "Port": 80}, "TargetHealth": {"State": state}} for target_id, state in targets
    ]})


def _run(expected: list[str] | None, health: str, *, aws_fails: bool = False) -> bool:
    expected_line = "" if expected is None else f"expected_target_ids='{json.dumps(expected)}'\n"
    script = (
        "set -uo pipefail\n"
        "region=eu-west-1\ntarget_group_arn=arn:test\n"
        f"{expected_line}"
        "aws() { printf '%s' \"$AWS_STUB_JSON\"; [[ -z \"${AWS_FAIL:-}\" ]]; }\n"
        f"{_function_source()}"
        "check_all_expected_alb_targets_healthy\n"
    )
    env = {"AWS_STUB_JSON": health, "PATH": subprocess.os.environ.get("PATH", "")}
    if aws_fails:
        env["AWS_FAIL"] = "1"
    result = subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def test_two_expected_nodes_both_healthy_passes() -> None:
    assert _run(["i-a", "i-b"], _health(("i-a", "healthy"), ("i-b", "healthy")))


def test_third_node_down_fails_even_though_two_are_healthy() -> None:
    health = _health(("i-a", "healthy"), ("i-b", "healthy"), ("i-c", "unhealthy"))
    assert not _run(["i-a", "i-b", "i-c"], health)


def test_three_healthy_nodes_pass_instead_of_failing_forever() -> None:
    health = _health(("i-a", "healthy"), ("i-b", "healthy"), ("i-c", "healthy"))
    assert _run(["i-a", "i-b", "i-c"], health)


@pytest.mark.parametrize(
    ("expected", "health"),
    [
        (["i-a", "i-b"], _health(("i-a", "healthy"))),  # expected node not registered
        (["i-a", "i-b"], _health(("i-a", "healthy"), ("i-b", "healthy"), ("i-x", "healthy"))),  # unknown target
        (["i-a", "i-b"], _health(("i-a", "healthy"), ("i-b", "draining"))),
        ([], _health()),  # empty expectation must not pass vacuously
        (None, _health(("i-a", "healthy"))),  # expectation missing entirely
    ],
)
def test_topology_mismatch_or_empty_expectation_fails_closed(expected, health) -> None:
    assert not _run(expected, health)


def test_aws_error_fails_closed() -> None:
    assert not _run(["i-a", "i-b"], _health(("i-a", "healthy"), ("i-b", "healthy")), aws_fails=True)


def test_no_fixed_target_count_remains_in_probe_script() -> None:
    script = PROBE.read_text(encoding="utf-8")
    assert '"$healthy" == 2' not in script and "check_two_healthy_alb_targets" not in script
