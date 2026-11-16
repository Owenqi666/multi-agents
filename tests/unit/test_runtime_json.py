"""Strict runtime JSON rejects ambiguous evidence before schema validation."""

import json
import math

import pytest

from multi_agent.adapters import runtime_decode
from multi_agent.adapters.runtime_decode import (
    RuntimeJSONError,
    result_value,
    strict_json_loads,
)


@pytest.mark.parametrize(
    "text,category",
    [
        ('{"secret-key": 1, "secret-key": 2}', "duplicate keys"),
        ('{"outer": [{"secret-key": 1, "secret-key": 1}]}', "duplicate keys"),
        ('{"a": 1, "\\u0061": 2}', "duplicate keys"),
        ('{"😀": 1, "\\ud83d\\ude00": 2}', "duplicate keys"),
        ("NaN", "not finite"),
        ("Infinity", "not finite"),
        ("-Infinity", "not finite"),
        ("1e9999", "not finite"),
        ("-1e9999", "not finite"),
        ('{"nested": [0, NaN]}', "not finite"),
        ('{"secret-key": "\\ud800"}', "invalid Unicode"),
        ('{"\\udfff": "secret-value"}', "invalid Unicode"),
        ('["\\ud800\\ud800"]', "invalid Unicode"),
        ('["\\udc00\\ud800"]', "invalid Unicode"),
        ('"\ud800"', "invalid Unicode"),
        ("plain\udffftext", "invalid Unicode"),
    ],
)
def test_runtime_json_rejects_ambiguous_or_invalid_values(text, category) -> None:
    with pytest.raises(RuntimeJSONError, match=category) as raised:
        strict_json_loads(text)
    message = str(raised.value)
    assert len(message) < 80
    assert "secret-key" not in message
    assert "secret-value" not in message
    assert raised.value.__cause__ is not None
    assert not isinstance(raised.value, json.JSONDecodeError)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("null", None),
        ("true", True),
        ("false", False),
        ("0", 0),
        ("-15", -15),
        ("123456789012345678901234567890", 123456789012345678901234567890),
        ("1.0", 1.0),
        ("-0.0", -0.0),
        ("1.5e2", 150.0),
        ("1e-9999", 0.0),
        ('""', ""),
        ('"语言 😀"', "语言 😀"),
        ('"\\ud83d\\ude00"', "😀"),
        ('"\\ud800\\udc00"', "𐀀"),
        ('"\\udbff\\udfff"', "\U0010ffff"),
        ('"\\u0000"', "\0"),
        ('{"A": 1, "a": 2}', {"A": 1, "a": 2}),
        ('{"é": 1, "e\\u0301": 2}', {"é": 1, "e\u0301": 2}),
        ('{"escaped\\nkey": [true, null, {}]}', {"escaped\nkey": [True, None, {}]}),
    ],
)
def test_runtime_json_preserves_valid_scalars_unicode_and_values(
    text, expected
) -> None:
    actual = strict_json_loads(text)
    assert actual == expected
    assert type(actual) is type(expected)
    if text == "-0.0":
        assert math.copysign(1, actual) == -1


def test_runtime_json_preserves_member_order_without_normalizing_keys() -> None:
    parsed = strict_json_loads('{"z": 1, "a": 2, "é": 3, "e\\u0301": 4}')
    assert tuple(parsed) == ("z", "a", "é", "e\u0301")


@pytest.mark.parametrize("kind", ["array", "object", "mixed"])
@pytest.mark.parametrize("depth", [127, 128, 129])
def test_runtime_json_container_depth_boundary(kind, depth) -> None:
    text = "0"
    for level in range(depth):
        if kind == "array" or kind == "mixed" and level % 2:
            text = "[" + text + "]"
        else:
            text = '{"value":' + text + "}"
    if depth == 129:
        with pytest.raises(RuntimeJSONError, match="nesting") as raised:
            strict_json_loads(text)
        assert raised.value.__cause__ is not None
    else:
        value = strict_json_loads(text)
        for _ in range(depth):
            value = value[0] if isinstance(value, list) else value["value"]
        assert value == 0


def test_runtime_json_empty_container_counts_toward_depth() -> None:
    assert strict_json_loads("[" * 128 + "]" * 128) is not None
    with pytest.raises(RuntimeJSONError, match="nesting"):
        strict_json_loads("[" * 129 + "]" * 129)


