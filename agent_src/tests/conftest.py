from __future__ import annotations

import sys
from pathlib import Path


# Evaluation helpers live beside agent_src, while CI intentionally puts only
# agent_src on PYTHONPATH for runtime imports.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
