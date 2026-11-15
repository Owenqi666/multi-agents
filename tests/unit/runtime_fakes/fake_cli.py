"""Standalone fake executable; all modeled protocol traffic is synthetic."""

import json
import os
import signal
import subprocess
import sys
import time

args = sys.argv[1:]
mode = os.environ.get("FAKE_RUNTIME_MODE", "success")
provider = "claude" if "claude" in os.path.basename(sys.argv[0]) else "codex"


def capture(kind, **values):
    if path := os.environ.get("FAKE_RUNTIME_CAPTURE"):
        with open(path, "a") as stream:
            stream.write(json.dumps({"kind": kind, **values}) + "\n")


def emit(message):
    os.write(1, (json.dumps(message) + "\n").encode())


capture(
    "spawn",
    argv=args,
    pid=os.getpid(),
    cwd=os.getcwd(),
    api_key_present="ANTHROPIC_API_KEY" in os.environ,
    auth_token_present="ANTHROPIC_AUTH_TOKEN" in os.environ,
)

if args == ["--version"]:
    print("fake-" + provider + " 1.0")
    sys.exit(0)
if args == ["auth", "status"]:
    assert provider == "claude"
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}, indent=2))
    sys.exit(0)

if mode == "early-exit":
    sys.exit(17)
if mode == "malformed":
    os.write(1, b"not json\n")
    time.sleep(60)
if mode == "hang-start":
    time.sleep(60)
if mode.startswith("grandchild-"):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ]
    )
    capture("grandchild", pid=child.pid)
    if mode == "grandchild-exit":
        sys.exit(23)
    if mode != "grandchild-success":
        time.sleep(60)

report = {"summary": "ok", "findings": []}
if provider == "claude":
    assert args[0] == "-p"
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--safe-mode" in args and "--no-session-persistence" in args
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ

    def cancelled(*_):
        if mode == "ignore-cancel":
            return
        emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "terminal_reason": "aborted_streaming",
            }
        )
        sys.exit(0)

    signal.signal(signal.SIGINT, cancelled)
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-" + str(os.getpid()),
            "model": "echo-not-evidence",
        }
    )
    emit({"type": "unknown-event", "custom": 42})
    os.write(2, b"fake stderr\n")
    if mode in ("wait", "ignore-cancel"):
        emit(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "waiting"}]},
            }
        )
        time.sleep(60)
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "structured_output": report,
        "result": json.dumps(report),
        "modelUsage": {"actual-claude": {"canonicalModel": "actual-claude"}},
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "total_cost_usd": 0.04,
    }
    if mode == "rate-error":
        emit({"type": "rate_limit_event", "rate_limit_info": {"resetsAt": 12345}})
        result.update(
            is_error=True,
            terminal_reason="api_error",
            api_error_status=429,
            result="limited",
        )
    if mode == "model-error":
        result.update(
            is_error=True,
            terminal_reason="api_error",
            api_error_status=404,
            result="unknown model",
        )
    if mode == "bad-result":
        result.pop("structured_output")
        result["result"] = "invalid JSON"
    emit(result)
    if mode == "duplicate":
        emit(result)
    if mode == "hang-after-result":
        time.sleep(60)
    os._exit(0)

assert args == ["app-server"]
thread_id = "thread-" + str(os.getpid())
turn_id = "turn-1"
for line in sys.stdin:
    message = json.loads(line)
    capture("request", message=message)
    method = message.get("method")
    if method is None:
        if message.get("id") == "approval":
            assert message["result"] == {"decision": "decline"}
        continue
    params = message.get("params", {})
    identifier = message.get("id")
    result = {}
    if method == "initialize":
        assert params["clientInfo"]["name"] == "multi-agent-review"
        result = {"userAgent": "fake"}
    elif method == "initialized":
        continue
    elif method == "account/read":
        assert params == {"refreshToken": False}
        result = {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}
    elif method == "model/list":
        assert params["includeHidden"] is False
        name = "model-2" if "cursor" in params else "model-1"
        result = {
            "data": [
                {
                    "id": name,
                    "hidden": False,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "high"},
                        {"reasoningEffort": "low"},
                    ],
                }
            ],
            "nextCursor": None if "cursor" in params else "page-2",
        }
    elif method == "thread/start":
        assert params["sandbox"] == "read-only"
        assert params["approvalPolicy"] == "never"
        assert params["ephemeral"] is True
        assert params["cwd"] == os.getcwd()
        result = {
            "thread": {"id": thread_id},
            "model": "echo-not-evidence",
            "reasoningEffort": "ultra",
            "instructionSources": [],
        }
    elif method == "turn/start":
        assert params["effort"] == "high"
        assert params["sandboxPolicy"] == {"type": "readOnly"}
        assert params["approvalPolicy"] == "never"
        assert params["threadId"] == thread_id
        emit(
            {
                "id": identifier,
                "result": {"turn": {"id": turn_id, "status": "inProgress"}},
            }
        )
        emit(
            {
                "id": "approval",
                "method": "item/commandExecution/requestApproval",
                "params": {},
            }
        )
        emit({"method": "future/unknown", "params": {"value": 42}})
        emit(
            {
                "method": "model/rerouted",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "fromModel": "first",
                    "toModel": "second",
                    "reason": "highRiskCyberActivity",
                },
            }
        )
        os.write(2, b"fake stderr\n")
        if mode in ("wait", "ignore-cancel"):
            emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "type": "agentMessage",
                            "text": "waiting",
                            "phase": "commentary",
                        },
                    },
                }
            )
            continue
        if mode == "late-contradictory":
            time.sleep(float(os.environ.get("FAKE_TERMINAL_DELAY", "0.12")))
        text = "invalid JSON" if mode == "bad-result" else json.dumps(report)
        turn = {
            "id": turn_id,
            "status": "completed",
            "items": [{"type": "agentMessage", "phase": "final_answer", "text": text}],
        }
        if mode == "model-error":
            turn.update(
                status="failed",
                items=[],
                error={"message": "not supported", "codexErrorInfo": "modelNotFound"},
            )
        emit(
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": turn},
            }
        )
        if mode in ("duplicate", "contradictory", "late-contradictory"):
            if mode == "late-contradictory":
                time.sleep(float(os.environ.get("FAKE_SECOND_DELAY", "0.06")))
            if mode in ("contradictory", "late-contradictory"):
                turn.update(
                    status="failed",
                    items=[],
                    error={"message": "late failure", "codexErrorInfo": "other"},
                )
            emit(
                {
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": turn},
                }
            )
        if mode == "fast-exit":
            os._exit(0)
        continue
    elif method == "turn/interrupt":
        assert params == {"threadId": thread_id, "turnId": turn_id}
        if mode == "ignore-cancel":
            continue
        emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "interrupted"},
                },
            }
        )
    else:
        raise AssertionError(method)
    emit({"id": identifier, "result": result})
