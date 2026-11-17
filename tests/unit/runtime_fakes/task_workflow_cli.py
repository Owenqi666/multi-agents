"""Workflow-specific synthetic reports over the unchanged fake CLI transport."""

import hashlib
import json
import os
import runpy
import sys
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
    before = {}
    for name, encoded in settings["writes"].items():
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
        after=settings["writes"],
    )
    if settings.get("ready"):
        Path(settings["ready"]).touch()


source = Path("planning-input.json")
if settings["role"] == "planning" and source.is_file():
    frozen = json.loads(source.read_text())
    if provider == "claude":
        report = {
            "schema_version": 1,
            "id": "T-0200",
            "goal": "Implement the original frontend requirement",
            "baseline_commit": frozen["frozen_rules"]["baseline_commit"],
            "write_scope": ["frontend/**"],
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

if settings["role"] == "implementation":
    os.environ["FAKE_IMPLEMENTATION"] = "1"
    os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(
        {
            "task_hash": settings["task_hash"],
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

if settings["role"] == "review":
    os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(
        settings.get("report", {"summary": "Synthetic review", "findings": []})
    )
    if Path("review-input.json").is_file() and arguments[:1] in (
        ["-p"],
        ["app-server"],
    ):
        capture(
            "review-input",
            cwd=str(Path.cwd()),
            payload=json.loads(Path("review-input.json").read_text()),
            diff_hex=Path("diff.patch").read_bytes().hex(),
        )

runpy.run_path(settings["transport"], run_name="__main__")
