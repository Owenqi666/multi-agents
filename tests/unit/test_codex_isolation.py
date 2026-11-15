"""Boundary regressions for Codex isolation; no installed CLI is invoked."""

import asyncio
import copy
import json
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from multi_agent.adapters import codex_isolation
from multi_agent.adapters.codex_isolation import CodexIsolation, isolated_environment
from multi_agent.adapters.probe.process import Process, SpawnFailed
from multi_agent.adapters.probe.recording import Recorder, Redactor
from multi_agent.adapters.runtime import _Rpc
from multi_agent.adapters.runtime_decode import RuntimeFailure
from multi_agent.core.events import FailureCategory


@pytest.mark.parametrize(
    "identifier",
    [
        {"opaque": "synthetic-config-secret"},
        ["synthetic-config-secret"],
        True,
        1.0,
        None,
    ],
)
def test_rpc_rejects_malformed_server_request_without_echoing_id(tmp_path, identifier):
    path = tmp_path / "rpc.jsonl"
    sent = []
    with Recorder(path, Redactor()) as recorder:

        class Transport:
            _recorder = recorder

            async def send(self, message):
                sent.append(message)
                recorder.emit("send", proc="codex", message=message)
                return True

            async def read(self):
                message = {
                    "id": identifier,
                    "method": "unknown/server/request",
                    "params": {"echo": "synthetic-config-secret"},
                }
                recorder.emit("recv", proc="codex", stream="stdout", message=message)
                return message

        with pytest.raises(RuntimeFailure, match="malformed JSON-RPC server request"):
            asyncio.run(_Rpc(Transport()).request("config/read", {}))
    assert [message.get("method") for message in sent] == ["config/read"]
    assert "synthetic-config-secret" not in path.read_text()


def test_config_read_records_only_audit_shape_without_mutating_response(tmp_path):
    sentinel = "synthetic-config-secret-never-persist"
    response = {
        "id": 7,
        "result": {
            "config": {
                "default_permissions": sentinel,
                "env": {"ORDINARY_NAME": sentinel},
                "http_headers": {"Authorization": "Bearer " + sentinel},
                "array": [sentinel, {"nested": sentinel}],
            },
            "layers": [
                {
                    "name": {"type": "user", "file": sentinel, "profile": sentinel},
                    "version": sentinel,
                    "config": {sentinel: [sentinel]},
                    "disabledReason": sentinel,
                },
                {
                    "name": {"type": sentinel},
                    "config": sentinel,
                    "disabledReason": {"private": sentinel},
                },
                sentinel,
            ],
            "origins": {sentinel: {"version": sentinel}},
            "unexpected": sentinel,
        },
        "unexpected": sentinel,
    }
    unchanged = copy.deepcopy(response)
    path = tmp_path / "records.jsonl"
    with Recorder(path, Redactor()) as recorder:
        recorder.emit(
            "send",
            proc="codex",
            message={"id": 7, "method": "config/read", "params": {}},
        )
        recorder.emit("recv", proc="codex", stream="stdout", message=response)
        # Duplicate responses must remain sanitized after the first match.
        recorder.emit("recv", proc="codex", stream="stdout", message=response)
    assert response == unchanged
    text = path.read_text()
    assert sentinel not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert records[1]["message"] == records[2]["message"]
    assert records[1]["message"] == {
        "id": 7,
        "config_read_audit": {
            "raw_payload_omitted": True,
            "has_result": True,
            "has_error": False,
            "result_type": "object",
            "error_type": "missing",
            "config_type": "object",
            "config_count": 4,
            "layers_type": "array",
            "layers_count": 3,
            "origins_type": "object",
            "origins_count": 1,
            "layer_sources": [
                {"source_type": "user", "disabled": True},
                {"source_type": "unrecognized", "disabled": None},
                {"source_type": "unrecognized", "disabled": None},
            ],
        },
    }


