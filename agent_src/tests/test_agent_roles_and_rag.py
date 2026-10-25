from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from core import shadow_pipeline
from core.agents.messages import ObservationReport, stage_event
from core.agents.roles import ExecutionAgent, VerificationAgent, handoff_run_id
from core.evidence.evidence_store import SQLiteEvidenceStore, evidence_content_hash
from core.moodle.diagnosis import diagnose_moodle_alert
from core.rag.runbook_chunks import StaticRunbookRetriever, runbook_chunk_id
from core.reasoning import citation_validator as cv
from core.reasoning import llm_diagnosis
from core.reasoning.llm_client import ScriptedDiagnosisLLM
from core.reasoning.llm_diagnosis import DiagnosisMode, build_knowledge_links, reason_about_alert
from core.schema.evidence import Evidence
from evaluation.benchmark import llm_gate_ablation
from evaluation.benchmark.diagnosis_ablation import (
    CASES,
    oracle_responder,
    run_accuracy,
    run_robustness,
)
from evaluation.benchmark.scorer import Scorer, llm_usage_run_fields

NOW = datetime.now(timezone.utc)
A, B = "moodle-app-a", "moodle-app-b"


def _sample(evidence_id, query_id, value, instance):
    return SimpleNamespace(
        source="prometheus",
        evidence_id=evidence_id,
        collected_at=NOW,
        metadata={"payload_json": json.dumps({
            "query_id": query_id, "sample_value": value, "labels": {"instance": instance},
        })},
    )


def _con01():
    return [
        _sample("ev-web-a", "moodle_node_web_running", 1.0, A),
        _sample("ev-web-b", "moodle_node_web_running", 0.0, B),
    ]


def _output(runbook_refs=(), citations=None):
    return json.dumps({"status": "hypothesis", "hypotheses": [{
        "cause_code": "web_container_not_running",
        "confidence": 0.7,
        "rationale": "node b web container stopped",
        "citations": citations or [
            {"evidence_id": "ev-web-a", "query_id": "moodle_node_web_running", "instance": A, "value": 1.0},
            {"evidence_id": "ev-web-b", "query_id": "moodle_node_web_running", "instance": B, "value": 0.0},
        ],
        "runbook_refs": list(runbook_refs),
    }]})


# --- RAG ------------------------------------------------------------------


def test_static_retriever_returns_alert_scoped_chunks_with_stable_provenance():
    retriever = StaticRunbookRetriever()
    chunks = retriever.retrieve("MoodleNodeCpuHigh", "cpu pressure")
    assert chunks and {chunk["source_file"] for chunk in chunks} == {"moodle/runbook_moodle_storage_resources.md"}
    first = chunks[0]
    assert set(first) >= {"chunk_id", "source_file", "heading", "chunk_index", "source_sha256", "chunk_sha256", "text"}
    assert first["chunk_id"] == runbook_chunk_id(first["source_file"], first["chunk_index"], first["text"])
    assert StaticRunbookRetriever().retrieve("MoodleNodeCpuHigh", "cpu pressure") == chunks
    assert retriever.retrieve("UnknownAlert") == []


def test_validator_rejects_runbook_refs_that_were_not_provided_and_conflicting_samples():
    result = cv.validate_llm_output(_output(["rb-x"]), _con01(), now=NOW, runbook_chunk_ids={"rb-real"})
    assert cv.RUNBOOK_REF_NOT_FOUND in result.violation_codes()
    assert cv.validate_llm_output(_output(["rb-real"]), _con01(), now=NOW, runbook_chunk_ids={"rb-real"}).accepted

    conflicting = _con01() + [_sample("ev-web-b2", "moodle_node_web_running", 1.0, B)]
    assert cv.CITATION_CONFLICTS_WITH_STORED in cv.validate_llm_output(_output(), conflicting, now=NOW).violation_codes()


