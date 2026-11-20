#!/usr/bin/env python3
"""Episode runner and campaign driver for the local Moodle lab (see lab.sh for usage).

One episode: verify baseline -> optional background load -> inject one fault or an ambiguous/overlapping variant ->
wait for detection -> collector snapshot (BEFORE any decision) -> behavior policy picks one action from the reviewed
ladder -> execute through the lab executor -> allowed/forbidden/related probes over a stability window -> outcome ->
reset -> re-verify baseline. Every episode, including failures and timeouts, is persisted as an artifact directory
with a manifest of SHA-256 checksums.

Outcome vocabulary (a2 schema): success (executed AND probes confirm), failure (executed, probes did not confirm),
not_executed (human hand-off), denied (executor refused), timeout (detection or executor timeout), cancelled
(runner stopped), missing_evidence (setup/leak/probe-infrastructure problem). Only `success` and `failure` can ever
become a training label; denied/not-executed/timeout/cancelled are never "failed repair".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import statistics
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

import lab_faults
import lab_probes
from action_ladder import ACTION_SPECS, CALL_HUMAN, CATALOG_ACTIONS, COST_LADDER, MATCHING_ACTION, WAIT_AND_RECHECK, spec
from lab_collector import Collector, LeakageError, assert_cutoff
from lab_env import (OUT_DIR, LabCommandError, firing_alerts, iso_ms, parse_iso, sha256_bytes, sha256_file, utc_now)
from lab_executor import LabApprover, LabExecutor, build_request

POLICY_VERSION = "lab-behavior-policy-v1"
EPISODE_SCHEMA = "aihs-lab-episode-v1"
OUTCOMES = ("success", "failure", "not_executed", "denied", "timeout", "cancelled", "missing_evidence")
# Background load stays well below the 70% CPU alert threshold (node = 2 vCPU): 0.3 cpu = 15%, 0.7 cpu = 35%.
LOAD_CHOICES = (("none", None, 0.0, 0.55), ("light_a", "a", 0.3, 0.15), ("light_b", "b", 0.3, 0.15), ("medium_a", "a", 0.7, 0.15))
INJECT_ORDER = ("BIZ-02", "NET-01", "DB-01", "SEC-02", "RES-01", "CON-01")  # BIZ-02 needs the DB and web-a; NET-01 needs both webs; CON-01 last
CANCEL = threading.Event()


def log(message: str) -> None:
    print(f"{iso_ms(utc_now())} {message}", flush=True)


# ---- planning ----------------------------------------------------------------------------------------------------
def build_plan(campaign: str, count: int, seed: int, *, pair_fraction: float = 0.25, transient_fraction: float = 0.12,
               matching_fraction: float = 0.5) -> list[dict]:
    """Deterministic, balanced episode plan. Ground-truth fields here are audit data, never features."""
    rng = random.Random(f"{campaign}:{seed}")
    scenarios = list(lab_faults.LAB_SCENARIOS)
    order: list[str] = []
    while len(order) < count:
        block = scenarios[:]
        rng.shuffle(block)
        order.extend(block)
    other_cycles = {s: [] for s in scenarios}
    plan = []
    for seq in range(count):
        primary = order[seq]
        faults = [primary]
        kind = "single"
        offset = 0.0
        if rng.random() < pair_fraction:
            faults.append(rng.choice([s for s in scenarios if s != primary]))
            kind = "ambiguous_pair"
            offset = float(rng.choice([0, 3, 8, 15]))
        matching = MATCHING_ACTION[rng.choice(faults)]
        transient = rng.random() < transient_fraction
        if rng.random() < matching_fraction:
            action = matching
        else:
            if not other_cycles[primary]:
                others = [a for a in COST_LADDER if a != matching]
                rng.shuffle(others)
                other_cycles[primary] = others
            action = other_cycles[primary].pop()
        if transient and rng.random() < 0.6:
            action = WAIT_AND_RECHECK
        pick = rng.random()
        load, load_node, load_cpus = "none", None, 0.0
        acc = 0.0
        for name, node, cpus, weight in LOAD_CHOICES:
            acc += weight
            if pick <= acc:
                load, load_node, load_cpus = name, node, cpus
                break
        episode_id = "ep-" + hashlib.sha256(f"{campaign}:{seed}:{seq}".encode()).hexdigest()[:12]
        plan.append({
            "seq": seq, "episode_id": episode_id, "kind": kind, "faults": faults, "inject_offset_s": offset,
            "background_load": {"name": load, "node": load_node, "cpus": load_cpus},
            "transient": transient, "heal_after_s": float(rng.choice([8, 15, 25])) if transient else None,
            "settle_s": float(rng.randint(8, 40)), "planned_action": action,
            "matching_action": matching, "exploration": action != matching,
        })
    return plan


def choose_action(snapshot_index: dict, features: dict, plan_item: dict, predicted_at: str) -> dict:
    """Behavior policy: the action was drawn at plan time; this step stamps and validates it AFTER the snapshot.

    The policy sees only the snapshot index/features and may not choose anything outside the reviewed action space.
    Not an AIHS proposal: proposal.source is `other` in the A2 export.
    """
    action = plan_item["planned_action"]
    spec(action)  # fail closed: unknown action raises
    assert_cutoff(snapshot_index, predicted_at)
    return {"action_id": action, "policy_version": POLICY_VERSION, "predicted_at": predicted_at,
            "features_sha256": snapshot_index["features_sha256"],
            "evidence_bundle_sha256": snapshot_index["evidence_bundle_sha256"],
            "evidence_cutoff_at": snapshot_index["evidence_cutoff_at"],
            "feature_count": len(features)}


def classify(*, detected: bool, action: str | None, response: dict | None, http_status: int | None,
             recovered: bool | None, probes_available: bool = True, cancelled: bool = False) -> tuple[str, str]:
    """Map the observed facts to (outcome, status). Never turns an unexecuted action into a failed repair."""
    if cancelled:
        return "cancelled", "cancelled"
    if not detected:
        return "timeout", "detection_timeout"
    if action == CALL_HUMAN:
        return "not_executed", "human_handoff"
    if action is None:
        return "not_executed", "no_action"
    s = spec(action)
    if s.scenario:  # catalog action went through the executor
        status = (response or {}).get("status")
        if http_status in (403, 409, 423, 400) or status in ("denied", "awaiting_approval", "disabled", "in_progress"):
            return "denied", f"executor_{status or 'rejected'}"
        if status in ("failed", "partial_failure"):
            failure = (response or {}).get("failure") or {}
            return ("timeout", "executor_timeout") if failure.get("status") == "timeout" else ("failure", "executor_error")
        if status != "executed":
            return "missing_evidence", "executor_unknown_status"
    if not probes_available:
        return "missing_evidence", "probe_infrastructure_unavailable"
    return ("success", "recovered_stable") if recovered else ("failure", "probes_not_confirmed")


# ---- episode -----------------------------------------------------------------------------------------------------
def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def write_manifest(directory: Path) -> dict:
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[path.relative_to(directory).as_posix()] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {"schema": "aihs-lab-episode-manifest-v1", "created_at": iso_ms(utc_now()), "files": files,
                "files_sha256": sha256_bytes(json.dumps(files, sort_keys=True).encode())}
    write_json(directory / "manifest.json", manifest)
    return manifest


def lab_code_sha256() -> str:
    here = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(here.glob("lab_*.py")) + [here / "node_probe.py", here / "docker-compose.yml"]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class Campaign:
    def __init__(self, name: str, out_root: Path, *, worker: str, stability_s: float, verify_timeout_s: float,
                 detect_timeout_s: float, wait_s: float):
        self.name = name
        self.root = out_root / name
        self.worker = worker
        self.stability_s = stability_s
        self.verify_timeout_s = verify_timeout_s
        self.detect_timeout_s = detect_timeout_s
        self.wait_s = wait_s
        self.root.mkdir(parents=True, exist_ok=True)
        self.approver = LabApprover.load_or_create(out_root / "_keys" / "lab-approval-ed25519.pem")
        self.executor = LabExecutor(self.root / f"executor-state-{worker}.sqlite3", self.approver.public_key)
        self.code_sha = lab_code_sha256()

    def episode_dir(self, item: dict) -> Path:
        return self.root / "episodes" / f"{item['seq']:05d}-{item['episode_id']}"

    def done(self, item: dict) -> bool:
        return (self.episode_dir(item) / "manifest.json").is_file()

    # -- phases --
    def wait_for_detection(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not CANCEL.is_set():
            try:
                if firing_alerts():
                    return iso_ms(utc_now())
            except Exception:
                pass
            time.sleep(2)
        return None

    def verify_recovery(self, directory: Path) -> tuple[bool | None, str | None, int]:
        """Return (recovered, stable_since, rounds). recovered=None means the probes themselves were unavailable."""
        started = time.monotonic()
        stable_since = None
        stable_mono = None
        rounds = 0
        with (directory / "probes.jsonl").open("w") as handle:
            while time.monotonic() - started < self.verify_timeout_s and not CANCEL.is_set():
                try:
                    round_ = lab_probes.probe_round()
                except Exception as exc:
                    round_ = {"at": iso_ms(utc_now()), "ok": False, "error": type(exc).__name__}
                rounds += 1
                handle.write(json.dumps(round_, sort_keys=True) + "\n")
                handle.flush()
                if round_["ok"]:
                    if stable_mono is None:
                        stable_mono, stable_since = time.monotonic(), round_["at"]
                    if time.monotonic() - stable_mono >= self.stability_s:
                        return True, stable_since, rounds
                else:
                    stable_mono, stable_since = None, None
                time.sleep(4)
        return False, None, rounds

    def run_episode(self, item: dict) -> dict:
        directory = self.episode_dir(item)
        directory.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        ep = {"schema": EPISODE_SCHEMA, "episode_id": item["episode_id"], "seq": item["seq"], "campaign": self.name,
              "worker": self.worker, "lab_code_sha256": self.code_sha, "environment": "lab",
              "plan": item, "timeline": {}, "outcome": None, "status": None, "reasons": []}
        tl = ep["timeline"]
        injected: list[str] = []
        heal_timer: threading.Timer | None = None
        detected_at = None
        action = None
        response = None
        http_status = None
        recovered: bool | None = None
        try:
            base = lab_probes.ensure_baseline()
            write_json(directory / "baseline_pre.json", base)
            tl["baseline_verified_at"] = base["at"]
            if not base["ok"]:
                ep["outcome"], ep["status"] = "missing_evidence", "baseline_not_clean"
                return self._finish(ep, directory, t0, injected)
            load = item["background_load"]
            if load["node"]:
                lab_faults.start_background_load(load["node"], load["cpus"])
                time.sleep(12)  # let the load show up in the CPU series before the fault
            tl["load_started_at"] = iso_ms(utc_now()) if load["node"] else None
            ordered = sorted(item["faults"], key=INJECT_ORDER.index)
            for i, scenario in enumerate(ordered):
                if i and item["inject_offset_s"]:
                    time.sleep(item["inject_offset_s"])
                lab_faults.inject(scenario)
                injected.append(scenario)
                tl.setdefault("injected_at", []).append(iso_ms(utc_now()))
            detected_at = self.wait_for_detection(self.detect_timeout_s)
            tl["detected_at"] = detected_at
            if CANCEL.is_set():
                ep["outcome"], ep["status"] = classify(detected=bool(detected_at), action=None, response=None, http_status=None, recovered=None, cancelled=True)
                return self._finish(ep, directory, t0, injected)
            if not detected_at:
                ep["outcome"], ep["status"] = classify(detected=False, action=None, response=None, http_status=None, recovered=None)
                return self._finish(ep, directory, t0, injected)
            time.sleep(item["settle_s"])
            # ---- snapshot: strictly before any decision exists ----
            collector = Collector(directory)
            try:
                snap = collector.collect("snapshot", detected_at, extra_forbidden=[item["episode_id"]])
            except LeakageError as exc:
                ep["outcome"], ep["status"], ep["reasons"] = "missing_evidence", "leakage_blocked", [str(exc)[:200]]
                return self._finish(ep, directory, t0, injected)
            features = json.loads((directory / "snapshot" / "features.json").read_text())
            tl["evidence_cutoff_at"] = snap["evidence_cutoff_at"]
            if item["transient"]:
                heal_timer = threading.Timer(item["heal_after_s"], lambda: [lab_faults.reset(s) for s in reversed(ordered)])
                heal_timer.daemon = True
                heal_timer.start()
            predicted_at = iso_ms(utc_now())
            proposal = choose_action(snap, features, item, predicted_at)
            action = proposal["action_id"]
            write_json(directory / "proposal.json", proposal)
            tl["predicted_at"] = predicted_at
            # ---- act ----
            s = spec(action)
            if action == CALL_HUMAN:
                pass
            elif action == WAIT_AND_RECHECK:
                tl["executed_at"] = iso_ms(utc_now())
                time.sleep(self.wait_s)
                response, http_status = {"status": "executed", "mutated": False, "completed_nodes": [], "observe_only": True}, 200
                tl["execution_finished_at"] = iso_ms(utc_now())
            else:
                key = f"lab-{item['episode_id']}-{hashlib.sha256(os.urandom(8)).hexdigest()[:8]}"
                request = build_request(action, key, self.approver)
                nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:32]
                tl["executed_at"] = iso_ms(utc_now())
                http_status, response = self.executor.execute(request, nonce)
                tl["execution_finished_at"] = iso_ms(utc_now())
                write_json(directory / "execution.json", {"request_action_sha256": request["action_sha256"], "http_status": http_status,
                                                          "scenario_id": request["scenario_id"], "target_scope": request["target_scope"],
                                                          "receipt": response})
            executed = action not in (CALL_HUMAN,) and (response or {}).get("status") == "executed"
            probes_available = True
            if executed:
                recovered, stable_since, rounds = self.verify_recovery(directory)
                tl["verified_at"] = iso_ms(utc_now())
                tl["stable_since"] = stable_since
                ep["verification"] = {"rounds": rounds, "stability_window_s": self.stability_s, "timeout_s": self.verify_timeout_s}
                try:
                    from lab_env import prom_query
                    prom_query("up")
                except Exception:
                    probes_available = False
            if s.scenario or action in (WAIT_AND_RECHECK, CALL_HUMAN):
                ep["outcome"], ep["status"] = classify(detected=True, action=action, response=response, http_status=http_status,
                                                       recovered=recovered, probes_available=probes_available, cancelled=CANCEL.is_set())
            return self._finish(ep, directory, t0, injected, heal_timer)
        except Exception as exc:  # contained: the episode is still persisted and the lab reset
            ep["outcome"], ep["status"] = "missing_evidence", "runner_exception"
            ep["reasons"].append(f"{type(exc).__name__}: {str(exc)[:200]}")
            (directory / "runner-error.txt").write_text(traceback.format_exc())
            return self._finish(ep, directory, t0, injected, heal_timer)

    def _finish(self, ep: dict, directory: Path, t0: float, injected: list[str], heal_timer: threading.Timer | None = None) -> dict:
        if heal_timer is not None:
            heal_timer.cancel()
        reset_ok = True
        try:
            lab_faults.reset_all()
            post = lab_probes.ensure_baseline(max_wait=300)
            reset_ok = post["ok"]
            write_json(directory / "baseline_post.json", post)
        except Exception as exc:
            reset_ok = False
            ep["reasons"].append(f"reset_error: {type(exc).__name__}: {str(exc)[:200]}")
        ep["timeline"]["reset_done_at"] = iso_ms(utc_now())
        ep["reset_verified"] = reset_ok
        ep["duration_seconds"] = round(time.monotonic() - t0, 1)
        write_json(directory / "episode.json", ep)
        write_manifest(directory)
        return ep


# ---- campaign driver -----------------------------------------------------------------------------------------------
def summarize(root: Path) -> dict:
    cells: Counter = Counter()
    durations = []
    outcomes: Counter = Counter()
    for path in sorted((root / "episodes").glob("*/episode.json")):
        ep = json.loads(path.read_text())
        action = (json.loads((path.parent / "proposal.json").read_text())["action_id"]
                  if (path.parent / "proposal.json").is_file() else "none")
        primary = ep["plan"]["faults"][0] + ("+" + ep["plan"]["faults"][1] if len(ep["plan"]["faults"]) > 1 else "")
        cells[f"{primary}|{action}|{ep['outcome']}"] += 1
        outcomes[ep["outcome"]] += 1
        durations.append(ep.get("duration_seconds", 0))
    return {"episodes": sum(outcomes.values()), "by_outcome": dict(outcomes), "cells": dict(sorted(cells.items())),
            "median_duration_seconds": statistics.median(durations) if durations else None}


def run_batch(args) -> int:
    root_out = Path(args.out)
    campaign = Campaign(args.campaign, root_out, worker=args.worker, stability_s=args.stability, verify_timeout_s=args.verify_timeout,
                        detect_timeout_s=args.detect_timeout, wait_s=args.wait)
    plan = build_plan(args.campaign, args.episodes, args.seed)
    write_json(campaign.root / "plan.json", {"campaign": args.campaign, "seed": args.seed, "episodes": args.episodes, "plan": plan})
    mine = [p for p in plan if p["seq"] % args.workers == args.worker_index]
    todo = [p for p in mine if not campaign.done(p)]
    log(f"campaign={args.campaign} worker={args.worker} planned={len(mine)} remaining={len(todo)}")
    durations: list[float] = []
    unhealthy = 0
    for n, item in enumerate(todo, 1):
        if CANCEL.is_set():
            log("cancel requested; stopping before next episode")
            break
        started = time.monotonic()
        ep = campaign.run_episode(item)
        durations.append(time.monotonic() - started)
        unhealthy = 0 if ep.get("reset_verified") else unhealthy + 1
        median = statistics.median(durations)
        remaining = len(todo) - n
        progress = {"campaign": args.campaign, "worker": args.worker, "done": len(mine) - remaining, "total": len(mine),
                    "median_episode_seconds": round(median, 1), "eta_seconds": round(median * remaining),
                    "last": {"seq": item["seq"], "outcome": ep["outcome"], "status": ep["status"]}, "updated_at": iso_ms(utc_now()),
                    "summary": summarize(campaign.root)}
        write_json(root_out / f"progress-{args.worker}.json", progress)
        log(f"episode seq={item['seq']} outcome={ep['outcome']} status={ep['status']} {ep['duration_seconds']}s "
            f"done={progress['done']}/{progress['total']} eta={progress['eta_seconds']}s")
        if unhealthy >= 3:
            log("lab failed to return to baseline three times in a row; stopping for operator attention")
            return 3
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    vb = sub.add_parser("verify-baseline")
    vb.add_argument("--quiet", action="store_true")
    for name in ("batch", "pilot", "episode"):
        p = sub.add_parser(name)
        p.add_argument("--campaign", default="pilot" if name != "batch" else "campaign-1")
        p.add_argument("--episodes", type=int, default=24)
        p.add_argument("--seed", type=int, default=20261115)
        p.add_argument("--workers", type=int, default=1)
        p.add_argument("--worker-index", type=int, default=0)
        p.add_argument("--worker", default=os.environ.get("LAB_PROJECT", "aihs-lab"))
        p.add_argument("--out", default=str(OUT_DIR))
        p.add_argument("--stability", type=float, default=25.0)
        p.add_argument("--verify-timeout", type=float, default=100.0)
        p.add_argument("--detect-timeout", type=float, default=150.0)
        p.add_argument("--wait", type=float, default=20.0, help="seconds an observe-only wait_and_recheck waits")
        if name == "episode":
            p.add_argument("--scenario", choices=lab_faults.LAB_SCENARIOS, default="DB-01")
            p.add_argument("--action", choices=sorted(ACTION_SPECS), default=None)
    summ = sub.add_parser("summarize")
    summ.add_argument("--campaign", required=True)
    summ.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: CANCEL.set())
    signal.signal(signal.SIGINT, lambda *_: CANCEL.set())
    if args.command == "verify-baseline":
        result = lab_probes.verify_baseline()
        print(json.dumps(result if not args.quiet else {"ok": result["ok"], "fixture_markers_present": result["fixture_markers_present"]}, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "summarize":
        print(json.dumps(summarize(Path(args.out) / args.campaign), indent=2, sort_keys=True))
        return 0
    if args.command == "episode":
        plan = build_plan(f"adhoc-{args.scenario}", 1, args.seed)[0]
        plan.update({"faults": [args.scenario], "kind": "single", "inject_offset_s": 0.0, "transient": False, "heal_after_s": None,
                     "background_load": {"name": "none", "node": None, "cpus": 0.0},
                     "matching_action": MATCHING_ACTION[args.scenario]})
        plan["planned_action"] = args.action or MATCHING_ACTION[args.scenario]
        plan["exploration"] = plan["planned_action"] != plan["matching_action"]
        campaign = Campaign(args.campaign, Path(args.out), worker=args.worker, stability_s=args.stability,
                            verify_timeout_s=args.verify_timeout, detect_timeout_s=args.detect_timeout, wait_s=args.wait)
        ep = campaign.run_episode(plan)
        print(json.dumps({k: ep[k] for k in ("episode_id", "outcome", "status", "duration_seconds", "timeline")}, indent=2, sort_keys=True))
        return 0
    return run_batch(args)


if __name__ == "__main__":
    sys.exit(main())
