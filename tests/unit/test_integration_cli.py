"""Integrate verified managed deliveries through the normal CLI and real Git."""

import json
import subprocess
import sys
import time
from copy import copy

import test_task_workflow_cli as helpers
from test_task_batch import plan

from multi_agent.core.task_execution import task_execution_hash

workflow = helpers.workflow


def implement(workflow, task_id, scope, writes, *, baseline=None, depends_on=None):
    inputs = copy(workflow)
    if baseline is not None:
        inputs.baseline = baseline
    planned = plan(inputs, task_id, scope, depends_on=depends_on)
    program = helpers.executable(
        workflow,
        "claude",
        "implementation",
        task_hash=task_execution_hash(planned["card"]),
        writes={path: value.hex() for path, value in writes.items()},
    )
    task = helpers.successful(
        *helpers.run_arguments(
            workflow, planned["planning_run_id"], program, directory=f"run-{task_id}"
        )
    )
    assert task["verification"] == "verified"
    return task, program


def create_args(workflow, *tasks):
    ref = helpers.git(workflow.repository, "symbolic-ref", "HEAD")
    return (
        "integration",
        "create",
        *(task["task_run_id"] for task in tasks),
        "--database",
        workflow.database,
        "--repository",
        workflow.repository,
        "--target",
        ref,
    )


def show(workflow, integration):
    return helpers.successful(
        "integration",
        "show",
        integration["integration_id"],
        "--database",
        workflow.database,
    )