def test_knowledge_links_bind_conclusion_evidence_and_cited_runbook_chunks():
    chunks = StaticRunbookRetriever().retrieve("MoodleWebContainerMissing")
    rule = diagnose_moodle_alert("MoodleWebContainerMissing", _con01())
    diagnosis = reason_about_alert(
        "MoodleWebContainerMissing", _con01(), rule_diagnosis=rule, mode=DiagnosisMode.LLM,
        client=ScriptedDiagnosisLLM(_output([chunks[0]["chunk_id"]])), runbook_chunks=chunks, now=NOW,
    )
    links = build_knowledge_links(diagnosis, chunks)
    assert links["decided_by"] == "llm_validated"
    assert links["conclusion_cause_code"] == "web_container_not_running"
    assert links["evidence_refs"] == ["ev-web-a", "ev-web-b"]
    assert [ref["chunk_id"] for ref in links["runbook_refs"]] == [chunks[0]["chunk_id"]]
    assert "text" not in links["runbook_refs"][0]
    assert len(links["runbook_context"]) == len(chunks)

    rule_links = build_knowledge_links(rule, chunks)
    assert rule_links["decided_by"] == "rules" and rule_links["runbook_refs"] == []


# --- Runtime agents through the pipeline ---------------------------------


def _alert(name="MoodleWebContainerMissing"):
    return {
        "fingerprint": f"agents-{name}",
        "startsAt": "2026-10-25T04:00:00Z",
        "status": "firing",
        "labels": {"alertname": name, "service": "moodle", "environment": "staging"},
        "annotations": {"summary": "Moodle web container missing on node b"},
    }


def _collector(rows):
    def collect(alert, *, incident_id, resource_id, evidence_store, prometheus_url, queries=None):
        refs = []
        for evidence_id, query_id, value, instance in rows:
            item = Evidence(
                evidence_id=evidence_id, incident_id=incident_id, resource_id=resource_id, source="prometheus",
                collected_at=datetime.now(timezone.utc), summary=f"{query_id}={value}", content_hash="pending",
                metadata={"payload_json": json.dumps({
                    "query_id": query_id, "sample_value": value, "labels": {"instance": instance},
                })},
            )
            item = item.model_copy(update={"content_hash": evidence_content_hash(item)})
            if evidence_store.get_by_id(evidence_id) is None:
                evidence_store.append(item)
            refs.append(evidence_id)
        return {"evidence_refs": refs, "status": "complete", "sources": ["prometheus"], "errors": []}
    return collect


_CON01_ROWS = [
    ("ev-a1", "moodle_fault_con01_web_stopped", 0.0, A), ("ev-a2", "moodle_fault_con01_web_stopped", 1.0, B),
    ("ev-a3", "moodle_node_web_running", 1.0, A), ("ev-a4", "moodle_node_web_running", 0.0, B),
    ("ev-a5", "moodle_node_up", 1.0, A), ("ev-a6", "moodle_node_up", 1.0, B),
    ("ev-a7", "moodle_cadvisor_up", 1.0, A), ("ev-a8", "moodle_cadvisor_up", 1.0, B),
]


def _setup(monkeypatch, tmp_path, *, mode="rules", core_mode="investigate"):
    monkeypatch.setenv("AIOPS_UNIFIED_CORE_MODE", core_mode)
    monkeypatch.setenv("AIOPS_DIAGNOSIS_MODE", mode)
    monkeypatch.setenv("AIOPS_EVIDENCE_DB_PATH", str(tmp_path / "agents.db"))
    monkeypatch.delenv("VECTOR_DB_PATH", raising=False)
    monkeypatch.setattr(shadow_pipeline, "collect_shadow_evidence", _collector(_CON01_ROWS))


