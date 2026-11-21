"""Decode only verified runtime fields; all unrecognized messages stay in raw logs.

Protocol sources: protocol/codex/schema/v2/{TurnCompletedNotification,
ItemCompletedNotification,ThreadTokenUsageUpdatedNotification,
ModelReroutedNotification,ThreadStartResponse,TurnStartParams,TurnStartResponse}.json
and CodexErrorInfo in the merged schema; Claude fields are recorded in
tests/fixtures/runtime/claude-2.1.325/*.jsonl. Model reroutes describe notifications
in the raw transcript. Reported model fields are populated independently.
"""

import json
import math
from io import BytesIO
from typing import Any, Literal, TypedDict, cast

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


class RuntimeJSONError(ValueError):
    """Runtime JSON has invalid values, encoding or excessive nesting."""


class _RuntimeJSONValueError(ValueError):
    """Internal rejection with a fixed message that never contains input data."""


_MAX_JSON_DEPTH = 128


def strict_json_loads(text: str) -> Any:
    """Preserve syntax errors; reject ambiguous or non-UTF-8 JSON values.

    There is no transport byte limit. The container depth limit counts a root
    object or array as one; strings, including ordinary non-JSON text, do not
    contribute to that limit.
    """
    if not isinstance(text, str):
        raise TypeError("runtime JSON input must be text")
    duplicate_keys = False

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate_keys
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys = True
            else:
                result[key] = value
        return result

    try:
        text.encode("utf-8")
        value = json.loads(text, object_pairs_hook=object_pairs)
        # Finish syntax parsing before reporting semantic rejection: a JSON
        # fragment followed by ordinary text retains JSONDecodeError behavior.
        if duplicate_keys:
            raise _RuntimeJSONValueError("runtime JSON contains duplicate keys")
    except json.JSONDecodeError:
        raise
    except UnicodeEncodeError as error:
        raise RuntimeJSONError("runtime JSON contains invalid Unicode") from error
    except _RuntimeJSONValueError as error:
        raise RuntimeJSONError(str(error)) from error
    except RecursionError as error:
        raise RuntimeJSONError("runtime JSON nesting limit exceeded") from error
    except ValueError as error:
        # For example, Python's integer digit limit. Its diagnostic is not
        # an input artifact and must not be echoed into runtime error reports.
        raise RuntimeJSONError("runtime JSON value rejected by parser") from error
    return _json_value(value)


