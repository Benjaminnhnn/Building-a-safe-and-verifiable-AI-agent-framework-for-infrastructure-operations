#!/usr/bin/env python3
"""Run one thesis trial (scenario x method x repetition) and emit dataset rows.

Writes, under ``--dataset-dir``:
- ``main.jsonl``: one main-trial row for ``evaluation/benchmark/thesis_profile``;
- ``no-gate.jsonl`` and ``health-only.jsonl``: the paired RQ1/RQ2 ablation
  rows, for ai_agent trials only;
- ``<run_id>/``: raw evidence and the 7-stage audit trace bound by SHA-256.

Methods:
- ai_agent on a live-allowlisted scenario delegates the whole lifecycle to
  ``moodle-safe-executor-drill.sh`` (prepare, two approvals, execute, signed
  Independent Verifier) and builds the row from its evidence;
- ai_agent and ansible on SEC-01 record a denied handoff, then a human
  remediates;
- ansible runs ``ansible/playbooks/baseline-remediation.yml``;
- manual follows ``OPERATOR_ACTION_CATALOG`` with operator-entered times.

Every trial also runs the RQ1 dangerous-proposal injection in a sandbox.
``--simulate`` exercises the same row construction with a fake clock and
fake operations. Its rows are labeled ``simulated`` and the acceptance gate
rejects them: they test the pipeline and never count as evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent_src"))

from evaluation.benchmark import dangerous_injection as injection  # noqa: E402
from evaluation.benchmark import thesis_profile  # noqa: E402
from evaluation.benchmark.ansible_baseline import AnsibleBaseline  # noqa: E402
from evaluation.benchmark.manual_protocol import OPERATOR_ACTION_CATALOG  # noqa: E402

METHODS = ("manual", "ansible", "ai_agent")
STAGES = ("observe", "triage", "diagnose", "plan", "gate_request", "execute", "verify")
TIMESTAMPS = (
    "t_inject", "t_detect", "t_incident", "t_plan", "t_gate",
    "t_execute_start", "t_execute_end", "t_verify", "t_resolved",
)
AUTHORITY = {"manual": "manual_operator", "ansible": "fixed_ansible_playbook", "ai_agent": "approved_safe_executor"}
MODEL_NAME = "deterministic-evidence-rules"
VERIFY_WINDOW_SECONDS = 120
VERIFY_INTERVAL_SECONDS = 30
ALERT_TIMEOUT_SECONDS = {"BIZ-01": 420, "BIZ-02": 420, "SEC-01": 180}
# The agent's evidence-derived cause code, mapped to the thesis root cause.
CAUSE_SCENARIO = {
    "moodle_maintenance_mode_stuck": "BIZ-01",
    "moodle_adhoc_task_queue_stuck": "BIZ-02",
    "web_container_not_running": "CON-01",
    "node_rds_path_probe_failed": "DB-01",
    "node_cpu_pressure": "RES-01",
    "application_path_not_isolated": "NET-01",
    "rds_ingress_unreviewed_source": "SEC-01",
}
UNKNOWN_ROOT_CAUSE = {"category": "unknown", "service": "unknown", "component": "unknown"}


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Clock:
    """Real time, or a fake clock that advances instead of sleeping."""

    def __init__(self, simulate: bool) -> None:
        self.simulate = simulate
        self._now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now if self.simulate else datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if self.simulate:
            self._now += timedelta(seconds=seconds)
        else:
            time.sleep(seconds)


class Trial:
    """Evidence, timestamps and audit stages for one run."""

    def __init__(self, args: argparse.Namespace, clock: Clock, run_id: str, method: str | None = None) -> None:
        self.args = args
        self.clock = clock
        self.method = method or args.method
        self.run_id = run_id
        self.directory = args.dataset_dir / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.evidence: dict[str, str] = {}
        self.times: dict[str, str] = {}
        self.events: list[dict[str, Any]] = []

    def mark(self, name: str, at: str | None = None) -> str:
        self.times[name] = at or utc(self.clock.now())
        return self.times[name]

    def add_evidence(self, path: Path) -> str:
        ref = str(path.resolve())
        self.evidence[ref] = sha256_file(path)
        return ref

    def save(self, name: str, payload: Any) -> str:
        path = self.directory / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.add_evidence(path)

    def stage(self, stage: str, actor: str, at: str, outcome: str = "completed", details: dict | None = None) -> None:
        self.events.append({"stage": stage, "actor": actor, "outcome": outcome, "occurred_at": at, "details": details or {}})

    def write_audit_trace(self, incident_id: str) -> str:
        raw_refs = sorted(self.evidence)
        lines = []
        for index, event in enumerate(self.events, 1):
            lines.append(json.dumps({
                "event_id": f"{self.run_id}-{index}",
                "incident_id": incident_id,
                "run_id": self.run_id,
                "event_type": "stage",
                "stage": event["stage"],
                "outcome": event["outcome"],
                "actor": event["actor"],
                "evidence_refs": raw_refs,
                "action_id": None,
                "output_digest": None,
                "details": event["details"],
                "occurred_at": event["occurred_at"],
            }, sort_keys=True))
        path = self.directory / f"{self.run_id}-audit.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.add_evidence(path)


# ---------------------------------------------------------------------------
# Live operations (staging). Each call goes through a reviewed script.
# ---------------------------------------------------------------------------


def _run(command: list[str], env: dict[str, str] | None = None, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout, check=False)


def _lib(command: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return _run(["bash", "-c", f'source automation/lib/moodle-fault-common.sh && load_moodle_environment && {command}'], timeout=timeout)


class LiveOps:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._public_url: str | None = None

    def public_url(self) -> str:
        if self._public_url is None:
            result = _lib('printf "%s" "$public_url"')
            if result.returncode != 0 or not result.stdout.startswith("http"):
                raise RuntimeError("cannot read the staging public URL from Terraform outputs")
            self._public_url = result.stdout.strip()
        return self._public_url

    def baseline(self) -> dict[str, Any]:
        result = _run(["bash", "automation/moodle-environment-baseline.sh", "verify"])
        return {"passed": result.returncode == 0, "stdout": result.stdout[-4000:]}

    def inject(self, scenario: str) -> dict[str, Any]:
        result = _run(["bash", "automation/moodle-fault-inject.sh", scenario], env={"MOODLE_FAULT_CONFIRM": "staging"})
        if result.returncode != 0:
            raise RuntimeError(f"fault injection failed: {result.stderr[-500:]}")
        return {"stdout": result.stdout[-2000:]}

    def wait_alert(self, scenario: str) -> None:
        alert = thesis_profile.THESIS_SCENARIOS[scenario].alert_name
        timeout = ALERT_TIMEOUT_SECONDS.get(scenario, 150)
        result = _lib(f"wait_for_prometheus_alert {alert} {timeout}", timeout=timeout + 60)
        if result.returncode != 0:
            raise RuntimeError(f"{alert} did not fire within {timeout}s")

    def reset(self, scenario: str) -> bool:
        return _run(["bash", "automation/moodle-fault-reset.sh", scenario]).returncode == 0

    def health_only(self) -> bool:
        result = _run(["curl", "--silent", "--output", "/dev/null", "--max-time", "10", "--write-out", "%{http_code}", f"{self.public_url()}/healthz.php"])
        return result.stdout.strip() == "200"

    def contract(self, scenario: str) -> dict[str, Any]:
        script = "moodle-human-remediation-verify.sh" if scenario in thesis_profile.HUMAN_ONLY_SCENARIOS else "moodle-scenario-verify.sh"
        result = _run(["bash", f"automation/{script}", scenario])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {"status": "failed", "checks": [], "error": result.stderr[-500:]}
        return payload

    def ansible(self, scenario: str) -> dict[str, Any]:
        playbook = AnsibleBaseline().get_playbook(scenario)
        result = _run([
            "ansible-playbook", "-i", "terraform/.artifacts/moodle-inventory.yml",
            playbook.playbook_file, "--tags", playbook.tag,
        ])
        return {"returncode": result.returncode, "stdout": result.stdout[-4000:]}

    def ansible_injection(self, scenario: str, directory: Path) -> dict[str, bool]:
        path = directory / "dangerous-proposals.yml"
        path.write_text(json.dumps([{"name": "dangerous proposals", "hosts": "moodle", "gather_facts": False, "tasks": injection.render_ansible_check_tasks(scenario)}]), encoding="utf-8")
        listed = _run(["ansible-playbook", "-i", "terraform/.artifacts/moodle-inventory.yml", str(path), "--check", "--list-tasks"])
        # A fixed playbook has no gate: every task Ansible accepts would run.
        return {proposal["action_id"]: listed.returncode == 0 for proposal in injection.proposals(scenario)}

    def operator(self, prompt: str) -> str:
        if not sys.stdin.isatty():
            raise RuntimeError("manual steps need an interactive operator terminal")
        return input(f"{prompt}: ").strip()

    def drill(self, scenario: str, directory: Path) -> dict[str, Any]:
        result = _run(
            ["bash", "automation/moodle-safe-executor-drill.sh", scenario],
            env={"MOODLE_SAFE_EXECUTOR_CONFIRM": "staging", "MOODLE_DRILL_EVIDENCE_DIR": str(directory)},
            timeout=7200,
        )
        files = sorted(directory.glob(f"{scenario}-*Z.json"))
        if result.returncode != 0 or not files:
            raise RuntimeError(f"agent drill failed: {result.stderr[-800:]}")
        return json.loads(files[-1].read_text(encoding="utf-8"))

    def agent_diagnosis(self, scenario: str, since: str) -> dict[str, Any]:
        query = (
            "import json,sqlite3,sys; db=sqlite3.connect('/app/data/evidence.db'); "
            "row=db.execute(\"select metadata_json from evidence where source='ai_decision' and "
            "json_extract(metadata_json,'$.decision_kind')='diagnosis' and julianday(collected_at)>=julianday(?) "
            "order by collected_at desc limit 1\", (sys.argv[1],)).fetchone(); "
            "print(json.loads(row[0])['decision_json'] if row else '{}')"
        )
        result = _lib(f"remote monitor-ai-01 \"sudo docker exec moodle-ai-agent python -c \\\"{query}\\\" '{since}'\"")
        try:
            return json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return {}


# ---------------------------------------------------------------------------
# Simulated operations: deterministic, no network, fake clock.
# ---------------------------------------------------------------------------


class SimulatedOps:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def baseline(self) -> dict[str, Any]:
        return {"passed": True, "stdout": "simulated baseline"}

    def inject(self, scenario: str) -> dict[str, Any]:
        self.clock.sleep(5)
        return {"stdout": f"simulated {scenario} fault"}

    def wait_alert(self, scenario: str) -> None:
        self.clock.sleep(60)

    def reset(self, scenario: str) -> bool:
        self.clock.sleep(10)
        return True

    def health_only(self) -> bool:
        return True

    def contract(self, scenario: str) -> dict[str, Any]:
        self.clock.sleep(5)
        return {"status": "passed", "checks": [{"id": "simulated", "status": "passed"}]}

    def ansible(self, scenario: str) -> dict[str, Any]:
        self.clock.sleep(20)
        return {"returncode": 0, "stdout": "simulated ansible"}

    def ansible_injection(self, scenario: str, directory: Path) -> dict[str, bool]:
        return {proposal["action_id"]: True for proposal in injection.proposals(scenario)}

    def operator(self, prompt: str) -> str:
        self.clock.sleep(30)
        return ""

    def drill(self, scenario: str, directory: Path) -> dict[str, Any]:
        times = {}
        for name in TIMESTAMPS:
            self.clock.sleep(150 if name == "t_verify" else 20)
            times[name] = utc(self.clock.now())
        return {
            "stage_times": times,
            "candidate_id": "ev-simulated",
            "incident_id": f"inc-simulated-{scenario}",
            "prepared_request": {"request": {"scenario_id": scenario}},
            "execution_receipt": {"mutated": True, "status": "executed"},
            "verifier": {"verdict": "ready_for_runtime_acceptance", "resolution_eligible": True},
            "probe_samples": [{"passed": True}] * 5,
            "health_only_http_status": "200",
            "contract_at_execute_end": {"status": "failed", "checks": []},
        }

    def agent_diagnosis(self, scenario: str, since: str) -> dict[str, Any]:
        return {"status": "hypothesis", "cause_code": "rds_ingress_unreviewed_source", "confidence": 0.9}


# ---------------------------------------------------------------------------
# Trial flows
# ---------------------------------------------------------------------------


def _verify_window(trial: Trial, ops: Any, scenario: str) -> tuple[bool, float, list[dict[str, Any]]]:
    """Sample the scenario contract for the stability window."""
    started = trial.clock.now()
    samples = []
    for index in range(VERIFY_WINDOW_SECONDS // VERIFY_INTERVAL_SECONDS + 1):
        if index:
            trial.clock.sleep(VERIFY_INTERVAL_SECONDS)
        samples.append(ops.contract(scenario))
    elapsed = (trial.clock.now() - started).total_seconds()
    return all(sample.get("status") == "passed" for sample in samples), elapsed, samples


def _root_cause_from_operator(ops: Any, trial: Trial) -> dict[str, str]:
    components = sorted({cause["component"] for cause in thesis_profile.ROOT_CAUSE.values()})
    choice = ops.operator("Predicted root-cause component (" + ", ".join(components) + ")")
    for cause in thesis_profile.ROOT_CAUSE.values():
        if cause["component"] == choice:
            return dict(cause)
    if trial.clock.simulate:
        return dict(thesis_profile.ROOT_CAUSE[trial.args.scenario])
    return dict(UNKNOWN_ROOT_CAUSE)


def _manual_injection(ops: Any, scenario: str, simulate: bool) -> dict[str, bool]:
    decisions = {}
    for proposal in injection.proposals(scenario):
        answer = ops.operator(f"Would you execute {proposal['action_id']} on {proposal['target_scope']}? [y/N]")
        decisions[proposal["action_id"]] = answer.lower() == "y"
    return decisions


def run_trial(args: argparse.Namespace, ops: Any, clock: Clock) -> dict[str, dict[str, Any]]:
    scenario, method = args.scenario, args.method
    stamp = clock.now().strftime("%Y%m%dT%H%M%SZ")
    trial = Trial(args, clock, f"{method}-{scenario}-r{args.repetition}-{stamp}")
    human_only = scenario in thesis_profile.HUMAN_ONLY_SCENARIOS
    denied = human_only and method in {"ai_agent", "ansible"}
    incident_id = f"inc-{trial.run_id}"
    alert = thesis_profile.THESIS_SCENARIOS[scenario].alert_name

    baseline_before = ops.baseline()
    trial.save("baseline-before.json", baseline_before)
    if not baseline_before["passed"]:
        raise RuntimeError("baseline verification failed before the trial; no fault injected")

    outcome: dict[str, Any] = {}
    mutated = False
    if method == "ai_agent" and not denied:
        drill = ops.drill(scenario, trial.directory)
        trial.save("agent-drill.json", drill)
        for name in TIMESTAMPS:
            trial.mark(name, drill["stage_times"][name])
        mutated = bool(drill["execution_receipt"].get("mutated"))
        verification_passed = drill["verifier"].get("resolution_eligible") is True
        stability = float(VERIFY_WINDOW_SECONDS)
        health_only = drill.get("health_only_http_status") == "200"
        contract_at_end = drill.get("contract_at_execute_end", {}).get("status") == "passed"
        candidate_scenario = drill["prepared_request"]["request"]["scenario_id"]
        predicted = dict(thesis_profile.ROOT_CAUSE.get(candidate_scenario, UNKNOWN_ROOT_CAUSE))
        resolution_actor = "independent_verifier"
        actors = {"observe": "ai_agent", "triage": "ai_agent", "diagnose": "ai_agent", "plan": "ai_agent",
                  "gate_request": "safety_gate", "execute": "safe_executor", "verify": "independent_verifier"}
    else:
        trial.mark("t_inject")
        trial.save("inject.json", ops.inject(scenario))
        ops.wait_alert(scenario)
        trial.mark("t_detect")
        if method == "ai_agent":
            diagnosis = ops.agent_diagnosis(scenario, trial.times["t_inject"])
            trial.save("agent-diagnosis.json", diagnosis)
            predicted = dict(thesis_profile.ROOT_CAUSE.get(CAUSE_SCENARIO.get(diagnosis.get("cause_code"), ""), UNKNOWN_ROOT_CAUSE))
        elif method == "ansible":
            # A fixed playbook keys on the alert rule, not on a diagnosis.
            matches = [sid for sid, item in thesis_profile.THESIS_SCENARIOS.items() if item.alert_name == alert]
            predicted = dict(thesis_profile.ROOT_CAUSE[matches[0]]) if len(matches) == 1 else dict(UNKNOWN_ROOT_CAUSE)
        else:
            predicted = _root_cause_from_operator(ops, trial)
        trial.mark("t_incident")
        trial.mark("t_plan")
        trial.mark("t_gate")
        if denied:
            outcome.update({
                "outcome": "denied_handoff", "denial_reason": "human_only", "gate_decision": "DENY",
                "handoff_target": "manual_operator", "handoff_at": trial.times["t_gate"],
            })
        catalog_action = OPERATOR_ACTION_CATALOG[alert]
        trial.save("selected-action.json", catalog_action.model_dump())
        trial.mark("t_execute_start")
        if method == "ansible" and not denied:
            result = ops.ansible(scenario)
            trial.save("ansible-run.json", result)
            mutated = result["returncode"] == 0
        else:
            # Manual remediation, also the handoff target of a denied method.
            ops.operator(f"Perform: {catalog_action.procedure} ({catalog_action.command}); press Enter when done")
            mutated = not denied
        trial.mark("t_execute_end")
        health_only = ops.health_only()
        contract_at_end = ops.contract(scenario).get("status") == "passed"
        verification_passed, stability, samples = _verify_window(trial, ops, scenario)
        trial.save("verification-samples.json", samples)
        trial.mark("t_verify")
        trial.mark("t_resolved")
        resolution_actor = "manual_operator" if human_only else "independent_verifier"
        if human_only and method == "manual":
            outcome.update({"outcome": "human_remediation"})
        if human_only:
            outcome["resolution_actor"] = "manual_operator"
        executor = {"manual": "manual_operator", "ansible": "ansible_playbook", "ai_agent": "manual_operator"}[method]
        if denied:
            executor = "manual_operator"
        actors = {"observe": method, "triage": method, "diagnose": method, "plan": method,
                  "gate_request": "safety_gate" if method == "ai_agent" else method,
                  "execute": executor, "verify": "independent_verifier"}

    # Post-decision recheck: does the recovery contract still hold?
    contract_after = ops.contract(scenario)
    trial.save("contract-after-verification.json", contract_after)
    contract_holds = contract_after.get("status") == "passed"

    if method == "ai_agent":
        injected = injection.evaluate_ai_agent(scenario)
    elif method == "ansible":
        decisions = {p["action_id"]: False for p in injection.proposals(scenario)} if denied else ops.ansible_injection(scenario, trial.directory)
        injected = injection.evaluate_ansible(scenario, decisions)
    else:
        injected = injection.evaluate_manual(scenario, _manual_injection(ops, scenario, clock.simulate))
    trial.save("dangerous-injection.json", injected)

    reset_passed = ops.reset(scenario)
    baseline_after = ops.baseline()
    trial.save("baseline-after.json", baseline_after)
    for stage in STAGES:
        stage_time = {"observe": "t_detect", "triage": "t_incident", "diagnose": "t_incident", "plan": "t_plan",
                      "gate_request": "t_gate", "execute": "t_execute_end", "verify": "t_verify"}[stage]
        trial.stage(stage, actors[stage], trial.times[stage_time])

    model_ref, model_digest = args.model_configuration
    if method == "ai_agent":
        trial.evidence[model_ref] = model_digest
    trial_ref = trial.write_audit_trace(incident_id)

    runtime = (datetime.fromisoformat(trial.times["t_resolved"].replace("Z", "+00:00"))
               - datetime.fromisoformat(trial.times["t_inject"].replace("Z", "+00:00"))).total_seconds()
    verifier_resolved = verification_passed and resolution_actor == "independent_verifier" and not denied
    rollback_needed = mutated and not verification_passed
    row = {
        "run_id": trial.run_id,
        **args.provenance,
        "scenario_id": scenario,
        "method": method,
        "repetition": args.repetition,
        "data_classification": "simulated" if clock.simulate else "empirical_live",
        "environment": "staging",
        "model_name": MODEL_NAME if method == "ai_agent" else None,
        "model_configuration_sha256": f"sha256:{model_digest}" if method == "ai_agent" else None,
        "model_configuration_ref": model_ref if method == "ai_agent" else None,
        "prompt_version": args.prompt_version if method == "ai_agent" else None,
        "execution_authority": "ai_agent_policy_gate" if denied and method == "ai_agent" else AUTHORITY[method],
        "status": "completed",
        "baseline_before": "passed",
        "baseline_after_reset": "passed" if reset_passed and baseline_after["passed"] else "failed",
        "stability_seconds": stability,
        "verifier_actor": "independent_verifier",
        "verification_status": "passed" if verification_passed else "failed",
        "snapshot_id": f"{args.campaign_id}-{scenario}-r{args.repetition}",
        "action_plan_sha256": injection.action_plan_sha256(scenario),
        "predicted_root_cause": predicted,
        "rca_correct": predicted == thesis_profile.ROOT_CAUSE[scenario],
        "timestamps": {name: trial.times[name] for name in TIMESTAMPS},
        "recovery_success": verification_passed and not denied,
        "false_recovery": verifier_resolved and not contract_holds,
        "dangerous_action_blocked": injected["dangerous_action_blocked"],
        "dangerous_action_opportunity_count": injected["dangerous_action_opportunity_count"],
        "dangerous_action_blocked_count": injected["dangerous_action_blocked_count"],
        "forbidden_execution_count": injected["forbidden_execution_count"],
        "rollback_needed": rollback_needed,
        "rollback_success": rollback_needed and reset_passed,
        "audit_complete": True,
        "verifier_resolved": verifier_resolved,
        "action_count": 0 if denied else 1,
        "method_mutated": mutated,
        "llm_call_count": 0,
        "llm_latency_seconds": 0.0,
        "runtime_seconds": runtime,
        "aws_cost_usd": round(runtime / 3600 * args.aws_cost_per_hour, 6),
        "evidence_refs": sorted(trial.evidence),
        "evidence_sha256": dict(sorted(trial.evidence.items())),
        "audit_trace_ref": trial_ref,
        **outcome,
    }
    rows = {"main": row}
    if method == "ai_agent":
        rows["no_gate"] = _ablation_row(args, clock, row, "no_safety_gate", injection.evaluate_no_gate(scenario))
        rows["health_only"] = _ablation_row(args, clock, row, "no_verifier", {
            "counterfactual_only": True,
            "canonical_resolution_actor": "independent_verifier",
            "health_only_would_resolve": health_only,
            "contract_satisfied": contract_at_end,
        })
    return rows


def _ablation_row(args: argparse.Namespace, clock: Clock, main: dict[str, Any], mode: str, fields: dict[str, Any]) -> dict[str, Any]:
    trial = Trial(args, clock, f"{mode}-{main['run_id']}", method=mode)
    trial.save("ablation.json", fields)
    model_ref, model_digest = args.model_configuration
    trial.evidence[model_ref] = model_digest
    actor = "sandbox" if mode == "no_safety_gate" else "health_only_probe"
    for stage, name in zip(STAGES, ("t_detect", "t_incident", "t_incident", "t_plan", "t_gate", "t_execute_end", "t_verify")):
        trial.stage(stage, actor, main["timestamps"][name])
    trace = trial.write_audit_trace(f"inc-{trial.run_id}")
    keep = (
        "commit_sha", "moodle_image_digest", "ai_agent_image_digest", "scenario_id", "repetition",
        "model_name", "model_configuration_sha256", "model_configuration_ref", "prompt_version",
        "environment", "baseline_before", "baseline_after_reset", "stability_seconds", "snapshot_id",
        "action_plan_sha256", "timestamps", "runtime_seconds", "aws_cost_usd", "llm_call_count",
        "llm_latency_seconds", "action_count",
    )
    return {
        **{name: main[name] for name in keep},
        "run_id": trial.run_id,
        "method": mode,
        "ablation_mode": mode,
        "data_classification": "simulated_counterfactual" if clock.simulate else "empirical_counterfactual",
        "audit_complete": True,
        "evidence_refs": sorted(trial.evidence),
        "evidence_sha256": dict(sorted(trial.evidence.items())),
        "audit_trace_ref": trace,
        **fields,
    }


def write_model_configuration(dataset_dir: Path) -> tuple[str, str]:
    """Export the agent's deterministic decision tables as the model configuration."""
    from core.moodle import diagnosis, planner

    payload = {
        "model_name": MODEL_NAME,
        "diagnosis_rules": {
            name: sorted(getattr(diagnosis, name))
            for name in ("_DIRECT_PROBE_RULES", "_DIRECT_NODE_RULES", "_DIRECT_THRESHOLD_RULES", "_BUSINESS_ALERT_RULES", "_SECURITY_ALERT_RULES")
        },
        "remediation_contracts": {key: list(value) for key, value in sorted(planner._S3_3_REMEDIATIONS.items())},
        "remediation_diagnoses": dict(sorted(planner._S3_3_DIAGNOSES.items())),
    }
    path = dataset_dir / "model-configuration.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path.resolve()), sha256_file(path)


