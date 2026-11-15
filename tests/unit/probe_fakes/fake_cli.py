"""Offline subprocess fixture; never delegates to an installed model CLI."""

import json
import os
import signal
import sys
import time
from pathlib import Path

SETTINGS = json.loads(os.environ.get("MULTI_AGENT_PROBE_FAKE", "{}"))
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
PROMPTS = {
    "turn": "Reply with exactly: OK",
    "bad-model": "Reply with exactly: OK",
    "structured": "Set verdict to the string ok.",
    "readonly": (
        "This is a permission check in a disposable directory. Step 1: read "
        "README.md in the current directory and note the marker it contains. "
        "Step 2: try once to create a file named probe-write-check.txt in the "
        "current directory, by any means available to you, even if you expect "
        "it to fail. Step 3: reply with two lines: the marker, then exactly "
        "what happened when you tried to create the file."
    ),
    "cancel": "Write the integers from 1 to 3000, one per line, with no other text.",
}
THREAD = "thread-from-server"
TURN = "turn-from-server"
BAD_MODEL = "multi-agent-probe-nonexistent-model"
PROVIDER = "codex" if Path(sys.argv[0]).name.endswith("codex") else "claude"
EXPECTED_STEP = SETTINGS.get(
    "expected_step",
    SETTINGS.get("expected_by_provider", {}).get(PROVIDER, f"{PROVIDER}-turn"),
)
EXPECTED_KIND = EXPECTED_STEP.removeprefix(f"{PROVIDER}-")


def emit(value):
    print(json.dumps(value), flush=True)


def receive():
    line = sys.stdin.readline()
    assert line, "unexpected stdin EOF"
    return json.loads(line)


def respond(request, result):
    emit({"id": request["id"], "result": result})


def wait_forever():
    while True:
        time.sleep(60)


def checked(**extra):
    emit({"fake": "checked", **extra})


def inspect_workdir(kind):
    cwd = Path.cwd()
    assert (cwd / "README.md").read_text()
    assert (cwd / "AGENTS.md").exists() == (kind == "readonly")
    assert (cwd / "CLAUDE.md").exists() == (kind == "readonly")
    assert not (cwd / ".git").exists()
    if SETTINGS.get("workdir_report"):
        Path(SETTINGS["workdir_report"]).write_text(str(cwd))
    if SETTINGS.get("contents_report"):
        Path(SETTINGS["contents_report"]).write_text(
            json.dumps(
                {
                    path.name: path.read_text()
                    for path in cwd.iterdir()
                    if path.is_file()
                }
            )
        )
    if SETTINGS.get("change_files"):
        (cwd / "README.md").write_text("changed by fake")
        (cwd / "AGENTS.md").unlink()
        (cwd / "added.txt").write_text("created by fake")


def payloads():
    if SETTINGS.get("raw_lines"):
        print("not-json", flush=True)
        print('[1, "two"]', flush=True)
        print('"scalar"', flush=True)
        print("x" * (1024 * 1024 + 37), flush=True)
        print("stderr one", file=sys.stderr, flush=True)
        print("stderr two", file=sys.stderr, flush=True)
    if SETTINGS.get("secrets"):
        emit(
            {
                "home": str(Path.home()),
                "workdir": str(Path.cwd()),
                "contact": "probe@example.test",
                "access_token": "sensitive-token-value",
                "userId": "sensitive-user-value",
                "inputTokens": 12,
                "outputTokens": 5,
                "threadId": THREAD,
                "session_id": "session-from-fake",
                "nested": {"organization-name": "sensitive-org-value"},
            }
        )


def items():
    for cursor, following in ((None, "items-page-two"), ("items-page-two", None)):
        request = receive()
        assert request["method"] == "thread/items/list"
        expected = {"threadId": THREAD}
        if cursor is not None:
            expected["cursor"] = cursor
        assert request["params"] == expected
        if SETTINGS.get("items_hang"):
            wait_forever()
        if cursor is None and SETTINGS.get("items_delay"):
            time.sleep(SETTINGS["items_delay"])
        if SETTINGS.get("items_error"):
            emit(
                {
                    "id": request["id"],
                    "error": {"code": -1, "message": "fake items error"},
                }
            )
            return
        respond(request, {"data": [], "nextCursor": following})


def late_activity():
    if SETTINGS.get("settle_request"):
        emit(
            {
                "id": "server-settle",
                "method": "item/fileChange/requestApproval",
                "params": {},
            }
        )
        assert receive() == {"id": "server-settle", "result": {"decision": "decline"}}
    if SETTINGS.get("late_notifications"):
        emit(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": THREAD,
                    "turnId": TURN,
                    "tokenUsage": {"total": {"inputTokens": 12}},
                },
            }
        )
        emit(
            {
                "method": "model/rerouted",
                "params": {
                    "threadId": THREAD,
                    "turnId": TURN,
                    "fromModel": "one",
                    "toModel": "two",
                    "reason": "synthetic",
                },
            }
        )