@pytest.mark.parametrize("payload", ["secret", ["secret"], {"unknown": "secret"}, None])
def test_malformed_config_read_result_never_records_arbitrary_payload(
    tmp_path, payload
):
    path = tmp_path / "records.jsonl"
    response = {"id": "config", "result": payload}
    unchanged = copy.deepcopy(response)
    with Recorder(path, Redactor()) as recorder:
        recorder.emit(
            "send", proc="codex", message={"id": "config", "method": "config/read"}
        )
        recorder.emit("recv", proc="codex", message=response)
    assert "secret" not in path.read_text()
    assert response == unchanged
    record = json.loads(path.read_text().splitlines()[-1])
    assert record["message"]["config_read_audit"]["raw_payload_omitted"] is True


@pytest.mark.parametrize(
    "error",
    [
        {"code": -32603, "message": "secret", "data": {"env": ["secret"]}},
        {"code": "secret", "message": "secret"},
        ["secret"],
        "secret",
    ],
)
def test_config_read_errors_are_summarized_before_recording(tmp_path, error):
    path = tmp_path / "records.jsonl"
    response = {"id": 1, "error": error, "unexpected": "secret"}
    unchanged = copy.deepcopy(response)
    with Recorder(path, Redactor()) as recorder:
        recorder.emit("send", proc="codex", message={"id": 1, "method": "config/read"})
        recorder.emit("recv", proc="codex", message=response)
    assert "secret" not in path.read_text()
    assert response == unchanged
    record = json.loads(path.read_text().splitlines()[-1])
    summary = record["message"]["config_read_audit"]
    assert summary["has_error"] is True
    assert summary["has_result"] is False
    if isinstance(error, dict):
        assert summary["error_code"] == (-32603 if error["code"] == -32603 else None)


def test_config_read_correlation_uses_process_and_id_and_preserves_other_messages(
    tmp_path,
):
    path = tmp_path / "records.jsonl"
    normal = {"id": 1, "result": {"config": {"plain": "keep"}, "counter": 12}}
    with Recorder(path, Redactor()) as recorder:
        recorder.emit("send", proc="one", message={"id": 1, "method": "config/read"})
        recorder.emit("recv", proc="two", message=normal)
        recorder.emit("recv", proc="one", message={**normal, "id": 2})
        recorder.emit("send", proc="one", message={"id": 1, "method": "model/list"})
        recorder.emit("recv", proc="one", message=normal)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[1]["message"] == normal
    assert records[2]["message"]["config_read_audit"]["raw_payload_omitted"] is True
    assert records[4]["message"] == normal


def test_config_read_does_not_replace_incoming_request_with_same_id(tmp_path):
    path = tmp_path / "records.jsonl"
    request = {"id": 1, "method": "unknown/server/request", "params": {"counter": 2}}
    with Recorder(path, Redactor()) as recorder:
        recorder.emit("send", proc="codex", message={"id": 1, "method": "config/read"})
        recorder.emit("recv", proc="codex", message=request)
        recorder.emit(
            "recv", proc="codex", message={"id": 1, "result": {"unknown": "secret"}}
        )
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[1]["message"] == request
    assert "secret" not in path.read_text()
    assert records[2]["message"]["config_read_audit"]["raw_payload_omitted"] is True


@pytest.mark.parametrize(
    "identifier", ["wrong-secret-id", True, 1.0, ["secret"], {"secret": "secret"}, None]
)
def test_pending_config_read_masks_unknown_or_malformed_reply_ids(tmp_path, identifier):
    path = tmp_path / "records.jsonl"
    response = {"id": identifier, "result": {"env": {"SECRET": "secret"}}}
    with Recorder(path, Redactor()) as recorder:
        recorder.emit("send", proc="codex", message={"id": 1, "method": "config/read"})
        recorder.emit("recv", proc="codex", message=response)
        recorder.emit("recv", proc="codex", message={"error": {"message": "secret"}})
    text = path.read_text()
    assert "secret" not in text
    records = [json.loads(line) for line in text.splitlines()]
    for record in records[1:]:
        assert record["message"]["id"] is None
        assert record["message"]["config_read_audit"]["raw_payload_omitted"] is True


