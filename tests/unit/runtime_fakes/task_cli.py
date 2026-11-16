"""Synthetic implementation writes around an unchanged fake transport copy."""

import json
import os
import runpy
import sys
from pathlib import Path

provider = "claude" if "claude" in Path(sys.argv[0]).name else "codex"
args = sys.argv[1:]
implementation = os.environ.get("FAKE_IMPLEMENTATION") == "1"
content = b"synthetic implementation\n"


def write_implementation():
    target = Path("src/implemented.txt")
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(content)
    if capture := os.environ.get("FAKE_RUNTIME_CAPTURE"):
        with open(capture, "a") as stream:
            stream.write(
                json.dumps(
                    {
                        "kind": "task-write",
                        "provider": provider,
                        "pid": os.getpid(),
                        "cwd": os.getcwd(),
                        "path": target.as_posix(),
                        "content_hex": content.hex(),
                    }
                )
                + "\n"
            )


if implementation and provider == "claude" and args[:1] == ["-p"]:
    write_implementation()
elif (
    implementation and provider == "codex" and args == ["app-server", "--strict-config"]
):
    original_stdin = sys.stdin

    def task_requests():
        for line in original_stdin:
            message = json.loads(line)
            if message.get("method") == "turn/start":
                write_implementation()
            yield line

    sys.stdin = task_requests()

# Preserve the old fake's provider selection when run_path replaces argv[0].
# FAKE_RUNTIME_REPORT and every transport mode pass through without rewriting.
transport = Path(os.environ["FAKE_TASK_TRANSPORT_ROOT"]) / f"{provider}-transport.py"
runpy.run_path(str(transport), run_name="__main__")