def sandbox(first):
    for index, (command, policy) in enumerate(
        (
            (["cat", "README.md"], {"type": "readOnly"}),
            (["touch", "probe-write-check.txt"], {"type": "readOnly"}),
            (
                ["touch", "probe-write-control.txt"],
                {"type": "workspaceWrite", "writableRoots": [str(Path.cwd())]},
            ),
        )
    ):
        request = first if index == 0 else receive()
        assert request["method"] == "command/exec"
        params = request["params"]
        assert params["command"] == command
        assert Path(params["cwd"]).resolve() == Path.cwd().resolve()
        actual_policy = dict(params["sandboxPolicy"])
        if "writableRoots" in actual_policy:
            actual_policy["writableRoots"] = [
                str(Path(path).resolve()) for path in actual_policy["writableRoots"]
            ]
        assert actual_policy == policy
        if index == SETTINGS.get("sandbox_error_at"):
            emit(
                {
                    "id": request["id"],
                    "error": {"code": -1, "message": "fake sandbox error"},
                }
            )
        else:
            respond(
                request, {"stdout": "fake command output", "stderr": "", "exitCode": 0}
            )
    checked()


def codex():
    assert sys.argv[1:] in (["--version"], ["app-server"])
    if sys.argv[1:] == ["--version"]:
        print("fake-codex 0.0", flush=True)
        return
    if SETTINGS.get("exit_at") == "before-request":
        return
    request = receive()
    assert request["method"] == "initialize"
    assert request["params"]["clientInfo"]["name"] == "multi-agent-probe"
    assert request["params"]["clientInfo"]["version"] == "0.1.0"
    assert not request["params"].get("capabilities", {}).get("experimentalApi")
    if SETTINGS.get("hang_at") == "initialize":
        wait_forever()
    if SETTINGS.get("exit_at") == "initialize":
        return
    respond(request, {"userAgent": "fake-server"})
    initialized = receive()
    assert initialized["method"] == "initialized"
    assert "id" not in initialized
    request = receive()
    if EXPECTED_KIND == "sandbox":
        inspect_workdir("sandbox")
        sandbox(request)
    elif EXPECTED_KIND == "env":
        assert request["method"] == "account/read"
        assert request["params"]["refreshToken"] is False
        if SETTINGS.get("env_error"):
            emit(
                {
                    "id": request["id"],
                    "error": {"code": -1, "message": "fake account error"},
                }
            )
        else:
            respond(request, {"account": {"email": "probe@example.test"}})
        for hidden in (False, True):
            for cursor, following in ((None, "page-two"), ("page-two", None)):
                request = receive()
                assert request["method"] == "model/list"
                params = request.get("params", {})
                assert params.get("includeHidden", False) is hidden
                assert params.get("cursor") == cursor
                respond(request, {"data": [], "nextCursor": following})
        request = receive()
        assert request["method"] == "account/rateLimits/read"
        respond(request, {"rateLimits": {}})
        checked()
    else:
        assert request["method"] == "thread/start"
        thread_params = request["params"]
        assert Path(thread_params["cwd"]).resolve() == Path.cwd().resolve()
        assert thread_params["sandbox"] == "read-only"
        assert thread_params["approvalPolicy"] == "never"
        assert thread_params["ephemeral"] is True
        if SETTINGS.get("error_at") == "thread/start":
            emit(
                {
                    "id": request["id"],
                    "error": {"code": -1, "message": "fake thread error"},
                }
            )
            assert sys.stdin.read() == ""
            checked()
            return
        respond(
            request,
            {
                "thread": {"id": THREAD},
                "model": "reported-model",
                "reasoningEffort": "high",
            },
        )
        request = receive()
        assert request["method"] == "turn/start"
        params = request["params"]
        assert params["threadId"] == THREAD
        assert params["sandboxPolicy"]["type"] == "readOnly"
        assert params["approvalPolicy"] == "never"
        assert len(params["input"]) == 1
        assert params["input"][0]["type"] == "text"
        prompt = params["input"][0]["text"]
        kind = EXPECTED_KIND
        assert prompt == PROMPTS[kind]
        expected_model = BAD_MODEL if kind == "bad-model" else SETTINGS["codex_model"]
        assert params["model"] == thread_params["model"] == expected_model
        assert params["effort"] == SETTINGS["codex_effort"]
        assert ("outputSchema" in params) == (kind == "structured")
        if kind == "structured":
            assert params["outputSchema"] == SCHEMA
        inspect_workdir(kind)
        if SETTINGS.get("error_at") == "turn/start":
            emit(
                {
                    "id": request["id"],
                    "error": {"code": -1, "message": "fake turn error"},
                }
            )
            assert sys.stdin.read() == ""
            checked()
            return
        if SETTINGS.get("complete_before_response"):
            emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": THREAD,
                        "turn": {"id": TURN, "status": "completed"},
                    },
                }
            )
            respond(request, {"turn": {"id": TURN}})
            items()
            late_activity()
            checked()
            assert sys.stdin.read() == ""
            return
        respond(request, {"turn": {"id": TURN}})
        if SETTINGS.get("exit_at") == "turn/start":
            return
        if SETTINGS.get("foreign_completion"):
            emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "other-thread",
                        "turn": {"id": "other-turn", "status": "completed"},
                    },
                }
            )
            emit({"id": "after-foreign", "method": "fake/after-foreign", "params": {}})
            answer = receive()
            assert answer["id"] == "after-foreign" and answer["error"]["code"] == -32601
        if SETTINGS.get("approvals"):
            for index, method in enumerate(
                (
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                    "fake/unknown",
                )
            ):
                request_id = f"server-{index}"
                emit({"id": request_id, "method": method, "params": {}})
                answer = receive()
                assert answer["id"] == request_id
                if index < 2:
                    assert answer["result"]["decision"] == "decline"
                else:
                    assert answer["error"]["code"] == -32601
                    assert isinstance(answer["error"]["message"], str)
        payloads()
        if kind == "cancel" and SETTINGS.get("cancel_mode") != "early":
            request = receive()
            assert request["method"] == "turn/interrupt"
            assert request["params"] == {"threadId": THREAD, "turnId": TURN}
            respond(request, {})
            if SETTINGS.get("cancel_mode") == "exit":
                checked()
                return
            if SETTINGS.get("cancel_mode") == "hang":
                wait_forever()
        emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": THREAD,
                    "turn": {"id": TURN, "status": "completed"},
                },
            }
        )
        items()
        late_activity()
        checked()
    assert sys.stdin.read() == "", "unexpected request after conversation ended"
    if SETTINGS.get("ignore_eof"):
        wait_forever()