def object_value(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{name} must be a JSON object")
    return value


def string_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _agent_text(value: object) -> str:
    # AgentMessage.text has no schema minimum length. A nonselected empty
    # message must not invalidate an otherwise usable explicit final answer.
    if not isinstance(value, str):
        raise TypeError("agentMessage.text must be a string")
    return value


def _validated_json_value(value: object, depth: int) -> JSONValue:
    if isinstance(value, str):
        value.encode("utf-8")
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _RuntimeJSONValueError("runtime JSON number is not finite")
        return value
    if isinstance(value, (list, dict)) and depth >= _MAX_JSON_DEPTH:
        raise _RuntimeJSONValueError("runtime JSON nesting limit exceeded")
    if isinstance(value, list):
        return [_validated_json_value(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            key.encode("utf-8")
            result[key] = _validated_json_value(item, depth + 1)
        return result
    raise ValueError("result contains a non-JSON value")


def _json_value(value: object) -> JSONValue:
    try:
        return _validated_json_value(value, 0)
    except UnicodeEncodeError as error:
        raise RuntimeJSONError("runtime JSON contains invalid Unicode") from error
    except _RuntimeJSONValueError as error:
        raise RuntimeJSONError(str(error)) from error
    except RecursionError as error:
        raise RuntimeJSONError("runtime JSON nesting limit exceeded") from error


def result_value(value: object, *, structured: bool) -> JSONValue:
    if structured and isinstance(value, str):
        return cast(JSONValue, strict_json_loads(value))
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


class CodexReroute(TypedDict):
    source: Literal["model/rerouted"]
    from_model: str
    to_model: str
    reason: str


def _recorded_codex_reroutes(raw: bytes, session_id: str) -> list[CodexReroute] | None:
    sent: set[tuple[str, str | int]] = set()
    starts: dict[tuple[str, str | int], tuple[int, str]] = {}
    replies: dict[tuple[str, str | int], tuple[int, str] | None] = {}
    notifications: list[tuple[int, str, object]] = []
    for line_number, line in enumerate(BytesIO(raw)):
        row = object_value(strict_json_loads(line.decode("utf-8")), "transcript row")
        outbound = row.get("kind") == "send"
        inbound = row.get("kind") == "recv" and row.get("stream") == "stdout"
        if not (outbound or inbound) or "message" not in row:
            continue
        process = string_value(row.get("proc"), "recorded process")
        message = object_value(row["message"], "recorded JSON-RPC message")
        method = message.get("method")
        if (
            inbound
            and method == "model/rerouted"
            and message.keys().isdisjoint({"id", "result", "error"})
        ):
            notifications.append((line_number, process, message.get("params")))
            continue
        identifier = message.get("id")
        if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
            continue
        key = (process, identifier)
        if outbound and isinstance(method, str):
            if key in sent:
                raise ValueError("recorded JSON-RPC request id was reused")
            sent.add(key)
            if method == "thread/start":
                starts[key] = (line_number, "thread")
            elif method == "turn/start":
                params = object_value(message.get("params"), "turn/start params")
                if params.get("threadId") == session_id:
                    starts[key] = (line_number, "turn")
        elif inbound and "method" not in message and key in starts:
            if key in replies:
                raise ValueError("duplicate recorded JSON-RPC reply")
            if "error" in message:
                replies[key] = None
                continue
            _, field = starts[key]
            result = object_value(message.get("result"), "start result")
            value = object_value(result.get(field), "started object")
            replies[key] = (line_number, string_value(value.get("id"), "started id"))

    threads = [
        (key[0], reply[0])
        for key, reply in replies.items()
        if starts[key][1] == "thread" and reply is not None and reply[1] == session_id
    ]
    if len(threads) != 1:
        return None
    process, thread_reply = threads[0]
    turns = [
        (start, replies.get(key))
        for key, (start, field) in starts.items()
        if key[0] == process and field == "turn"
    ]
    if len(turns) != 1:
        return None
    turn_start, turn_reply = turns[0]
    if turn_reply is None or turn_start <= thread_reply:
        return None

    turn_id = turn_reply[1]
    evidence: list[CodexReroute] = []
    for line_number, emitter, payload in notifications:
        if emitter != process or line_number <= turn_start:
            continue
        params = object_value(payload, "reroute notification params")
        if string_value(params.get("threadId"), "reroute thread id") != session_id:
            continue
        if string_value(params.get("turnId"), "reroute turn id") != turn_id:
            continue
        evidence.append(
            {
                "source": "model/rerouted",
                "from_model": string_value(
                    params.get("fromModel"), "reroute from model"
                ),
                "to_model": string_value(params.get("toModel"), "reroute to model"),
                "reason": string_value(params.get("reason"), "reroute reason"),
            }
        )
    return evidence


def codex_reroutes(raw: bytes, session_id: str) -> list[CodexReroute] | None:
    """Read reported reroutes from an already authenticated attempt transcript.

    An empty list means the attempt's session and sole turn were confirmed.
    None means the transcript could not establish that context or was malformed.
    """
    try:
        return _recorded_codex_reroutes(raw, session_id)
    except ValueError:
        return None


class CodexDecoder:
    def __init__(self, thread_id: str, turn_id: str, *, structured: bool) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.structured = structured
        self.final_text: str | None = None
        self.unknown_text: str | None = None

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
                text = _agent_text(item.get("text"))
                if item.get("phase") == "final_answer":
                    self.final_text = text
                elif item.get("phase") is None:
                    self.unknown_text = text
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
            if data.get("type") == "agentMessage":
                text = _agent_text(data.get("text"))
                if data.get("phase") == "final_answer":
                    self.final_text = text
                elif data.get("phase") is None:
                    self.unknown_text = text
        # MessagePhase permits absent/null values for legacy providers. Only a
        # successful turn can promote the last such message to an answer; never
        # search backwards for parseable JSON or override an explicit final.
        selected_text = (
            self.final_text if self.final_text is not None else self.unknown_text
        )
        if selected_text is None:
            raise ValueError("completed turn has no final answer")
        text = string_value(selected_text, "completed answer")
        return (
            Completed(
                result_value(text, structured=self.structured),
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
