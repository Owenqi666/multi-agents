"""Standalone fake executable; all modeled protocol traffic is synthetic."""

import copy
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

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
    if delay := os.environ.get("FAKE_VERSION_DELAY"):
        capture("version-delay", seconds=float(delay))
        time.sleep(float(delay))
    if provider == "codex" and mode == "wrong-version":
        print("codex-cli 0.168.0")
    else:
        print("codex-cli 0.169.0" if provider == "codex" else "fake-claude 1.0")
    if mode == "version-hang":
        sys.stdout.flush()
        time.sleep(60)
    sys.exit(int(os.environ.get("FAKE_VERSION_EXIT", "0")))
if args == ["auth", "status"]:
    assert provider == "claude"
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    print(
        os.environ.get(
            "FAKE_AUTH_JSON",
            json.dumps({"loggedIn": True, "authMethod": "claude.ai"}, indent=2),
        )
    )
    sys.exit(int(os.environ.get("FAKE_AUTH_EXIT", "0")))

if mode == "early-exit":
    sys.exit(17)
if mode == "malformed":
    os.write(1, b"not json\n")
    time.sleep(60)
if mode == "hang-start":
    if ready := os.environ.get("FAKE_HANG_READY"):
        capture("hang-ready", pid=os.getpid())
        Path(ready).touch()
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
if "FAKE_RUNTIME_REPORT" in os.environ:
    report = json.loads(os.environ["FAKE_RUNTIME_REPORT"])
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

