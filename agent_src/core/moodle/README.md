# Moodle adapter and live handoff

`core/moodle/` contains the Moodle-specific runtime boundary. The runtime alert path is:

`Alertmanager ? FastAPI/Celery ? process_moodle_alert ? evidence collection ? diagnosis/remediation candidate`

`alert_integration.py` accepts firing Moodle alerts for staging in `shadow` or `investigate` mode. It never dispatches an action. Operators use the authenticated `/api/moodle/remediation/{candidate_id}/prepare` endpoint, obtain two independent action-bound signatures, then call `/execute`. The safe executor returns a receipt. The separate verifier signs its live result; the Agent validates it and writes the only authoritative `RESOLVED` transition to its runtime audit DB.

- `adapter.py`, `contract.py`, `diagnosis.py`, and `planner.py` hold Moodle inventory, evidence mapping, diagnosis, and the reviewed action catalog.
- `live_handoff.py` revalidates the candidate against fresh evidence before preparing or executing a request.
- Offline Moodle replay lives in `evaluation/agent_replay/moodle_pipeline.py`; it is not imported by the live alert path.

The staging execution switch defaults off. This package does not authorize production execution.
