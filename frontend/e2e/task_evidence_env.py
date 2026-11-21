"""Seed verified standalone task plans before starting the offline workbench."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/unit/web_support"))

import offline_env
from offline_fixture import EVIDENCE_PLANS, TASK_EVIDENCE

serve = offline_env._serve


def _seed_then_serve(root, env, port, frontend_dist):
    repository = root / "repository"
    data = root / "data"
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, env=env, text=True
    ).strip()
    plans = {}
    for provider, (_, _, requirement) in EVIDENCE_PLANS.items():
        requirements = root / "runtime" / f"{provider}-requirements.txt"
        requirements.write_text(requirement, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "multi_agent",
                "task",
                "plan",
                "--repository",
                str(repository),
                "--baseline",
                baseline,
                "--requirements",
                str(requirements),
                "--database",
                str(data / "reviews.sqlite3"),
                "--directory",
                str(data / f"{provider}-planning"),
                "--plan-review",
                "gpt-6.2-sol",
                "high",
                "--codex-bin",
                str(root / "bin" / "codex"),
                "--claude-bin",
                str(root / "bin" / "claude"),
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{provider} plan did not finish: {result.stderr or result.stdout}"
            )
        plan = json.loads(result.stdout)
        if plan["phase"] != "ready" or plan["verification"] != "verified":
            raise RuntimeError(f"{provider} plan is not verified")
        plans[provider] = plan["planning_run_id"]
    print(json.dumps(plans), flush=True)
    serve(root, env, port, frontend_dist)


if __name__ == "__main__":
    offline_env.SCENARIOS += (TASK_EVIDENCE,)
    offline_env._serve = _seed_then_serve
    offline_env.main()
