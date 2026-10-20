"""Evaluation-only conversion of ground-truth signals into replay observations.

This module is the explicit oracle boundary. It may read observed fixture
signals, but it never passes scenario ids, expected root causes, or expected
remediations to the agents under evaluation.
"""

from __future__ import annotations

from core.schema.scenario import ScenarioGroundTruth
from pydantic import BaseModel, Field


class ReplayObservation(BaseModel):
    resource_id: str
    source: str = "evaluation_fixture"
    summary: str
    raw_ref: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


def observations_from_scenario(scenario: ScenarioGroundTruth) -> list[ReplayObservation]:
    """Build agent-safe inputs from fixture observations only."""
    observations: list[ReplayObservation] = []
    for index, signal in enumerate(scenario.observed_signals, start=1):
        resource_id = scenario.affected_resources[
            min(index - 1, len(scenario.affected_resources) - 1)
        ]
        observations.append(
            ReplayObservation(
                resource_id=resource_id,
                summary=signal,
                raw_ref=f"evaluation_fixture:{index}",
                metadata={"signal": signal, "simulated": "true"},
            )
        )
    return observations
