"""Offline shadow replay of the locked C0 proposals through the adapter, Gate and handoff preflight.

Reads ONLY the proposal file (no labels, outcomes or A2 records) and the draft mapping.
Nothing is executed or approved and no score is computed: the output is the per-proposal
adapter/Gate verdict, which depends on the proposed action, the mapping and the catalog only.

Replay clock: C0 proposals were generated in batch after the fact, so ``predicted_at`` is the
batch time, far later than the evidence. The primary run replays each decision at
``evidence_cutoff_at + 1s`` (evidence freshness as at decision time); a control run uses the
recorded batch ``predicted_at`` and must fail closed with ``stale_evidence``.
The scenario in the context is the one the draft mapping binds to the proposed action, so the
Gate verdict means "this typed request is acceptable inside its own reviewed scope", not "the
action is right for this incident". Whether it is right is a stage-4 scoring question.
"""

import argparse
import collections
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent_src"))

from core.evidence.adapters import SafeActionRequest  # noqa: E402
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash  # noqa: E402
from core.moodle.aihs_c0 import C0Proposal, adapt_c0_proposal, load_c0_mapping  # noqa: E402
from core.moodle.aihs_contract import DecisionContext  # noqa: E402
from core.moodle.aihs_shadow import evaluate_aihs_shadow  # noqa: E402
from core.moodle.aihs_staging_handoff import prepare_aihs_staging_handoff  # noqa: E402
from core.safety.action_catalog import ActionCatalog  # noqa: E402
from core.safety.safe_action_audit import SafeActionAuditStore  # noqa: E402
from core.safety.safe_executor_client import action_digest  # noqa: E402
from core.schema.action import RollbackPlan, TypedAction  # noqa: E402
from core.schema.common import Environment  # noqa: E402
from core.schema.evidence import Evidence  # noqa: E402

EVAL = ROOT / "evaluation/aihs"
DEFAULT_PROPOSALS = EVAL / ".artifacts/phase2/paper/proposals-test.jsonl"
DEFAULT_MAPPING = EVAL / "a3-c0-action-mapping-draft.json"
# The Gate needs an input-independent planner confidence; C0's own p_success is a model output and is
# deliberately NOT used as an authorization input. 0.9 sits above the staging auto threshold (0.8).
TRUSTED_CONFIDENCE = 0.9


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def put_evidence(store, incident_id, snapshot_sha256, at):
    refs = [f"{incident_id}-ev-{n}" for n in (1, 2, 3)]
    for index, ref in enumerate(refs, 1):
        item = Evidence(
            evidence_id=ref, incident_id=incident_id, resource_id="moodle-app", source="prometheus",
            observed_at=at, collected_at=at,
            summary=f"replay stand-in {index}/3 anchored to snapshot sha256 {snapshot_sha256[:16]}",
            content_hash="pending",
        )
        store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))
    return refs


def reviewed_request(rule, context, catalog, seed):
    entry = catalog.lookup(rule.catalog_action_id)
    digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return SafeActionRequest(
        request_id=f"req-{digest}", idempotency_key=f"idem-{digest}",
        catalog_action_id=rule.catalog_action_id, scenario_id=rule.scenario_id,
        target_scope=rule.target_scope,
        action=TypedAction(
            action_id=f"act-{digest}", incident_id=context.incident_id,
            action_type=entry.action_type, target_resource_id=rule.target_resource_id,
            environment=Environment.STAGING,
            reason=f"Offline replay of a C0 proposal inside the {rule.scenario_id} reviewed scope.",
            evidence_refs=list(context.evidence_refs),
            preconditions=list(rule.preconditions),
            expected_outcome="Reapply the reviewed staging baseline; an independent verifier must confirm recovery.",
            reversible=True,
            rollback_plan=RollbackPlan(
                available=True, rollback_action_id=entry.rollback_action,
                method=f"Reapply the reviewed safe baseline using {entry.rollback_action}.",
            ),
            requires_approval=True,
        ),
    )


