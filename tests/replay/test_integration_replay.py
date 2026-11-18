"""Replay ordered integration outcomes without Git, a clock, or a runtime."""

import json
from pathlib import Path

import pytest

from multi_agent.core.integration import (
    IntegrationCandidate,
    IntegrationConflict,
    IntegrationMemberApplied,
    IntegrationMemberConflicted,
    IntegrationProgress,
)
from multi_agent.core.task_execution import task_execution_value
from multi_agent.core.transitions import apply_integration

SCENARIOS = json.loads(
    (
        Path(__file__).parent / "scenarios" / "integration" / "ordered_integration.json"
    ).read_bytes()
)


def _event(value):
    fields = {key: item for key, item in value.items() if key not in ("kind", "error")}
    match value["kind"]:
        case "applied":
            return IntegrationMemberApplied(**fields)
        case "conflicted":
            return IntegrationMemberConflicted(
                fields["index"], IntegrationConflict(tuple(fields["paths"]))
            )
        case "candidate":
            return IntegrationCandidate(**fields)
        case _:
            raise ValueError("unexpected integration replay event")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario["name"])
def test_integration_replay(scenario):
    ordered = scenario["members"]
    for _ in range(2):
        state = IntegrationProgress(len(ordered))
        for value in scenario["events"]:
            event = _event(value)
            if "error" in value:
                with pytest.raises(ValueError, match=value["error"]):
                    apply_integration(state, event)
            else:
                state = apply_integration(state, event)

        assert ordered[: state.applied_count] == scenario["applied"]
        assert task_execution_value(state.result) == scenario["result"]
        if isinstance(state.result, IntegrationConflict):
            assert ordered[state.applied_count] == scenario["conflicted"]
