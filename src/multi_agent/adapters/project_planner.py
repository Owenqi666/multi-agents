"""One read-only planning turn with a provider-compatible structured document."""

import asyncio
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

from multi_agent.core.events import (
    Completed,
    Error,
    FailureCategory,
    ProcessExited,
    SessionStarted,
    Usage,
    thaw_json,
)
from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ProjectPlan

from .base import Permission, SessionConfig, TurnHandle
from .runtime import RuntimeAdapter
from .runtime_decode import RuntimeFailure

PROMPT_VERSION = "project-plan-v2"
_TEXT = {"type": "string"}
_TEXTS = {"type": "array", "items": _TEXT}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_PLAN_SCHEMA = _object(
    {
        "title": _TEXT,
        "architecture": _TEXT,
        "shared_interfaces": _TEXTS,
        "stages": {
            "type": "array",
            "items": _object(
                {
                    "stage_id": _TEXT,
                    "title": _TEXT,
                    "goal": _TEXT,
                    "approach": _TEXT,
                    "workflow": _TEXTS,
                    "depends_on": _TEXTS,
                    "acceptance_criteria": _TEXTS,
                    "deliverables": _TEXTS,
                }
            ),
        },
    }
)
_VALIDATOR = Draft202012Validator(_PLAN_SCHEMA)


def _validate_text(value: str, field: str) -> None:
    if not value.strip() or "\0" in value:
        raise ValueError(f"{field} must contain nonblank text without NUL")
    value.encode("utf-8")


def parse_project_plan(value: object) -> ProjectPlan:
    """Validate model output before publishing any stage to the caller."""
    invalid = next(_VALIDATOR.iter_errors(value), None)
    if invalid is not None:
        raise ValueError(f"invalid plan at {invalid.json_path} ({invalid.validator})")
    plan = cast(ProjectPlan, value)
    _validate_text(plan["title"], "title")
    _validate_text(plan["architecture"], "architecture")
    for item in plan["shared_interfaces"]:
        _validate_text(item, "shared_interfaces")
    if not plan["stages"]:
        raise ValueError("project plan must include at least one stage")
    previous: set[str] = set()
    for stage in plan["stages"]:
        identifier = stage["stage_id"]
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier) is None:
            raise ValueError(
                "stage_id must be 1–64 ASCII letters, digits, hyphens or underscores"
            )
        if identifier in previous:
            raise ValueError(f"duplicate stage ID {identifier}")
        dependencies = stage["depends_on"]
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"{identifier} repeats a dependency")
        if not set(dependencies) <= previous:
            raise ValueError(
                f"{identifier} depends on a stage that does not precede it"
            )
        for field, text in (
            ("title", stage["title"]),
            ("goal", stage["goal"]),
            ("approach", stage["approach"]),
        ):
            _validate_text(text, f"{identifier}.{field}")
        for field, items in (
            ("workflow", stage["workflow"]),
            ("acceptance_criteria", stage["acceptance_criteria"]),
            ("deliverables", stage["deliverables"]),
        ):
            if not items:
                raise ValueError(
                    f"{identifier}.{field} must contain at least one entry"
                )
            for item in items:
                _validate_text(item, f"{identifier}.{field}")
        previous.add(identifier)
    return plan


async def _capture_events(
    adapter: RuntimeAdapter, turn: TurnHandle, evidence: dict[str, Any]
) -> None:
    async for event in adapter.events(turn):
        if isinstance(event, SessionStarted):
            evidence["session_id"] = event.session_id
            if event.reported_model is not None:
                evidence["reported_model"] = event.reported_model
                evidence["reported_model_source"] = event.raw_type
            if event.reported_effort is not None:
                evidence["reported_effort"] = event.reported_effort
                evidence["reported_effort_source"] = event.raw_type
        elif isinstance(event, Completed) and event.reported_model is not None:
            evidence["reported_model"] = event.reported_model
            evidence["reported_model_source"] = event.raw_type
        elif isinstance(event, ProcessExited):
            evidence["exit_code"] = event.exit_code
        elif isinstance(event, Usage):
            evidence["usage"].append(
                {"raw_type": event.raw_type, "counts": dict(event.counts)}
            )