assert args == ["app-server", "--strict-config"]
config_audited = False
requirements_audited = False
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
    elif method == "config/read":
        assert params == {"includeLayers": True, "cwd": os.getcwd()}
        config_path = Path(os.environ["CODEX_HOME"]) / "config.toml"
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
        # Synthetic rust-v0.169.0 ConfigToml serialization: effective values
        # include explicit nulls/defaults; the source layer remains the TOML.
        nullable_keys = (
            "allow_symlinked_codex_home",
            "analytics",
            "approvals_reviewer",
            "apps",
            "apps_mcp_product_sku",
            "audio",
            "auto_review",
            "browser_use",
            "check_for_update_on_startup",
            "client_search_provider",
            "compact_prompt",
            "computer_use",
            "daybreak",
            "desktop",
            "developer_instructions",
            "disable_paste_burst",
            "experimental_compact_prompt_file",
            "experimental_realtime_start_instructions",
            "experimental_realtime_webrtc_call_base_url",
            "experimental_realtime_ws_backend_prompt",
            "experimental_realtime_ws_base_url",
            "experimental_realtime_ws_model",
            "experimental_realtime_ws_startup_context",
            "experimental_thread_store",
            "experimental_thread_store_endpoint",
            "experimental_use_unified_exec_tool",
            "feedback",
            "forced_chatgpt_workspace_id",
            "forced_login_method",
            "ghost_snapshot",
            "goals",
            "hooks",
            "instructions",
            "js_repl_node_module_dirs",
            "js_repl_node_path",
            "log_dir",
            "mcp_enterprise_managed_auth",
            "mcp_oauth_callback_port",
            "mcp_oauth_callback_url",
            "mcp_optional_startup_grace_ms",
            "model",
            "model_auto_compact_token_limit",
            "model_auto_compact_token_limit_scope",
            "model_catalog_json",
            "model_context_window",
            "model_instructions_file",
            "model_post_turn_compact_threshold_percent",
            "model_provider",
            "model_reasoning_effort",
            "model_reasoning_summary",
            "model_verbosity",
            "notice",
            "notify",
            "openai_base_url",
            "oss_provider",
            "otel",
            "personality",
            "plan_mode_reasoning_effort",
            "profile",
            "projects",
            "realtime",
            "responses_api_metadata",
            "review_model",
            "sandbox_mode",
            "sandbox_workspace_write",
            "service_tier",
            "show_raw_agent_reasoning",
            "sqlite_home",
            "suppress_unstable_features_warning",
            "thread_unload_delay_secs",
            "tool_output_token_limit",
            "tool_suggest",
            "tools",
            "tui",
            "windows",
        )
        result = {
            "config": {
                **dict.fromkeys(nullable_keys),
                "background_terminal_max_timeout": 300000,
                "file_opener": "vscode",
                "include_permissions_instructions": True,
                "chatgpt_base_url": "https://chatgpt.com/backend-api/",
                "project_root_markers": [".git"],
                "mcp_oauth_credentials_store": "auto",
                "hide_agent_reasoning": False,
                "profiles": {},
                "model_providers": {},
                "mcp_servers": {},
                "marketplaces": {},
                **copy.deepcopy(config),
            },
            "layers": [
                {
                    "name": {"type": "user", "file": str(config_path)},
                    "version": "synthetic",
                    "config": config,
                }
            ],
            "origins": {},
        }
        effective = result["config"]
        profile = effective["permissions"][effective["default_permissions"]]
        if mode == "config-missing":
            result.pop("config")
        elif mode == "config-malformed":
            result["config"] = []
        elif mode == "config-missing-mcp-servers":
            effective.pop("mcp_servers")
        elif mode == "config-missing-instructions":
            effective.pop("instructions")
        elif mode == "config-ignored-profile":
            effective["default_permissions"] = ":read-only"
        elif mode == "config-profile-extends":
            profile["extends"] = ":read-only"
        elif mode == "config-extra-filesystem":
            profile["filesystem"]["/"] = "read"
        elif mode == "config-extra-profile":
            effective["permissions"]["unwanted"] = {"filesystem": {"/": "read"}}
        elif mode == "config-network-enabled":
            profile["network"]["enabled"] = True
        elif mode == "config-bool-doc-limit":
            effective["project_doc_max_bytes"] = False
        elif mode == "config-null-permission-key":
            profile["unexpected_isolation_key"] = None
        elif mode == "config-null-top-level":
            effective["unexpected_isolation_key"] = None
        elif mode in ("config-system-layer", "config-mdm-layer"):
            source = (
                {"type": "system", "file": "/etc/codex/config.toml"}
                if mode == "config-system-layer"
                else {"type": "mdm", "domain": "synthetic", "key": "config"}
            )
            result["layers"].append(
                {
                    "name": source,
                    "version": "foreign",
                    "config": {"instructions": "synthetic-config-poison"},
                }
            )
        elif mode == "config-mcp-servers":
            effective["mcp_servers"] = {
                "unwanted": {
                    "command": "synthetic-server",
                    "env": {"ORDINARY_NAME": "synthetic-config-poison"},
                }
            }
        elif mode == "config-instructions":
            effective["instructions"] = "synthetic-config-poison"
        elif mode == "config-error":
            emit(
                {
                    "id": identifier,
                    "error": {
                        "code": -32603,
                        "message": "synthetic-config-poison",
                        "data": {"echo": "synthetic-config-poison"},
                    },
                }
            )
            continue
        elif mode in ("config-bool-id", "config-float-id", "config-wrong-id"):
            identifier = {
                "config-bool-id": True,
                "config-float-id": float(identifier),
                "config-wrong-id": identifier + 100,
            }[mode]
            result["untrusted_payload"] = "synthetic-config-poison"
        config_audited = True
    elif method == "configRequirements/read":
        assert config_audited
        assert params == {}
        requirements = None
        if mode == "requirements-empty":
            requirements = {}
        elif mode == "requirements-instructions":
            requirements = {
                "additionalDeveloperInstructions": "synthetic-managed-instructions"
            }
        elif mode == "requirements-unknown-null":
            requirements = {"unexpected_managed_field": None}
        elif mode == "requirements-login-methods":
            requirements = {"allowedLoginMethods": ["chatgpt"]}
        result = {"requirements": requirements}
        if mode == "requirements-missing":
            result.pop("requirements")
        requirements_audited = True
    elif method == "account/read":
        assert config_audited and requirements_audited
        assert params == {"refreshToken": False}
        result = {
            "account": None if mode == "account-logged-out" else {"type": "chatgpt"},
            "requiresOpenaiAuth": True,
        }
    elif method == "model/list":
        assert config_audited and requirements_audited
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
        assert config_audited and requirements_audited
        assert "sandbox" not in params and "sandboxPolicy" not in params
        assert params["approvalPolicy"] == "never"
        assert params["ephemeral"] is True
        assert params["cwd"] == os.getcwd()
        result = {
            "thread": {"id": thread_id},
            "model": "echo-not-evidence",
            "reasoningEffort": "ultra",
            "approvalPolicy": "never",
            "cwd": os.getcwd(),
            "instructionSources": [],
        }
        if mode == "thread-instructions":
            result["instructionSources"] = ["/synthetic/foreign/AGENTS.md"]
        elif mode == "mcp-before-turn":
            emit(
                {
                    "method": "mcpServer/startupStatus/updated",
                    "params": {"name": "unexpected", "status": "ready"},
                }
            )
    elif method == "turn/start":
        assert params["effort"] == "high"
        assert "sandbox" not in params and "sandboxPolicy" not in params
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
        if mode == "mcp-after-terminal":
            emit(
                {
                    "method": "mcpServer/startupStatus/updated",
                    "params": {"name": "unexpected", "status": "ready"},
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
