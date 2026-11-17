"""Test-only review controls around the unchanged phase-one fake transport."""

import json
import os
import runpy
import sys
from pathlib import Path

executable = Path(sys.argv[0])
settings = json.loads(executable.with_suffix(".json").read_text())
os.environ["FAKE_RUNTIME_MODE"] = settings.get("mode", "success")
os.environ["FAKE_RUNTIME_CAPTURE"] = settings["capture"]
report = settings.get(
    "report", {"summary": "Synthetic independent review", "findings": []}
)
targets = Path("confirmation-targets.json")
if targets.is_file():
    report = {
        "summary": "Synthetic evidence-based cross-confirmation",
        "confirmations": [
            {
                "target_seat": item["target_seat"],
                "finding_id": item["finding_id"],
                "decision": "agree",
                "reason": "The exact quoted candidate line matches the frozen source.",
            }
            for item in json.loads(targets.read_text())
        ],
    }
os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(report)
if Path("review-input.json").is_file() and sys.argv[1] in ("-p", "app-server"):
    files = {
        path.as_posix(): path.read_bytes().hex()
        for path in Path(".").rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    with Path(settings["bundles"]).open("a") as stream:
        stream.write(
            json.dumps({"provider": settings["provider"], "files": files}) + "\n"
        )
runpy.run_path(settings["transport"], run_name="__main__")
