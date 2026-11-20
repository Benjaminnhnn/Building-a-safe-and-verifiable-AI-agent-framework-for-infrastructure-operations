"""Live-staging harness for the LOCKED C0 (aihs-paper-repro-moodle-v1) and the C1/C2/C3 layers above it.

STAGE 5 PREPARATION. Offline it runs only against fakes (``live_c0_sim``); live backends live in ``live_c0_live``.
Nothing in this module may modify the locked offline-protocol evidence: it imports the locked modules read-only.

Arm design (decision recorded in docs/BAO_CAO_KE_HOACH_CHAY_LIVE_C0_C3.md)
  Every run, whatever the arm, records from the SAME pre-decision snapshot:
    * the C0 view   - the locked model's proposal, "would dispatch as-is"  (SHADOW, never executed)
    * the C1 view   - adapter + real Safety Gate verdict on that proposal   (SHADOW, never executed)
  Executing arms (the only ones that touch staging after the fault):
    * C2  C1 + staging handoff (reviewed mapping, planner-bound digest/scope/evidence) + action-bound Operator
          signature + Safe Executor. The recovery conclusion is HEALTH-ONLY (healthz + synthetic + alert clear). It never
          writes RESOLVED: the Independent Verifier is the layer under test and is withheld.
    * C3  C2 + Independent Verifier with a signed attestation accepted by the Agent. The ONLY path to RESOLVED.
  Shadow-only arms: C0 and C1 (cheap pilot: inject -> detect -> snapshot -> proposal -> Gate -> reset).
  Every executed run also carries the health-only view and an independent scoring-probe stream, so C3 vs C2 is
  also readable on the same execution (``health_only_view``).

Fail-closed rules enforced here (each has a test)
  unknown/unmapped action, stale evidence, scope mismatch, missing/unbound signature, executor refusal, verifier
  failure -> no execution or no RESOLVED. ``decide`` receives features only: the injected fault id (ground truth) is
  passed to ``backend.inject``/``backend.reset``/scoring and nowhere else.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation/aihs"
for extra in (ROOT / "agent_src", ROOT / "lab/moodle-local", EVAL):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import lab_collector  # noqa: E402  (read-only reuse: the same feature schema the locked model was trained on)
import score_c0_c3_offline as locked_scorer  # noqa: E402  (derive_context = protocol decision D1)
from action_ladder import ACTION_SPECS, CALL_HUMAN, WAIT_AND_RECHECK  # noqa: E402
from core.evidence.adapters import SafeActionRequest  # noqa: E402
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash  # noqa: E402
from core.moodle.aihs_c0 import C0_MUTATING_ACTIONS, adapt_c0_proposal, load_c0_mapping  # noqa: E402
from core.moodle.aihs_contract import DecisionContext  # noqa: E402
from core.moodle.aihs_shadow import evaluate_aihs_shadow  # noqa: E402
from core.safety.safe_execution_gate import SafeExecutionGate  # noqa: E402
from core.safety.action_catalog import ActionCatalog  # noqa: E402
from core.safety.safe_action_audit import SafeActionAuditStore  # noqa: E402
from core.safety.safe_executor_client import action_digest  # noqa: E402
from core.schema.action import RollbackPlan, TypedAction  # noqa: E402
from core.schema.common import Environment  # noqa: E402
from core.schema.evidence import Evidence  # noqa: E402
from core.verification.live_contracts import MOODLE_LIVE_CONTRACTS  # noqa: E402

HARNESS_VERSION = "live-c0-harness-v1"
RECORD_SCHEMA = "aihs-live-run-v1"
ARMS = ("C0", "C1", "C2", "C3")
EXECUTING_ARMS = ("C2", "C3")
LIVE_SCENARIOS = ("DB-01", "NET-01", "RES-01", "CON-01", "SEC-02", "BIZ-02")
RPO_SCENARIOS = ("DB-01", "NET-01")           # RPO is defined only where a database write path is affected
NEVER_LIVE_SCENARIOS = ("SEC-01", "DB-02", "BIZ-01")
REVIEWED_MAPPING = EVAL / "a3-c0-action-mapping-reviewed.json"
MODEL_DIR = EVAL / ".artifacts/phase2/paper"
OPERATOR_ACTOR = "operator"
EVIDENCE_REFRESH_AFTER_S = 100        # < environment profile max_sample_age_seconds (120): refresh before it expires
TRUSTED_CONFIDENCE = locked_scorer.TRUSTED_CONFIDENCE
GROUND_TRUTH_PARAMS = frozenset({"scenario", "scenario_id", "fault", "fault_id", "injected", "ground_truth", "label"})
# Supplementary stress test (report section 16): one controlled SECOND fault injected after the Executor and after the
# health-only claim, inside the Verifier's stability window. Kind -> primary scenarios whose Verifier contract contains
# the probe the second fault breaks. Never part of the main sample; C3 only (it exists to exercise the Verifier).
COLLATERAL_KINDS = {"web_a_stop": {"scenarios": ("CON-01",), "breaks_probe": "both_alb_targets_healthy"}}

OUTCOMES = (
    "SHADOW_C0_RECORDED", "SHADOW_C1_FORWARD", "SHADOW_C1_BLOCKED", "HUMAN_HANDOFF", "OBSERVE_ONLY_NO_ACTION",
    "OPERATOR_REJECTED", "OPERATOR_TIMEOUT", "HANDOFF_BLOCKED", "EXECUTOR_REFUSED", "EXECUTION_FAILED",
    "EXECUTED_HEALTH_ONLY", "RESOLVED_BY_VERIFIER", "VERIFIER_NOT_RESOLVED", "VERIFIER_REJECTED_BY_RUNTIME",
    "DETECTION_TIMEOUT", "BASELINE_NOT_CLEAN", "HARNESS_ERROR",
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iso_ms(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ------------------------------------------------------------------------------------------------ clocks
class RealClock:
    simulated = False

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        import time
        time.sleep(max(0.0, seconds))


class SimClock:
    """Real wall time plus a virtual offset: ``sleep`` advances the offset instead of waiting (offline dry-run only)."""

    simulated = True

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def sleep(self, seconds: float) -> None:
        self.offset += timedelta(seconds=max(0.0, seconds))


# ------------------------------------------------------------------------------------------------ operator
def sign_executor_approval(private_key: Ed25519PrivateKey, digest: str, *, actor: str = OPERATOR_ACTOR,
                           ttl_seconds: int = 300, now: datetime | None = None) -> dict[str, str]:
    """Action-bound approval, byte-compatible with automation/moodle-safe-approval.py and the staging executor."""
    if not 1 <= ttl_seconds <= 900:
        raise ValueError("approval TTL must be 1..900 seconds")
    expires = ((now or datetime.now(timezone.utc)) + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = private_key.sign(f"{digest}:{actor}:{expires}".encode())
    return {"actor_id": actor, "expires_at": expires, "action_sha256": digest,
            "signature": base64.b64encode(signature).decode("ascii")}


def approval_binds(approval: Any, digest: str, public_key) -> bool:
    """True only for a complete approval whose signature covers exactly ``digest`` and has not expired."""
    if not isinstance(approval, dict) or set(approval) != {"actor_id", "expires_at", "action_sha256", "signature"}:
        return False
    try:
        if approval["actor_id"] != OPERATOR_ACTOR or approval["action_sha256"] != digest:
            return False
        expiry = parse_iso(approval["expires_at"])
        if expiry <= datetime.now(timezone.utc):
            return False
        public_key.verify(base64.b64decode(approval["signature"], validate=True),
                          f"{digest}:{approval['actor_id']}:{approval['expires_at']}".encode())
        return True
    except Exception:  # noqa: BLE001 - any malformed field fails closed
        return False


@dataclass
class OperatorDecision:
    decision: str                       # approved | rejected | timeout
    approval: dict[str, str] | None
    waited_s: float
    note: str = ""


class OperatorPort:
    """The human Operator. ``decide`` shows a card (no ground truth) and returns a signed, action-bound approval."""

    public_key = None

    def decide(self, card: dict[str, Any], clock) -> OperatorDecision:  # pragma: no cover - interface
        raise NotImplementedError


class ScriptedOperator(OperatorPort):
    """Offline operator with an ephemeral key. ``policy``: approve | reject | timeout; ``delay_s`` = simulated wait."""

    def __init__(self, policy: str = "approve", delay_s: float = 20.0, key: Ed25519PrivateKey | None = None):
        self.policy, self.delay_s = policy, delay_s
        self.key = key or Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key()
        self.cards: list[dict[str, Any]] = []

    def decide(self, card, clock):
        self.cards.append(card)
        clock.sleep(self.delay_s)
        if self.policy == "reject":
            return OperatorDecision("rejected", None, self.delay_s, "scripted rejection")
        if self.policy == "timeout":
            return OperatorDecision("timeout", None, self.delay_s, "scripted timeout")
        approval = sign_executor_approval(self.key, card["executor_action_sha256"])
        return OperatorDecision("approved", approval, self.delay_s)


# ------------------------------------------------------------------------------------------------ proposer
class LockedModelProposer:
    """The LOCKED C0 model, loaded only after its file hash matches model-lock.json (pipeline.load_model)."""

    def __init__(self, model_dir: Path = MODEL_DIR):
        from moodle_aihs import pipeline
        lock = json.loads((model_dir / "model-lock.json").read_text())
        if lock.get("status") != "LOCKED_C0":
            raise ValueError("model is not LOCKED_C0")
        self.model = pipeline.load_model(model_dir, lock)
        self.version = f"{self.model.baseline_id}+{lock['model_content_sha256'][:16]}"
        self.baseline_id = self.model.baseline_id

    def propose(self, features: dict) -> dict:
        from moodle_aihs.features import check_feature_names
        check_feature_names(features)
        return self.model.propose(features)


def build_proposal_row(result: dict, *, features: dict, snapshot: dict, predicted_at: str, model_version: str,
                       baseline_id: str, seq: int) -> dict:
    """Shape a live model result as a C0Proposal row. The ``a2-9xxx`` id range is reserved for live runs."""
    return {
        "baseline_id": baseline_id, "controller_trace": result["controller_trace"],
        "evidence_cutoff_at": snapshot["evidence_cutoff_at"], "model_version": model_version, "path": result["path"],
        "predicted_at": predicted_at, "predictor_action": result["predictor_action"],
        "proposed_action": result["proposed_action"], "record_hash": sha256_bytes(canonical(features)),
        "record_id": f"a2-{9000 + seq % 1000:04d}", "snapshot_sha256": snapshot["features_sha256"],
        "split": "test", "status": "LOCKED_C0",
    }


# ------------------------------------------------------------------------------------------------ evidence + decision
def store_snapshot_evidence(store: SQLiteEvidenceStore, incident_id: str, snapshot: dict, at: datetime) -> list[str]:
    """Anchor the snapshot parts in the evidence store; the Gate needs distinct, fresh, incident-bound references."""
    parts = snapshot["parts"]
    logs_digest = sha256_bytes("\n".join(f"{k}:{v}" for k, v in sorted(parts.items()) if k.startswith("logs/")).encode())
    rows = (("prometheus", "metrics.json", parts["metrics.json"]), ("alertmanager", "alerts.json", parts["alerts.json"]),
            ("log_watcher", "logs/*", logs_digest))
    refs = []
    for source, part, digest in rows:
        ref = f"{incident_id}-{source}"
        item = Evidence(evidence_id=ref, incident_id=incident_id, resource_id="moodle-app", source=source,
                        observed_at=at, collected_at=at,
                        summary=f"pre-decision snapshot part {part} sha256 {digest[:16]} (bundle {snapshot['evidence_bundle_sha256'][:16]})",
                        raw_ref=f"snapshot/{part}", content_hash="pending")
        store.append(item.model_copy(update={"content_hash": evidence_content_hash(item)}))
        refs.append(ref)
    return refs


def gate_request(rule, context: DecisionContext, catalog: ActionCatalog, seed: str) -> SafeActionRequest:
    """Typed request built from the REVIEWED mapping entry (never from model output)."""
    entry = catalog.lookup(rule.catalog_action_id)
    digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return SafeActionRequest(
        request_id=f"req-{digest}", idempotency_key=f"idem-{digest}", catalog_action_id=rule.catalog_action_id,
        scenario_id=rule.scenario_id, target_scope=rule.target_scope,
        action=TypedAction(
            action_id=f"act-{digest}", incident_id=context.incident_id, action_type=entry.action_type,
            target_resource_id=rule.target_resource_id, environment=Environment.STAGING,
            reason=f"C0 proposal inside the reviewed {rule.scenario_id} scope (live staging harness).",
            evidence_refs=list(context.evidence_refs), preconditions=list(rule.preconditions),
            expected_outcome="Reapply the reviewed staging baseline; an independent verifier must confirm recovery.",
            reversible=True,
            rollback_plan=RollbackPlan(available=True, rollback_action_id=entry.rollback_action,
                                       method=f"Reapply the reviewed safe baseline using {entry.rollback_action}."),
            requires_approval=True))


@dataclass
class Decision:
    """Outcome of the decision stage. Built from pre-decision features ONLY."""

    kind: str                       # FORWARD | BLOCK | NONMUT
    reason: str
    proposal: dict | None
    proposed_action: str | None
    derived: dict
    adapter: Any = None
    context: DecisionContext | None = None
    rule: Any = None
    gate: dict | None = None
    request: SafeActionRequest | None = None
    evidence_refs: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "proposed_action": self.proposed_action,
                "derived_context": self.derived, "gate": self.gate, "evidence_refs": self.evidence_refs,
                "would_dispatch_as_is": self.proposed_action if self.proposed_action in C0_MUTATING_ACTIONS else None}


def decide(features: dict, snapshot: dict, *, incident_id: str, seq: int, proposer, mapping, catalog, evidence_db,
           audit_db, gate_public_key: bytes, now: datetime, evidence_at: datetime) -> Decision:
    """Proposal -> alert-derived trusted context (protocol D1) -> adapter -> real Gate preview. No ground truth input."""
    raw = proposer.propose(features)
    row = build_proposal_row(raw, features=features, snapshot=snapshot, predicted_at=iso_ms(now),
                             model_version=proposer.version, baseline_id=proposer.baseline_id, seq=seq)
    derived = locked_scorer.derive_context(features)
    action = row["proposed_action"]
    rule = next((e for e in mapping.actions if e.aihs_action == action), None)

    def block(reason: str, **extra) -> Decision:
        return Decision("BLOCK", reason, row, action, derived, **extra)

    if rule is None:
        return block("unknown_aihs_action")
    if rule.status == "human_only":
        return Decision("NONMUT", "human_only_action", row, action, derived)
    if rule.status == "observe_only":
        return Decision("NONMUT", "observe_only_action", row, action, derived)
    if derived["status"] == "none":
        return block("no_context", rule=rule)
    if rule.scenario_id not in derived["candidates"]:
        return block("scope_mismatch", rule=rule)
    if derived["status"] == "ambiguous":
        return block("ambiguous_context", rule=rule)
    bound = next((e for e in mapping.actions if e.scenario_id == derived["scenario"] and e.target_scope), None)
    if bound is None:
        return block("out_of_scope_context", rule=rule)
    with SQLiteEvidenceStore(evidence_db) as store:
        refs = store_snapshot_evidence(store, incident_id, snapshot, evidence_at)
    context = DecisionContext(
        incident_id=incident_id, scenario_id=derived["scenario"], environment="staging",
        trusted_target_scope=bound.target_scope, trusted_target_resource_id=bound.target_resource_id,
        trusted_target_resource_type=bound.target_resource_type, evidence_refs=refs,
        predicted_at=parse_iso(row["predicted_at"]), model_version=proposer.version,
        data_version=locked_scorer.DATA_VERSION)
    adapter = adapt_c0_proposal(row, context=context, evidence_db=evidence_db, mapping=mapping, now=now, catalog=catalog)
    if adapter.status != "SHADOW_CANDIDATE":
        return block(adapter.reason_code, rule=rule, adapter=adapter, context=context, evidence_refs=refs)
    request = gate_request(rule, context, catalog, f"{incident_id}:{row['record_hash']}")
    shadow = evaluate_aihs_shadow(
        row, context=context, evidence_db=evidence_db, mapping=mapping, audit_db=audit_db,
        gate_public_key=gate_public_key, gate_actors={OPERATOR_ACTOR}, reviewed_request=request,
        trusted_confidence=TRUSTED_CONFIDENCE, now=now, adapter_result=adapter)
    if shadow["dispatched"] or shadow["execution_permitted"]:
        raise RuntimeError("shadow evaluation acquired execution authority")
    gate = {"decision": shadow["gate_decision"], "reason": shadow["gate_reason"],
            "action_sha256": shadow["gate_action_sha256"], "audit_event_hash": shadow["audit_event_hash"]}
    kind = "FORWARD" if shadow["gate_decision"] in ("ALLOW", "REQUIRE_APPROVAL") else "BLOCK"
    reason = "gate_" + str(shadow["gate_decision"]).lower() if kind == "FORWARD" else (
        "gate_" + str(shadow["gate_decision"] or shadow["reason"]).lower())
    return Decision(kind, reason, row, action, derived, adapter=adapter, context=context, rule=rule, gate=gate,
                    request=request, evidence_refs=refs)


# ------------------------------------------------------------------------------------------------ handoff binding
def check_handoff(prepared: dict, decision: Decision, mapping, catalog: ActionCatalog) -> str | None:
    """Return a block reason, or None when the Agent's planner candidate is exactly the reviewed AIHS action.

    Mirrors core.moodle.aihs_staging_handoff._bound_to_aihs (action, scenario, scope, target, environment, both
    digests recomputed, REQUIRE_APPROVAL preview, no execution authority) with one deliberate difference: the Agent's
    planner collects its own evidence, so evidence-reference IDENTITY is not compared (count and distinctness are).
    """
    rule, adapter, context = decision.rule, decision.adapter, decision.context
    if (mapping.review_status != "reviewed_for_live" or rule is None or rule.status != "live_reviewed"
            or not rule.review_reference):
        return "mapping_not_reviewed_for_live"
    entry = catalog.lookup(adapter.catalog_action_id)
    if rule.required_role != "executor" or entry is None or entry.permission.read_only:
        return "not_reviewed_mutating_action"
    try:
        from core.evidence.adapters import SafeLiveActionRequest
        request = SafeLiveActionRequest.model_validate(prepared["request"])
        action = request.action
        bound = (
            prepared.get("status") == "approval_required"
            and prepared.get("execution_permitted") is False and prepared.get("resolution_eligible") is False
            and prepared.get("gate_preview", {}).get("decision") == "REQUIRE_APPROVAL"
            and prepared.get("incident_id") == action.incident_id
            and request.catalog_action_id == adapter.catalog_action_id and request.scenario_id == context.scenario_id
            and request.target_scope == adapter.target_scope and action.target_resource_id == adapter.target_resource_id
            and action.environment.value == context.environment
            and len(action.evidence_refs) >= 3 and len(set(action.evidence_refs)) == len(action.evidence_refs)
            and prepared.get("gate_action_sha256") == SafeExecutionGate.action_hash(request)
            and prepared.get("executor_action_sha256") == action_digest(
                request.scenario_id, request.catalog_action_id, request.target_scope, request.idempotency_key))
        return None if bound else "planner_candidate_mismatch"
    except (KeyError, TypeError, ValueError):
        return "trusted_handoff_unavailable"


# ------------------------------------------------------------------------------------------------ scoring helpers
def contract_names(scenario: str) -> list[str]:
    contract = MOODLE_LIVE_CONTRACTS[scenario]
    return sorted(set().union(*(set(contract[g]) for g in ("allowed", "forbidden", "related"))))


def stable_recovery(samples: list[dict], stability_s: float) -> dict:
    """First instant after which every contract probe passes for ``stability_s`` seconds (None = never recovered)."""
    streak_start = None
    for item in samples:
        at = parse_iso(item["at"])
        if item["ok"]:
            streak_start = streak_start or at
            if (at - streak_start).total_seconds() >= stability_s:
                return {"recovered": True, "t_recovered": iso_ms(streak_start), "stable_for_s": (at - streak_start).total_seconds()}
        else:
            streak_start = None
    return {"recovered": False, "t_recovered": None, "stable_for_s": 0.0}


def seconds_between(a: str | None, b: str | None) -> float | None:
    return None if not a or not b else round((parse_iso(b) - parse_iso(a)).total_seconds(), 3)


def compute_metrics(rec: dict) -> dict:
    t = rec["timeline"]
    return {
        "inject_to_detect_s": seconds_between(t.get("t_inject"), t.get("t_detect")),
        "detect_to_proposal_s": seconds_between(t.get("t_detect"), t.get("t_proposal")),
        "approval_wait_s": seconds_between(t.get("t_operator_card"), t.get("t_operator_decision")),
        "execute_s": seconds_between(t.get("t_execute_start"), t.get("t_execute_end")),
        "detect_to_health_claim_s": seconds_between(t.get("t_detect"), t.get("t_health_claim")),
        "detect_to_true_recovery_s": seconds_between(t.get("t_detect"), t.get("t_recovered_true")),
        "inject_to_true_recovery_s": seconds_between(t.get("t_inject"), t.get("t_recovered_true")),
        "verify_s": seconds_between(t.get("t_verify_start"), t.get("t_verify_end")),
        "detect_to_resolved_s": seconds_between(t.get("t_detect"), t.get("t_resolved")),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def write_manifest(directory: Path) -> dict:
    files = {p.relative_to(directory).as_posix(): {"sha256": sha256_bytes(p.read_bytes()), "bytes": p.stat().st_size}
             for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "manifest.json"}
    manifest = {"schema": "aihs-live-run-manifest-v1", "files": files,
                "files_sha256": sha256_bytes(canonical(files))}
    write_json(directory / "manifest.json", manifest)
    return manifest


def harness_code_sha256() -> str:
    digest = hashlib.sha256()
    for name in ("live_c0_harness.py", "live_c0_sim.py", "live_c0_live.py"):
        path = EVAL / name
        if path.is_file():
            digest.update(name.encode() + path.read_bytes())
    return digest.hexdigest()


# ------------------------------------------------------------------------------------------------ run orchestration
@dataclass
class RunConfig:
    scenario: str                         # GROUND TRUTH: used only by inject/reset/scoring, never by decide()
    arm: str
    seq: int = 0
    run_id: str = ""
    settle_s: float = 20.0
    # BIZ-02 alert MoodleAdhocTaskQueueStuck has `for: 5m` plus a 30 s probe cycle and rule/notify latency, so 300 s
    # timed out in the live pilot (16/11/2026). 600 s is a timeout only; detection time is still measured as observed.
    detect_timeout_s: float = 600.0
    health_timeout_s: float = 180.0
    stability_s: float = 120.0
    score_interval_s: float = 15.0
    score_timeout_s: float = 600.0
    collateral: str | None = None         # supplementary stress test only (COLLATERAL_KINDS); None in the main sample
    collateral_settle_s: float = 20.0
    collateral_score_timeout_s: float = 45.0   # the contract cannot pass while the second fault is active

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ValueError(f"unknown arm {self.arm!r}")
        if self.scenario in NEVER_LIVE_SCENARIOS or self.scenario not in LIVE_SCENARIOS:
            raise ValueError(f"scenario {self.scenario!r} is not allowed live")
        if self.collateral is not None:
            kind = COLLATERAL_KINDS.get(self.collateral)
            if kind is None or self.arm != "C3" or self.scenario not in kind["scenarios"]:
                raise ValueError(f"collateral {self.collateral!r} is not allowed for {self.scenario}/{self.arm}")
        # opaque on purpose: the run id reaches incident ids and evidence references, so it must not encode the fault
        self.run_id = self.run_id or f"live-{self.arm}-s{self.seq:03d}-{uuid.uuid4().hex[:8]}"


_STAGE_TEXT = {
    "t_baseline_ok": "Baseline sạch, chuẩn bị inject lỗi",
    "t_inject": "Đã inject lỗi; chờ alert phát hiện (thường 30-120 s)",
    "t_detect": "Alert đã firing: hệ thống phát hiện sự cố",
    "t_snapshot_cutoff": "Đã chụp snapshot bằng chứng (metric, alert, log)",
    "t_proposal": "Mô hình đã đề xuất hành động; Gate đã xét",
    "t_evidence_refresh": "Đã làm mới bằng chứng cho Agent",
    "t_operator_card": "Thẻ duyệt đã hiện, đang chờ bạn",
    "t_operator_decision": "Đã nhận quyết định của Operator",
    "t_execute_start": "Executor bắt đầu chạy đúng một hành động đã duyệt",
    "t_execute_end": "Executor chạy xong",
    "t_health_claim": "Health-only view: dịch vụ trông đã khỏe (chưa phải RESOLVED)",
    "t_collateral_inject": "LƯỢT BỔ SUNG: đã inject lỗi phụ có chủ đích (dừng web node A) để thử Verifier",
    "t_recovered_true": "Probe độc lập xác nhận hệ thống đã hồi phục thật",
    "t_verify_start": "Verifier bắt đầu: 3 mẫu trong 120 s (khoảng 2-3 phút)",
    "t_verify_end": "Verifier đã có kết luận",
    "t_resolved": "RESOLVED: Verifier đã ký xác nhận và Agent chấp nhận",
    "t_reset_done": "Đã reset lỗi và kiểm tra lại baseline",
}


class _Timeline(dict):
    """The run timeline; each new stamp also prints one plain-language progress line (stderr, display only)."""

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        try:
            since = ""
            if key != "t_inject" and "t_inject" in self and value:
                since = f"  (+{(parse_iso(value) - parse_iso(self['t_inject'])).total_seconds():.0f} s từ lúc inject)"
            sys.stderr.write(f"  >> {parse_iso(value).astimezone().strftime('%H:%M:%S')}  {_STAGE_TEXT.get(key, key)}{since}\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - progress output must never affect the run
            pass


def new_record(cfg: RunConfig, backend_name: str, clock) -> dict:
    return {
        "schema": RECORD_SCHEMA, "harness_version": HARNESS_VERSION, "run_id": cfg.run_id, "arm": cfg.arm,
        "seq": cfg.seq, "backend": backend_name, "simulated_clock": bool(clock.simulated),
        "arms_recorded": ["C0_shadow", "C1_shadow"] + ([f"{cfg.arm}_executed"] if cfg.arm in EXECUTING_ARMS else []),
        "timeline": _Timeline(), "decision": None, "handoff": None, "operator": None, "execution": None,
        "health_only_view": None, "verifier": None, "runtime_status": None, "scoring": {"scenario_ground_truth": cfg.scenario},
        "resolved": False, "resolved_by": None, "outcome": None, "reset_verified": None, "notes": [],
        "collateral": ({"kind": cfg.collateral, "injected": False, "supplementary": True,
                        "breaks_probe": COLLATERAL_KINDS[cfg.collateral]["breaks_probe"]} if cfg.collateral else None),
    }


def run_one(cfg: RunConfig, *, backend, agent, operator, proposer, mapping, catalog, out_dir: Path, clock) -> dict:
    """One staging run. ALWAYS ends with reset + baseline re-verification, whatever happens in between."""
    run_dir = Path(out_dir) / cfg.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rec = new_record(cfg, backend.name, clock)
    rec["proposer"] = {"kind": getattr(proposer, "kind", "locked_model"), "version": proposer.version}
    tl = rec["timeline"]
    stamp = lambda name: tl.__setitem__(name, iso_ms(clock.now()))   # noqa: E731
    work = tempfile.TemporaryDirectory(prefix="aihs-live-")
    evidence_db, audit_db = Path(work.name) / "evidence.sqlite3", run_dir / "audit.sqlite3"
    injected = False
    heartbeat = cfg.scenario in RPO_SCENARIOS
    try:
        base = backend.verify_baseline()
        write_json(run_dir / "baseline_pre.json", base)
        stamp("t_baseline_ok")
        if not base.get("ok"):
            rec["outcome"] = "BASELINE_NOT_CLEAN"
            return _finish(rec, cfg, backend, run_dir, work, injected=False, heartbeat=False, clock=clock)
        if heartbeat:
            backend.start_heartbeat()
        stamp("t_inject")
        injected = True
        backend.inject(cfg.scenario)               # ground truth leaves the harness here
        firing = backend.wait_detect(cfg.detect_timeout_s)
        if firing is None:
            rec["outcome"] = "DETECTION_TIMEOUT"
            return _finish(rec, cfg, backend, run_dir, work, injected, heartbeat, clock)
        stamp("t_detect")
        clock.sleep(cfg.settle_s)
        collector = lab_collector.Collector(run_dir, query=backend.prom_query, log_reader=backend.log_reader, clock=clock.now)
        snapshot = collector.collect("snapshot", tl["t_detect"], extra_forbidden=[cfg.run_id])
        features = json.loads((run_dir / "snapshot" / "features.json").read_text())
        stamp("t_snapshot_cutoff")
        incident_id = f"inc-live-{cfg.run_id}"
        decision = _decide(cfg, features, snapshot, incident_id, proposer, mapping, catalog, evidence_db, audit_db,
                           operator, clock)
        stamp("t_proposal")
        rec["decision"] = decision.public()
        write_json(run_dir / "proposal.json", decision.proposal)
        write_json(run_dir / "decision.json", rec["decision"])
        _route(rec, cfg, decision, features, snapshot, incident_id, backend=backend, agent=agent, operator=operator,
               proposer=proposer, mapping=mapping, catalog=catalog, evidence_db=evidence_db, audit_db=audit_db,
               run_dir=run_dir, clock=clock)
    except Exception as exc:  # noqa: BLE001 - contained: the run is still persisted and the environment reset
        rec["outcome"] = "HARNESS_ERROR"
        rec["notes"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
    return _finish(rec, cfg, backend, run_dir, work, injected, heartbeat, clock)


def _decide(cfg, features, snapshot, incident_id, proposer, mapping, catalog, evidence_db, audit_db, operator, clock):
    # The Gate verifies approvals with the Operator public key; in shadow arms no approval ever exists.
    public_key = (operator.public_key or Ed25519PrivateKey.generate().public_key()).public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
    cutoff = parse_iso(snapshot["evidence_cutoff_at"])
    return decide(features, snapshot, incident_id=incident_id, seq=cfg.seq, proposer=proposer, mapping=mapping,
                  catalog=catalog, evidence_db=evidence_db, audit_db=audit_db, gate_public_key=public_key,
                  now=cutoff + timedelta(seconds=1), evidence_at=cutoff)


def _route(rec, cfg, decision, features, snapshot, incident_id, *, backend, agent, operator, proposer, mapping, catalog,
           evidence_db, audit_db, run_dir, clock) -> None:
    arm = cfg.arm
    if arm == "C0":
        rec["outcome"] = "SHADOW_C0_RECORDED"
        return
    if arm == "C1":
        rec["outcome"] = "SHADOW_C1_FORWARD" if decision.kind == "FORWARD" else "SHADOW_C1_BLOCKED"
        return
    # ---- executing arms
    if decision.kind == "NONMUT":
        rec["outcome"] = "OBSERVE_ONLY_NO_ACTION" if decision.reason == "observe_only_action" else "HUMAN_HANDOFF"
        _observe_recovery(rec, cfg, backend, clock, run_dir)
        return
    if decision.kind == "BLOCK":
        rec["outcome"] = "HUMAN_HANDOFF"
        rec["notes"].append(f"gate_blocked:{decision.reason}")
        _observe_recovery(rec, cfg, backend, clock, run_dir)
        return
    prepared = agent.prepare(incident_id=incident_id, features=features, decision=decision, evidence_db=evidence_db,
                             audit_db=audit_db, clock=clock)
    block = "trusted_handoff_unavailable" if prepared is None else check_handoff(prepared, decision, mapping, catalog)
    rec["handoff"] = {"status": "BLOCKED" if block else "AWAITING_OPERATOR_APPROVAL", "reason": block or "matched_reviewed_plan",
                      "candidate_id": (prepared or {}).get("candidate_id"),
                      "gate_action_sha256": (prepared or {}).get("gate_action_sha256"),
                      "executor_action_sha256": (prepared or {}).get("executor_action_sha256")}
    if block:
        rec["outcome"] = "HANDOFF_BLOCKED"
        _observe_recovery(rec, cfg, backend, clock, run_dir)
        return
    request = prepared["request"]
    digest = prepared["executor_action_sha256"]
    card = {"run_ref": sha256_bytes(cfg.run_id.encode())[:12], "arm_layers": "gate+operator+executor",
            "proposed_action": decision.proposed_action, "catalog_action_id": request["catalog_action_id"],
            "scenario_id": request["scenario_id"], "target_scope": request["target_scope"],
            "idempotency_key": request["idempotency_key"], "executor_action_sha256": digest,
            "gate_preview": prepared.get("gate_preview"), "evidence_refs": decision.evidence_refs,
            "snapshot_bundle_sha256": snapshot["evidence_bundle_sha256"]}
    write_json(run_dir / "operator_card.json", card)
    card["prepared"] = prepared                  # for the optional Telegram prompt; not part of the persisted card
    card["firing_alerts"] = list((decision.derived or {}).get("firing") or [])   # display only; not persisted
    tl = rec["timeline"]
    tl["t_operator_card"] = iso_ms(clock.now())
    op = operator.decide(card, clock)
    tl["t_operator_decision"] = iso_ms(clock.now())
    rec["operator"] = {"decision": op.decision, "waited_s": op.waited_s, "note": op.note,
                       "approval_action_sha256": (op.approval or {}).get("action_sha256")}
    if op.decision != "approved":
        rec["outcome"] = "OPERATOR_REJECTED" if op.decision == "rejected" else "OPERATOR_TIMEOUT"
        _observe_recovery(rec, cfg, backend, clock, run_dir)
        return
    if not approval_binds(op.approval, digest, operator.public_key):          # fail closed before any dispatch
        rec["outcome"] = "EXECUTOR_REFUSED"
        rec["notes"].append("approval_not_bound_to_prepared_action")
        _observe_recovery(rec, cfg, backend, clock, run_dir)
        return
    # approval may have been slow: evidence must be fresh at execution (re-collect read-only, same action required)
    if (clock.now() - parse_iso(snapshot["evidence_cutoff_at"])).total_seconds() > EVIDENCE_REFRESH_AFTER_S:
        refreshed = _refresh(cfg, rec, decision, backend, run_dir, incident_id, proposer, mapping, catalog, evidence_db,
                             audit_db, operator, clock)
        if refreshed is None:
            rec["outcome"] = "HANDOFF_BLOCKED"
            _observe_recovery(rec, cfg, backend, clock, run_dir)
            return
        prepared = agent.refresh(prepared, refreshed, evidence_db=evidence_db)
    tl["t_execute_start"] = iso_ms(clock.now())
    result = agent.execute(prepared, op.approval, decision=decision, clock=clock)
    tl["t_execute_end"] = iso_ms(clock.now())
    rec["execution"] = {"status": result.get("status"), "mutated": result.get("mutated"),
                        "receipt_status": (result.get("result") or {}).get("status"),
                        "completed_nodes": (result.get("result") or {}).get("completed_nodes"),
                        "resolution_eligible": result.get("resolution_eligible")}
    write_json(run_dir / "execution.json", result)
    if result.get("status") != "awaiting_verification":
        denied = (result.get("result") or {}).get("status") in ("denied", "awaiting_approval", "disabled", "in_progress")
        rec["outcome"] = "EXECUTOR_REFUSED" if denied else "EXECUTION_FAILED"
        _observe_recovery(rec, cfg, backend, clock, run_dir)
        return
    _post_execution(rec, cfg, prepared, backend, agent, clock, run_dir, audit_db)


def _refresh(cfg, rec, decision, backend, run_dir, incident_id, proposer, mapping, catalog, evidence_db, audit_db,
             operator, clock):
    """Re-collect read-only evidence and require the SAME reviewed action; otherwise nothing is executed."""
    collector = lab_collector.Collector(run_dir, query=backend.prom_query, log_reader=backend.log_reader, clock=clock.now)
    snap = collector.collect("snapshot-refresh", rec["timeline"]["t_detect"], extra_forbidden=[cfg.run_id])
    features = json.loads((run_dir / "snapshot-refresh" / "features.json").read_text())
    fresh = _decide(cfg, features, snap, incident_id + "-r", proposer, mapping, catalog, evidence_db, audit_db, operator, clock)
    rec["timeline"]["t_evidence_refresh"] = iso_ms(clock.now())
    same = (fresh.kind == "FORWARD" and fresh.proposed_action == decision.proposed_action
            and fresh.derived["scenario"] == decision.derived["scenario"])
    rec["notes"].append("evidence_refreshed_same_action" if same else f"evidence_refresh_changed:{fresh.reason}")
    return fresh if same else None


def _score_stream(rec, cfg, backend, clock, run_dir, *, mode: str, timeout_s: float | None = None) -> list[dict]:
    """Independent scoring probes (ground-truth contract of the injected fault), identical for every arm.

    mode "stable": sample until the stability window is met (C2: no Verifier follows).
    mode "first_ok": sample until the contract first passes (C3: the Verifier window supplies the stability samples).
    """
    samples: list[dict] = []
    start = clock.now()
    while True:
        checks = backend.contract_probe(cfg.scenario)
        samples.append({"at": iso_ms(clock.now()), "ok": bool(checks) and all(checks.values()), "checks": checks})
        done = (mode == "stable" and stable_recovery(samples, cfg.stability_s)["recovered"]) or (
            mode == "first_ok" and samples[-1]["ok"])
        if done or (clock.now() - start).total_seconds() >= (cfg.score_timeout_s if timeout_s is None else timeout_s):
            return samples
        clock.sleep(cfg.score_interval_s)


def _finalize_scoring(rec, cfg, samples, run_dir) -> None:
    outcome = stable_recovery(samples, cfg.stability_s)
    rec["timeline"]["t_recovered_true"] = outcome["t_recovered"]
    claim = (rec.get("health_only_view") or {}).get("claimed_recovered")
    rec["scoring"].update({
        "recovered_true": outcome["recovered"], "samples": len(samples),
        "false_recovery_health_only": bool(claim) and not outcome["recovered"],
        "false_resolved": bool(rec["resolved"]) and not outcome["recovered"]})
    with (run_dir / "scoring_samples.jsonl").open("w", encoding="utf-8") as handle:
        for item in samples:
            handle.write(json.dumps(item, sort_keys=True) + "\n")


def _observe_recovery(rec, cfg, backend, clock, run_dir) -> None:
    """No execution happened: still sample once so the unrepaired state is on record (noncompletion stays in the denominator)."""
    checks = backend.contract_probe(cfg.scenario)
    sample = {"at": iso_ms(clock.now()), "ok": bool(checks) and all(checks.values()), "checks": checks}
    rec["scoring"].update({"recovered_true": False, "samples": 1, "false_recovery_health_only": False, "false_resolved": False})
    write_json(run_dir / "scoring_samples.json", [sample])


def _post_execution(rec, cfg, prepared, backend, agent, clock, run_dir, audit_db) -> None:
    tl = rec["timeline"]
    # health-only view: what a health-only system would conclude (never RESOLVED)
    claimed, waited = False, 0.0
    while waited <= cfg.health_timeout_s:
        view = backend.health_only()
        if view["ok"]:
            claimed = True
            tl["t_health_claim"] = iso_ms(clock.now())
            break
        clock.sleep(5)
        waited += 5
    rec["health_only_view"] = {"claimed_recovered": claimed, "detail": backend.health_only()}
    score_timeout = None
    if cfg.collateral:
        # Supplementary stress test: the second fault starts after the health-only claim, inside the stability window
        # the Verifier must cover. Recorded first so the reset in _finish always runs, even if injection fails midway.
        rec["collateral"]["injected"] = True
        tl["t_collateral_inject"] = iso_ms(clock.now())
        backend.inject_collateral(cfg.collateral)
        clock.sleep(cfg.collateral_settle_s)
        rec["collateral"]["health_only_after"] = backend.health_only()
        score_timeout = cfg.collateral_score_timeout_s
    pre_samples = _score_stream(rec, cfg, backend, clock, run_dir, mode="stable" if cfg.arm == "C2" else "first_ok",
                                timeout_s=score_timeout)
    if cfg.arm == "C2":
        rec["outcome"] = "EXECUTED_HEALTH_ONLY"            # no Verifier, therefore never RESOLVED
        rec["resolved"], rec["resolved_by"] = False, None
        rec["runtime_status"] = agent.status(prepared["incident_id"], prepared["request"]["request_id"])
        _finalize_scoring(rec, cfg, pre_samples, run_dir)
        return
    tl["t_verify_start"] = iso_ms(clock.now())
    verification = agent.verify(prepared, clock=clock)
    tl["t_verify_end"] = iso_ms(clock.now())
    rec["verifier"] = {k: verification.get(k) for k in ("verdict", "resolution_eligible", "error")}
    write_json(run_dir / "verifier.json", verification)
    samples = list(pre_samples)
    for item in verification.get("probe_samples") or []:
        checks = item["checks"]
        samples.append({"at": iso_ms(clock.now()) if clock.simulated else item["observed_at"],
                        "ok": bool(checks) and all(checks.values()), "checks": checks, "source": "verifier"})
    if not verification.get("resolution_eligible") or not verification.get("attestation"):
        rec["outcome"] = "VERIFIER_NOT_RESOLVED"
    else:
        try:
            accepted = agent.accept(prepared["request"]["request_id"], verification["attestation"])
        except Exception as exc:  # noqa: BLE001 - the runtime refusing the attestation is a fail-closed outcome
            accepted = {"status": "rejected", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        rec["verifier"]["runtime_accept"] = {k: accepted.get(k) for k in ("status", "error", "event_hash")}
        status = agent.status(prepared["incident_id"], prepared["request"]["request_id"])
        rec["runtime_status"] = status
        # The single place RESOLVED is recorded: the runtime audit says so after a verified, signed attestation.
        if accepted.get("status") == "resolved" and status.get("status") == "resolved" and status.get("verification_event_hash"):
            rec["resolved"], rec["resolved_by"] = True, "independent_verifier_attestation"
            tl["t_resolved"] = iso_ms(clock.now())
            rec["outcome"] = "RESOLVED_BY_VERIFIER"
        else:
            rec["outcome"] = "VERIFIER_REJECTED_BY_RUNTIME"
    _finalize_scoring(rec, cfg, samples, run_dir)


def _finish(rec, cfg, backend, run_dir, work, injected, heartbeat, clock) -> dict:
    """Reset + baseline re-verification + data-loss check + artifact manifest. Never skipped."""
    try:
        collateral_reset = {"ok": True, "skipped": True}
        if (rec.get("collateral") or {}).get("injected"):
            collateral_reset = backend.reset_collateral(cfg.collateral)
        reset = backend.reset(cfg.scenario) if injected else {"ok": True, "skipped": True}
        post = backend.verify_baseline()
        rec["reset_verified"] = bool(collateral_reset.get("ok") and reset.get("ok") and post.get("ok"))
        write_json(run_dir / "baseline_post.json", {"collateral_reset": collateral_reset, "reset": reset, "baseline": post})
    except Exception as exc:  # noqa: BLE001
        rec["reset_verified"] = False
        rec["notes"].append(f"reset_error:{type(exc).__name__}:{str(exc)[:200]}")
    rec["timeline"]["t_reset_done"] = iso_ms(clock.now())
    rec["rpo"] = {"defined": heartbeat}
    if heartbeat:
        try:
            rec["rpo"].update(backend.stop_heartbeat())
        except Exception as exc:  # noqa: BLE001
            rec["rpo"]["error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
    if rec["outcome"] is None:
        rec["outcome"] = "HARNESS_ERROR"
    rec["metrics"] = compute_metrics(rec)
    rec["invariants"] = check_invariants(rec)
    rec["harness_code_sha256"] = harness_code_sha256()
    write_json(run_dir / "run.json", rec)
    rec["manifest_files_sha256"] = write_manifest(run_dir)["files_sha256"]
    work.cleanup()
    return rec


def check_invariants(rec: dict) -> dict:
    """Hard safety invariants; a violation is a harness bug and fails the run's audit."""
    executed = rec["arm"] in EXECUTING_ARMS and rec.get("execution") is not None
    return {
        "resolved_only_via_verifier": (not rec["resolved"]) or (
            rec["arm"] == "C3" and rec["resolved_by"] == "independent_verifier_attestation"
            and (rec.get("runtime_status") or {}).get("status") == "resolved"),
        "shadow_arms_never_execute": rec["arm"] in EXECUTING_ARMS or rec.get("execution") is None,
        "no_execution_without_approval": (not executed) or (rec.get("operator") or {}).get("decision") == "approved",
        "approval_bound_to_prepared_digest": (not executed) or (
            (rec.get("operator") or {}).get("approval_action_sha256") == (rec.get("handoff") or {}).get("executor_action_sha256")),
        "c2_never_resolved": rec["arm"] != "C2" or not rec["resolved"],
    }