def replay_one(row, *, mapping, catalog, evidence_db, audit_db, public_key, clock, tag=""):
    """Run one proposal; returns a label-free record. ``clock`` is "decision" or "recorded"."""
    record_id = row.get("record_id", "invalid") if isinstance(row, dict) else "invalid"
    action = row.get("proposed_action") if isinstance(row, dict) else None
    rule = next((e for e in mapping.actions if e.aihs_action == action), None)
    incident_id = f"inc-c0-{clock}-{record_id}{tag}"
    snapshot = row.get("snapshot_sha256", "0" * 64) if isinstance(row, dict) else "0" * 64
    try:
        cutoff = parse_time(row["evidence_cutoff_at"])
        recorded = parse_time(row["predicted_at"])
    except (KeyError, TypeError, ValueError):
        cutoff = recorded = datetime(2000, 1, 1, tzinfo=timezone.utc)
    decision_time = cutoff + timedelta(seconds=1) if clock == "decision" else recorded
    with SQLiteEvidenceStore(evidence_db) as store:
        refs = put_evidence(store, incident_id, snapshot, cutoff)
    bound = rule if rule is not None and rule.scenario_id else None
    context = DecisionContext(
        incident_id=incident_id, scenario_id=bound.scenario_id if bound else "UNK-00", environment="staging",
        trusted_target_scope=bound.target_scope if bound else "none",
        trusted_target_resource_id=bound.target_resource_id if bound else "none",
        trusted_target_resource_type=bound.target_resource_type if bound else "application",
        evidence_refs=refs, predicted_at=decision_time, model_version=str(row.get("model_version", "unknown")) if isinstance(row, dict) else "unknown",
        data_version="phase2-freeze-17efc7b6ad13bd7a7b92",
    )
    now = decision_time + timedelta(seconds=1)
    adapter = adapt_c0_proposal(
        row, context=context, evidence_db=evidence_db, mapping=mapping, now=now,
        catalog=catalog, allow_draft_for_replay=True,
    )
    request = reviewed_request(bound, context, catalog, str(row.get("record_hash"))) if bound and bound.required_role else None
    shadow = evaluate_aihs_shadow(
        row, context=context, evidence_db=evidence_db, mapping=mapping, audit_db=audit_db,
        gate_public_key=public_key, gate_actors={"replay-operator"}, reviewed_request=request,
        trusted_confidence=TRUSTED_CONFIDENCE, now=now, adapter_result=adapter,
    )
    # Handoff preflight uses the strict adapter (drafts are NOT live_reviewed) so it must block.
    strict = adapt_c0_proposal(
        row, context=context, evidence_db=evidence_db, mapping=mapping, now=now, catalog=catalog,
    )
    handoff = prepare_aihs_staging_handoff(
        row, context=context, evidence_db=evidence_db, mapping=mapping, candidate_id=f"no-planner-candidate-{record_id}{tag}",
        gate_public_key=public_key, gate_actors={"replay-operator"}, audit_db=audit_db, now=now, adapter_result=strict,
    )
    executor_digest = (
        action_digest(request.scenario_id, request.catalog_action_id, request.target_scope, request.idempotency_key)
        if request else None
    )
    if handoff["dispatched"] or handoff["execution_permitted"] or shadow["dispatched"] or shadow["execution_permitted"]:
        raise RuntimeError("replay acquired execution authority")
    return {
        "record_id": record_id, "record_hash": row.get("record_hash") if isinstance(row, dict) else None,
        "clock": clock, "proposed_action": action, "path": row.get("path") if isinstance(row, dict) else None,
        "adapter_status": adapter.status, "adapter_reason": adapter.reason_code,
        "disposition": shadow["disposition"], "gate_decision": shadow["gate_decision"],
        "gate_reason": shadow["gate_reason"], "gate_action_sha256": shadow["gate_action_sha256"],
        "executor_action_sha256": executor_digest, "scenario_id": adapter.scenario_id if bound else None,
        "target_scope": adapter.target_scope, "evidence_ref_count": len(refs),
        "handoff_status": handoff["status"], "handoff_reason": handoff["reason"],
        "shadow_audit_event_hash": shadow["audit_event_hash"], "handoff_audit_event_hash": handoff["audit_event_hash"],
        "execution_permitted": False, "dispatched": False,
    }


def verdict_of(entry):
    if entry["gate_decision"]:
        return entry["gate_decision"]
    return "HUMAN" if entry["adapter_reason"] in {"human_only_action", "observe_only_action"} else "BLOCKED_BEFORE_GATE"