@pytest.mark.parametrize(
    "identifier", [True, 1.0, ["secret"], {"secret": "secret"}, None]
)
def test_pending_config_read_rejects_malformed_server_request_ids(tmp_path, identifier):
    path = tmp_path / "records.jsonl"
    message = {
        "id": identifier,
        "method": "unknown/request",
        "params": {"env": "secret"},
    }
    unchanged = copy.deepcopy(message)
    with Recorder(path, Redactor()) as recorder:
        recorder.emit("send", proc="codex", message={"id": 1, "method": "config/read"})
        recorder.emit("recv", proc="codex", message=message)
    assert "secret" not in path.read_text()
    assert message == unchanged
    recorded = json.loads(path.read_text().splitlines()[-1])["message"]
    assert recorded["id"] is None
    assert recorded["config_read_audit"]["raw_payload_omitted"] is True


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("payload", ['["secret"]', '"secret"', "invalid JSON secret"])
def test_pending_config_read_omits_unstructured_output(tmp_path, stream, payload):
    path = tmp_path / "records.jsonl"
    with Recorder(path, Redactor()) as recorder:
        recorder.emit("send", proc="codex", message={"id": 1, "method": "config/read"})
        recorder.emit("recv", proc="codex", stream=stream, text=payload)
        recorder.emit("recv", proc="codex", message={"id": 1, "result": {}})
        recorder.emit("recv", proc="codex", stream=stream, text="ordinary later output")
    assert "secret" not in path.read_text()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[1]["text"] == "<CONFIG_READ_PAYLOAD_OMITTED>"
    assert records[1]["config_read_text_audit"] == {
        "raw_payload_omitted": True,
        "value_type": "string",
        "character_count": len(payload),
    }
    assert records[-1]["text"] == "ordinary later output"


def test_secret_only_redaction_preserves_business_paths_emails_and_unregistered_fields(
    tmp_path,
):
    redactor = Redactor(home=tmp_path)
    redactor.add_secret("synthetic-private-credential")
    original = {
        "email": "reviewer@example.test",
        "access_token": "unregistered-code-constant",
        "path": str(tmp_path / "source.py"),
        "nested": [
            {"synthetic-private-credential": "Bearer synthetic-private-credential"}
        ],
        "counts": [0, False, None],
    }
    unchanged = copy.deepcopy(original)
    result = redactor.redact_secrets(original)
    assert original == unchanged
    assert result == {
        **original,
        "nested": [{"<REDACTED>": "Bearer <REDACTED>"}],
    }


@pytest.mark.parametrize("secret", ["synthetic-token-sentinel", 'quoted"slash\\\n雪'])
def test_registered_secret_is_removed_from_text_errors_stderr_and_nested_values(
    tmp_path, secret
):
    redactor = Redactor()
    redactor.add_secret(secret)
    path = tmp_path / "records.jsonl"
    original = {
        "id": 2,
        "error": {"code": -1, "message": "unexpected credential: " + secret},
        "values": [{secret: [secret]}, 12, False, None],
    }
    unchanged = copy.deepcopy(original)
    with Recorder(path, redactor) as recorder:
        recorder.emit("recv", proc="codex", message=original)
        recorder.emit("recv", proc="codex", stream="stderr", text="Bearer " + secret)
        recorder.emit("cleanup_incomplete", detail="failure with " + secret)
        for ensure_ascii in (False, True):
            recorder.emit(
                "recv",
                proc="codex",
                stream="stderr",
                text=json.dumps({"ordinary": secret}, ensure_ascii=ensure_ascii),
            )
    assert original == unchanged
    text = path.read_text()
    records = [json.loads(line) for line in text.splitlines()]
    assert secret not in text
    assert secret not in json.dumps(records, ensure_ascii=False)
    assert (
        records[0]["message"]["error"]["message"] == "unexpected credential: <REDACTED>"
    )
    assert records[0]["message"]["values"] == [
        {"<REDACTED>": ["<REDACTED>"]},
        12,
        False,
        None,
    ]
    assert records[1]["text"] == "Bearer <REDACTED>"
    assert records[2]["detail"] == "failure with <REDACTED>"
    assert all("<REDACTED>" in record["text"] for record in records[3:])


