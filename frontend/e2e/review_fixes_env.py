"""Extend the existing offline bridge with dependent leaves and partial confirmation."""

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/unit/web_support"))

import fake_runtime as runtime
import offline_env

SCENARIO = "review-fixes"
create_fixture = offline_env.create_fixture
planning = runtime._planning
response = runtime._response


def fixture(root, scenario):
    env = create_fixture(root, scenario)
    repo = root / "repository"
    # Keep real registration and capture. Give the log a known second page.
    amount_test = "assert Path('backend/amount.py').read_text() == 'AMOUNT = 1\\n'\n"
    if scenario == "claim":
        amount_test = "assert Path('artifact.bin').read_text().startswith('export const status')\n"
    (repo / "checks" / "test.py").write_text(
        "from pathlib import Path\n"
        + amount_test
        + "print('offline diagnostic ' * 520)\n"
        "print('END OF OFFLINE DIAGNOSTIC')\n"
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Offline Fixture",
            "-c",
            "user.email=offline@example.invalid",
            "commit",
            "-qam",
            "Long offline diagnostic",
        ],
        cwd=repo,
        env=env,
        check=True,
    )
    for provider in ("codex", "claude"):
        launcher = root / "bin" / provider
        launcher.write_text(
            f"#!{sys.executable}\nimport sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from review_fixes_env import fake_main\nfake_main()\n"
        )
    return env


def dependent_planning(frozen):
    value = planning(frozen)
    if "cards" in value:
        upstream, downstream = value["cards"]
        downstream["depends_on"] = [upstream["id"]]
    return value


def claim_planning(frozen):
    if Path("task-card.json").is_file():
        return planning(frozen)
    requirement, handoff = frozen["requirements"].split("\n", 1)
    context = json.loads(handoff)
    selected = context["selected_stage"]
    if selected["stage_id"] == "report" and requirement.startswith("Split only"):
        selected["stage_id"] = "summary"
        frozen = {**frozen, "requirements": requirement + "\n" + json.dumps(context)}
    return planning(frozen)


def review(frozen):
    source = Path("source/candidate")
    kind = frozen["kind"]
    if kind == "executed_task_review":
        path, incomplete = runtime.STATUS, runtime.DRAFT
    else:
        path, incomplete = runtime.BANNER, runtime.WELCOME
    file = source / path
    findings = []
    if file.is_file() and file.read_text() == incomplete:
        findings.append(
            {
                "id": "offline-blocker",
                "severity": "P1",
                "category": "correctness",
                "side": "candidate",
                "path": path,
                "line_start": 1,
                "line_end": 1,
                "quote": incomplete.rstrip("\n"),
                "title": "Incomplete offline output",
                "impact": "The output misses the required value.",
                "rationale": "The frozen line shows the uncorrected value.",
            }
        )
    return {"summary": "Checked the offline output.", "findings": findings}, "success"


def offline_response(root, scenario, provider, prompt):
    if prompt.startswith("Implementation template:"):
        report, mode = runtime._implement(root, scenario, prompt)
        payload = json.loads(prompt.split(runtime._INPUT, 1)[1])
        card = payload.get("original_member", payload)["task_input"]["card"]
        (path,) = card["write_scope"]
        if path == runtime.STATUS and "rework" not in payload:
            Path(path).write_text(runtime.DRAFT)
        if path == runtime.BANNER and "original_member" in payload:
            Path(path).write_text('export const banner = "reviewed";\n')
        return report, mode
    if (targets := Path("confirmation-targets.json")).is_file():
        frozen = Path("review-input.json").read_bytes()
        marker = root / "runtime" / f"confirm-{sha256(frozen).hexdigest()}"
        if provider == "claude":
            try:
                marker.touch(exist_ok=False)
            except FileExistsError:
                pass
            else:
                return {}, "model-error"
        return {
            "summary": "Confirmed the cited frozen line.",
            "confirmations": [
                {
                    "target_seat": item["target_seat"],
                    "finding_id": item["finding_id"],
                    "decision": "agree",
                    "reason": "The frozen line shows the value.",
                }
                for item in json.loads(targets.read_text())
            ],
        }, "success"
    if (source := Path("review-input.json")).is_file():
        return review(json.loads(source.read_text()))
    return response(root, scenario, provider, prompt)


def fake_main():
    root = Path(sys.argv[0]).resolve().parents[1]
    if (root / "runtime" / "scenario").read_text() == SCENARIO:
        runtime._planning = dependent_planning
        runtime._response = offline_response
    else:
        runtime._planning = claim_planning
    runtime.main()


if __name__ == "__main__":
    offline_env.SCENARIOS += (SCENARIO, "claim")
    offline_env.create_fixture = fixture
    offline_env.main()
