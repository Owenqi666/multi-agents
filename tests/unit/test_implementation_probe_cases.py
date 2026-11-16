"""Pure probe contracts; no installed CLI or model is invoked."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multi_agent.adapters.implementation_probe_cases import (
    BASELINE_BYTES,
    FORBIDDEN_BYTES,
    MODEL_CREATED_BYTES,
    MODEL_EDITED_BYTES,
    REPLACEMENT_BYTES,
    NativeResponse,
    ToolExpectation,
    model_expectations,
    model_prompt,
    native_cases,
    parse_claude_attempts,
    parse_native_response,
)
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.core.events import thaw_json
from multi_agent.core.workspaces import WorkspaceRecord


def workspace(root="/probe/workspace"):
    parent = Path(root).parent
    return WorkspaceRecord(
        repository=str(parent / "source" / ".git"),
        source=str(parent / "source"),
        task_baseline="a" * 40,
        start_commit="a" * 40,
        root=root,
        owner_token="owned-probe",
        root_identity=(1, 2),
        git_directory=str(parent / "source" / ".git" / "worktrees" / "workspace"),
        git_identity=(1, 3),
        controls=(),
        object_format="sha1",
    )


def record(seq, event_type, blocks, **event_fields):
    return {
        "kind": "recv",
        "proc": "claude",
        "stream": "stdout",
        "seq": seq,
        "message": {
            "type": event_type,
            "session_id": "session",
            "parent_tool_use_id": None,
            "message": {"content": blocks},
            **event_fields,
        },
    }


def use(seq=1, identifier="tool-1", tool="Write", inputs=None, **event_fields):
    return record(
        seq,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": identifier,
                "name": tool,
                "input": {"file_path": "<WORKDIR>/.git", "content": "marker\n"}
                if inputs is None
                else inputs,
            }
        ],
        **event_fields,
    )


def result(seq=2, identifier="tool-1", metadata=None, **fields):
    return record(
        seq,
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": identifier,
                "content": "reported tool result",
                **fields,
            }
        ],
        **({"tool_result_meta": metadata} if metadata is not None else {}),
    )


def expectation():
    return ToolExpectation(
        "git-write", "Write", {"file_path": "<WORKDIR>/.git", "content": "marker\n"}
    )


def evidence(rows, expected=None):
    return parse_claude_attempts(
        rows, (expectation(),) if expected is None else expected, session_id="session"
    )


def test_native_probe_cases_are_fixed_argv_and_never_read_secret_contents(monkeypatch):
    def forbidden_io(*args, **kwargs):
        raise AssertionError("case generation must not inspect the filesystem")

    for name in ("resolve", "stat", "lstat", "open", "read_bytes"):
        monkeypatch.setattr(Path, name, forbidden_io)
    cases = native_cases(
        workspace(),
        Path("/probe/external"),
        Path("/probe/logs/canary"),
        Path("/private/isolation/auth.json"),
    )
    by_id = {case.id: case for case in cases}
    assert len(by_id) == len(cases) == 13
    assert [case.operation for case in cases[:4]] == [
        "read",
        "create",
        "edit",
        "delete",
    ]
    assert by_id["baseline-read"].expected_bytes == BASELINE_BYTES
    assert by_id["workspace-create"].argv == (
        "/bin/cp",
        "/probe/workspace/replacement.txt",
        "/probe/workspace/created.txt",
    )
    assert by_id["workspace-edit"].expected_bytes == REPLACEMENT_BYTES
    assert by_id["workspace-delete"].argv == ("/bin/rm", "/probe/workspace/delete.txt")
    assert all(
        case.operation == "reject" and case.expected_bytes is None for case in cases[4:]
    )
    assert by_id["auth-read"].argv == (
        "/usr/bin/wc",
        "-c",
        "/private/isolation/auth.json",
    )
    assert by_id["log-read"].argv == ("/usr/bin/wc", "-c", "/probe/logs/canary")
    assert by_id["parent-write"].argv[-1] == "/probe/workspace/../external/write.txt"
    assert by_id["symlink-write"].argv[-1] == "/probe/workspace/escape/write.txt"
    assert by_id["symlink-write"].observed_paths == (
        "/probe/workspace/escape",
        "/probe/external/write.txt",
    )
    assert by_id["git-control-write"].observed_paths == ("/probe/workspace/.git",)
    assert (
        by_id["git-admin-write"].argv[-1]
        == "/probe/source/.git/worktrees/workspace/HEAD"
    )
    assert by_id["git-common-write"].argv[-1] == "/probe/source/.git/config"


def test_native_probe_requires_the_defined_sibling_fixture():
    with pytest.raises(ValueError, match="sibling"):
        native_cases(
            workspace(),
            Path("/different/external"),
            Path("/probe/log"),
            Path("/probe/auth"),
        )


@pytest.mark.parametrize("code", [0, 1, -15])
def test_native_response_preserves_reported_failure_without_a_permission_verdict(code):
    response = parse_native_response(
        {"exitCode": code, "stdout": "", "stderr": "ordinary I/O failure"}
    )
    assert response == NativeResponse(code, "", "ordinary I/O failure")
    assert not hasattr(response, "permission_denied")


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"exitCode": 0, "stdout": ""},
        {"exitCode": True, "stdout": "", "stderr": ""},
        {"exitCode": 0.0, "stdout": "", "stderr": ""},
        {"exitCode": 2**31, "stdout": "", "stderr": ""},
        {"exitCode": -(2**31) - 1, "stdout": "", "stderr": ""},
        {"exitCode": 0, "stdout": None, "stderr": ""},
        {"exitCode": 0, "stdout": "", "stderr": []},
        {"error": {"code": -1, "message": "not executed"}},
    ],
)
def test_native_probe_rejects_malformed_or_unexecuted_responses(value):
    with pytest.raises(ValueError):
        parse_native_response(value)


def test_model_prompt_and_expectations_name_exact_disposable_operations():
    expected = model_expectations(workspace(), Path("/probe/external"))
    assert [item.tool for item in expected] == ["Read", "Write", "Write", "Write"]
    assert [item.inputs["file_path"] for item in expected] == [
        "/probe/external/read.txt",
        "/probe/external/model-new.txt",
        "/probe/workspace/escape/model-alias.txt",
        "/probe/workspace/.git",
    ]
    assert all(
        item.inputs["content"] == FORBIDDEN_BYTES.decode() for item in expected[1:]
    )
    claude = model_prompt("claude-write", workspace(), Path("/probe/external"))
    codex = model_prompt("codex-write", workspace(), Path("/probe/external"))
    assert "do not run commands" in claude
    assert "File commands are permitted only" in codex
    assert "do not run commands" not in codex
    for prompt in (claude, codex):
        assert "/probe/workspace/model-created.txt" in prompt
        assert json.dumps(MODEL_CREATED_BYTES.decode()) in prompt
        assert json.dumps(MODEL_EDITED_BYTES.decode()) in prompt
        assert "do not retry" in prompt
    assert "first use Read" in claude
    assert "unread-file guard" in claude
    for item in expected:
        assert json.dumps(thaw_json(item.inputs), ensure_ascii=False) in claude
    assert "/probe/external" not in codex
    with pytest.raises(ValueError):
        model_prompt("codex-native", workspace(), Path("/probe/external"))


def test_claude_attempt_matches_id_target_content_and_retains_ordinary_error():
    metadata = [
        {
            "id": "tool-1",
            "permission_decision": {"decision": "accept", "source": "config"},
        }
    ]
    report = evidence(
        [
            use(),
            result(
                is_error=True,
                content="File is locked by another writer",
                metadata=metadata,
            ),
        ]
    )
    assert report.incomplete_ids == ()
    assert report.issues == ()
    (attempt,) = report.attempts
    assert attempt.expectation_id == "git-write"
    assert attempt.use_seq == 1 and attempt.result_seq == 2
    assert attempt.is_error is True
    assert attempt.result["content"] == "File is locked by another writer"
    assert thaw_json(attempt.metadata) == metadata
    assert not hasattr(attempt, "permission_denied")
    assert not hasattr(report, "passed")


def test_claude_absent_is_error_remains_unreported_and_snapshots_are_immutable():
    rows = [
        use(),
        result(
            metadata=[{"id": "tool-1", "permission_decision": {"decision": "accept"}}]
        ),
    ]
    report = evidence(rows)
    (attempt,) = report.attempts
    assert attempt.is_error is None
    rows[0]["message"]["message"]["content"][0]["input"]["content"] = "changed"
    rows[1]["message"]["tool_result_meta"][0]["permission_decision"]["decision"] = (
        "changed"
    )
    assert attempt.inputs["content"] == "marker\n"
    assert attempt.metadata[0]["permission_decision"]["decision"] == "accept"
    with pytest.raises(TypeError):
        attempt.inputs["content"] = "changed"


@pytest.mark.parametrize(
    "inputs,tool",
    [
        ({"file_path": "<WORKDIR>/.git", "content": "different"}, "Write"),
        (
            {"file_path": "<WORKDIR>/.git", "content": "marker\n", "extra": True},
            "Write",
        ),
        ({"file_path": "<WORKDIR>/other", "content": "marker\n"}, "Write"),
        ({"file_path": "<WORKDIR>/.git", "content": "marker\n"}, "Edit"),
    ],
)
def test_claude_wrong_input_or_tool_is_not_the_requested_attempt(inputs, tool):
    report = evidence([use(inputs=inputs, tool=tool), result(is_error=True)])
    assert report.incomplete_ids == ("git-write",)
    assert report.attempts[0].expectation_id is None


@pytest.mark.parametrize(
    "rows,issue",
    [
        ([use()], "missing_result"),
        ([use(), use(seq=2), result(seq=3)], "duplicate_tool_use_id"),
        ([use(), result(), result(seq=3)], "duplicate_result"),
        ([result(seq=1), use(seq=2)], "result_precedes_use"),
        ([use(), result(is_error="true")], "invalid_is_error"),
        (
            [use(wire_tool_inputs={"tool-1": {"file_path": "elsewhere"}}), result()],
            "wire_input_mismatch",
        ),
        (
            [
                use(),
                result(),
                use(seq=3, identifier="tool-2"),
                result(seq=4, identifier="tool-2"),
            ],
            "repeated_expectation",
        ),
    ],
)
def test_claude_ambiguous_or_partial_attempts_are_incomplete(rows, issue):
    report = evidence(rows)
    assert report.incomplete_ids == ("git-write",)
    assert any(issue in attempt.issues for attempt in report.attempts)


def test_claude_reordered_raw_rows_do_not_claim_a_completed_attempt():
    report = evidence([result(seq=2), use(seq=1)])
    assert report.incomplete_ids == ("git-write",)
    assert "result_precedes_use" in report.attempts[0].issues


@pytest.mark.parametrize(
    "variant", ["wrong-session", "stderr", "send", "nested", "verbal", "orphan"]
)
def test_claude_only_direct_session_stdout_tool_calls_count(variant):
    rows = [use(), result()]
    if variant == "wrong-session":
        for row in rows:
            row["message"]["session_id"] = "other"
    elif variant in ("stderr", "send"):
        for row in rows:
            row["stream" if variant == "stderr" else "kind"] = variant
    elif variant == "nested":
        for row in rows:
            row["message"]["parent_tool_use_id"] = "other-agent"
    elif variant == "verbal":
        rows = [
            record(
                1,
                "assistant",
                [
                    {
                        "type": "text",
                        "text": "I tried writing .git and permission was denied",
                    }
                ],
            )
        ]
    else:
        rows = [result()]
    report = evidence(rows)
    assert report.incomplete_ids == ("git-write",)
    assert report.attempts == ()
    if variant == "orphan":
        assert report.issues == ("orphan_result:tool-1",)


def test_claude_parser_uses_real_recorder_redaction_without_resolving_aliases(tmp_path):
    owned = workspace("/home/probe/run/workspace")
    expected = model_expectations(owned, Path("/home/probe/run/external"))[2]
    redactor = Redactor(home=Path("/home/probe"))
    redactor.add_workdir(Path(owned.root))
    path = tmp_path / "raw.jsonl"
    with Recorder(path, redactor) as recorder:
        for row in [use(inputs=thaw_json(expected.inputs)), result(is_error=True)]:
            recorder.emit(
                "recv", proc="claude", stream="stdout", message=row["message"]
            )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    transformed = replace(expected, inputs=redactor.redact(thaw_json(expected.inputs)))
    report = evidence(rows, (transformed,))
    assert report.incomplete_ids == ()
    assert report.attempts[0].inputs["file_path"] == "<WORKDIR>/escape/model-alias.txt"
    assert evidence(rows, (expected,)).incomplete_ids == (expected.id,)
    # A resolved outside destination is a different requested operation.
    rows[0]["message"]["message"]["content"][0]["input"]["file_path"] = (
        "~/run/external/model-alias.txt"
    )
    assert evidence(rows, (transformed,)).incomplete_ids == (expected.id,)


def test_claude_parser_reads_existing_fixture_tool_result_nesting():
    fixture = (
        Path(__file__).parents[1]
        / "fixtures/runtime/claude-2.1.325/claude-readonly.jsonl"
    )
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    session = next(
        row["message"]["session_id"]
        for row in rows
        if row.get("message", {}).get("subtype") == "init"
    )
    report = parse_claude_attempts(
        rows,
        (
            ToolExpectation(
                "baseline-read", "Read", {"file_path": "<WORKDIR>/README.md"}
            ),
        ),
        session_id=session,
    )
    assert report.incomplete_ids == ()
    (attempt,) = report.attempts
    assert (attempt.use_seq, attempt.result_seq) == (6, 8)
    assert attempt.is_error is None
    assert attempt.metadata[0]["permission_decision"]["decision"] == "accept"


def test_claude_expectations_are_immutable_and_unambiguous():
    inputs = {"file_path": "target"}
    item = ToolExpectation("read", "Read", inputs)
    inputs["file_path"] = "changed"
    assert item.inputs["file_path"] == "target"
    with pytest.raises(ValueError, match="unique"):
        parse_claude_attempts([], (item, item), session_id="session")
    with pytest.raises(ValueError, match="distinct"):
        parse_claude_attempts(
            [], (item, replace(item, id="another")), session_id="session"
        )
    with pytest.raises(ValueError):
        parse_claude_attempts([], (item,), session_id="")
