"""Implementation reports remain exact model text with narrow path semantics."""

import json
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from multi_agent.adapters.runtime_decode import RuntimeJSONError
from multi_agent.cli.task_output import TaskImplementationReport, parse_task_output
from multi_agent.core.tasks import TaskCard

LIMIT = 1024 * 1024


@pytest.fixture
def card():
    return TaskCard(
        1,
        "T-0100",
        "Implement the exact task",
        "a" * 40,
        ("src/**",),
        (),
        (),
        ("Working source",),
        ("Keep the interface",),
        ("unit",),
        (),
    )


def payload(card, **changes):
    return {
        "task_hash": card.task_hash,
        "outcome": "completed",
        "summary": "",
        "required_paths": [],
        **changes,
    }


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_task_output_preserves_report_text_and_frozen_values(card):
    summary = "  原始说明\r\nKeep this text.\n末尾空格  "
    paths = ["src/app/[slug]/page.tsx", "src/(group)/{literal}.tsx", "src"]
    value = payload(
        card, outcome="scope_blocked", summary=summary, required_paths=paths
    )
    report = parse_task_output(wire(value), card)
    assert report == TaskImplementationReport(
        card.task_hash, "scope_blocked", summary, tuple(paths)
    )
    paths.append("later.py")
    assert report.required_paths == (
        "src/app/[slug]/page.tsx",
        "src/(group)/{literal}.tsx",
        "src",
    )
    with pytest.raises(FrozenInstanceError):
        report.summary = "changed"


@pytest.mark.parametrize("summary", ["", "  \r\n\t", "已完成\n原样保留  "])
def test_task_output_completed_summary_is_only_a_report(card, summary):
    report = parse_task_output(wire(payload(card, summary=summary)), card)
    assert report == TaskImplementationReport(card.task_hash, "completed", summary, ())


@pytest.mark.parametrize(
    "changes",
    [
        {"task_hash": "f" * 64},
        {"outcome": "completed", "required_paths": ["src/new.py"]},
        {"outcome": "scope_blocked", "summary": "", "required_paths": ["src/a"]},
        {
            "outcome": "scope_blocked",
            "summary": " \r\n\t\u2003 ",
            "required_paths": ["src/a"],
        },
        {"outcome": "scope_blocked", "summary": "Need a path", "required_paths": []},
    ],
)
def test_task_output_rejects_wrong_task_and_invalid_outcome_semantics(card, changes):
    with pytest.raises(ValueError):
        parse_task_output(wire(payload(card, **changes)), card)


def test_task_output_binds_entire_current_card_not_only_task_id(card):
    revised = replace(card, goal="A different requested implementation")
    assert revised.id == card.id and revised.task_hash != card.task_hash
    with pytest.raises(ValueError, match="task hash"):
        parse_task_output(wire(payload(card)), revised)


@pytest.mark.parametrize(
    "missing", ["task_hash", "outcome", "summary", "required_paths"]
)
def test_task_output_requires_all_four_fields(card, missing):
    value = payload(card)
    del value[missing]
    with pytest.raises(ValueError):
        parse_task_output(wire(value), card)


@pytest.mark.parametrize(
    "changes",
    [
        {"task_hash": "a" * 63},
        {"task_hash": "A" * 64},
        {"task_hash": "a" * 64 + "\n"},
        {"task_hash": None},
        {"outcome": "failed"},
        {"outcome": True},
        {"summary": None},
        {"summary": ["report"]},
        {"required_paths": "src/new.py"},
        {"required_paths": [1]},
        {"actual_model": "requested-model"},
        {"tests_passed": True},
    ],
)
def test_task_output_rejects_schema_changes_and_extra_evidence(card, changes):
    with pytest.raises(ValueError):
        parse_task_output(wire(payload(card, **changes)), card)


@pytest.mark.parametrize("text", ["", "[]", "null", "true", "1", '"report"', "{} {}"])
def test_task_output_requires_one_json_object(card, text):
    with pytest.raises(ValueError):
        parse_task_output(text, card)


def test_task_output_rejects_trailing_text_and_markdown(card):
    raw = wire(payload(card))
    for text in (raw + " trailing", "```json\n" + raw + "\n```", raw + raw):
        with pytest.raises(ValueError):
            parse_task_output(text, card)


def test_task_output_rejects_duplicate_keys_before_schema(card):
    raw = wire(payload(card))
    text = raw[:-1] + ',"summary":"replacement"}'
    with pytest.raises(RuntimeJSONError, match="duplicate"):
        parse_task_output(text, card)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e9999"])