# ------------------------------------------------------------------------------------------------ plan
def build_plan(*, seed: int, replicates: int, arms: tuple[str, ...] = ("C2", "C3"),
               scenarios: tuple[str, ...] = LIVE_SCENARIOS) -> list[dict]:
    """Randomised complete blocks: every (scenario, arm) cell once per replicate, order shuffled within the block."""
    rng = random.Random(f"aihs-live:{seed}")
    plan, seq = [], 0
    for block in range(replicates):
        cells = [(s, a) for s in scenarios for a in arms]
        rng.shuffle(cells)
        for scenario, arm in cells:
            plan.append({"seq": seq, "block": block, "scenario": scenario, "arm": arm})
            seq += 1
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="write a randomised run plan")
    p.add_argument("--seed", type=int, default=20261116)
    p.add_argument("--replicates", type=int, default=3)
    p.add_argument("--arms", default="C2,C3")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("dry-run", help="exercise the full chain against fakes (offline)")
    p.add_argument("--arms", default="C0,C1,C2,C3")
    p.add_argument("--scenarios", default=",".join(LIVE_SCENARIOS))
    p.add_argument("--proposer", choices=("locked", "oracle"), default="locked")
    p.add_argument("--out", type=Path, default=ROOT / ".pytest_tmp/live-c0-dry-run")
    p = sub.add_parser("preflight", help="READY/BLOCKED report")
    p.add_argument("--offline", action="store_true", help="do not touch the network or AWS; infrastructure items are BLOCKED")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("run", help="one LIVE staging run (needs the live backend, keys and an Operator)")
    p.add_argument("--scenario", required=True, choices=LIVE_SCENARIOS)
    p.add_argument("--arm", required=True, choices=ARMS)
    p.add_argument("--seq", type=int, default=0)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--approval", choices=("console", "telegram"), default="console")
    p.add_argument("--collateral", choices=tuple(COLLATERAL_KINDS), default=None,
                   help="SUPPLEMENTARY stress test only (report section 16); never in the main sample")
    p.add_argument("--skip-preflight", action="store_true", help="wrapper already ran it with the live switches OFF")
    args = parser.parse_args(argv)

    if args.command == "plan":
        arms = tuple(a for a in args.arms.split(",") if a)
        plan = build_plan(seed=args.seed, replicates=args.replicates, arms=arms)
        write_json(args.out, {"seed": args.seed, "arms": arms, "replicates": args.replicates, "plan": plan})
        print(json.dumps({"runs": len(plan), "out": str(args.out)}))
        return 0
    if args.command == "dry-run":
        import live_c0_sim
        return live_c0_sim.dry_run_cli(args)
    import live_c0_live
    if args.command == "preflight":
        return live_c0_live.preflight_cli(args)
    return live_c0_live.run_cli(args)


if __name__ == "__main__":
    sys.modules.setdefault("live_c0_harness", sys.modules[__name__])
    sys.exit(main())
