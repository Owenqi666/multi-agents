"""Choose a synthetic runtime response from the real frozen role inputs."""

import json
import os
import runpy
import sys
import time
import tomllib
from pathlib import Path

from offline_fixture import (
    AMOUNT,
    BANNER,
    CLAUDE_MODELS,
    CODEX_MODELS,
    DRAFT,
    EVIDENCE_PLANS,
    PLANNING_QUESTION,
    PLANNING_QUESTION_REQUIREMENTS,
    READY,
    STANDALONE_PLAN,
    STATUS,
    TASK_EVIDENCE,
    WELCOME,
    project_document,
)

from multi_agent.core.events import Message
from multi_agent.core.task_execution import task_execution_hash
from multi_agent.store.task_test_store import TaskTestStore

_FAKES = Path(__file__).resolve().parents[1] / "runtime_fakes"
_INPUT = "Frozen implementation input (canonical JSON):\n"


def _planning(frozen):
    if (path := Path("task-card.json")).is_file():
        card = json.loads(path.read_text())
        return {
            "task_hash": task_execution_hash(card),
            "decision": "ready",
            "reason": "The offline card covers the selected stage scope.",
            "blocking_issues": [],
        }

    def card(identifier, path):
        return {
            "schema_version": 1,
            "id": identifier,
            "goal": f"Deliver {path}",
            "baseline_commit": frozen["frozen_rules"]["baseline_commit"],
            "write_scope": [path],
            "depends_on": [item["task_id"] for item in frozen["dependencies"]],
            "interface_contracts": [],
            "deliverables": [f"The updated {path}"],
            "acceptance_criteria": ["The named build and test suite passes"],
            "test_refs": ["build", "test"],
            "open_questions": [],
            "proposed_tests": [],
        }

    for identifier, path, requirement in EVIDENCE_PLANS.values():
        if frozen["requirements"] == requirement:
            return card(identifier, path)
    if frozen["requirements"] == PLANNING_QUESTION_REQUIREMENTS:
        question = {"question": PLANNING_QUESTION, "blocks_dispatch": True}
        resolution = next(
            (
                decision["resolution"]
                for decision in frozen["decisions"]
                if decision["question"] == PLANNING_QUESTION
                and decision["source"] == "user_declared"
            ),
            None,
        )
        if resolution is not None:
            question["resolution"] = resolution
        return {**card("T-0812", AMOUNT), "open_questions": [question]}
    requirement, handoff = frozen["requirements"].split("\n", 1)
    selected = json.loads(handoff)["selected_stage"]["stage_id"]
    if requirement.startswith("Split only selected_stage") and selected == "summary":
        return {"cards": [card("T-0800", STATUS), card("T-0801", BANNER)]}
    if requirement.startswith("Implement only selected_stage") and selected == "amount":
        return card("T-0799", AMOUNT)
    raise ValueError(f"unexpected offline planning stage: {selected}")


def _review(root, scenario, provider, frozen):
    if frozen.get("kind") == "executed_integration_review":
        if scenario == "dual-partial" and provider == "claude":
            first = root / "runtime" / f"{frozen['integration_id']}-first-claude-seat"
            try:
                first.touch(exist_ok=False)
            except FileExistsError:
                pass
            else:
                return {}, "model-error"
        if (
            scenario == "rework"
            and Path(f"source/candidate/{STATUS}").read_text() == DRAFT
        ):
            return {
                "summary": "The summary still exposes the draft status.",
                "findings": [
                    {
                        "id": "status-blocker",
                        "severity": "P1",
                        "category": "correctness",
                        "side": "candidate",
                        "path": STATUS,
                        "line_start": 1,
                        "line_end": 1,
                        "quote": DRAFT.rstrip("\n"),
                        "title": "Status is still draft",
                        "impact": "The summary does not display the ready state.",
                        "rationale": "The candidate still returns the draft value.",
                    }
                ],
            }, "success"
    return {"summary": "The offline candidate is ready.", "findings": []}, "success"


