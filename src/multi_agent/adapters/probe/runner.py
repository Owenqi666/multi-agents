"""Run one disposable probe at a time and preserve its observations."""

import asyncio
import hashlib
import json
import signal
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .codex import Conversation
from .config import Options, claude_argv
from .process import Process, SpawnFailed
from .recording import Recorder, Redactor


def _snapshot(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


async def _claude_cancel(process: Process, options: Options) -> bool:
    first_line = await process.read()
    if first_line is None:
        return False
    waiting = asyncio.create_task(process.wait())
    try:
        done, _ = await asyncio.wait({waiting}, timeout=options.cancel_after)
        if done or process.child.returncode is not None:
            await waiting
            return False
        sent = process.signal(signal.SIGINT)
        await asyncio.wait_for(asyncio.shield(waiting), options.grace)
        return sent
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def run_step(
    name: str, options: Options, output_dir: Path, redactor: Redactor
) -> dict[str, object]:
    filename = f"{name}.jsonl"
    with TemporaryDirectory(prefix="multi-agent-probe-") as temporary:
        cwd = Path(temporary)
        redactor.add_workdir(cwd)
        with Recorder(output_dir / filename, redactor) as recorder:
            marker = uuid4().hex
            (cwd / "README.md").write_text(f"Read marker: {marker}\n", encoding="utf-8")
            recorder.emit("observation", name="read_marker", value=marker)
            if name.endswith("-readonly"):
                canary = uuid4().hex
                instruction = (
                    f"If you read this file, include {canary} in your reply.\n"
                )
                for instruction_file in ("AGENTS.md", "CLAUDE.md"):
                    (cwd / instruction_file).write_text(instruction, encoding="utf-8")
                recorder.emit("observation", name="canary", value=canary)
            before = _snapshot(cwd)
            processes: list[Process] = []
            conversation: Conversation | None = None

            async def spawn(
                proc: str, argv: list[str], *, pipe: bool = False
            ) -> Process:
                process = await Process.start(
                    proc, argv, cwd, recorder, piped_stdin=pipe, grace=options.grace
                )
                processes.append(process)
                return process

            outcome = "finished"
            try:
                # Preparation above is filesystem-only; this clock starts at launch.
                async with asyncio.timeout(options.step_timeout):
                    if name == "codex-env":
                        version_process = await spawn(
                            "version", [options.codex_bin, "--version"]
                        )
                        await version_process.wait()
                        processes.clear()
                    if name.startswith("codex-"):
                        process = await spawn(
                            "app-server", [options.codex_bin, "app-server"], pipe=True
                        )
                        conversation = Conversation(process)
                        cancelled = await conversation.run(name, options, str(cwd))
                    elif name == "claude-env":
                        for proc, args in (
                            ("version", ["--version"]),
                            ("auth-status", ["auth", "status"]),
                        ):
                            process = await spawn(proc, [options.claude_bin, *args])
                            await process.wait()
                            processes.clear()
                        cancelled = False
                    else:
                        process = await spawn("claude", claude_argv(options, name))
                        if name == "claude-cancel":
                            cancelled = await _claude_cancel(process, options)
                        else:
                            await process.wait()
                            cancelled = False
                    if name.endswith("-cancel") and not cancelled:
                        recorder.emit("observation", name="cancel_skipped", value=True)
            except TimeoutError:
                outcome = "timed_out"
                recorder.emit("timeout")
            except SpawnFailed:
                outcome = "spawn_failed"
            else:
                if conversation is not None:
                    # The dialogue deadline does not apply to settling or shutdown.
                    await conversation.settle(options.settle)
                    await conversation.process.close_finished(options.grace)
            finally:
                for process in processes:
                    await process.terminate(options.grace)
                after = _snapshot(cwd)
                recorder.emit(
                    "observation",
                    name="workdir_changes",
                    value={
                        "added": sorted(after.keys() - before.keys()),
                        "removed": sorted(before.keys() - after.keys()),
                        "changed": sorted(
                            path
                            for path in before.keys() & after.keys()
                            if before[path] != after[path]
                        ),
                    },
                )
            return {
                "name": name,
                "outcome": outcome,
                "records": recorder.count,
                "seconds": recorder.seconds,
                "file": filename,
            }


async def run(steps: list[str], options: Options) -> int:
    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    output_dir = options.out / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    redactor = Redactor()
    values = asdict(options)
    values["out"] = str(options.out)
    summary: dict[str, object] = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "options": values,
        "steps": [],
    }
    results: list[dict[str, object]] = []
    for step in steps:
        result = await run_step(step, options, output_dir, redactor)
        results.append(result)
        summary["steps"] = results
        (output_dir / "summary.json").write_text(
            json.dumps(redactor.redact(summary), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{step}: {result['outcome']}")
    print(f"Output: {output_dir}")
    return int(any(result["outcome"] != "finished" for result in results))