def claude():
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("fake-claude 0.0", flush=True)
        return
    if argv == ["auth", "status"]:
        if SETTINGS.get("multiline_auth"):
            print(
                json.dumps(
                    {
                        "email": "probe@example.test",
                        "orgId": "private-org-id",
                        "orgName": 'private org "quoted" \\ path',
                        "subscriptionType": "private-subscription",
                        "planTier": "private-tier",
                        "loggedIn": False,
                        "authMethod": "oauth",
                        "apiKeySource": "none",
                    },
                    indent=2,
                ),
                flush=True,
            )
            print(
                '  "orgName": "private first", "planTier": "private second", "authMethod": "oauth"',
                flush=True,
            )
        else:
            emit({"loggedIn": False, "email": "probe@example.test"})
        checked()
        sys.exit(1)
    assert argv[0] == "-p"
    prompt = argv[1]
    if "special_prompt" in SETTINGS:
        assert prompt == SETTINGS["special_prompt"]
    else:
        assert prompt == PROMPTS[EXPECTED_KIND]
    kind = EXPECTED_KIND
    expected_model = BAD_MODEL if kind == "bad-model" else SETTINGS["claude_model"]
    expected = [
        "-p",
        prompt,
        "--model",
        expected_model,
        "--effort",
        SETTINGS["claude_effort"],
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    expected += SETTINGS.get(
        "session_args",
        [
            "--safe-mode",
            "--tools",
            "Read,Grep,Glob",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
        ],
    )
    if kind == "structured":
        assert argv[-2] == "--json-schema"
        assert json.loads(argv[-1]) == SCHEMA
        argv = argv[:-2]
    assert argv == expected
    assert sys.stdin.read() == ""
    inspect_workdir(kind)
    if SETTINGS.get("hang_at") == "claude":
        emit({"fake": "ready"})
        wait_forever()
    if kind == "cancel" and SETTINGS.get("cancel_mode") == "early":
        return
    emit({"fake": "first-line"})
    payloads()
    if kind == "cancel":
        wait_forever()
    checked()
    if SETTINGS.get("hold_output"):
        child_pid = os.fork()
        if child_pid == 0:
            time.sleep(30)
            os._exit(0)
        Path(SETTINGS["holder_report"]).write_text(
            json.dumps({"pid": child_pid, "parent_exits_at": time.monotonic()})
        )
        sys.stdout.write("stdout held tail without newline")
        sys.stdout.flush()
        sys.stderr.write("stderr held tail without newline")
        sys.stderr.flush()
        return
    if SETTINGS.get("unterminated_tail"):
        sys.stdout.write("stdout tail without newline")
        sys.stdout.flush()
        sys.stderr.write("stderr tail without newline")
        sys.stderr.flush()


def received_interrupt(signum, frame):
    checked(signal="SIGINT")
    sys.exit(0)


# Reproduce slow interpreter startup before signal handlers, PID evidence, or
# readiness exist; tests must synchronize on readiness instead of spawn time.
startup_delay = SETTINGS.get("startup_delay", 0)
if startup_delay:
    time.sleep(startup_delay)

if SETTINGS.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if SETTINGS.get("ignore_int"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
else:
    signal.signal(signal.SIGINT, received_interrupt)
if SETTINGS.get("pid_file"):
    Path(SETTINGS["pid_file"]).write_text(str(os.getpid()))

for env_name, expected_value in SETTINGS.get("check_environment", {}).items():
    if PROVIDER == "claude" and env_name in {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }:
        assert env_name not in os.environ
    else:
        assert os.environ.get(env_name) == expected_value

if Path(sys.argv[0]).name.endswith("codex"):
    codex()
else:
    claude()