def test_task_output_rejects_nonfinite_values_before_schema(card, number):
    raw = wire(payload(card)).replace('"summary":""', '"summary":' + number)
    with pytest.raises(RuntimeJSONError, match="finite"):
        parse_task_output(raw, card)


@pytest.mark.parametrize("escaped", [False, True])
def test_task_output_rejects_invalid_unicode(card, escaped):
    text = json.dumps(payload(card, summary="\ud800"), ensure_ascii=escaped)
    with pytest.raises(ValueError, match="Unicode|UTF-8"):
        parse_task_output(text, card)


def test_task_output_uses_the_strict_json_depth_boundary(card):
    # The root object contributes one container; 127 arrays reach depth 128.
    raw = wire(payload(card))
    at_limit = raw.replace('"summary":""', '"summary":' + "[" * 127 + "0" + "]" * 127)
    with pytest.raises(ValueError) as caught:
        parse_task_output(at_limit, card)
    assert not isinstance(
        caught.value, RuntimeJSONError
    )  # Schema, not depth rejection.
    over_limit = raw.replace('"summary":""', '"summary":' + "[" * 128 + "0" + "]" * 128)
    with pytest.raises(RuntimeJSONError, match="nesting"):
        parse_task_output(over_limit, card)


def test_task_output_enforces_exact_utf8_byte_limit(card):
    remaining = LIMIT - len(wire(payload(card)).encode("utf-8"))
    summary = "雪" * (remaining // 3) + "x" * (remaining % 3)
    raw = wire(payload(card, summary=summary))
    assert len(raw) < LIMIT and len(raw.encode("utf-8")) == LIMIT
    assert parse_task_output(raw, card).summary == summary
    with pytest.raises(ValueError, match="byte|MiB"):
        parse_task_output(raw + " ", card)


@pytest.mark.parametrize("text", [None, b"{}", {}, 1, True])
def test_task_output_requires_text_input(card, text):
    with pytest.raises(TypeError):
        parse_task_output(text, card)


@pytest.mark.parametrize(
    "path",
    [
        "",
        " ",
        "/src/file.py",
        "//server/share",
        "src\\file.py",
        "src//file.py",
        "src/",
        ".",
        "..",
        "./src/file.py",
        "src/../file.py",
        "src/./file.py",
        "src/file.",
        "src/file ",
        "src./file.py",
        "src /file.py",
        "src/*.py",
        "src/**/file.py",
        "src/file?.py",
        ".git",
        ".GIT/config",
        "src/.GiT/config",
        "src/.git./config",
        "src/.git /config",
    ],
)
def test_task_output_rejects_unsafe_or_pattern_paths(card, path):
    with pytest.raises(ValueError):
        parse_task_output(
            wire(
                payload(
                    card,
                    outcome="scope_blocked",
                    summary="Need scope",
                    required_paths=[path],
                )
            ),
            card,
        )


@pytest.mark.parametrize("codepoint", [0, 9, 10, 13, 31, 127, 128, 159])
def test_task_output_rejects_unicode_control_characters_in_paths(card, codepoint):
    value = payload(
        card,
        outcome="scope_blocked",
        summary="Need scope",
        required_paths=["src/a" + chr(codepoint) + "b.py"],
    )
    with pytest.raises(ValueError):
        parse_task_output(wire(value), card)


@pytest.mark.parametrize(
    "paths",
    [
        ["src/file.py", "src/file.py"],
        ["src/file.py", "SRC/FILE.py"],
        ["src/café.py", "src/cafe\u0301.py"],
        ["src/Straße.py", "src/STRASSE.py"],
    ],
)
def test_task_output_rejects_normalized_path_alias_duplicates(card, paths):
    with pytest.raises(ValueError, match="duplicate|alias"):
        parse_task_output(
            wire(
                payload(
                    card,
                    outcome="scope_blocked",
                    summary="Need scope",
                    required_paths=paths,
                )
            ),
            card,
        )


def test_task_output_preserves_literal_paths_without_scope_expansion(card, monkeypatch):
    paths = ["outside-card/file.py", "src/[slug]/{name}/(group)/cafe\u0301.tsx", "src"]
    text = wire(
        payload(
            card, outcome="scope_blocked", summary="Need scope", required_paths=paths
        )
    )
    before = card.to_dict()

    def forbidden(*args, **kwargs):
        pytest.fail("output parsing must not read files or start processes")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert parse_task_output(text, card).required_paths == tuple(paths)
    assert card.to_dict() == before
