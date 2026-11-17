"""Reviewable native test-policy canary; prepare never launches a process.

The run command is for a specifically authorized user-terminal S9 acceptance.
Its synthetic artifact proves no candidate, planning, or model execution claim.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.core.task_events import TaskCandidateFrozen
from multi_agent.core.task_execution import task_execution_value
from multi_agent.core.task_test_execution import (
    TaskTestCheckout,
    TaskTestPolicy,
    TaskTestScratch,
)
from multi_agent.core.task_test_input import TaskTestInput
from multi_agent.core.tasks import NamedCommand
from multi_agent.core.workspaces import WorkspaceDelivery

from .codex_isolation import CodexIsolation
from .runtime_decode import strict_json_loads
from .test_isolation import PROFILE, VERSION
from .test_recording import _state
from .test_runtime import TaskTestRuntimeAdapter

CASE = "native-test-boundary"
_MAX_PLAN = 2 * 1024 * 1024


def _encoded(value: Any) -> bytes:
    return (
        json.dumps(
            task_execution_value(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _identity(path: Path) -> tuple[int, int]:
    value = path.lstat()
    return value.st_dev, value.st_ino


def _source_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "src/multi_agent/adapters" / name
        for name in (
            "test_runtime.py",
            "test_recording.py",
            "test_isolation.py",
            "test_probe.py",
            "base.py",
            "codex_isolation.py",
            "ownership.py",
            "runtime.py",
            "runtime_decode.py",
            "probe/process.py",
            "probe/recording.py",
        )
    ]
    paths += [
        root / "protocol/codex/schema/v2" / name
        for name in (
            "CommandExecParams.json",
            "CommandExecResponse.json",
            "CommandExecOutputDeltaNotification.json",
            "CommandExecTerminateParams.json",
            "CommandExecTerminateResponse.json",
            "ConfigWarningNotification.json",
            "RemoteControlStatusChangedNotification.json",
        )
    ]
    paths.append(root / "protocol/codex/schema/ServerNotification.json")
    paths.append(root / "protocol/codex/config.schema.json")
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def _scripts(root: Path) -> tuple[str, str]:
    original = root / "original"
    control = root / "context" / "control"
    logs = root / "logs"
    database = root / "database-canary"
    credentials = root / "credential-canary"
    first = "\n".join(
        [
            "import json,os,pathlib,socket",
            "p=pathlib.Path",
            "checks={}",
            "p('build-artifact').write_text('built')",
            "p(os.environ['XDG_CACHE_HOME'],'shared').write_text('cached')",
            "p(os.environ['HOME'],'home-write').write_text('home')",
            "p(os.environ['TMPDIR'],'tmp-write').write_text('tmp')",
            "checks['child_scratch_writable']=True",
            "def denied(name,path,write=False):",
            " try:",
            "  p(path).write_text('SHOULD-NOT-WRITE') if write else p(path).read_bytes()",
            " except OSError: checks[name]=True",
            " else: checks[name]=False",
            f"denied('original_write',{str(original / 'file')!r},True)",
            f"denied('original_git_write',{str(original / '.git' / 'file')!r},True)",
            f"denied('credential_read',{str(credentials)!r})",
            f"denied('context_read',{str(control)!r})",
            f"denied('database_read',{str(database)!r})",
            f"denied('logs_write',{str(logs / 'forbidden')!r},True)",
            "denied('temporary_auth_read',p('auth-canary-path').read_text())",
            "s=socket.socket()",
            "try: s.bind(('127.0.0.1',0))",
            "except OSError: checks['network_denied']=True",
            "else: checks['network_denied']=False",
            "finally: s.close()",
            "print(json.dumps(checks,sort_keys=True))",
            "raise SystemExit(0 if all(checks.values()) else 1)",
        ]
    )
    second = "import os,pathlib; p=pathlib.Path; assert p('build-artifact').read_text()=='built'; assert p(os.environ['XDG_CACHE_HOME'],'shared').read_text()=='cached'; print('shared build and cache verified')"
    return first, second


def _fixed_plan(directory: Path) -> dict[str, Any]:
    root = directory / CASE
    scripts = _scripts(root)
    return {
        "version": 1,
        "case": CASE,
        "directory": str(directory.resolve()),
        "required_cli_version": VERSION,
        "profile": PROFILE,
        "timeout": 20.0,
        "grace": 2.0,
        "output_bytes_limit": 1024 * 1024,
        "python": str(Path(sys.executable).resolve()),
        "runtime_read_paths": [str(Path(sys.base_prefix).resolve())],
        "source_hashes": _source_hashes(),
        "commands": [
            [str(Path(sys.executable).resolve()), "-c", script] for script in scripts
        ],
        "allowed_writes": [str(root / "checkout"), str(root / "scratch")],
        "required_stop_scope": "command-request-and-owned-runtime-v1",
        "unconfirmed": [
            "command_group_gone",
            "all_descendants_stopped",
            "producer_stdout_eof",
            "producer_stderr_eof",
        ],
        "permission_status": "unverified; run only after specific user authorization in user terminal",
        "cleanup": "Host authentication roots are removed only after actual owned stop; retain this entire canary directory and logs for review, never automatically delete or rerun.",
    }


def prepare_plan(output_dir: Path) -> Path:
    directory = output_dir.expanduser().resolve()
    directory.mkdir(mode=0o700)
    value = {**_fixed_plan(directory), "owner": uuid4().hex}
    raw = _encoded(value)
    plan = directory / "plan.json"
    plan.write_bytes(raw)
    (directory / ".probe-owner.json").write_bytes(
        _encoded(
            {
                "owner": value["owner"],
                "directory_identity": _identity(directory),
                "plan_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    )
    for path in (plan, directory / ".probe-owner.json"):
        path.chmod(0o400)
    return plan


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_PLAN:
            raise ValueError("probe input is not a bounded regular file")
        raw = bytearray()
        while len(raw) <= before.st_size:
            chunk = os.read(descriptor, min(8192, before.st_size + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if (
            len(raw) != before.st_size
            or _state(os.fstat(descriptor)) != _state(before)
            or _state(path.lstat()) != _state(before)
        ):
            raise ValueError("probe input changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _binary_hash(path: Path) -> tuple[tuple[int, int], str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
            raise ValueError("probe executable is not a bounded regular file")
        remaining = before.st_size
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("probe executable was truncated")
            digest.update(chunk)
            remaining -= len(chunk)
        if (
            os.read(descriptor, 1)
            or _state(os.fstat(descriptor)) != _state(before)
            or _state(path.lstat()) != _state(before)
        ):
            raise ValueError("probe executable changed while reading")
        return (before.st_dev, before.st_ino), digest.hexdigest()
    finally:
        os.close(descriptor)


class _CanaryAdapter(TaskTestRuntimeAdapter):
    def _create_isolation(self) -> CodexIsolation:
        isolation = super()._create_isolation()
        try:
            sentinel = isolation.codex_home / "test-credential-canary"
            sentinel.write_text("harmless-canary-not-a-credential")
            sentinel.chmod(0o600)
            # Only a harmless path is exposed, never authentication contents.
            (Path(self.checkout.root) / "auth-canary-path").write_text(str(sentinel))
        except BaseException:
            isolation.cleanup()
            raise
        return isolation


async def run_plan(
    plan_path: Path, *, case: str, executable: str = "codex"
) -> dict[str, Any]:
    raw = _read(plan_path)
    plan = strict_json_loads(raw.decode())
    owner = strict_json_loads(_read(plan_path.parent / ".probe-owner.json").decode())
    if (
        not isinstance(plan, dict)
        or not isinstance(owner, dict)
        or case != CASE
        or plan.get("case") != CASE
        or plan.get("version") != 1
        or plan.get("directory") != str(plan_path.parent.resolve())
        or owner.get("directory_identity") != list(_identity(plan_path.parent))
        or owner.get("owner") != plan.get("owner")
        or owner.get("plan_sha256") != hashlib.sha256(raw).hexdigest()
        or any(
            _encoded(plan.get(key)) != _encoded(value)
            for key, value in _fixed_plan(plan_path.parent.resolve()).items()
        )
    ):
        raise ValueError("probe plan or source binding differs")
    root = plan_path.parent / CASE
    expected = [[plan["python"], "-c", script] for script in _scripts(root)]
    if plan.get("commands") != expected:
        raise ValueError("probe commands differ from the reviewed case")
    resolved = (
        shutil.which(executable) if not Path(executable).is_absolute() else executable
    )
    if resolved is None:
        raise ValueError("probe executable was not found")
    binary = Path(resolved).resolve(strict=True)
    binary_identity, binary_hash = _binary_hash(binary)
    policy = TaskTestPolicy(
        executable=str(binary),
        executable_identity=binary_identity,
        executable_sha256=binary_hash,
        timeout=20.0,
        grace=2.0,
        output_bytes_limit=1024 * 1024,
        runtime_read_paths=tuple(plan["runtime_read_paths"]),
    )
    root.mkdir(mode=0o700)
    for name in ("checkout", "scratch", "original", "context", "logs"):
        (root / name).mkdir(mode=0o700)
    for name in ("home", "tmp", "cache"):
        (root / "scratch" / name).mkdir(mode=0o700)
    for path in (root / "checkout" / ".git", root / "original" / ".git"):
        path.mkdir(mode=0o700)
    for path in (
        root / "original" / "file",
        root / "original" / ".git" / "file",
        root / "context" / "control",
        root / "database-canary",
        root / "credential-canary",
    ):
        path.write_text("harmless-permission-canary")
    commands = tuple(
        NamedCommand(f"permission-{i}", argv[0], tuple(argv[1:]))
        for i, argv in enumerate(expected, 1)
    )
    candidate = TaskCandidateFrozen(
        WorkspaceDelivery(
            "0" * 40, "1" * 40, (), "0" * 64, "synthetic-permission-probe", "2" * 40
        ),
        "synthetic-probe-only",
        "0" * 64,
    )
    value = TaskTestInput(
        task_run_id="permission-canary",
        implementation_attempt=1,
        task_id="T-0050",
        task_hash="0" * 64,
        rules_hash="0" * 64,
        task_input_hash="0" * 64,
        candidate=candidate,
        commands=commands,
        declaration_source="Explicit native permission canary; not task evidence",
        source_kind="permission_canary",
        task_history_length=0,
        task_history_hash="0" * 64,
    )
    checkout = TaskTestCheckout(
        input=value,
        repository=str(root / "original" / ".git"),
        database_path=str(root / "database-canary"),
        database_identity=_identity(root / "database-canary"),
        database_parent_identity=_identity(root),
        root=str(root / "checkout"),
        root_identity=_identity(root / "checkout"),
        git_directory=str(root / "checkout" / ".git"),
        git_identity=_identity(root / "checkout" / ".git"),
        owner_token=plan["owner"],
        object_format="sha1",
        initial_digest="0" * 64,
        controls=(),
        objects=(),
        observed_test_attempt=None,
        test_history_length=0,
        test_history_hash="0" * 64,
    )
    scratch = TaskTestScratch(
        path=str(root / "scratch"),
        identity=_identity(root / "scratch"),
        home_identity=_identity(root / "scratch/home"),
        tmp_identity=_identity(root / "scratch/tmp"),
        cache_identity=_identity(root / "scratch/cache"),
    )
    results = []
    for sequence, command in enumerate(commands, 1):
        adapter = _CanaryAdapter(
            checkout=checkout,
            scratch=scratch,
            policy=policy,
            log_dir=root / "logs" / f"command-{sequence}",
        )
        try:
            result = await adapter.run(command, sequence=sequence)
        finally:
            (root / f"result-{sequence}.json").write_bytes(_encoded(adapter.snapshot()))
        results.append(result)
        if result.problems or not result.receipt.processes_stopped:
            break
    checks: Any = None
    shared_output: str | None = None
    if results and results[0].receipt.logs_sealed:
        try:
            checks = strict_json_loads(
                _read(root / "logs/command-1/stdout.bin").decode("utf-8")
            )
        except (ValueError, UnicodeError):
            checks = None
    if len(results) == 2 and results[1].receipt.logs_sealed:
        shared_output = _read(root / "logs/command-2/stdout.bin").decode(
            "utf-8", errors="replace"
        )
    required_checks = {
        "child_scratch_writable",
        "original_write",
        "original_git_write",
        "credential_read",
        "context_read",
        "database_read",
        "logs_write",
        "temporary_auth_read",
        "network_denied",
    }
    observed_permissions = (
        isinstance(checks, dict)
        and set(checks) == required_checks
        and all(value is True for value in checks.values())
        and shared_output == "shared build and cache verified\n"
    )
    passed = (
        observed_permissions
        and len(results) == len(commands)
        and all(
            result.exit_code == 0
            and not result.problems
            and result.receipt.processes_stopped
            and result.receipt.logs_sealed
            for result in results
        )
    )
    report = {
        "case": case,
        "passed": passed,
        "permission_status": "observed canary results only",
        "actual_checks": checks,
        "shared_output": shared_output,
        "stop_scope": policy.required_stop_scope,
        "all_descendants_stopped": None,
        "results": task_execution_value(tuple(results)),
    }
    (root / "summary.json").write_bytes(_encoded(report))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--output-dir", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--case", choices=(CASE,), required=True)
    run.add_argument("--codex-bin", default="codex")
    args = parser.parse_args(argv)
    if args.operation == "prepare":
        print(prepare_plan(args.output_dir))
        return 0
    result = asyncio.run(run_plan(args.plan, case=args.case, executable=args.codex_bin))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