def test_alert_path_runs_three_agents_and_records_a_hash_chained_stage_ledger(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = shadow_pipeline.run_shadow_if_enabled(_alert())

    assert result["remediation_candidate"]["scenario_id"] == "CON-01"
    stages = [(item["stage"], item["actor"], item["outcome"]) for item in result["agent_trace"]]
    assert stages == [
        ("observe", "observer-agent", "completed"),
        ("diagnose", "diagnosis-agent", "completed"),
        ("plan", "planner-agent", "completed"),
        ("gate_request", "safety-gate", "awaiting_approval"),
    ]
    with SQLiteEvidenceStore(str(tmp_path / "agents.db")) as store:
        ledger = store.list_audit_events(result["incident_id"])
        assert store.verify_audit_integrity(run_id=result["incident_id"]) == 4
    assert {event["incident_id"] for event in ledger} == {result["incident_id"]}
    plan_event = next(event for event in ledger if event["stage"] == "plan")
    assert plan_event["action_id"] == result["remediation_candidate"]["action"]["action_id"]
    assert set(plan_event["evidence_refs"]) == set(result["remediation_candidate"]["evidence_refs"])


def test_shadow_mode_without_candidate_has_no_gate_request(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, core_mode="shadow")
    result = shadow_pipeline.run_shadow_if_enabled(_alert())
    assert [item["stage"] for item in result["agent_trace"]] == ["observe", "diagnose", "plan"]


def test_pipeline_llm_mode_shows_static_runbooks_and_links_them(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="llm")

    def responder(prompt):
        data = json.loads(prompt)
        chunk_id = data["runbook_chunks_untrusted"][0]["chunk_id"]
        rows = [row for row in data["observations_untrusted"] if row["query_id"] == "moodle_node_web_running"]
        return json.dumps({"status": "hypothesis", "hypotheses": [{
            "cause_code": "web_container_not_running", "confidence": 0.7, "rationale": "r",
            "citations": [{k: row[k] for k in ("evidence_id", "query_id", "instance", "value")} for row in rows],
            "runbook_refs": [chunk_id],
        }]})

    client = ScriptedDiagnosisLLM(responder)
    monkeypatch.setattr(llm_diagnosis, "build_default_client", lambda: client)
    result = shadow_pipeline.run_shadow_if_enabled(_alert())
    assert result["diagnosis"]["reasoning"]["source"] == "llm_validated"
    assert result["diagnosis"]["reasoning"]["rag_chunk_count"] > 0
    with SQLiteEvidenceStore(str(tmp_path / "agents.db")) as store:
        record = json.loads(store.get_by_id(result["decision_evidence_refs"][0]).metadata["decision_json"])
    links = record["knowledge_links"]
    assert links["decided_by"] == "llm_validated"
    assert links["runbook_refs"][0]["source_file"] == "moodle/runbook_moodle_service.md"


def test_diagnosis_rag_can_be_disabled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="llm")
    monkeypatch.setenv("AIOPS_DIAGNOSIS_RAG", "off")
    client = ScriptedDiagnosisLLM(json.dumps({"status": "insufficient_evidence", "hypotheses": []}))
    monkeypatch.setattr(llm_diagnosis, "build_default_client", lambda: client)
    shadow_pipeline.run_shadow_if_enabled(_alert())
    assert json.loads(client.calls[0])["runbook_chunks_untrusted"] == []


def test_handoff_agents_append_to_a_separate_hash_chained_run(tmp_path):
    db = tmp_path / "handoff.db"
    SQLiteEvidenceStore(db).close()
    action = {"incident_id": "inc-1", "action_id": "act-1", "evidence_refs": ["ev-1", "ev-2", "ev-3"]}

    def prepare(**kwargs):
        return {"incident_id": "inc-1", "request": {"request_id": "req-1", "action": action},
                "gate_action_sha256": "g", "executor_action_sha256": "e"}

    def execute(**kwargs):
        return {"status": "awaiting_verification", "execution_permitted": True}

    agent = ExecutionAgent(evidence_db=db, prepare=prepare, execute=execute)
    agent.prepare(candidate_id="cand-1")
    agent.execute(candidate_id="cand-1", request_data={"request_id": "req-1", "action": action})
    attestation = SimpleNamespace(payload=SimpleNamespace(incident_id="inc-1", signed_at=NOW))
    VerificationAgent(evidence_db=db, accept=lambda **_: {"status": "resolved", "event_hash": "h"}).run(
        request_id="req-1", attestation=attestation,
    )
    with SQLiteEvidenceStore(db) as store:
        events = store.list_audit_events(handoff_run_id("inc-1"))
        assert store.list_audit_events("inc-1") == []
    assert [(event["stage"], event["outcome"]) for event in events] == [
        ("gate_request", "awaiting_approval"), ("execute", "completed"), ("verify", "completed"),
    ]
    assert all(event["action_id"] == "act-1" for event in events[:2])


def test_handoff_agent_returns_result_even_when_ledger_is_unavailable(tmp_path):
    result = VerificationAgent(evidence_db=tmp_path / "missing" / "x.db", accept=lambda **_: {"status": "failed"}).run(
        request_id="r", attestation=SimpleNamespace(payload=SimpleNamespace(incident_id="inc-2", signed_at=NOW)),
    )
    assert result == {"status": "failed"}


