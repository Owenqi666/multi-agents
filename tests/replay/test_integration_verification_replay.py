"""Verify that late integration test conclusions are historical."""

import json
from pathlib import Path

import pytest

from multi_agent.core.integration import IntegrationCandidate
from multi_agent.core.integration_testing import IntegrationTestResult
from multi_agent.core.integration_verification import IntegrationTestRequest
from multi_agent.core.state import Disposition
from multi_agent.core.task_test_execution import TaskTestPolicy
from multi_agent.core.tasks import FrozenProjectRules, ProjectRules
from multi_agent.core.transitions import (
    finish_integration_test,
    reserve_integration_test,
)

FIXTURE = (
    Path(__file__).parent / "scenarios" / "integration" / "verification_attempts.json"
)


def test_integration_verification_attempt_replay():
    scenario = json.loads(FIXTURE.read_bytes())
    request = IntegrationTestRequest(
        IntegrationCandidate(
            "refs/multi-agent/integrations/example", "c" * 40, "d" * 40
        ),
        FrozenProjectRules(
            "/synthetic/project/.git",
            "a" * 40,
            "b" * 40,
            "synthetic test rules",
            ProjectRules(1, (), (), (), None),
        ),
        TaskTestPolicy(
            executable="/synthetic/codex",
            executable_identity=(1, 2),
            executable_sha256="a" * 64,
            timeout=10,
            grace=1,
            output_bytes_limit=1024,
        ),
        "/synthetic/checkout",
    )
    for _ in range(2):
        current = None
        late = []
        for item in scenario["events"]:
            if "error" in item:
                with pytest.raises(ValueError, match=item["error"]):
                    reserve_integration_test(current, request)
                continue
            if item["event"] == "reserve":
                current = reserve_integration_test(current, request)
                assert current.number == item["attempt"]
                continue
            assert current is not None
            result = IntegrationTestResult(
                status=item["status"],
                process_stopped=True,
                commands=(),
                reason=None,
                preparation_path=None,
                preparation_sha256=None,
            )
            current, disposition = finish_integration_test(
                current, item["attempt"], result
            )
            assert disposition == item["disposition"]
            if disposition == Disposition.HISTORY_ONLY:
                late.append({"attempt": item["attempt"], "status": result.status})
        assert current is not None and current.result is not None
        assert {"attempt": current.number, "status": current.result.status} == scenario[
            "current"
        ]
        assert late == scenario["late"]
