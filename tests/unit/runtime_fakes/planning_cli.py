"""Planning-only synthetic wrapper around an unchanged fake transport copy."""

import hashlib
import json
import os
import runpy
import signal
import sys
import time
from pathlib import Path

provider = "claude" if "claude" in Path(sys.argv[0]).name else "codex"
context = Path.cwd()
source = context / "planning-input.json"
case = os.environ.get("FAKE_PLANNING_CASE", "success")
if source.is_file():
    frozen = json.loads(source.read_text())
    if provider == "claude":
        report = {
            "schema_version": 1,
            "id": "T-0100",
            "goal": "Implement the frozen original requirement",
            "baseline_commit": frozen["frozen_rules"]["baseline_commit"],
            "write_scope": ["src/**"],
            "depends_on": [item["task_id"] for item in frozen["dependencies"]],
            "interface_contracts": [],
            "deliverables": ["An implementation"],
            "acceptance_criteria": ["The named unit checks pass"],
            "test_refs": ["unit"],
            "open_questions": [],
            "proposed_tests": [],
        }
        if case in ("unresolved", "invented-resolution"):
            question = {"question": "Which deployment?", "blocks_dispatch": True}
            if case == "invented-resolution":
                question["resolution"] = "Model-decided production"
            report["open_questions"] = [question]
    else:
        card = json.loads((context / "task-card.json").read_text())
        task_hash = hashlib.sha256(
            json.dumps(
                card, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        decision = os.environ.get("FAKE_PLANNING_DECISION", "ready")
        report = {
            "task_hash": task_hash,
            "decision": decision,
            "reason": "Synthetic review of the exact frozen card",
            "blocking_issues": ["Add coverage"] if decision == "needs_revision" else [],
        }
    if case == "invalid-artifact":
        report = {"invalid": "preserve this actual result"}
    os.environ["FAKE_RUNTIME_REPORT"] = json.dumps(report)

    if case == "completed-after-cancel" and provider == "claude":
        # The actual fake reaches its result write before the host accepts cancel.
        # SIGINT releases that write with Completed, rather than synthesizing a
        # cancellation terminal. This exercises the core's accepted-cancel fold.
        original_signal, original_write = signal.signal, os.write
        cancelled = False

        def accepted_interrupt(*_):
            global cancelled
            cancelled = True

        def install(signum, handler):
            return original_signal(
                signum, accepted_interrupt if signum == signal.SIGINT else handler
            )

        def gated_write(fd, raw):
            if fd == 1:
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    value = {}
                if value.get("type") == "result" and value.get("is_error") is False:
                    Path(os.environ["FAKE_PLANNING_READY"]).touch()
                    while not cancelled:
                        time.sleep(0.005)
            return original_write(fd, raw)

        signal.signal = install
        os.write = gated_write

# run_path changes argv[0]; a provider-named unchanged transport copy preserves
# the old fake's provider selection without changing any shared source.
transport = (
    Path(os.environ["FAKE_PLANNING_TRANSPORT_ROOT"]) / f"{provider}-transport.py"
)
runpy.run_path(str(transport), run_name="__main__")