def test_registered_secrets_match_longest_first_and_can_be_added_after_recorder(
    tmp_path,
):
    redactor = Redactor()
    path = tmp_path / "records.jsonl"
    with Recorder(path, redactor) as recorder:
        redactor.add_secret("prefix-credential")
        redactor.add_secret("prefix-credential-suffix")
        recorder.emit("recv", text="prefix-credential-suffix")
    assert json.loads(path.read_text())["text"] == "<REDACTED>"


@pytest.mark.parametrize("value", ["", None, 0])
def test_secret_registration_rejects_empty_or_non_string_values(value):
    with pytest.raises((ValueError, TypeError)):
        Redactor().add_secret(value)


def subscription_auth():
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "synthetic-id-credential",
            "access_token": "synthetic-access-credential",
            "refresh_token": "synthetic-refresh-credential",
            "account_id": "synthetic-account-identifier",
        },
        "last_refresh": "2026-11-15T00:00:00Z",
    }


@pytest.fixture
def isolation_context(tmp_path, monkeypatch):
    source = tmp_path / "user-codex"
    source.mkdir()
    auth_file = source / "auth.json"
    auth_file.write_text(json.dumps(subscription_auth()))
    monkeypatch.setenv("CODEX_HOME", str(source))
    cwd = tmp_path / "frozen"
    cwd.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    temporary = tmp_path / "isolations"
    temporary.mkdir()
    monkeypatch.setattr(
        codex_isolation,
        "mkdtemp",
        lambda **kwargs: tempfile.mkdtemp(dir=temporary, **kwargs),
    )
    path = logs / "records.jsonl"
    with Recorder(path, Redactor()) as recorder:
        yield SimpleNamespace(
            cwd=cwd,
            logs=logs,
            source=source,
            auth_file=auth_file,
            temporary=temporary,
            path=path,
            recorder=recorder,
            create=lambda: CodexIsolation.create(
                cwd, recorder, isolated_environment(), log_dir=logs
            ),
        )