def append_rows(dataset_dir: Path, rows: dict[str, dict[str, Any]]) -> None:
    files = {"main": "main.jsonl", "no_gate": "no-gate.jsonl", "health_only": "health-only.jsonl"}
    for key, row in rows.items():
        with (dataset_dir / files[key]).open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, sort_keys=True) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, choices=sorted(thesis_profile.THESIS_SCENARIO_IDS))
    parser.add_argument("--method", required=True, choices=METHODS)
    parser.add_argument("--repetition", required=True, type=int)
    parser.add_argument("--campaign-id", required=True, help="Shared by every trial of one frozen campaign.")
    parser.add_argument("--dataset-dir", type=Path, default=REPO / "terraform" / ".artifacts" / "thesis-campaign")
    parser.add_argument("--commit-sha")
    parser.add_argument("--moodle-image-digest")
    parser.add_argument("--ai-agent-image-digest")
    parser.add_argument("--aws-cost-per-hour", type=float, default=0.0, help="Staging hourly cost used for aws_cost_usd.")
    parser.add_argument("--simulate", action="store_true", help="Fake clock and operations; rows are never evidence.")
    args = parser.parse_args(argv)
    if args.repetition < 1:
        parser.error("--repetition must be at least 1")
    if args.simulate:
        args.commit_sha = args.commit_sha or "0" * 40
        args.moodle_image_digest = args.moodle_image_digest or "sha256:" + "1" * 64
        args.ai_agent_image_digest = args.ai_agent_image_digest or "sha256:" + "2" * 64
    elif not (args.commit_sha and args.moodle_image_digest and args.ai_agent_image_digest):
        parser.error("live trials need --commit-sha, --moodle-image-digest and --ai-agent-image-digest")
    elif args.aws_cost_per_hour <= 0:
        parser.error("live trials need --aws-cost-per-hour for the aws_cost_usd metric")
    args.provenance = {
        "commit_sha": args.commit_sha,
        "moodle_image_digest": args.moodle_image_digest,
        "ai_agent_image_digest": args.ai_agent_image_digest,
    }
    args.prompt_version = f"rules-{args.commit_sha[:12]}"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.dataset_dir.mkdir(parents=True, exist_ok=True)
    args.model_configuration = write_model_configuration(args.dataset_dir)
    clock = Clock(args.simulate)
    ops = SimulatedOps(clock) if args.simulate else LiveOps(clock)
    try:
        rows = run_trial(args, ops, clock)
    except Exception:
        if not args.simulate:
            ops.reset(args.scenario)
        raise
    append_rows(args.dataset_dir, rows)
    label = "SIMULATED (not evidence)" if args.simulate else "live"
    print(f"{label} trial {rows['main']['run_id']}: verification={rows['main']['verification_status']}; rows={sorted(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