def test_runtime_json_parser_recursion_error_has_fixed_message_and_cause(
    monkeypatch,
) -> None:
    original = RecursionError("secret parser payload")

    def recurse(*args, **kwargs):
        raise original

    monkeypatch.setattr(runtime_decode.json, "loads", recurse)
    with pytest.raises(RuntimeJSONError, match="nesting") as raised:
        strict_json_loads("[]")
    assert raised.value.__cause__ is original
    assert "secret" not in str(raised.value)


def test_runtime_json_parser_value_error_has_fixed_message_and_cause(
    monkeypatch,
) -> None:
    original = ValueError("secret parser payload")

    def reject(*args, **kwargs):
        raise original

    monkeypatch.setattr(runtime_decode.json, "loads", reject)
    with pytest.raises(RuntimeJSONError, match="parser") as raised:
        strict_json_loads("123")
    assert raised.value.__cause__ is original
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ordinary text",
        "{",
        "[1,]",
        "01",
        "{} trailing",
        '"unterminated',
        '{"same": 1, "same": 2} trailing',
        '{"duplicate": {"same": 1, "same": 2}, "missing":}',
    ],
)
def test_runtime_json_syntax_errors_remain_jsondecodeerror(text) -> None:
    with pytest.raises(json.JSONDecodeError) as raised:
        strict_json_loads(text)
    assert not isinstance(raised.value, RuntimeJSONError)
    assert raised.value.doc == text


def test_runtime_json_does_not_limit_plain_text_or_valid_string_size() -> None:
    text = "plain text " * (128 * 1024)
    with pytest.raises(json.JSONDecodeError):
        strict_json_loads(text)
    assert strict_json_loads(json.dumps(text)) == text
    assert result_value(text, structured=False) == text
    assert strict_json_loads(json.dumps("[" * 1000)) == "[" * 1000


@pytest.mark.parametrize("value", [b"{}", bytearray(b"{}"), None, 7])
def test_runtime_json_loader_requires_text(value) -> None:
    with pytest.raises(TypeError):
        strict_json_loads(value)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        {"bad": "\ud800"},
        {"\udfff": "secret-value"},
        ["valid", {"nested": ["\ud800"]}],
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_decoded_result_rejects_invalid_unicode_and_nonfinite_values(
    structured, value
) -> None:
    with pytest.raises(RuntimeJSONError) as raised:
        result_value(value, structured=structured)
    assert raised.value.__cause__ is not None
    assert "secret-value" not in str(raised.value)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("depth", [127, 128, 129])
def test_decoded_result_uses_same_container_depth(structured, depth) -> None:
    value = 0
    for _ in range(depth):
        value = [value]
    if depth == 129:
        with pytest.raises(RuntimeJSONError, match="nesting"):
            result_value(value, structured=structured)
    else:
        actual = result_value(value, structured=structured)
        for _ in range(depth):
            actual = actual[0]
        assert actual == 0


def test_decoded_result_rejects_cycles_without_unbounded_recursion() -> None:
    value = []
    value.append(value)
    with pytest.raises(RuntimeJSONError, match="nesting"):
        result_value(value, structured=False)


@pytest.mark.parametrize(
    "text", ['{"ready": false, "ready": true}', '"\\ud800"', "NaN", "1e9999"]
)
def test_structured_result_uses_strict_json_loader(text) -> None:
    with pytest.raises(RuntimeJSONError):
        result_value(text, structured=True)


def test_unstructured_string_is_data_and_structured_string_is_json() -> None:
    text = '{"ready": false, "ready": true}'
    assert result_value(text, structured=False) == text
    assert result_value('"\\ud83d\\ude00"', structured=True) == "😀"
    assert result_value("true", structured=True) is True
    with pytest.raises(json.JSONDecodeError):
        result_value("plain text", structured=True)


def test_decoded_result_preserves_value_and_returns_independent_containers() -> None:
    value = {"😀": [True, 1, 1.5, None, "Exact\ntext"]}
    result = result_value(value, structured=False)
    assert result == value
    value["😀"].append("later mutation")
    assert result == {"😀": [True, 1, 1.5, None, "Exact\ntext"]}


@pytest.mark.parametrize("value", [{1: "not a string key"}, {"set"}, (1, 2), object()])
def test_decoded_result_keeps_existing_non_json_type_rejection(value) -> None:
    with pytest.raises(ValueError, match="non-JSON"):
        result_value(value, structured=False)