def assert_private_resources(isolation):
    for path in (
        isolation.root,
        isolation.root / "home",
        isolation.codex_home,
        isolation.root / "tmp",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for name in ("auth.json", "config.toml"):
        assert stat.S_IMODE((isolation.codex_home / name).stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", ["missing", None, "chatgpt"])
@pytest.mark.parametrize("account", ["missing", None, "synthetic-account-identifier"])
def test_isolation_preserves_legacy_subscription_and_oauth_refresh_metadata(
    isolation_context, mode, account
):
    context = isolation_context
    auth = subscription_auth()
    if mode == "missing":
        del auth["auth_mode"]
    else:
        auth["auth_mode"] = mode
    if account == "missing":
        del auth["tokens"]["account_id"]
    else:
        auth["tokens"]["account_id"] = account
    auth["tokens"].update(
        oauth_provenance={
            "issuer": "https://auth.example.invalid",
            "client_id": "synthetic-client-id",
        },
        login_owner_id="synthetic-owner-id",
        unrelated_user_state="do-not-copy",
    )
    auth["unrelated_user_state"] = "do-not-copy"
    context.auth_file.write_text(json.dumps(auth))
    original = context.auth_file.read_bytes()
    (context.source / "AGENTS.md").write_text("do-not-copy")
    (context.source / "config.toml").write_text(
        '[mcp_servers.hostile]\ncommand="do-not-copy"\n'
    )
    for name in (
        "memories/MEMORY.md",
        "skills/hostile/SKILL.md",
        "plugins/hostile.json",
    ):
        target = context.source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("do-not-copy")
    isolation = context.create()
    try:
        copied = json.loads((isolation.codex_home / "auth.json").read_text())
        expected = copy.deepcopy(auth)
        del expected["OPENAI_API_KEY"]
        del expected["unrelated_user_state"]
        del expected["tokens"]["unrelated_user_state"]
        assert copied == expected
        assert context.auth_file.read_bytes() == original
        assert sorted(p.name for p in isolation.codex_home.iterdir()) == [
            "auth.json",
            "config.toml",
        ]
        assert not list((isolation.root / "home").iterdir())
        assert_private_resources(isolation)
        assert isolation.environment == {
            **isolated_environment(),
            "HOME": str(isolation.root / "home"),
            "CODEX_HOME": str(isolation.codex_home),
            "TMPDIR": str(isolation.root / "tmp"),
        }
        context.recorder.emit("diagnostic", value=expected["tokens"])
        text = context.path.read_text()
        for secret in (
            "synthetic-id-credential",
            "synthetic-access-credential",
            "synthetic-refresh-credential",
            "synthetic-owner-id",
            "https://auth.example.invalid",
            "synthetic-client-id",
        ):
            assert secret not in text
        features = isolation.config["features"]
        assert all(
            features[key] is False
            for key in (
                "context_management",
                "token_budget",
                "image_generation",
                "agent_message_board",
                "agent_message_board_mcp",
                "multi_env_copy_file",
            )
        )
        assert "plugin_hooks" not in features
    finally:
        assert isolation.cleanup() is True
    assert not isolation.root.exists()


@pytest.mark.parametrize(
    "field",
    [
        "OPENAI_API_KEY",
        "personal_access_token",
        "bedrock_api_key",
        "bedrock_access_keys",
        "agent_identity",
    ],
)
def test_isolation_does_not_convert_other_credentials_to_subscription(
    isolation_context, field
):
    context = isolation_context
    auth = subscription_auth()
    del auth["auth_mode"]
    auth[field] = "synthetic-secret"
    context.auth_file.write_text(json.dumps(auth))
    with pytest.raises(RuntimeFailure) as failure:
        context.create()
    assert failure.value.event.failure_category == FailureCategory.AUTH_EXPIRED
    assert not list(context.temporary.iterdir())
    assert "synthetic-secret" not in context.path.read_text()


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "bad-json",
        "non-object",
        "other-mode",
        "missing-tokens",
        "empty-token",
        "bad-account",
        "bad-owner",
        "bad-provenance",
        "bad-refresh",
    ],
)
def test_invalid_auth_fails_without_leaking_or_retaining_resources(
    isolation_context, invalid
):
    context = isolation_context
    auth = subscription_auth()
    if invalid == "missing":
        context.auth_file.unlink()
    elif invalid == "bad-json":
        context.auth_file.write_text("synthetic-secret invalid json")
    elif invalid == "non-object":
        context.auth_file.write_text('["synthetic-secret"]')
    else:
        if invalid == "other-mode":
            auth["auth_mode"] = "chatgptAuthTokens"
        elif invalid == "missing-tokens":
            del auth["tokens"]
        elif invalid == "empty-token":
            auth["tokens"]["access_token"] = ""
        elif invalid == "bad-account":
            auth["tokens"]["account_id"] = ["synthetic-secret"]
        elif invalid == "bad-owner":
            auth["tokens"]["login_owner_id"] = {"secret": "synthetic-secret"}
        elif invalid == "bad-provenance":
            auth["tokens"]["oauth_provenance"] = {"issuer": "synthetic-secret"}
        else:
            auth["last_refresh"] = ["synthetic-secret"]
        context.auth_file.write_text(json.dumps(auth))
    with pytest.raises(RuntimeFailure) as failure:
        context.create()
    assert failure.value.event.failure_category == FailureCategory.AUTH_EXPIRED
    assert failure.value.event.raw_type == "adapter.isolation_auth"
    assert not list(context.temporary.iterdir())
    assert "synthetic-secret" not in context.path.read_text()