def _implement(root, scenario, prompt):
    value = json.loads(prompt.split(_INPUT, 1)[1])
    integration_rework = "original_member" in value
    parent = value["original_member"] if integration_rework else value
    (path,) = parent["task_input"]["card"]["write_scope"]
    planned = {
        AMOUNT: "AMOUNT = 1\n",
        STATUS: DRAFT if scenario == "rework" and not integration_rework else READY,
        BANNER: WELCOME,
    }
    Path(path).write_text(planned[path], encoding="utf-8")
    waiting = scenario == "cancel" and path in (STATUS, BANNER)
    if waiting:
        marker = root / "runtime" / f"implementation-{os.getpid()}"
        marker.write_text(path, encoding="utf-8")
    return {
        "task_hash": value["task_hash"],
        "outcome": "completed",
        "summary": f"Delivered {path} in the offline fixture.",
        "required_paths": [],
    }, "wait" if waiting else "success"


def _response(root, scenario, provider, prompt):
    if prompt.startswith("Implementation template:"):
        return _implement(root, scenario, prompt)
    if (path := Path("planning-input.json")).is_file():
        frozen = json.loads(path.read_text())
        if (
            scenario == STANDALONE_PLAN
            and provider == "claude"
            and frozen["requirements"] == EVIDENCE_PLANS["codex"][2]
        ):
            context = json.loads(Path(".planning-context.json").read_text())
            if context["stage"] == "draft" and context["attempt"] == 1:
                return {}, "model-error"
        return _planning(frozen), "success"
    if (path := Path("consultation-input.json")).is_file():
        frozen = json.loads(path.read_text())
        return {
            "draft_goal": frozen["original_goal"],
            "background": [
                {"claim": "The repository has a workflow", "path": "AGENTS.md"}
            ],
            "assumptions": [],
            "questions": [],
            "remaining_disagreements": [],
        }, "success"
    if Path("project-goal.json").is_file():
        return project_document(), "success"
    if (path := Path("review-input.json")).is_file():
        return _review(root, scenario, provider, json.loads(path.read_text()))
    raise ValueError("unexpected offline model role")


def _emit_codex_usage(emit, thread_id, turn_id):
    def breakdown(input_tokens, output_tokens):
        return {
            "inputTokens": input_tokens,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": output_tokens,
            "reasoningOutputTokens": 0,
            "totalTokens": input_tokens + output_tokens,
        }

    first = breakdown(12, 5)
    for total, last in ((first, first), (breakdown(31, 13), breakdown(19, 8))):
        emit(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {
                        "total": total,
                        "last": last,
                        "modelContextWindow": 131072,
                    },
                },
            }
        )


def _transport_source():
    # The same audited transport is used by the existing adapter tests. The
    # offline fixture admits low and supplies the complete synthetic catalog.
    source = (_FAKES / "fake_cli.py").read_text()
    allow_low = {
        'assert args[args.index("--effort") + 1] == "high"': (
            'assert args[args.index("--effort") + 1] in ("low", "high")'
        ),
        'assert params["effort"] == "high"': 'assert params["effort"] in ("low", "high")',
    }
    for old, new in allow_low.items():
        if source.count(old) != 1:
            raise RuntimeError(f"unexpected fake transport effort check: {old}")
        source = source.replace(old, new)
    first = source.index('        name = "model-2" if "cursor" in params')
    last = source.index('    elif method == "thread/start":', first)
    catalog = [
        {
            "id": model,
            "hidden": False,
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort} for effort in ("low", "high")
            ],
        }
        for model in (*CODEX_MODELS, *CLAUDE_MODELS)
    ]
    source = (
        source[:first]
        + f"        result = {{'data': {catalog!r}, 'nextCursor': None}}\n"
        + source[last:]
    )
    turn = '    elif method == "turn/start":\n'
    source = source.replace(
        turn,
        turn
        + '        report, mode = offline_response(params["input"][0]["text"], params["model"])\n',
    )
    terminal = '        if mode == "model-error":\n'
    return source.replace(
        terminal,
        '        if offline_evidence and implementation and mode == "success":\n'
        "            offline_usage(emit, thread_id, turn_id)\n" + terminal,
    )


def install_transports(root):
    source = _transport_source()
    for provider in ("codex", "claude"):
        (root / "runtime" / f"{provider}-transport.py").write_text(
            source, encoding="utf-8"
        )


