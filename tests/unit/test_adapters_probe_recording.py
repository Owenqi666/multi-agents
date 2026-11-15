import json
from pathlib import Path

import pytest

from multi_agent.adapters.probe import recording
from multi_agent.adapters.probe.recording import Recorder, Redactor


@pytest.mark.parametrize(
    "key",
    [
        "email",
        "USER_ID",
        "Account-Id",
        "orgId",
        "organization_id",
        "ORG-NAME",
        "organizationName",
        "workspace_name",
        "planType",
        "subscription-type",
        "accessToken",
        "client_secret",
        "PASSWORD",
        "service-api-key",
    ],
)
def test_redaction_masks_private_string_values_only(key: str, tmp_path: Path) -> None:
    redactor = Redactor(home=tmp_path / "home")
    source = {"nested": [{key: "private", "unknown": "keep"}], "counters": {key: 12}}

    assert redactor.redact(source) == {
        "nested": [{key: "<REDACTED>", "unknown": "keep"}],
        "counters": {key: 12},
    }
    assert redactor.redact({key: True}) == {key: True}
    assert redactor.redact({key: None}) == {key: None}
    assert source["nested"][0][key] == "private"


def test_redaction_preserves_counters_identifiers_and_unknown_fields(
    tmp_path: Path,
) -> None:
    data = {
        "inputTokens": 12,
        "outputTokens": 4,
        "fraction": 0.25,
        "threadId": "thread-original",
        "session_id": "session-original",
        "future": {"enabled": False, "missing": None, "values": [1, "untouched"]},
    }
    assert Redactor(home=tmp_path / "home").redact(data) == data


@pytest.mark.parametrize(
    "key",
    ["chatgptAccountId", "installationId", "serverName", "planTier", "balance"],
)
def test_redaction_masks_live_identifiers_without_changing_numbers(key: str) -> None:
    redactor = Redactor()
    assert redactor.redact({key: "private-value"}) == {key: "<REDACTED>"}
    assert redactor.redact({key: 42}) == {key: 42}


def test_redaction_preserves_probe_correlation_ids() -> None:
    data = {
        key: f"value-{key}"
        for key in (
            "tool_use_id",
            "session_id",
            "threadId",
            "turnId",
            "request_id",
            "environmentId",
        )
    }
    data.update({"inputTokens": 5, "outputTokens": 3})
    assert Redactor().redact(data) == data


@pytest.mark.parametrize(
    "private_value", ['quote: " then tail', r"path\and\tail", 'both \\" tail']
)
def test_text_redaction_handles_json_escapes_and_multiple_fields(
    private_value: str,
) -> None:
    value = json.dumps(private_value)
    text = f'  "orgName" : {value}, "planTier": "private-tier", "authMethod":"oauth",'
    assert Redactor().redact(text) == (
        '  "orgName" : "<REDACTED>", "planTier": "<REDACTED>", "authMethod":"oauth",'
    )


def test_text_redaction_keeps_unmatched_values_and_non_strings() -> None:
    line = (
        '"balance": 12, "loggedIn": true, "apiKeySource": "none", "request_id": "req-1"'
    )
    assert Redactor().redact(line) == line
    assert (
        Redactor().redact(r'"org\u0049d": "private"') == r'"org\u0049d": "<REDACTED>"'
    )


def test_redaction_replaces_path_aliases_and_emails_in_keys_and_values(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home_alias = tmp_path / "linked-home"
    home_alias.symlink_to(home, target_is_directory=True)
    workdir = home / "workdir"
    workdir.mkdir()
    workdir_alias = tmp_path / "linked-workdir"
    workdir_alias.symlink_to(workdir, target_is_directory=True)
    redactor = Redactor(home=home_alias)
    redactor.add_workdir(workdir_alias)
    source = {
        f"{home}/person+probe@example.test": [
            f"{home_alias}/config",
            f"{workdir_alias}/README.md",
            f"{workdir}/README.md",
            "Mail <one@example.test> or two+label@example.org.",
        ]
    }

    assert redactor.redact(source) == {
        "~/<EMAIL>": [
            "~/config",
            "<WORKDIR>/README.md",
            "<WORKDIR>/README.md",
            "Mail <<EMAIL>> or <EMAIL>.",
        ]
    }


def test_recorder_flushes_ordered_redacted_json_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([10.0, 10.25, 10.5, 10.75])
    monkeypatch.setattr(recording, "monotonic", lambda: next(ticks))
    path = tmp_path / "step.jsonl"
    home = tmp_path / "home"
    with Recorder(path, Redactor(home=home)) as recorder:
        recorder.emit("recv", proc="claude", stream="stdout", text=f"{home}/file")
        assert json.loads(path.read_text()) == {
            "seq": 1,
            "t": 0.25,
            "kind": "recv",
            "proc": "claude",
            "stream": "stdout",
            "text": "~/file",
        }
        recorder.emit("recv", proc="claude", stream="stderr", text="alice@example.test")
        assert recorder.count == 2
        assert recorder.seconds == 0.75
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [entry["seq"] for entry in records] == [1, 2]
    assert [entry["stream"] for entry in records] == ["stdout", "stderr"]
    assert records[1]["text"] == "<EMAIL>"


def test_recorder_keeps_large_output_and_redacts_summary_with_same_rules(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "workdir"
    redactor = Redactor(home=home)
    redactor.add_workdir(workdir)
    path = tmp_path / "step.jsonl"
    large = "x" * (1024 * 1024 + 1)
    message = {"text": large, "api_key": "secret-value", "inputTokens": 9}
    with Recorder(path, redactor) as recorder:
        recorder.emit("recv", proc="app-server", stream="stdout", message=message)
    record = json.loads(path.read_text())
    assert record["message"] == {
        "text": large,
        "api_key": "<REDACTED>",
        "inputTokens": 9,
    }
    summary = {
        "options": {"out": str(home / "runs"), "api-key": "private-key"},
        "steps": [{"cwd": str(workdir), "owner": "owner@example.test"}],
    }
    serialized = json.dumps(redactor.redact(summary))
    for private in (str(home), str(workdir), "private-key", "owner@example.test"):
        assert private not in serialized
    assert json.loads(serialized) == {
        "options": {"out": "~/runs", "api-key": "<REDACTED>"},
        "steps": [{"cwd": "<WORKDIR>", "owner": "<EMAIL>"}],
    }
