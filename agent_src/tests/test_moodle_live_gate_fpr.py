from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from core.safety.safe_action_audit import SafeActionAuditStore


SCRIPT = Path(__file__).resolve().parents[2] / "automation" / "moodle-live-gate-fpr.py"
spec = importlib.util.spec_from_file_location("moodle_live_gate_fpr", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_real_gate_and_complete_human_labels_produce_rate(tmp_path):
    db = tmp_path / "audit.sqlite3"
    with SafeActionAuditStore(db) as audit:
        allowed = audit.append(incident_id="inc", event_type="live_gate",
                               payload={"decision": "ALLOW", "request_id": "a"})
        denied = audit.append(incident_id="inc", event_type="live_gate",
                              payload={"decision": "DENY", "request_id": "b"})
        audit.append(incident_id="inc", event_type="model_proposal_shadow_gate",
                     payload={"gate_decision": "DENY"})
        audit.append(incident_id="inc", event_type="live_gate",
                     payload={"decision": "REQUIRE_APPROVAL", "request_id": "c"})
    result = module.report(db)
    assert result["decisions"] == {"ALLOW": 1, "DENY": 1, "REQUIRE_APPROVAL": 1}
    assert result["false_block_rate"] is None
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({"event_id": denied["event_id"], "classification": "legitimate_safe",
                                  "reviewer": "independent-reviewer", "evidence_ref": "review-1"}) + "\n")
    assert module.report(db, labels)["false_block_rate"] is None
    with labels.open("a") as handle:
        handle.write(json.dumps({"event_id": allowed["event_id"], "classification": "legitimate_safe",
                                 "reviewer": "independent-reviewer", "evidence_ref": "review-2"}) + "\n")
    result = module.report(db, labels)
    assert result["rate_status"] == "measured"
    assert result["false_block_rate"] == 0.5
    assert result["false_denial_event_ids"] == [denied["event_id"]]
