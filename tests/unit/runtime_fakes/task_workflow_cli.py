"""Workflow-specific synthetic reports over the unchanged fake CLI transport."""

import hashlib
import json
import os
import runpy
import sys
import time
from pathlib import Path

executable = Path(sys.argv[0])
settings = json.loads(executable.with_suffix(".json").read_text())
provider = settings["provider"]
arguments = sys.argv[1:]
os.environ["FAKE_RUNTIME_MODE"] = settings.get("mode", "success")
os.environ["FAKE_RUNTIME_CAPTURE"] = settings["capture"]
os.environ.pop("FAKE_IMPLEMENTATION", None)


def capture(kind, **values):
    with Path(settings["capture"]).open("a") as stream:
        stream.write(json.dumps({"kind": kind, "provider": provider, **values}) + "\n")


def implement(prompt):
    dispatches = settings.get("dispatches")
    if dispatches is None:
        writes = settings["writes"]
    else:
        (task_hash,) = (key for key in dispatches if key in prompt)
        writes = dispatches[task_hash]
        barrier = Path(settings["barrier"])
        (barrier / task_hash).write_text(str(Path.cwd()))
        deadline = time.monotonic() + 15
        while not all((barrier / key).is_file() for key in dispatches):
            if time.monotonic() >= deadline:
                raise TimeoutError("peer implementation never reached the barrier")
            time.sleep(0.01)
        report = json.loads(os.environ["FAKE_RUNTIME_REPORT"])
        report["task_hash"] = task_hash
        os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(report)
        if task_hash == settings.get("wait_task_hash"):
            os.environ["FAKE_RUNTIME_MODE"] = "wait"
    before = {}
    for name, encoded in writes.items():
        path = Path(name)
        before[name] = path.read_bytes().hex() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes.fromhex(encoded))
    capture(
        "implementation",
        cwd=str(Path.cwd()),
        pid=os.getpid(),
        prompt=prompt,
        before=before,
        after=writes,
    )
    if settings.get("ready"):
        Path(settings["ready"]).touch()


source = Path("planning-input.json")
role = settings["role"]
if role == "stage-direct":
    if source.is_file():
        role = "planning"
    elif provider == "codex" and Path("frontend/status.js").is_file():
        role = "implementation"
if role == "planning" and source.is_file():
    frozen = json.loads(source.read_text())
    is_draft = not Path("task-card.json").is_file()
    if settings["role"] == "stage-direct" and arguments[:1] in (["-p"], ["app-server"]):
        capture("stage-card-input", draft=is_draft, requirements=frozen["requirements"])
    if is_draft:
        report = {
            "schema_version": 1,
            "id": settings.get("task_id", "T-0200"),
            "goal": "Implement the original frontend requirement",
            "baseline_commit": frozen["frozen_rules"]["baseline_commit"],
            "write_scope": settings.get("write_scope", ["frontend/**"]),
            "depends_on": [item["task_id"] for item in frozen["dependencies"]],
            "interface_contracts": [],
            "deliverables": ["The requested frontend status"],
            "acceptance_criteria": ["The complete named build and test suite passes"],
            "test_refs": ["build", "test"],
            "open_questions": [],
            "proposed_tests": [],
        }
        if settings.get("question"):
            report["open_questions"] = [
                {"question": "Which status?", "blocks_dispatch": True}
            ]
    else:
        card = json.loads(Path("task-card.json").read_text())
        report = {
            "task_hash": hashlib.sha256(
                json.dumps(
                    card, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "decision": settings.get("decision", "ready"),
            "reason": "Synthetic independent review of the exact frozen task card",
            "blocking_issues": ["Clarify the status"]
            if settings.get("decision") == "needs_revision"
            else [],
        }
    os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(report)
    if settings["role"] == "stage-direct" and not is_draft:
        Path(settings["capture"]).with_suffix(".task-hash").write_text(
            report["task_hash"]
        )

if role == "implementation":
    os.environ["FAKE_IMPLEMENTATION"] = "1"
    task_hash = (
        Path(settings["capture"]).with_suffix(".task-hash").read_text()
        if settings["role"] == "stage-direct"
        else settings.get("task_hash")
    )
    os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(
        {
            "task_hash": task_hash,
            "outcome": settings.get("outcome", "completed"),
            "summary": "Synthetic implementation of the exact task",
            "required_paths": settings.get("required_paths", []),
        }
    )
    if provider == "claude" and arguments[:1] == ["-p"]:
        implement(arguments[1])
    elif provider == "codex" and arguments == ["app-server", "--strict-config"]:
        original_stdin = sys.stdin

        def requests():
            for line in original_stdin:
                message = json.loads(line)
                if message.get("method") == "turn/start":
                    implement(message["params"]["input"][0]["text"])
                yield line

        sys.stdin = requests()

if role == "review":
    report = settings.get("report", {"summary": "Synthetic review", "findings": []})
    frozen_review = None
    confirmation = False
    if Path("review-input.json").is_file():
        frozen_review = json.loads(Path("review-input.json").read_text())
        if "reports_by_candidate" in settings or "modes_by_candidate" in settings:
            candidate = frozen_review["source"]["candidate"]
            report = settings.get("reports_by_candidate", {}).get(candidate, report)
            os.environ["FAKE_RUNTIME_MODE"] = settings.get(
                "modes_by_candidate", {}
            ).get(candidate, settings.get("mode", "success"))
        targets = Path("confirmation-targets.json")
        if settings.get("generate_confirmations", False) and targets.is_file():
            confirmation = True
            report = {
                "summary": "Synthetic cross-confirmation of the frozen candidate",
                "confirmations": [
                    {
                        "target_seat": item["target_seat"],
                        "finding_id": item["finding_id"],
                        "decision": settings.get("confirmation_decision", "agree"),
                        "reason": "Synthetic decision on the exact frozen reference.",
                    }
                    for item in json.loads(targets.read_text())
                ],
            }
    os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(report)
    if Path("review-input.json").is_file() and arguments[:1] in (
        ["-p"],
        ["app-server"],
    ):
        extra = (
            {
                "confirmation": confirmation,
                "logs_hex": {
                    command[stream]["path"]: Path(command[stream]["path"])
                    .read_bytes()
                    .hex()
                    for command in frozen_review["executed_suite"]["commands"]
                    for stream in ("stdout", "stderr")
                },
            }
            if settings.get("generate_confirmations")
            else {}
        )
        capture(
            "review-input",
            cwd=str(Path.cwd()),
            payload=frozen_review,
            diff_hex=Path("diff.patch").read_bytes().hex(),
            **extra,
        )

runpy.run_path(settings["transport"], run_name="__main__")