def summarize(results):
    table = collections.Counter((r["proposed_action"], verdict_of(r)) for r in results)
    return {
        "total": len(results),
        "by_action_and_verdict": {f"{a}|{v}": n for (a, v), n in sorted(table.items(), key=lambda kv: str(kv[0]))},
        "by_verdict": dict(collections.Counter(verdict_of(r) for r in results)),
        "by_adapter_reason": dict(collections.Counter(r["adapter_reason"] for r in results)),
        "by_handoff_status_reason": dict(collections.Counter(f"{r['handoff_status']}|{r['handoff_reason']}" for r in results)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proposals", type=Path, default=DEFAULT_PROPOSALS)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--out-dir", type=Path, default=EVAL / ".artifacts/phase3/c0-shadow-replay")
    parser.add_argument("--summary", type=Path, default=EVAL / "c0-shadow-replay-2026-11-16.json")
    args = parser.parse_args()

    raw = args.proposals.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    mapping = load_c0_mapping(args.mapping)
    catalog = ActionCatalog.load_moodle_7_scenario_catalog()
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        evidence_db, audit_db = Path(work) / "evidence.sqlite3", Path(work) / "audit.sqlite3"
        common = dict(mapping=mapping, catalog=catalog, evidence_db=evidence_db, audit_db=audit_db, public_key=public_key)
        primary = [replay_one(r, clock="decision", **common) for r in rows]
        recorded = [replay_one(r, clock="recorded", **common) for r in rows]
        controls = []
        base = rows[0]
        for number, (label, mutated) in enumerate((
            ("unknown_action_DROP_DATABASE", {**base, "proposed_action": "DROP_DATABASE"}),
            ("reset_only_action_reconcile_db01_safe_baseline", {**base, "proposed_action": "reconcile_db01_safe_baseline"}),
            ("out_of_space_action_disable_moodle_cli_maintenance_mode", {**base, "proposed_action": "disable_moodle_cli_maintenance_mode"}),
            ("alibaba_code_RI", {**base, "proposed_action": "RI"}),
            ("extra_field_scenario_injected", {**base, "scenario_id": "DB-01"}),
            ("wrong_baseline", {**base, "baseline_id": "aihs-public-prototype-as-is-v1"}),
            ("missing_proposed_action", {k: v for k, v in base.items() if k != "proposed_action"}),
            ("empty_proposal", {}),
        )):
            controls.append({"control": label, **replay_one(mutated, clock="decision", tag=f"-ctl{number}", **common)})
        with SafeActionAuditStore(audit_db, read_only=True) as audit:
            incidents = {f"inc-c0-{c}-{r['record_id']}" for c in ("decision", "recorded") for r in primary}
            incidents |= {f"inc-c0-decision-{base['record_id']}-ctl{n}" for n in range(8)} | {"inc-c0-decision-invalid-ctl7"}
            chains_ok = all(audit.verify_chain(i) for i in incidents)

    for path, data in (("primary-decision-clock.jsonl", primary), ("control-recorded-clock.jsonl", recorded),
                       ("control-malformed-and-unknown.jsonl", controls)):
        (args.out_dir / path).write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in data), encoding="utf-8")
    # Every proposal must validate as a C0 row before it is counted.
    schema_valid = sum(1 for r in rows if _valid(r))
    summary = {
        "schema_version": 1, "stage": "stage3_c0_shadow_replay_offline",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "proposals_file": args.proposals.relative_to(ROOT).as_posix() if args.proposals.is_relative_to(ROOT) else str(args.proposals),
        "proposals_file_sha256": hashlib.sha256(raw).hexdigest(),
        "mapping_file": args.mapping.relative_to(ROOT).as_posix(), "mapping_version": mapping.mapping_version,
        "mapping_review_status": mapping.review_status, "approved_live_reviewed_actions": sum(
            1 for e in mapping.actions if e.status == "live_reviewed"),
        "labels_opened": False, "scored": False, "executor_calls": 0, "resolved_incidents": 0,
        "trusted_confidence_used": TRUSTED_CONFIDENCE, "audit_chains_verified": chains_ok,
        "proposal_rows": len(rows), "proposal_rows_schema_valid": schema_valid,
        "primary_decision_clock": summarize(primary),
        "control_recorded_batch_clock": summarize(recorded),
        "negative_controls": [{k: c[k] for k in ("control", "adapter_reason", "disposition", "gate_decision", "handoff_reason")} for c in controls],
        "outputs_dir": args.out_dir.relative_to(ROOT).as_posix() if args.out_dir.is_relative_to(ROOT) else str(args.out_dir),
    }
    args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def _valid(row):
    try:
        C0Proposal.model_validate(row)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    main()
