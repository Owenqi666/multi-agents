"""One durable repair per lineage, with late reports only in history."""

import json
from pathlib import Path

import pytest

from multi_agent.core.state import Disposition
from multi_agent.core.transitions import (
    finish_integration_rework,
    reserve_integration_rework,
)

FIXTURE = Path(__file__).parent / "scenarios" / "integration" / "rework_lineage.json"


def test_integration_rework_lineage_replay():
    scenario = json.loads(FIXTURE.read_bytes())
    for _ in range(2):
        reservations = {}
        terminal = {}
        late = []
        for event in scenario["events"]:
            root = scenario["roots"][event["integration"]]
            if event["kind"] == "reserve":
                if "error" in event:
                    with pytest.raises(ValueError, match=event["error"]):
                        reserve_integration_rework(reservations.get(root))
                else:
                    reserve_integration_rework(reservations.get(root))
                    reservations[root] = event["request"]
            else:
                disposition = finish_integration_rework(root in terminal)
                assert disposition == event["disposition"]
                if disposition == Disposition.APPLIED:
                    terminal[root] = event["result"]
                else:
                    late.append(event["result"])
        assert reservations == scenario["reservations"]
        assert terminal == scenario["terminal"]
        assert late == scenario["late"]
