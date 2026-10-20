# Offline replay and benchmark helpers

This package contains replay, ground-truth scoring, baseline, and ablation code used for reproducible offline evaluation. It is outside `agent_src` so the live runtime does not import fixture answers or replay machinery.

Ground truth is evaluation-only and must not influence a live diagnosis, candidate, approval, or verification decision. Replay results demonstrate behavior on recorded or synthetic inputs; they are not evidence of deployed or live recovery.

Entry points include `moodle_pipeline.py`, `ground_truth.py`, `replay.py`, `mvp_pipeline.py`, and `ablation.py`. Automation and tests import them from `evaluation.agent_replay`.
