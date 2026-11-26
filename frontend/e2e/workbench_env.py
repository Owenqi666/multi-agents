"""Offline workbench for the inbox, job recovery, finding discussions, setup check and delivery.

Built on the offline bridge: real Git, SQLite and HTTP, substitute model CLIs. The
substitutes add what these pages need to show:

- a plan whose first and last stage can both be claimed at once; the last one fails in planning;
- an advisor that fails for a goal marked FAIL-CONSULT;
- an integration review where both seats report the same finding id, and where the Claude seat
  of the very first integration review waits until the service is stopped;
- an answer to a follow-up question, agreeing cross-confirmations, and a card for a correction
  planned from a finding;
- branches whose commits have no rules file and an invalid one.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/unit/web_support"))

import fake_runtime as runtime
import offline_env
from offline_fixture import STATUS, project_document

SCENARIO = "workbench"
EMPTY = "workbench-empty"
FAILING_GOAL = "FAIL-CONSULT"
FAILING_STAGE = "report"
CORRECTION = "修复审核意见"
FOLLOWUP = "Answer the user's question about the quoted review finding"
ANSWER = "The frozen line still returns the draft status, so any render of the summary shows it."

create_fixture = offline_env.create_fixture
serve = offline_env._serve
planning = runtime._planning
response = runtime._response


def _git(repository, env, *arguments):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Offline Fixture",
            "-c",
            "user.email=offline@example.invalid",
            *arguments,
        ],
        cwd=repository,
        env=env,
        check=True,
    )


def fixture(root, scenario):
    # The rework fixture leaves a draft status for the first integration review to find.
    env = create_fixture(root, "rework")
    repository = root / "repository"
    if scenario == EMPTY:
        shutil.rmtree(repository)
        repository.mkdir(mode=0o700)
        _git(repository, env, "init", "--quiet", "--initial-branch=main", "--template=")
    else:
        rules = repository / ".multi-agent" / "rules.yaml"
        _git(repository, env, "checkout", "--quiet", "-b", "legacy/no-rules")
        _git(repository, env, "rm", "--quiet", str(rules.relative_to(repository)))
        _git(repository, env, "commit", "--quiet", "-m", "Before the rules file")
        _git(repository, env, "checkout", "--quiet", "main")
        _git(repository, env, "checkout", "--quiet", "-b", "setup/invalid-rules")
        rules.write_text("version: 1\n", encoding="utf-8")
        _git(repository, env, "commit", "--quiet", "-am", "Incomplete rules file")
        _git(repository, env, "checkout", "--quiet", "main")
    for provider in ("codex", "claude"):
        launcher = root / "bin" / provider
        launcher.write_text(
            f"#!{sys.executable}\nimport sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from workbench_env import fake_main\nfake_main()\n"
        )
    return env


def workbench_document():
    document = project_document()
    for stage in document["stages"]:
        if stage["stage_id"] == FAILING_STAGE:
            stage["depends_on"] = []
    return document


def workbench_planning(frozen):
    if not Path("task-card.json").is_file() and frozen["requirements"].startswith(
        CORRECTION
    ):
        return {
            "schema_version": 1,
            "id": "T-0850",
            "goal": f"Correct {STATUS}",
            "baseline_commit": frozen["frozen_rules"]["baseline_commit"],
            "write_scope": [STATUS],
            "depends_on": [item["task_id"] for item in frozen["dependencies"]],
            "interface_contracts": [],
            "deliverables": [f"The corrected {STATUS}"],
            "acceptance_criteria": ["The named build and test suite passes"],
            "test_refs": ["build", "test"],
            "open_questions": [],
            "proposed_tests": [],
        }
    return planning(frozen)


def workbench_response(root, scenario, provider, prompt):
    if prompt.startswith(FOLLOWUP):
        return {"answer": ANSWER}, "success"
    if prompt.startswith("Implementation template:"):
        return response(root, scenario, provider, prompt)
    if (path := Path("planning-input.json")).is_file():
        frozen = json.loads(path.read_text())
        _, _, handoff = frozen["requirements"].partition("\n")
        if (
            not Path("task-card.json").is_file()
            and handoff.startswith("{")
            and json.loads(handoff)["selected_stage"]["stage_id"] == FAILING_STAGE
        ):
            return {}, "model-error"
        return workbench_planning(frozen), "success"
    if (path := Path("consultation-input.json")).is_file():
        if FAILING_GOAL in json.loads(path.read_text())["original_goal"]:
            return {}, "model-error"
        return response(root, scenario, provider, prompt)
    if Path("project-goal.json").is_file():
        return workbench_document(), "success"
    if (targets := Path("confirmation-targets.json")).is_file():
        return {
            "summary": "Confirmed the cited frozen line.",
            "confirmations": [
                {
                    "target_seat": item["target_seat"],
                    "finding_id": item["finding_id"],
                    "decision": "agree",
                    "reason": f"{provider} sees the same value on the frozen line.",
                }
                for item in json.loads(targets.read_text())
            ],
        }, "success"
    if (path := Path("review-input.json")).is_file():
        frozen = json.loads(path.read_text())
        if frozen.get("kind") == "executed_integration_review" and provider == "claude":
            try:
                (root / "runtime" / "first-claude-integration-review").touch(
                    exist_ok=False
                )
            except FileExistsError:
                pass
            else:
                return {
                    "summary": "Still reading the candidate.",
                    "findings": [],
                }, "wait"
    return response(root, scenario, provider, prompt)


def fake_main():
    runtime._planning = workbench_planning
    runtime._response = workbench_response
    runtime.main()


def _announce_then_serve(root, env, port, frontend_dist):
    print(json.dumps({"repository": str(root / "repository")}), flush=True)
    serve(root, env, port, frontend_dist)


if __name__ == "__main__":
    offline_env.SCENARIOS += (SCENARIO, EMPTY)
    offline_env.create_fixture = fixture
    offline_env._serve = _announce_then_serve
    offline_env.main()