async def run_project_planner(
    request: PlanningRequest,
    *,
    executable: str,
    timeout: float,
    grace: float,
    cwd: Path,
    log_dir: Path,
    prompt: str,
) -> tuple[ProjectPlan | None, dict[str, Any] | None, dict[str, Any]]:
    adapter = RuntimeAdapter(
        request.provider,
        executable=executable,
        timeout=timeout,
        deadline_at=asyncio.get_running_loop().time() + timeout,
        grace=grace,
        log_dir=log_dir,
        output_schema=_PLAN_SCHEMA,
    )
    evidence: dict[str, Any] = {
        "cli_version": None,
        "session_id": None,
        "reported_model": None,
        "reported_model_source": None,
        "reported_effort": None,
        "reported_effort_source": None,
        "usage": [],
        "exit_code": None,
    }
    document = None
    error = None
    try:
        environment = await adapter.check_environment()
        evidence["cli_version"] = environment.cli_version
        if environment.logged_in is False:
            raise RuntimeFailure(
                Error(
                    "runtime reports no login",
                    FailureCategory.AUTH_EXPIRED,
                    raw_type="environment",
                )
            )
        if environment.models is not None and request.model not in environment.models:
            raise RuntimeFailure(
                Error(
                    "selected planner is unavailable",
                    FailureCategory.MODEL_UNAVAILABLE,
                    raw_type="environment",
                )
            )
        if (
            environment.model_efforts is not None
            and request.model in environment.model_efforts
            and request.effort not in environment.model_efforts[request.model]
        ):
            raise RuntimeFailure(
                Error(
                    "selected effort is unsupported for this planner",
                    raw_type="environment",
                )
            )
        session = await adapter.start_session(
            SessionConfig(request.model, request.effort, Permission.READ_ONLY, cwd)
        )
        turn = await adapter.submit_turn(session, prompt)
        consumer = asyncio.create_task(_capture_events(adapter, turn, evidence))
        cancellation_accepted = False
        try:
            await asyncio.shield(consumer)
        except asyncio.CancelledError:
            try:
                cancellation_accepted = await adapter.cancel(turn)
            finally:
                await consumer
        result = await adapter.result(turn)
        if isinstance(result.terminal, Error):
            raise RuntimeFailure(result.terminal)
        if cancellation_accepted and isinstance(result.terminal, Completed):
            raise RuntimeFailure(
                Error(
                    "planner interrupted before publication", FailureCategory.CANCELLED
                )
            )
        if not isinstance(result.terminal, Completed) or evidence["exit_code"] != 0:
            raise RuntimeFailure(
                Error(
                    "planner did not complete with a successful process exit",
                    raw_type="project.runtime",
                )
            )
        try:
            document = parse_project_plan(thaw_json(result.terminal.result))
        except ValueError as invalid:
            raise RuntimeFailure(
                Error(str(invalid), raw_type="project.validation")
            ) from invalid
    except asyncio.CancelledError:
        receipt = adapter.ownership_receipt()
        error = (
            Error("planner interrupted during preflight", FailureCategory.CANCELLED)
            if receipt.processes_stopped
            else Error(
                "planner shutdown could not be confirmed",
                raw_type="adapter.cleanup_incomplete",
            )
        )
    except RuntimeFailure as failure:
        error = failure.event
    except (OSError, RuntimeError, TypeError, ValueError) as failure:
        error = Error(str(failure), raw_type="project.runtime")

    receipt = adapter.ownership_receipt()
    evidence["logs"] = [asdict(log) for log in receipt.logs]
    evidence["processes_stopped"] = receipt.processes_stopped
    evidence["outputs_complete"] = receipt.outputs_complete
    evidence["resources_released"] = receipt.resources_released
    if document is not None and (
        not receipt.resources_released
        or not receipt.outputs_complete
        or not receipt.logs_sealed
    ):
        document = None
        error = Error(
            "planner output, cleanup or logs could not be verified",
            raw_type="adapter.cleanup_incomplete",
        )
    return document, asdict(error) if error is not None else None, evidence
