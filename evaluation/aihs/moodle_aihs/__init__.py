"""AIHS-on-Moodle training pipeline (baseline C0).

Baselines (never mixed in one result):
  * ``aihs-paper-repro-moodle-v1``        - paper method reproduced on Moodle snapshots (the locked C0).
  * ``aihs-public-prototype-as-is-v1``    - public prototype behaviour, for reference only.

Model output is an UNTRUSTED PROPOSAL. Nothing in this package executes an action or touches the Safety Gate,
Executor or Verifier.
"""

PAPER_BASELINE_ID = "aihs-paper-repro-moodle-v1"
AS_IS_BASELINE_ID = "aihs-public-prototype-as-is-v1"
PIPELINE_VERSION = "moodle-aihs-pipeline-1"
