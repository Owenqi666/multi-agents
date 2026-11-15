"""Decode only verified runtime fields; all unrecognized messages stay in raw logs.

Protocol sources: protocol/codex/schema/v2/{TurnCompletedNotification,
ItemCompletedNotification,ThreadTokenUsageUpdatedNotification}.json and
CodexErrorInfo in the merged schema; Claude fields are recorded in
 tests/fixtures/runtime/claude-2.1.325/*.jsonl. Model reroutes remain raw evidence,
not an inferred statement about the model that actually produced a result.
"""

import json
import math
from typing import Any

from multi_agent.core.events import (
    AdapterEvent,
    Completed,
    Error,
    FailureCategory,
    JSONValue,
    Message,
    SessionStarted,
    Usage,
)


class RuntimeFailure(Exception):
    """A normalized boundary failure, including failures before a turn exists."""

    def __init__(self, event: Error) -> None:
        super().__init__(event.detail)
        self.event = event


def object_value(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be a JSON object")
    return value


def string_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("result contains a non-JSON value")


def result_value(value: object, *, structured: bool) -> JSONValue:
    if structured and isinstance(value, str):
        value = json.loads(value)
    return _json_value(value)


def counts(value: object, prefix: str = "") -> dict[str, int | float]:
    """Keep reported numeric fields, naming nested fields without summing them."""
    result: dict[str, int | float] = {}
    for key, item in object_value(value, "usage").items():
        name = prefix + key
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("non-finite usage counter")
            result[name] = item
        elif isinstance(item, dict):
            result.update(counts(item, name + "."))
    return result


def codex_failure(value: object, raw_type: str) -> Error:
    error = object_value(value, "turn.error")
    detail = string_value(error.get("message"), "turn.error.message")
    info = error.get("codexErrorInfo")
    category = (
        {
            "unauthorized": FailureCategory.AUTH_EXPIRED,
            "rateLimitExceeded": FailureCategory.RATE_LIMITED,
            "usageLimitExceeded": FailureCategory.RATE_LIMITED,
            "sessionBudgetExceeded": FailureCategory.RATE_LIMITED,
            "modelNotFound": FailureCategory.MODEL_UNAVAILABLE,
        }.get(info)
        if isinstance(info, str)
        else None
    )
    if isinstance(info, dict):
        for child in info.values():
            if isinstance(child, dict):
                status = child.get("httpStatusCode")
                if status == 401:
                    category = FailureCategory.AUTH_EXPIRED
                elif status == 429:
                    category = FailureCategory.RATE_LIMITED
    return Error(detail, category, raw_type=raw_type)


class CodexDecoder:
    def __init__(self, thread_id: str, turn_id: str, *, structured: bool) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.structured = structured
        self.final_text: str | None = None

    def decode(self, message: dict[str, Any]) -> tuple[AdapterEvent, ...]:
        method = message.get("method")
        if method not in (
            "item/completed",
            "thread/tokenUsage/updated",
            "turn/completed",
        ):
            return ()
        params = object_value(message.get("params"), "notification.params")
        if params.get("threadId") != self.thread_id:
            return ()
        turn = None
        if method == "turn/completed":
            turn = object_value(params.get("turn"), "turn")
            actual_turn = turn.get("id")
        else:
            actual_turn = params.get("turnId")
        if actual_turn != self.turn_id:
            return ()
        if method == "thread/tokenUsage/updated":
            return (Usage(counts(params.get("tokenUsage")), raw_type=method),)
        if method == "item/completed":
            item = object_value(params.get("item"), "item")
            if item.get("type") == "agentMessage":
                text = string_value(item.get("text"), "agentMessage.text")
                if item.get("phase") == "final_answer":
                    self.final_text = text
                return (Message(text, raw_type=method),)
            return ()
        assert turn is not None
        status = turn.get("status")
        if status == "interrupted":
            return (
                Error("turn interrupted", FailureCategory.CANCELLED, raw_type=method),
            )
        if status == "failed":
            return (codex_failure(turn.get("error"), method),)
        if status != "completed":
            raise ValueError("turn/completed has an invalid terminal status")
        items = turn.get("items")
        if not isinstance(items, list):
            raise TypeError("turn.items must be an array")
        for item in items:
            data = object_value(item, "turn item")
            if (
                data.get("type") == "agentMessage"
                and data.get("phase") == "final_answer"
            ):
                self.final_text = string_value(data.get("text"), "agentMessage.text")
        if self.final_text is None:
            raise ValueError("completed turn has no final answer")
        return (
            Completed(
                result_value(self.final_text, structured=self.structured),
                raw_type=method,
            ),
        )


def claude_events(
    message: dict[str, Any], *, structured: bool
) -> tuple[AdapterEvent, ...]:
    kind = string_value(message.get("type"), "event.type")
    if kind == "system" and message.get("subtype") == "init":
        return (
            SessionStarted(
                string_value(message.get("session_id"), "session_id"),
                raw_type="system/init",
            ),
        )
    if kind == "assistant":
        body = object_value(message.get("message"), "assistant.message")
        content = body.get("content")
        if not isinstance(content, list):
            raise ValueError("assistant content must be an array")
        return tuple(
            Message(string_value(part.get("text"), "content.text"), raw_type=kind)
            for value in content
            if (part := object_value(value, "content block")).get("type") == "text"
        )
    if kind != "result":
        return ()
    is_error = message.get("is_error")
    reason = string_value(message.get("terminal_reason"), "terminal_reason")
    if not isinstance(is_error, bool):
        raise TypeError("result.is_error must be a boolean")
    usage = counts(message.get("usage", {}))
    cost = message.get("total_cost_usd")
    if cost is not None:
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or (isinstance(cost, float) and not math.isfinite(cost))
        ):
            raise ValueError("total_cost_usd must be finite numeric evidence")
        usage["client_estimated_cost_usd"] = cost
    events: list[AdapterEvent] = []
    if usage:
        events.append(Usage(usage, raw_type=kind))
    if is_error:
        status = message.get("api_error_status")
        category = None
        if reason == "aborted_streaming":
            category = FailureCategory.CANCELLED
        elif status == 401:
            category = FailureCategory.AUTH_EXPIRED
        elif status == 429:
            category = FailureCategory.RATE_LIMITED
        elif status == 404:
            category = FailureCategory.MODEL_UNAVAILABLE
        detail = message.get("result")
        events.append(
            Error(
                detail if isinstance(detail, str) and detail else reason,
                category,
                raw_type=kind,
            )
        )
        return tuple(events)
    if reason != "completed":
        raise ValueError("successful result has an invalid terminal_reason")
    models = object_value(message.get("modelUsage", {}), "modelUsage")
    reported_model = None
    if len(models) == 1:
        key, value = next(iter(models.items()))
        entry = object_value(value, "modelUsage entry")
        reported_model = string_value(
            entry.get("canonicalModel", key), "reported model"
        )
    # Multiple reported models remain in the raw log. A scalar field must not
    # silently pick one or replace an absent report with the requested model.
    if "structured_output" in message:
        result = result_value(message["structured_output"], structured=False)
    else:
        result = result_value(message.get("result"), structured=structured)
    if structured and result is None:
        raise ValueError("successful result has no structured output")
    events.append(Completed(result, reported_model, raw_type=kind))
    return tuple(events)