def test_message_digest_ignores_production_time():
    kwargs = dict(incident_id="i", run_id="i", produced_by="observer-agent", alert_name="a", resource_id="r",
                  alert_evidence_id="e", evidence_status="complete")
    first = ObservationReport(**kwargs)
    second = ObservationReport(**kwargs, produced_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert first.digest() == second.digest()
    assert stage_event(first, outcome="completed")["event_id"] != stage_event(second, outcome="completed")["event_id"]


# --- AI evaluation --------------------------------------------------------


def _oracle(case):
    return ScriptedDiagnosisLLM(oracle_responder(case))


def test_robustness_guards_hold_under_noise_contradiction_and_injection():
    summary = run_robustness(client_factory=_oracle)["summary"]
    for perturbation, conditions in summary.items():
        for condition in ("rules", "llm", "llm_rag", "hybrid", "hybrid_rag"):
            assert conditions[condition]["false_diagnosis_rate"] == 0.0, (perturbation, condition)
        assert all(stats["secret_leaks"] == 0 for stats in conditions.values())
    # The scripted oracle ignores the contradiction; only the validator stops it.
    assert summary["contradiction"]["llm_unvalidated"]["false_diagnosis_rate"] == 1.0


def test_stability_is_reported_for_repeated_runs():
    report = run_accuracy(client_factory=_oracle, repeats=2, cases=CASES[:3])
    assert report["stability"]["llm"]["mean_consistency"] == 1.0
    assert report["stability"]["llm"]["repeats"] == 2


def test_cost_is_estimated_from_token_prices():
    class Priced(ScriptedDiagnosisLLM):
        def complete(self, **kwargs):
            completion = super().complete(**kwargs)
            return completion.__class__(completion.text, "priced", 0.5, prompt_tokens=1000, output_tokens=100)

    report = run_accuracy(
        client_factory=lambda case: Priced(oracle_responder(case)), cases=CASES[:2],
        usd_per_mtok_in=1.0, usd_per_mtok_out=10.0,
    )
    cost = report["summary"]["llm"]["cost"]
    assert cost["prompt_tokens"] == 2000 and cost["output_tokens"] == 200
    assert cost["estimated_usd"] == round(2000 / 1e6 + 200 / 1e6 * 10, 6)
    assert cost["mean_latency_seconds"] == 0.5
    assert report["scorer"]["llm"]["total_llm_tokens"] == 2200


def test_scorer_reads_llm_usage_from_diagnosis_reasoning():
    fields = llm_usage_run_fields({"reasoning": {"llm": {"usage": {
        "llm_call_count": 1, "prompt_tokens": 10, "output_tokens": 5, "latency_seconds": 0.2,
    }}}})
    assert fields == {"llm_call_count": 1, "llm_prompt_tokens": 10, "llm_output_tokens": 5, "llm_latency_seconds": 0.2}
    assert llm_usage_run_fields({"status": "hypothesis"})["llm_call_count"] == 0
    aggregate = Scorer().aggregate([{"scenario_id": "CON-01", "method": "m", "repetitions": 1, **fields}])
    assert aggregate.total_llm_tokens == 15 and aggregate.avg_llm_latency_seconds == 0.2


def test_llm_planner_gate_ablation_blocks_unsafe_proposals_and_keeps_reviewed_ones():
    report = llm_gate_ablation.run(
        client_factory=lambda scenario: ScriptedDiagnosisLLM(llm_gate_ablation.naive_plan_responder(scenario))
    )
    summary = report["summary"]
    unsafe = summary["unsafe_or_inappropriate_proposals"]
    assert unsafe > 0
    assert summary["llm_no_gate"]["forbidden_execution_count"] == unsafe
    assert summary["llm_with_gate"]["forbidden_execution_count"] == 0
    assert summary["llm_with_gate"]["reviewed_false_block_count"] == 0
    assert summary["unsafe_by_scenario"]["SEC-01"] > 0  # human-only: every proposal is inappropriate


def test_llm_planner_malformed_output_runs_nothing():
    assert llm_gate_ablation.parse_proposals("not json") == []
    assert llm_gate_ablation.parse_proposals('{"proposals": [{"action_id": ""}, 3]}') == []
    rows = llm_gate_ablation.evaluate_scenario("CON-01", ScriptedDiagnosisLLM("oops"))
    assert rows[0]["action_id"] is None and rows[0]["error"] == "no_parseable_proposals"
