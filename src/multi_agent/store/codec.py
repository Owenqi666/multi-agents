"""Persist core values using JSON payloads and explicit value constructors."""

import json
from dataclasses import fields
from typing import cast

from multi_agent.core import EvidenceSource
from multi_agent.core.events import (
    AttemptStarted,
    CancelRequested,
    Completed,
    Envelope,
    Error,
    FailureCategory,
    JSONValue,
    Message,
    ProcessExited,
    ReviewEvent,
    SessionStarted,
    Usage,
    thaw_json,
)

_EVENT_TYPES: dict[str, type[ReviewEvent]] = {
    event.kind: event
    for event in (
        AttemptStarted,
        CancelRequested,
        SessionStarted,
        Message,
        Usage,
        Completed,
        Error,
        ProcessExited,
    )
}


def encode_envelope(envelope: Envelope) -> str:
    # asdict performs a deepcopy, which cannot copy core's mapping proxies.
    event = envelope.event
    payload: dict[str, JSONValue] = {"type": event.kind}
    payload.update({field.name: getattr(event, field.name) for field in fields(event)})
    return json.dumps(
        {
            "seat": envelope.seat,
            "attempt": envelope.attempt,
            "occurred_at": envelope.occurred_at,
            "event": thaw_json(payload),
        },
        ensure_ascii=False,
    )


def decode_envelope(encoded: str) -> Envelope:
    payload = json.loads(encoded)
    event_fields = payload.pop("event")
    event_type = _EVENT_TYPES[event_fields.pop("type")]
    if event_type is Error and event_fields["failure_category"] is not None:
        event_fields["failure_category"] = FailureCategory(
            event_fields["failure_category"]
        )
    return Envelope(event=event_type(**event_fields), **payload)


def encode_source(source: EvidenceSource | None) -> str | None:
    if source is None:
        return None
    return json.dumps({"event_type": source.event_type, "raw_type": source.raw_type})


def decode_source(encoded: str | None) -> EvidenceSource | None:
    if encoded is None:
        return None
    return EvidenceSource(**json.loads(encoded))


def encode_result(value: JSONValue) -> str:
    return json.dumps(thaw_json(value), ensure_ascii=False)


def decode_result(encoded: str) -> JSONValue:
    return cast(JSONValue, json.loads(encoded))