@pytest.mark.parametrize(
    "overlap",
    ["root-in-cwd", "root-in-logs", "logs-in-cwd", "cwd-in-logs", "root-in-cwd-alias"],
)
def test_isolation_rejects_realpath_overlap_before_reading_auth(
    isolation_context, monkeypatch, overlap
):
    context = isolation_context
    cwd, logs = context.cwd, context.logs
    if overlap.startswith("root-in"):
        parent = cwd if "cwd" in overlap else logs
        if overlap.endswith("alias"):
            alias = context.temporary / "alias"
            alias.symlink_to(parent, target_is_directory=True)
            parent = alias
        monkeypatch.setattr(
            codex_isolation, "mkdtemp", lambda **kw: tempfile.mkdtemp(dir=parent, **kw)
        )
    elif overlap == "logs-in-cwd":
        logs = cwd / "logs"
        logs.mkdir()
    else:
        cwd = logs / "frozen"
        cwd.mkdir()
    original_read = Path.read_bytes

    def no_auth_read(path):
        assert path != context.auth_file, (
            "credentials read before path separation was established"
        )
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_auth_read)
    with pytest.raises(RuntimeFailure) as failure:
        CodexIsolation.create(
            cwd, context.recorder, isolated_environment(), log_dir=logs
        )
    assert failure.value.event.raw_type == "adapter.isolation_path"
    assert not list(context.cwd.glob("multi-agent-codex-*"))
    assert not list(context.logs.glob("multi-agent-codex-*"))
    assert not list(context.temporary.glob("multi-agent-codex-*"))


def test_creation_cleanup_failure_preserves_original_auth_error(
    isolation_context, monkeypatch
):
    context = isolation_context
    context.auth_file.unlink()

    def failed_remove(path):
        raise OSError("synthetic removal failure")

    monkeypatch.setattr(codex_isolation.shutil, "rmtree", failed_remove)
    with pytest.raises(RuntimeFailure) as failure:
        context.create()
    assert failure.value.event.failure_category == FailureCategory.AUTH_EXPIRED
    roots = list(context.temporary.iterdir())
    assert len(roots) == 1
    assert stat.S_IMODE(roots[0].stat().st_mode) == 0o700
    records = [json.loads(line) for line in context.path.read_text().splitlines()]
    assert records[-1]["kind"] == "cleanup_incomplete"
    assert records[-1]["owner"] == str(roots[0])
    assert records[-1]["resources_removed"] is False
    assert "OSError" in failure.value.__notes__[0]


def test_resource_deletion_failure_retains_private_files_and_can_be_retried(
    isolation_context, monkeypatch
):
    isolation = isolation_context.create()
    with monkeypatch.context() as patch:

        def failed_remove(path):
            raise OSError("synthetic removal failure")

        patch.setattr(codex_isolation.shutil, "rmtree", failed_remove)
        with pytest.raises(OSError, match="synthetic removal failure"):
            isolation.cleanup()
        assert_private_resources(isolation)
    assert isolation.cleanup() is True
    assert isolation.cleanup() is True


async def live_child(isolation):
    process = await asyncio.wait_for(
        isolation.spawn(
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(60)",
            ],
            pipe=False,
            grace=0.05,
        ),
        3,
    )
    try:
        assert await asyncio.wait_for(process.read(), 3) == "ready"
    except BaseException:
        await isolation.terminate(process, 0.05)
        raise
    return process