def test_integration_writer_waits_for_an_existing_terminal_task_read(
    workflow, tmp_path
):
    task, _ = implement(
        workflow, "T-0288", "frontend/status.js", {"frontend/status.js": b"reader\n"}
    )
    held = tmp_path / "read-lock-held"
    release = tmp_path / "read-lock-release"
    blocked = tmp_path / "write-lock-contended"
    reader = """
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from multi_agent.cli import task_cli
from multi_agent.cli.main import main

read_lock = task_cli._existing_lease

@contextmanager
def wait_at_lock(*args):
    with read_lock(*args) as lease:
        Path(sys.argv[1]).touch()
        until = time.monotonic() + 10
        while not Path(sys.argv[2]).exists():
            if time.monotonic() >= until:
                raise RuntimeError('test did not signal read completion')
            time.sleep(0.005)
        yield lease

task_cli._existing_lease = wait_at_lock
raise SystemExit(main(sys.argv[3:]))
"""
    writer = """
import fcntl
import sys
from pathlib import Path
from multi_agent.cli.main import main

flock = fcntl.flock

def signal_contention(fd, operation):
    try:
        flock(fd, operation)
    except BlockingIOError:
        Path(sys.argv[1]).touch()
        raise

fcntl.flock = signal_contention
raise SystemExit(main(sys.argv[2:]))
"""
    children = []

    def spawn(script, *arguments):
        child = subprocess.Popen(
            [sys.executable, "-c", script, *map(str, arguments)],
            cwd=helpers.REPOSITORY,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        return child

    def await_signal(marker, child):
        until = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < until:
            time.sleep(0.005)
        assert marker.exists(), marker.name

    try:
        reading = spawn(
            reader,
            held,
            release,
            "task",
            "show",
            "--task",
            task["task_run_id"],
            "--database",
            workflow.database,
        )
        await_signal(held, reading)
        writing = spawn(writer, blocked, *create_args(workflow, task))
        await_signal(blocked, writing)
        release.touch()
        for child in (reading, writing):
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, (stdout, stderr)
            assert json.loads(stdout)["verification"] == "verified"
    finally:
        release.touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_integration_pins_independent_candidates_and_retains_historical_target(
    workflow,
):
    repo = workflow.repository
    first, _ = implement(
        workflow, "T-0210", "frontend/status.js", {"frontend/status.js": b"first\n"}
    )
    second, _ = implement(
        workflow,
        "T-0211",
        "frontend/other.js",
        {"frontend/other.js": b"second\n"},
        depends_on="T-0199",
    )
    staged = repo / "staged.txt"
    staged.write_text("staged\n")
    helpers.git(repo, "add", "staged.txt")
    staged.write_text("dirty\n")
    index = (repo / ".git/index").read_bytes()
    status = helpers.git(repo, "status", "--porcelain")

    result = helpers.successful(*create_args(workflow, second, first))
    shown = show(workflow, result)
    candidate = shown["candidate"]
    assert shown["verification"] == "verified"
    assert shown["candidate"] == result["candidate"]
    assert shown["applied"] == [second["task_run_id"], first["task_run_id"]]
    assert shown["target_baseline"] == workflow.baseline and shown["target_is_current"]
    assert helpers.git(repo, "rev-parse", candidate["ref"]) == candidate["commit"]
    assert (
        helpers.git(repo, "rev-parse", f"{candidate['commit']}^{{tree}}")
        == candidate["tree"]
    )
    assert (
        helpers.git(repo, "show", f"{candidate['commit']}:frontend/status.js")
        == "first"
    )
    assert (
        helpers.git(repo, "show", f"{candidate['commit']}:frontend/other.js")
        == "second"
    )
    assert helpers.git(repo, "rev-parse", "HEAD") == workflow.baseline
    assert (repo / ".git/index").read_bytes() == index
    assert helpers.git(repo, "status", "--porcelain") == status
    first_member, second_member = shown["members"]
    assert first_member["external_dependencies"][0]["source"] == "user_declared"
    assert first_member["external_dependencies"][0]["task_id"] == "T-0199"
    assert first_member["delivery"]["candidate"] == second["candidate"]
    assert (
        second_member["task_input"]["requirements"]
        == "Implement the requested change to frontend/status.js.\n"
    )
    assert second_member["task_input"]["card"]["acceptance_criteria"]
    assert second_member["attempt"] == first["implementation_attempt"]

    advanced = helpers.git(
        repo,
        "commit-tree",
        f"{workflow.baseline}^{{tree}}",
        "-p",
        workflow.baseline,
        "-m",
        "advance target",
    )
    helpers.git(repo, "update-ref", result["target_ref"], advanced, workflow.baseline)
    replacement = helpers.successful(*create_args(workflow, second, first))
    historical = show(workflow, result)
    assert replacement["integration_id"] != result["integration_id"]
    assert replacement["candidate"]["ref"] != candidate["ref"]
    assert replacement["target_baseline"] == advanced
    assert (
        historical["candidate"] == candidate
        and historical["verification"] == "verified"
    )
    assert historical["target_baseline"] == workflow.baseline
    assert historical["target_head"] == advanced and not historical["target_is_current"]
    assert helpers.git(repo, "rev-parse", "HEAD") == advanced


def test_integration_honors_dependent_baseline_and_rework_cumulative_changes(workflow):
    repo = workflow.repository
    upstream, implementation = implement(
        workflow,
        "T-0210",
        "frontend/**",
        {
            "frontend/status.js": helpers.FIRST_BYTES,
            "frontend/retained.js": helpers.RETAINED_BYTES,
        },
    )
    prior = helpers.successful(*create_args(workflow, upstream))
    assert (
        upstream["candidate"]
        not in helpers.git(repo, "rev-list", prior["candidate"]["commit"]).splitlines()
    )
    dependent, _ = implement(
        workflow,
        "T-0211",
        "frontend/status.js",
        {"frontend/status.js": b"dependent\n"},
        baseline=prior["candidate"]["commit"],
        depends_on="T-0210",
    )
    combined = helpers.successful(*create_args(workflow, dependent, upstream))
    assert combined["applied"] == [upstream["task_run_id"], dependent["task_run_id"]]
    assert (
        combined["members"][1]["delivery"]["baseline"] == prior["candidate"]["commit"]
    )
    assert (
        helpers.git(
            repo, "show", f"{combined['candidate']['commit']}:frontend/status.js"
        )
        == "dependent"
    )

    task_run_id = upstream["task_run_id"]
    test_program = workflow.base / "bin" / "codex-workflow-test"
    test_program.write_text(
        f"#!{sys.executable}\n" + (helpers.FAKES / "test_cli.py").read_text()
    )
    test_program.chmod(0o755)
    test_program.with_suffix(".json").write_text(
        json.dumps({"capture": str(workflow.base / "test-wire.jsonl")})
    )
    tested = helpers.successful(
        *helpers.suite_arguments(workflow, task_run_id, 1, test_program), code=1
    )
    reviewer = helpers.executable(
        workflow, "codex", "review", report=helpers.blocking_report()
    )
    reviewed = helpers.successful(
        *helpers.review_arguments(
            workflow, task_run_id, 1, tested["test_attempt"], reviewer
        ),
        code=1,
    )
    helpers.configure(
        implementation,
        task_hash=task_execution_hash(upstream["details"]["input"]["card"]),
        writes={"frontend/status.js": helpers.FIXED_BYTES.hex()},
    )
    revised = helpers.successful(
        "task",
        "rework",
        reviewed["round_id"],
        "--database",
        workflow.database,
        "--directory",
        workflow.base / "rework",
    )
    rejected = helpers.module_cli(*create_args(workflow, dependent, revised))
    assert rejected.returncode == 2
    assert (
        "T-0211 must rebase" in rejected.stderr
        and revised["candidate"] in rejected.stderr
    )

    current = helpers.successful(*create_args(workflow, revised))
    assert current["members"][0]["attempt"] == 2
    assert current["members"][0]["delivery"]["baseline"] == workflow.baseline
    final = current["candidate"]["commit"]
    assert (
        helpers.git(repo, "show", f"{final}:frontend/status.js")
        == helpers.FIXED_BYTES.decode().rstrip()
    )
    assert (
        helpers.git(repo, "show", f"{final}:frontend/retained.js")
        == helpers.RETAINED_BYTES.decode().rstrip()
    )
    assert show(workflow, combined)["candidate"] == combined["candidate"]


def test_integration_conflict_keeps_task_attribution_without_candidate(workflow):
    repo = workflow.repository
    first, _ = implement(
        workflow, "T-0210", "frontend/status.js", {"frontend/status.js": b"first\n"}
    )
    second, _ = implement(
        workflow, "T-0211", "frontend/status.js", {"frontend/status.js": b"second\n"}
    )
    result = helpers.successful(*create_args(workflow, first, second), code=1)
    shown = show(workflow, result)
    assert shown["status"] == "conflicted"
    assert shown["verification"] == "unverified" and shown["candidate"] is None
    assert shown["applied"] == [first["task_run_id"]]
    assert shown["conflict"] == {
        "category": "integration_conflict",
        "task_id": "T-0211",
        "task_run_id": second["task_run_id"],
        "attempt": second["implementation_attempt"],
        "candidate": second["candidate"],
        "paths": ["frontend/status.js"],
    }
    assert not helpers.git(repo, "for-each-ref", "refs/multi-agent/integrations")
    assert helpers.git(repo, "rev-parse", "HEAD") == workflow.baseline


def test_integration_applies_only_the_member_delta_and_proves_real_coverage(workflow):
    repo = workflow.repository
    first, _ = implement(
        workflow, "T-0210", "frontend/a.js", {"frontend/a.js": b"first\n"}
    )
    second, _ = implement(
        workflow,
        "T-0211",
        "frontend/b.js",
        {"frontend/b.js": b"second\n"},
        baseline=first["candidate"],
    )
    combined = helpers.successful(*create_args(workflow, second, first))
    commit = combined["candidate"]["commit"]
    assert combined["applied"] == [second["task_run_id"], first["task_run_id"]]
    assert helpers.git(repo, "show", f"{commit}:frontend/a.js") == "first"
    assert helpers.git(repo, "show", f"{commit}:frontend/b.js") == "second"

    second_only = helpers.successful(*create_args(workflow, second))
    baseline = second_only["candidate"]["commit"]
    assert (
        helpers.git(repo, "diff", "--name-only", workflow.baseline, baseline)
        == "frontend/b.js"
    )
    third, _ = implement(
        workflow,
        "T-0212",
        "frontend/c.js",
        {"frontend/c.js": b"third\n"},
        baseline=baseline,
        depends_on="T-0210",
    )
    rejected = helpers.module_cli(*create_args(workflow, first, third))
    assert rejected.returncode == 2
    assert (
        "T-0212 must rebase" in rejected.stderr
        and first["candidate"] in rejected.stderr
    )


def test_integration_refuses_case_colliding_tree_before_publishing(workflow):
    first, _ = implement(
        workflow, "T-0210", "frontend/Button.js", {"frontend/Button.js": b"first\n"}
    )
    second, _ = implement(
        workflow, "T-0211", "frontend/button.js", {"frontend/button.js": b"second\n"}
    )
    result = helpers.module_cli(*create_args(workflow, first, second))
    assert result.returncode == 2
    assert "T-0211" in result.stderr and "frontend/button.js" in result.stderr
    assert "file paths collide" in result.stderr
    assert not helpers.git(
        workflow.repository, "for-each-ref", "refs/multi-agent/integrations"
    )
