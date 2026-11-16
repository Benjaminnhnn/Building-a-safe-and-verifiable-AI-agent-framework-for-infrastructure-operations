"""Embargo/purge of incident-session groups that overlap in time across a split boundary (stdlib only).

Parallel lab workers run episodes concurrently, so the decision-time interval of the last group of an earlier split
can overlap the first group of the next one. Instead of letting the split silently leak across the boundary (or
blocking forever), the overlapping groups of the EARLIER split are removed from it and reported as purged.
The purged records are used by no split.
"""

from __future__ import annotations

from datetime import datetime


def _t(row):
    return datetime.fromisoformat(row["timeline"]["decision_at"].replace("Z", "+00:00"))


def _end(group):
    return max(_t(r) for r in group)


def purge_temporal_overlap(assigned: dict[str, list[list[dict]]]) -> tuple[dict[str, list[list[dict]]], list[list[dict]]]:
    """``assigned``: train/validation/test -> list of groups (each a list of records). Returns (clean, purged groups)."""
    out = {k: list(v) for k, v in assigned.items()}
    purged: list[list[dict]] = []
    for earlier, later in (("validation", "test"), ("train", "validation")):
        while out[earlier] and out[later]:
            later_start = min(_t(r) for g in out[later] for r in g)
            worst = max(out[earlier], key=_end)
            if _end(worst) < later_start:
                break
            out[earlier].remove(worst)
            purged.append(worst)
    return out, purged