def test_live_process_prevents_cleanup_until_real_termination(isolation_context):
    isolation = isolation_context.create()

    async def scenario():
        process = await live_child(isolation)
        try:
            assert isolation.cleanup() is False
            assert_private_resources(isolation)
            assert process.child.returncode is None
        finally:
            await isolation.terminate(process, 0.05)
        assert process.child.returncode is not None
        assert isolation.cleanup() is True

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unknown-start", "missing-executable"])
def test_spawn_failure_cleans_only_known_absent_process(
    isolation_context, monkeypatch, failure
):
    isolation = isolation_context.create()
    if failure == "unknown-start":

        async def failed_start(*args, **kwargs):
            raise OSError("start outcome unknown")

        monkeypatch.setattr(Process, "start", failed_start)
        expected = OSError
    else:
        expected = SpawnFailed
    with pytest.raises(expected):
        asyncio.run(
            isolation.spawn(
                ["/does-not-exist/multi-agent-test"], pipe=False, grace=0.05
            )
        )
    assert isolation.cleanup() is (failure == "missing-executable")
    if failure == "unknown-start":
        assert_private_resources(isolation)
        assert '"unknown_spawns": 1' in isolation_context.path.read_text()


@pytest.mark.parametrize("failure", ["before-stop", "after-exit", "unobserved-exit"])
def test_termination_failure_retains_resources_even_if_child_exit_is_known(
    isolation_context, monkeypatch, failure
):
    isolation = isolation_context.create()

    async def scenario():
        process = await live_child(isolation)
        original = process.terminate

        async def failed_stop(grace):
            if failure == "after-exit":
                await original(grace)
            if failure != "unobserved-exit":
                raise OSError("termination incomplete")

        try:
            monkeypatch.setattr(process, "terminate", failed_stop)
            with pytest.raises((OSError, RuntimeError)):
                await isolation.terminate(process, 0.05)
            assert isolation.cleanup() is False
            assert_private_resources(isolation)
            if failure == "after-exit":
                assert process.child.returncode is not None
        finally:
            monkeypatch.setattr(process, "terminate", original)
            await isolation.terminate(process, 0.05)
            assert isolation.cleanup() is True

    asyncio.run(scenario())


def test_cancel_during_spawn_waits_for_owned_process_cleanup(
    isolation_context, monkeypatch
):
    isolation = isolation_context.create()
    original_start = Process.start

    async def scenario():
        launched = asyncio.Event()
        release = asyncio.Event()
        captured = []

        async def gated_start(*args, **kwargs):
            process = await original_start(*args, **kwargs)
            captured.append(process)
            launched.set()
            await release.wait()
            return process

        monkeypatch.setattr(Process, "start", gated_start)
        caller = asyncio.create_task(
            isolation.spawn(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                pipe=False,
                grace=0.05,
            )
        )
        try:
            await asyncio.wait_for(launched.wait(), 3)
            caller.cancel()
            assert isolation.cleanup() is False
            assert_private_resources(isolation)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 3)
            assert captured[0].child.returncode is not None
            assert isolation.cleanup() is True
        finally:
            release.set()
            await asyncio.gather(caller, return_exceptions=True)
            for process in captured:
                await process.terminate(0.05)

    asyncio.run(scenario())


def test_cancel_during_termination_preserves_cancel_and_confirms_cleanup(
    isolation_context, monkeypatch
):
    isolation = isolation_context.create()

    async def scenario():
        process = await live_child(isolation)
        stopping = asyncio.Event()
        release = asyncio.Event()
        original = process.terminate

        async def gated_stop(grace):
            stopping.set()
            await release.wait()
            await original(grace)

        monkeypatch.setattr(process, "terminate", gated_stop)
        caller = asyncio.create_task(isolation.terminate(process, 0.05))
        try:
            await asyncio.wait_for(stopping.wait(), 3)
            caller.cancel()
            assert isolation.cleanup() is False
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 3)
            assert process.child.returncode is not None
            assert isolation.cleanup() is True
        finally:
            release.set()
            await asyncio.gather(caller, return_exceptions=True)
            await original(0.05)

    asyncio.run(scenario())