def _wait_for_cancel_turn(root):
    data = root / "data"
    state_path = data / "web-environment" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state["repository"] != str(root / "repository"):
        raise ValueError("offline environment belongs to another workspace")
    running = [job for job in state["jobs"].values() if job["state"] == "running"]
    if not any(
        job["action"] == "environment.refresh" and job["args"] == {"provider": "codex"}
        for job in running
    ):
        return
    job = next((job for job in running if job["action"] == "task.run"), None)
    if job is None:
        return
    identifier = job["job_id"]
    planning_id = job["args"]["planning_run_id"]
    worktree = str(data / f"web-run-{identifier}" / "workspace")
    deadline = time.monotonic() + 20
    with TaskTestStore.open_readonly(data / "reviews.sqlite3") as store:
        plan = store.load_planning(planning_id)
        if (
            plan.card is None
            or plan.card.write_scope not in ((STATUS,), (BANNER,))
            or plan.projection is None
            or plan.projection.executor.provider != "claude-code"
            or job["args"].get("implementation", {}).get("provider", "claude-code")
            != "claude-code"
        ):
            return
        while time.monotonic() < deadline:
            current = json.loads(state_path.read_text(encoding="utf-8"))["jobs"]
            if current[identifier]["state"] != "running":
                return
            plan = store.load_planning(planning_id)
            if plan.bound_task_run_id is not None:
                attempt = store.load_task(plan.bound_task_run_id).current_attempt
                if (
                    attempt is not None
                    and attempt.request.worktree == worktree
                    and attempt.runtime.session_id is not None
                    and any(
                        isinstance(event, Message)
                        and event.raw_type == "assistant"
                        and event.text == "waiting"
                        for event in attempt.events
                    )
                ):
                    return
            time.sleep(0.05)
    raise TimeoutError(
        "cancel fixture did not observe the active Claude implementation"
    )


def main():
    executable = Path(sys.argv[0]).resolve()
    root = executable.parents[1]
    provider = executable.name
    scenario = (root / "runtime" / "scenario").read_text()
    for name in ("HOME", "CODEX_HOME"):
        if not Path(os.environ[name]).resolve().is_relative_to(root):
            raise RuntimeError(
                f"offline runtime {name} escaped the temporary environment"
            )

    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        if (
            scenario == "cancel"
            and provider == "codex"
            and Path.cwd().name.startswith("multi-agent-environment-")
        ):
            _wait_for_cancel_turn(root)
        print("codex-cli 0.169.0" if provider == "codex" else "2.1.325 (Claude Code)")
        return

    if provider == "codex" and arguments == ["app-server", "--strict-config"]:
        config = tomllib.loads(
            (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text()
        )
        profile = config.get("default_permissions")
        if profile == "multi-agent-independent-tests":
            runpy.run_path(str(_FAKES / "test_cli.py"), run_name="__main__")
            return
        if profile == "multi-agent-owned-implementation":
            os.environ["FAKE_IMPLEMENTATION"] = "1"
    elif provider == "claude" and arguments[:1] == ["-p"]:
        if arguments[arguments.index("--model") + 1] not in CLAUDE_MODELS:
            raise ValueError("unexpected offline Claude model")
        prompt = arguments[1]
        if prompt.startswith("Implementation template:"):
            os.environ["FAKE_IMPLEMENTATION"] = "1"
        report, mode = _response(root, scenario, provider, prompt)
        os.environ.update(
            FAKE_RUNTIME_MODE=mode, FAKE_RUNTIME_REPORT=json.dumps(report)
        )

    def codex_response(prompt, model):
        if (
            scenario == TASK_EVIDENCE
            and prompt.startswith("Implementation template:")
            and model == "gpt-6.2-sol"
        ):
            return {}, "model-error"
        return _response(root, scenario, provider, prompt)

    runpy.run_path(
        str(root / "runtime" / f"{provider}-transport.py"),
        init_globals={
            "offline_response": codex_response,
            "offline_evidence": scenario == TASK_EVIDENCE,
            "offline_usage": _emit_codex_usage,
        },
        run_name="__main__",
    )
