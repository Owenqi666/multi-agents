"""Seed the offline bridge with real Git content and a synthetic login."""

import json
import os
import subprocess
import sys
from pathlib import Path

STANDALONE_PLAN = "standalone-plan"
SCENARIOS = ("accept", "rework", "dual-partial", "cancel", STANDALONE_PLAN)
AMOUNT = "backend/amount.py"
STATUS = "frontend/status.js"
BANNER = "frontend/banner.js"
INITIAL = 'export const status = "initial";\n'
DRAFT = 'export const status = "draft";\n'
READY = 'export const status = "ready";\n'
WELCOME = 'export const banner = "welcome";\n'
TASK_EVIDENCE = "task-evidence"
PLANNING_QUESTION_REQUIREMENTS = (
    "Implement the amount after the reporting cutoff decision.\n"
)
PLANNING_QUESTION = "Which reporting cutoff should the amount use?"
EVIDENCE_PLANS = {
    "codex": ("T-0810", AMOUNT, "Implement the standalone amount.\n"),
    "claude": ("T-0811", STATUS, "Implement the standalone status.\n"),
}
CODEX_MODELS = ("gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-terra", "gpt-6.2-luna")
CLAUDE_MODELS = (
    "claude-fable-5-2",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
)


def project_document():
    stages = (
        ("amount", [], "Implement backend/amount.py"),
        ("summary", ["amount"], "Implement frontend status and banner"),
        ("report", ["summary"], "Outline the monthly report"),
    )
    return {
        "title": "Offline project workbench",
        "architecture": "A backend amount and two independent frontend modules.",
        "shared_interfaces": ["Use the checked output format."],
        "stages": [
            {
                "stage_id": name,
                "title": f"Stage {name}",
                "goal": goal,
                "approach": goal,
                "workflow": ["Deliver only the selected stage"],
                "depends_on": dependencies,
                "acceptance_criteria": ["The named build and test suite passes"],
                "deliverables": [goal],
            }
            for name, dependencies, goal in stages
        ],
    }


def _rules(scenario):
    python = str(Path(sys.executable).resolve())
    return {
        "version": 1,
        "commands": {
            "build": {"executable": python, "args": ["checks/build.py"]},
            "test": {"executable": python, "args": ["checks/test.py"]},
        },
        "protected_paths": ["checks/**", ".agents/**"],
        "implementation": [
            {
                "paths": ["frontend/**"],
                "provider": "claude-code",
                "default_model": "claude-sonnet-5-5",
                "default_effort": "low",
            },
            {
                "paths": [AMOUNT],
                "provider": "codex",
                "default_model": "gpt-6.2-sol",
                "default_effort": "low",
                **(
                    {"allowed_models": ["gpt-6.2-sol", "gpt-6.1-terra"]}
                    if scenario in (TASK_EVIDENCE, STANDALONE_PLAN)
                    else {}
                ),
            },
        ],
    }


def create_fixture(root: Path, scenario: str) -> dict[str, str]:
    for name in ("home", "codex-home", "tmp", "bin", "runtime", "data", "repository"):
        (root / name).mkdir(mode=0o700)
    (root / "runtime" / "scenario").write_text(scenario, encoding="utf-8")
    auth = root / "codex-home" / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    key: f"offline-fixture-{key}"
                    for key in (
                        "id_token",
                        "access_token",
                        "refresh_token",
                        "account_id",
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    auth.chmod(0o600)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "codex-home"),
        "TMPDIR": str(root / "tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
    }

    repository = root / "repository"
    for folder in ("backend", "frontend", "checks", ".multi-agent"):
        (repository / folder).mkdir()
    files = {
        "AGENTS.md": "Synthetic offline workspace for the project flow.\n",
        AMOUNT: "AMOUNT = 0\n",
        STATUS: INITIAL,
        BANNER: 'export const banner = "initial";\n',
        ".multi-agent/rules.yaml": json.dumps(_rules(scenario), indent=2) + "\n",
        "checks/build.py": (
            "from pathlib import Path\n"
            f"Path('artifact.bin').write_bytes(Path({STATUS!r}).read_bytes())\n"
            "print('Offline build wrote artifact.bin')\n"
        ),
        "checks/test.py": (
            "import sys\n"
            "from pathlib import Path\n"
            f"assert Path({AMOUNT!r}).read_text() == 'AMOUNT = 1\\n'\n"
            f"assert Path('artifact.bin').read_text() in {(INITIAL, DRAFT, READY)!r}\n"
            f"if Path('artifact.bin').read_text() != {INITIAL!r}:\n"
            f"    assert Path({BANNER!r}).read_text() == {WELCOME!r}\n"
            "print('Offline checks passed')\n"
            "print('Offline independent test diagnostic', file=sys.stderr)\n"
        ),
    }
    for name, contents in files.items():
        (repository / name).write_text(contents, encoding="utf-8")
    for command in (
        ["init", "--quiet", "--initial-branch=main", "--template="],
        ["add", "."],
        ["commit", "--quiet", "-m", "Offline workbench baseline"],
    ):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Offline Fixture",
                "-c",
                "user.email=offline@example.invalid",
                *command,
            ],
            cwd=repository,
            env=env,
            check=True,
        )

    source = Path(__file__).resolve().parent
    launcher = (
        f"#!{sys.executable}\nimport sys\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "from fake_runtime import main\nmain()\n"
    )
    for provider in ("codex", "claude"):
        executable = root / "bin" / provider
        executable.write_text(launcher, encoding="utf-8")
        executable.chmod(0o755)
    return env
