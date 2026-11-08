#!/usr/bin/env python3
"""Measure false DENY decisions from real live Gate audit and human adjudications.

Input labels are JSONL with event_id, classification, reviewer, evidence_ref.
The script never labels a decision automatically and never counts shadow/demo Gate.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from core.safety.safe_action_audit import SafeActionAuditStore


def report(audit_db: Path, labels_path: Path | None = None, since: str | None = None) -> dict:
    start = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    if start is not None and start.tzinfo is None:
        raise ValueError("--since must include UTC offset")
    labels: dict[str, dict] = {}
    if labels_path is not None:
        for number, line in enumerate(labels_path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if (set(item) != {"event_id", "classification", "reviewer", "evidence_ref"}
                    or item["classification"] not in {"legitimate_safe", "unsafe", "uncertain"}
                    or any(not isinstance(item[key], str) or not item[key].strip()
                           for key in ("event_id", "reviewer", "evidence_ref"))
                    or item["event_id"] in labels):
                raise ValueError(f"invalid or duplicate adjudication at line {number}")
            labels[item["event_id"]] = item

    with SafeActionAuditStore(audit_db, read_only=True) as audit:
        rows = audit._connection.execute(
            "SELECT canonical_json FROM safe_action_audit WHERE event_type = 'live_gate' ORDER BY rowid"
        ).fetchall()
        events = [json.loads(row["canonical_json"]) for row in rows]
        for incident_id in {event["incident_id"] for event in events}:
            if not audit.verify_chain(incident_id):
                raise ValueError(f"invalid audit chain for incident {incident_id}")
    if start is not None:
        events = [event for event in events
                  if datetime.fromisoformat(event["occurred_at"]) >= start.astimezone(timezone.utc)]
    # Retry/idempotency denials of one already evaluated request are not fresh
    # policy decisions and would bias the measured rate.
    first_by_request: dict[str, dict] = {}
    for event in events:
        request_id = event["payload"].get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("live Gate audit is missing request_id")
        first_by_request.setdefault(request_id, event)
    events = list(first_by_request.values())
    known = {event["event_id"] for event in events}
    if set(labels) - known:
        raise ValueError("adjudication references a non-live Gate event")
    decisions = {"ALLOW": 0, "DENY": 0, "REQUIRE_APPROVAL": 0}
    eligible: list[dict] = []
    for event in events:
        decision = event["payload"].get("decision")
        if decision not in decisions:
            raise ValueError("live Gate audit has an unknown decision")
        decisions[decision] += 1
        # REQUIRE_APPROVAL means the policy has requested human authorization,
        # not that it blocked an otherwise safe action.
        if decision != "REQUIRE_APPROVAL":
            eligible.append(event)
    certain = [event for event in eligible if labels.get(event["event_id"], {}).get("classification")
               in {"legitimate_safe", "unsafe"}]
    safe = [event for event in certain if labels[event["event_id"]]["classification"] == "legitimate_safe"]
    false_denials = [event for event in safe if event["payload"]["decision"] == "DENY"]
    fully_adjudicated = len(certain) == len(eligible) and bool(eligible)
    return {
        "source": "actual_live_gate_audit_only",
        "audit_db": str(audit_db),
        "since": since,
        "decisions": decisions,
        "eligible_decisions": len(eligible),
        "adjudicated_certain": len(certain),
        "safe_adjudicated": len(safe),
        "false_denials": len(false_denials),
        "false_denial_event_ids": [event["event_id"] for event in false_denials],
        "false_block_rate": (len(false_denials) / len(safe)) if fully_adjudicated and safe else None,
        "rate_status": "measured" if fully_adjudicated and safe else "unavailable_incomplete_adjudication_or_no_safe_cases",
        "definition": "confirmed-safe DENY / all confirmed-safe live ALLOW or DENY decisions; REQUIRE_APPROVAL excluded",
        "note": "Human labels require linked evidence; shadow, injected-policy and offline demo decisions are excluded.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-db", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--since", help="UTC start of observation, e.g. 2026-11-08T00:00:00Z")
    args = parser.parse_args()
    print(json.dumps(report(args.audit_db, args.labels, args.since), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
