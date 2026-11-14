"""Offline AIHS shadow replay with persisted, sanitized audit evidence.

Actual public-prototype output uses its bundled synthetic data. A separate
NOP fixture exercises the real Gate with a test-only read-only mapping. No
AWS, approval, Executor, or live Moodle recovery is involved.
"""

import ast
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent_src"))

from core.evidence.adapters import SafeActionRequest  # noqa: E402
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash  # noqa: E402
from core.moodle.aihs_contract import DecisionContext, MappingDefinition, load_mapping  # noqa: E402
from core.moodle.aihs_shadow import evaluate_aihs_shadow  # noqa: E402
from core.safety.safe_action_audit import SafeActionAuditStore  # noqa: E402
from core.schema.action import RollbackPlan, TypedAction  # noqa: E402
from core.schema.common import ActionType, Environment, ResourceType  # noqa: E402
from core.schema.evidence import Evidence  # noqa: E402


EVAL = ROOT / "evaluation/aihs"
SOURCE = EVAL / ".artifacts/dcbrain-07314287ca0858b9a7d8dba951c96394487e2be2/AIHS_prototype"
PYTHON = EVAL / ".artifacts/venv/bin/python"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prototype_output():
    manifest = json.loads((EVAL / "source-manifest.json").read_text())
    for entry in manifest["source"]["files"]:
        path = SOURCE.parent / entry["path"]
        if not path.is_file() or sha(path) != entry["sha256"]:
            raise RuntimeError(f"unverified source file: {entry['path']}")
    completed = subprocess.run(
        [str(PYTHON), "AIHS_api.py"], cwd=SOURCE, text=True,
        capture_output=True, timeout=90, check=True,
    )
    output = ast.literal_eval(completed.stdout.strip())
    if not isinstance(output, dict):
        raise RuntimeError("AIHS output is not a dict")
    return output


def evidence_context(db, incident, now):
    refs = [f"{incident}-ev-{number}" for number in (1, 2, 3)]
    with SQLiteEvidenceStore(db) as store:
        for ref in refs:
            item = Evidence(
                evidence_id=ref, incident_id=incident, resource_id="moodle-app",
                source="prometheus", observed_at=now - timedelta(seconds=10),
                collected_at=now - timedelta(seconds=10),
                summary="synthetic numeric observation", content_hash="pending",
            )
            store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))
    return DecisionContext(
        incident_id=incident, scenario_id="CON-01", environment="staging",
        trusted_target_scope="moodle-app-b", trusted_target_resource_id="moodle-app",
        trusted_target_resource_type=ResourceType.APPLICATION,
        evidence_refs=refs, predicted_at=now,
        model_version="aihs-public-prototype-as-is-v1",
        data_version="bundled-synthetic-sha256-5b4c69b35de3",
    )


def test_only_mapping(base):
    raw = base.model_dump(mode="json")
    raw["review_status"] = "reviewed_for_shadow"
    raw["mapping_version"] = "test-only-observation-v1"
    entry = next(item for item in raw["actions"] if item["aihs_action"] == "NOP")
    entry.update({
        "status": "shadow_reviewed", "catalog_action_id": "wait_for_alb_target_health",
        "scenario_id": "CON-01", "target_scope": "moodle-app-b",
        "target_resource_id": "moodle-app", "target_resource_type": "application",
        "required_role": "verifier", "reason": "Synthetic read-only Gate fixture, not domain approval.",
    })
    return MappingDefinition.model_validate(raw)


def reviewed_test_request(context):
    return SafeActionRequest(
        request_id=f"req-{uuid4()}", idempotency_key=f"idem-{uuid4()}",
        catalog_action_id="wait_for_alb_target_health", scenario_id="CON-01",
        target_scope="moodle-app-b",
        action=TypedAction(
            action_id=f"act-{uuid4()}", incident_id=context.incident_id,
            action_type=ActionType.READ_HEALTH, target_resource_id="moodle-app",
            environment=Environment.STAGING, reason="test-only read-only observation",
            evidence_refs=list(context.evidence_refs),
            expected_outcome="ALB target state observed", reversible=True,
            rollback_plan=RollbackPlan(available=False),
        ),
    )


def main():
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    work = EVAL / ".artifacts" / f"b1-shadow-{run_id}"
    work.mkdir(parents=True, exist_ok=False)
    evidence_db, audit_db = work / "evidence.sqlite3", work / "audit.sqlite3"
    base = load_mapping(EVAL / "a3-action-mapping.json")
    output = prototype_output()
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    cases = []
    actual = evidence_context(evidence_db, "inc-synthetic-aihs-output", now)
    fixture = evidence_context(evidence_db, "inc-test-only-gate-fixture", now)
    for label, model_output, context, mapping, request, confidence in (
        ("actual_prototype_RI_unmapped", output, actual, base, None, None),
        ("unknown_action", {**output, "action": ["DROP_DATABASE"]}, actual, base, None, None),
        ("model_unavailable", None, actual, base, None, None),
        ("test_only_NOP_gate", {**output, "action": ["NOP"]}, fixture,
         test_only_mapping(base), reviewed_test_request(fixture), 0.9),
    ):
        result = evaluate_aihs_shadow(
            model_output, context=context, evidence_db=evidence_db,
            mapping=mapping, audit_db=audit_db, gate_public_key=public_key,
            gate_actors={"test-operator"}, reviewed_request=request,
            trusted_confidence=confidence, now=now + timedelta(seconds=1),
        )
        if result["execution_permitted"] or result["dispatched"] or result["resolution_eligible"]:
            raise RuntimeError("shadow evaluation acquired execution authority")
        cases.append({
            "case": label, "input_class": "public_prototype_synthetic" if label.startswith("actual_") else "synthetic_test_fixture",
            "model_version": result["model_version"], "mapping_version": result["mapping_version"],
            "adapter_status": result["adapter_status"], "adapter_reason": result["adapter_reason"],
            "disposition": result["disposition"], "gate_decision": result["gate_decision"],
            "gate_reason": result["gate_reason"], "audit_event_hash": result["audit_event_hash"],
            "execution_permitted": False, "dispatched": False,
        })

    with SafeActionAuditStore(audit_db, read_only=True) as audit:
        if not all(audit.verify_chain(incident) for incident in (actual.incident_id, fixture.incident_id)):
            raise RuntimeError("shadow audit chain is invalid")
    if [item["gate_decision"] for item in cases] != [None, None, None, "ALLOW"]:
        raise RuntimeError("unexpected shadow Gate outcomes")
    result = {
        "schema_version": 1, "stage": "B1.2_aihs_shadow_offline",
        "run_id": run_id, "source_commit": "07314287ca0858b9a7d8dba951c96394487e2be2",
        "scope": "offline_synthetic_no_live_moodle_recovery",
        "audit_db_local": str(audit_db.relative_to(ROOT)),
        "audit_chain_verified": True, "cases": cases,
        "approved_repository_mapping_count": 0,
        "executor_calls": 0, "resolved_incidents": 0,
    }
    path = EVAL / "b1-shadow-replay-2026-11-14.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"run_id": run_id, "cases": cases, "audit_chain_verified": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
