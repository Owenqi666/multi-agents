"""Prepare a reviewable permission plan; execute only an explicitly selected case.

Development tests replace the adapter/transport. Real execution is a separate
user-run acceptance step, never a side effect of preparing or importing a plan.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.cli.workspaces import (
    _git,
    create_workspace,
    executor_lock,
    remove_workspace,
)
from multi_agent.core.events import (
    Completed,
    Error,
    FailureCategory,
    SessionStarted,
    thaw_json,
)
from multi_agent.core.workspaces import WorkspaceRecord

from .base import Permission, SessionConfig
from .implementation_probe_cases import (
    BASELINE_BYTES,
    MODEL_CREATED_BYTES,
    MODEL_EDITED_BYTES,
    REPLACEMENT_BYTES,
    model_expectations,
    model_prompt,
    native_cases,
    parse_claude_attempts,
    parse_native_response,
)
from .implementation_runtime import ImplementationRuntimeAdapter
from .probe.recording import Recorder, Redactor
from .runtime import _Rpc, _Turn
from .runtime_decode import RuntimeFailure, object_value, result_value

PINNED_VERSIONS = {"codex": "codex-cli 0.169.0", "claude": "2.1.325 (Claude Code)"}
CASES = ("codex-native", "codex-write", "claude-write")
_MAX_JSON = 2 * 1024 * 1024


def _json_default(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported probe value: {type(value).__name__}")


def _encoded(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=2, default=_json_default
        )
        + "\n"
    ).encode("utf-8")


def _source_hashes() -> dict[str, str]:
    repository = Path(__file__).resolve().parents[3]
    protocol = repository / "protocol"
    if not protocol.is_dir():
        raise ValueError(
            "prepare this acceptance probe from the source checkout with protocol snapshots"
        )
    paths = sorted((repository / "src/multi_agent").rglob("*.py"))
    paths += sorted(path for path in protocol.rglob("*") if path.is_file())
    return {
        str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def case_definitions() -> dict[str, Any]:
    placeholder = WorkspaceRecord(
        "<COMMON>",
        "<SOURCE>",
        "0" * 40,
        "0" * 40,
        "<WORKDIR>",
        "<OWNER>",
        (0, 0),
        "<ADMIN>",
        (0, 0),
        (),
        "sha1",
    )
    native = native_cases(
        placeholder,
        Path("<EXTERNAL>"),
        Path("<LOG_CANARY>"),
        Path("<PRIVATE_AUTH_COPY>"),
    )
    return {
        "codex-native": {
            "provider": "codex",
            "model_calls": 0,
            "commands": [
                {
                    "id": case.id,
                    "argv": list(case.argv),
                    "operation": case.operation,
                    "observed_paths": list(case.observed_paths),
                }
                for case in native
            ],
        },
        "codex-write": {
            "provider": "codex",
            "model_calls": 1,
            "writes": ["<WORKDIR>/model-created.txt", "<WORKDIR>/edit.txt"],
            "negative_model_claims": False,
        },
        "claude-write": {
            "provider": "claude",
            "model_calls": 1,
            "writes": ["<WORKDIR>/model-created.txt", "<WORKDIR>/edit.txt"],
            "tool_expectations": model_expectations(placeholder, Path("<EXTERNAL>")),
            "permission_rejection_format": "unconfirmed; preserve actual tool metadata for review",
        },
    }


def _request(model: object, effort: object, provider: str) -> dict[str, str]:
    allowed = {"low", "medium", "high", "xhigh", "max"}
    if provider == "codex":
        allowed.add("ultra")
    if (
        not isinstance(model, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model) is None
    ):
        raise ValueError("model must be an explicit nonempty model identifier")
    if not isinstance(effort, str) or effort not in allowed:
        raise ValueError("effort is not in the pinned provider's supported vocabulary")
    return {"model": model, "effort": effort}


def _new_root(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise ValueError("probe output must not exist")
    root = path.parent.resolve(strict=True) / path.name
    if root != path.absolute():
        raise ValueError("probe output path must be canonical, without aliases")
    if any(part.rstrip(" .").casefold() == ".git" for part in root.parts):
        raise ValueError("probe output must be outside Git metadata")
    if any(
        (parent / ".git").exists() or (parent / ".git").is_symlink()
        for parent in root.parents
    ):
        raise ValueError("probe output must be outside existing worktrees")
    return root


def prepare_plan(
    root: Path,
    *,
    codex_model: str = "gpt-6.1-terra",
    codex_effort: str = "medium",
    claude_model: str = "claude-sonnet-5-5",
    claude_effort: str = "medium",
) -> dict[str, Any]:
    root = _new_root(root)
    plan = {
        "schema_version": 1,
        "owner": uuid4().hex,
        "root": str(root),
        "created_at": datetime.now(UTC).isoformat(),
        "source_hashes": _source_hashes(),
        "pinned_versions": PINNED_VERSIONS,
        "requests": {
            "codex": _request(codex_model, codex_effort, "codex"),
            "claude": _request(claude_model, claude_effort, "claude"),
        },
        "cases": case_definitions(),
    }
    # Normalize immutable records into plain JSON values for both API and disk.
    raw = _encoded(plan)
    plan = object_value(json.loads(raw), "prepared plan")
    root.mkdir(mode=0o700)
    info = root.lstat()
    owner = {
        "owner": plan["owner"],
        "root_identity": [info.st_dev, info.st_ino],
        "plan_sha256": hashlib.sha256(raw).hexdigest(),
    }
    with (root / ".probe-owner.json").open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(_encoded(owner))
    with (root / "plan.json").open("xb") as stream:
        stream.write(raw)
    return plan


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("probe JSON contains a duplicate key")
        result[key] = value
    return result


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("probe JSON must be a regular file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(_MAX_JSON + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_JSON:
        raise ValueError("probe JSON exceeds the size limit")
    value = json.loads(raw, object_pairs_hook=_unique)
    return object_value(value, "probe JSON"), raw


def _load_plan(path: Path, case: str) -> tuple[dict[str, Any], str]:
    if case not in CASES:
        raise ValueError("unknown probe case")
    if path.name != "plan.json" or path.is_symlink():
        raise ValueError("execute the original regular plan.json")
    root = path.parent.resolve(strict=True)
    if path.absolute() != root / "plan.json":
        raise ValueError("plan path must be canonical")
    plan, raw = _read_json(path)
    owner, _ = _read_json(root / ".probe-owner.json")
    info = root.lstat()
    expected_keys = {
        "schema_version",
        "owner",
        "root",
        "created_at",
        "source_hashes",
        "pinned_versions",
        "requests",
        "cases",
    }
    if (
        set(plan) != expected_keys
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
        or plan["root"] != str(root)
        or not isinstance(plan["owner"], str)
        or re.fullmatch(r"[0-9a-f]{32}", plan["owner"]) is None
        or not isinstance(plan["created_at"], str)
        or set(owner) != {"owner", "root_identity", "plan_sha256"}
        or owner["owner"] != plan["owner"]
        or owner["root_identity"] != [info.st_dev, info.st_ino]
        or owner["plan_sha256"] != hashlib.sha256(raw).hexdigest()
        or plan["source_hashes"] != _source_hashes()
        or plan["pinned_versions"] != PINNED_VERSIONS
        or plan["cases"] != json.loads(_encoded(case_definitions()))
    ):
        raise ValueError("probe plan is stale, changed, or no longer owned")
    requests = plan["requests"]
    if not isinstance(requests, dict) or set(requests) != {"codex", "claude"}:
        raise ValueError("invalid requested provider configuration")
    for provider, request in requests.items():
        if not isinstance(request, dict) or set(request) != {"model", "effort"}:
            raise ValueError("invalid requested model configuration")
        _request(request["model"], request["effort"], provider)
    return plan, hashlib.sha256(raw).hexdigest()


def _save(path: Path, report: dict[str, Any]) -> None:
    temporary = path.parent / (".report-" + uuid4().hex)
    with temporary.open("xb") as stream:
        stream.write(_encoded(report))
    temporary.replace(path)


def _file_state(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "size": None, "sha256": None}
    if stat.S_ISLNK(info.st_mode):
        content = os.readlink(path).encode("utf-8")
        kind = "symlink"
    elif stat.S_ISREG(info.st_mode):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            actual = os.fstat(fd)
            if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise ValueError("probe target changed while being observed")
            with os.fdopen(os.dup(fd), "rb") as stream:
                content = stream.read(_MAX_JSON + 1)
        finally:
            os.close(fd)
        if len(content) > _MAX_JSON:
            raise ValueError("probe target exceeds the size limit")
        kind = "file"
    else:
        return {
            "kind": "directory" if stat.S_ISDIR(info.st_mode) else "special",
            "size": info.st_size,
            "sha256": None,
        }
    return {
        "kind": kind,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _observe(paths: tuple[str, ...]) -> dict[str, Any]:
    return {path: _file_state(Path(path)) for path in paths}


def _expected_file(content: bytes) -> dict[str, Any]:
    return {
        "kind": "file",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _fixture_source(root: Path) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "--template=")
    entries = []
    for name, content in (
        ("baseline.txt", BASELINE_BYTES),
        ("edit.txt", BASELINE_BYTES),
        ("delete.txt", BASELINE_BYTES),
        ("replacement.txt", REPLACEMENT_BYTES),
    ):
        oid = (
            _git(
                source,
                "hash-object",
                "-w",
                "--no-filters",
                "--stdin",
                input_bytes=content,
            )
            .decode()
            .strip()
        )
        entries.append(f"100644 blob {oid}\t{name}\0".encode())
    tree = _git(source, "mktree", "-z", input_bytes=b"".join(entries)).decode().strip()
    baseline = (
        _git(source, "commit-tree", tree, "-m", "Synthetic permission probe baseline")
        .decode()
        .strip()
    )
    _git(source, "update-ref", "refs/heads/probe", baseline, "0" * len(baseline))
    _git(source, "symbolic-ref", "HEAD", "refs/heads/probe")
    return source, baseline


def _make_adapter(
    provider: str, *, workspace: WorkspaceRecord, log_dir: Path
) -> ImplementationRuntimeAdapter:
    return ImplementationRuntimeAdapter(
        provider,
        workspace=workspace,
        log_dir=log_dir,
        timeout=180,
        grace=2,
        output_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )


def _raw_rows(log_dir: Path) -> list[object]:
    rows: list[object] = []
    total = 0
    for path in sorted(log_dir.glob("*.jsonl")):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("runtime evidence must be a regular file")
            with os.fdopen(os.dup(fd), "rb") as stream:
                raw = stream.read(16 * _MAX_JSON + 1)
        finally:
            os.close(fd)
        total += len(raw)
        if total > 16 * _MAX_JSON:
            raise ValueError("runtime evidence exceeds the probe size limit")
        for line in raw.splitlines():
            if line.strip():
                rows.append(json.loads(line, object_pairs_hook=_unique))
    return rows


def _cleanup_diagnostic(rows: list[object]) -> bool:
    return any(
        isinstance(row, dict) and row.get("kind") == "cleanup_incomplete"
        for row in rows
    )


async def _run_native(
    adapter: ImplementationRuntimeAdapter,
    record: WorkspaceRecord,
    external: Path,
    log_dir: Path,
    report: dict[str, Any],
    report_path: Path,
) -> None:
    recorder = Recorder(log_dir / "codex-native.jsonl", Redactor())
    recorder.redactor.add_workdir(Path(record.root))
    state = _Turn(
        SessionConfig(
            "native-no-model", "medium", Permission.WORKSPACE_WRITE, Path(record.root)
        ),
        "",
    )
    isolation = None
    process = None
    report["cleanup"]["process_stopped"] = False
    try:
        isolation = adapter._create_isolation(state, recorder)
        version, code = await adapter._collect(
            [adapter.executable, "--version"],
            Path(record.root),
            recorder,
            isolation=isolation,
        )
        report["environment"] = {
            "cli_version": version.strip(),
            "version_exit_code": code,
        }
        if code != 0 or version.strip() != PINNED_VERSIONS["codex"]:
            raise ValueError("native probe requires the pinned Codex version")
        adapter._validate_turn_workspace(state)
        process = await adapter._spawn(
            [adapter.executable, "app-server", "--strict-config"],
            Path(record.root),
            recorder,
            pipe=True,
            isolation=isolation,
        )
        rpc = _Rpc(process)
        await rpc.initialize()
        await adapter._audit_codex(rpc, isolation)
        account = await rpc.request("account/read", {"refreshToken": False})
        if (
            not isinstance(account.get("account"), dict)
            or account["account"].get("type") != "chatgpt"
        ):
            raise ValueError("native probe requires an observed ChatGPT account")
        report["environment"]["account_type"] = account["account"]["type"]
        log_canary = log_dir / "private-canary.txt"
        log_canary.write_bytes(b"non-secret private log canary\n")
        cases = native_cases(
            record, external, log_canary, isolation.codex_home / "auth.json"
        )
        report["observations"] = [
            {"id": item.id, "argv": list(item.argv), "status": "incomplete"}
            for item in cases
        ]
        _save(report_path, report)
        for index, item in enumerate(cases):
            before = _observe(item.observed_paths)
            if item.operation == "reject" and any(
                value["kind"] == "missing" for value in before.values()
            ):
                raise ValueError(
                    "negative native target is absent; refusal would be inconclusive"
                )
            entry = report["observations"][index]
            entry["before"] = before
            response = parse_native_response(
                await rpc.request(
                    "command/exec",
                    {
                        "command": list(item.argv),
                        "cwd": record.root,
                        "timeoutMs": 10000,
                        "outputBytesCap": 8192,
                    },
                )
            )
            after = _observe(item.observed_paths)
            entry.update(
                after=after,
                result={
                    "exit_code": response.exit_code,
                    "stdout_sha256": hashlib.sha256(
                        response.stdout.encode("utf-8")
                    ).hexdigest(),
                    "stderr": recorder.redactor.redact(response.stderr),
                },
            )
            if item.operation == "reject":
                entry["status"] = (
                    "attempted_failed"
                    if response.exit_code != 0 and before == after
                    else "unexpected_success"
                )
            elif item.operation == "read":
                entry["status"] = (
                    "allowed"
                    if response.exit_code == 0
                    and response.stdout.encode("utf-8") == item.expected_bytes
                    and before == after
                    else "failed"
                )
            elif item.operation == "delete":
                entry["status"] = (
                    "allowed"
                    if response.exit_code == 0
                    and all(value["kind"] == "missing" for value in after.values())
                    else "failed"
                )
            else:
                entry["status"] = (
                    "allowed"
                    if response.exit_code == 0
                    and item.expected_bytes is not None
                    and all(
                        value == _expected_file(item.expected_bytes)
                        for value in after.values()
                    )
                    else "failed"
                )
            _save(report_path, report)
            if entry["status"] in ("failed", "unexpected_success"):
                report["status"] = "failed"
                break
        else:
            # Ordinary nonzero exits do not establish a permission-rejection
            # format. Preserve these observations for the live acceptance review.
            report["status"] = "review_required"
    finally:
        try:
            if isolation is not None:
                if process is not None:
                    await isolation.terminate(process, adapter.grace)
                    report["process_exit"] = process.child.returncode
                removed = isolation.cleanup()
                report["cleanup"]["process_stopped"] = removed
                if not removed:
                    report["cleanup"]["error"] = (
                        f"native isolation retained: {isolation.root}"
                    )
        finally:
            recorder.close()


async def _consume(
    adapter: ImplementationRuntimeAdapter, turn: Any, report: dict[str, Any]
) -> None:
    async for event in adapter.events(turn):
        report["events"].append(event)


async def _run_model(
    adapter: ImplementationRuntimeAdapter,
    case: str,
    record: WorkspaceRecord,
    external: Path,
    log_dir: Path,
    report: dict[str, Any],
    report_path: Path,
) -> None:
    provider = "claude" if case == "claude-write" else "codex"
    report["cleanup"]["process_stopped"] = False
    environment = await adapter.check_environment()
    report["environment"] = environment
    report["cleanup"]["process_stopped"] = True
    if (
        environment.cli_version != PINNED_VERSIONS[provider]
        or environment.logged_in is not True
    ):
        raise ValueError("model probe requires the pinned version and reported login")
    request = report["requested"]
    config = SessionConfig(
        request["model"],
        request["effort"],
        Permission.WORKSPACE_WRITE,
        Path(record.root),
    )
    session = await adapter.start_session(config)
    report["cleanup"]["process_stopped"] = False
    turn = await adapter.submit_turn(session, model_prompt(case, record, external))
    reader = asyncio.create_task(_consume(adapter, turn, report))
    try:
        await asyncio.shield(reader)
    except asyncio.CancelledError:
        try:
            async with asyncio.timeout(10):
                await adapter.cancel(turn)
                await asyncio.shield(reader)
                result = await adapter.result(turn)
                report["terminal"] = result.terminal
                report["process_exit"] = (
                    None
                    if result.process_exit is None
                    else result.process_exit.exit_code
                )
                report["cleanup"]["process_stopped"] = (
                    result.process_exit is not None
                    and not _cleanup_diagnostic(_raw_rows(log_dir))
                )
        except (
            TimeoutError,
            OSError,
            ValueError,
            RuntimeError,
            RuntimeFailure,
        ) as error:
            report["cleanup"]["error"] = f"cancel cleanup unconfirmed: {error}"
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        raise
    result = await adapter.result(turn)
    report["terminal"] = result.terminal
    report["process_exit"] = (
        None if result.process_exit is None else result.process_exit.exit_code
    )
    rows = _raw_rows(log_dir)
    report["cleanup"]["process_stopped"] = (
        result.process_exit is not None and not _cleanup_diagnostic(rows)
    )
    report["files_after"] = _observe(tuple(report["files_before"]))
    positive = {
        str(Path(record.root) / "model-created.txt"): _expected_file(
            MODEL_CREATED_BYTES
        ),
        str(Path(record.root) / "edit.txt"): _expected_file(MODEL_EDITED_BYTES),
    }
    positives_ok = all(
        report["files_after"][path] == expected for path, expected in positive.items()
    )
    unchanged = all(
        report["files_after"][path] == value
        for path, value in report["files_before"].items()
        if path not in positive
    )
    if (
        isinstance(result.terminal, Error)
        and result.terminal.failure_category == FailureCategory.CANCELLED
    ):
        report["status"] = "cancelled"
    elif (
        not positives_ok or not unchanged or not isinstance(result.terminal, Completed)
    ):
        report["status"] = "failed"
    elif not report["cleanup"]["process_stopped"]:
        report["status"] = "incomplete"
    elif case == "codex-write":
        report["status"] = "passed"
    else:
        identifiers = [
            event.session_id
            for event in report["events"]
            if isinstance(event, SessionStarted)
        ]
        if len(identifiers) != 1:
            report["status"] = "incomplete"
            return
        redactor = Redactor()
        redactor.add_workdir(Path(record.root))
        expected = tuple(
            replace(
                item,
                inputs=result_value(
                    redactor.redact(thaw_json(item.inputs)), structured=True
                ),
            )
            for item in model_expectations(record, external)
        )
        evidence = parse_claude_attempts(rows, expected, session_id=identifiers[0])
        report["observations"] = evidence
        report["status"] = (
            "incomplete"
            if evidence.incomplete_ids or evidence.issues
            else "review_required"
        )
    _save(report_path, report)


async def execute_plan(plan_path: Path, *, case: str) -> dict[str, Any]:
    try:
        plan, plan_hash = _load_plan(plan_path, case)
    except (OSError, UnicodeError) as error:
        raise ValueError(f"probe plan cannot be opened: {error}") from error
    case_root = Path(plan["root"]) / case
    try:
        case_root.mkdir(
            mode=0o700
        )  # Atomic reservation. Failed attempts are retained too.
    except OSError as error:
        raise ValueError(f"probe case output is unavailable: {error}") from error
    provider = "claude" if case == "claude-write" else "codex"
    report: dict[str, Any] = {
        "schema_version": 1,
        "case": case,
        "status": "running",
        "plan_sha256": plan_hash,
        "source_hashes": plan["source_hashes"],
        "requested": None if case == "codex-native" else plan["requests"][provider],
        "workspace": None,
        "baseline": None,
        "environment": None,
        "observations": [
            {**command, "status": "incomplete"}
            for command in plan["cases"]["codex-native"]["commands"]
        ]
        if case == "codex-native"
        else [],
        "terminal": None,
        "process_exit": None,
        "events": [],
        "files_before": {},
        "files_after": {},
        "cleanup": {"process_stopped": True, "workspace_removed": False, "error": None},
        "error": None,
    }
    report_path = case_root / "report.json"
    _save(report_path, report)
    try:
        source, baseline = _fixture_source(case_root)
        report["baseline"] = baseline
        external = case_root / "outside"
        external.mkdir()
        for name in ("read.txt", "write.txt"):
            (external / name).write_bytes(b"non-secret outside canary\n")
        log_dir = case_root / "logs"
        log_dir.mkdir()
        with executor_lock(source) as lease:
            record = create_workspace(lease, baseline, case_root / "workspace")
            report["workspace"] = record
            try:
                (Path(record.root) / "escape").symlink_to(
                    external, target_is_directory=True
                )
                observed = tuple(
                    str(path)
                    for path in (
                        Path(record.root) / "model-created.txt",
                        Path(record.root) / "edit.txt",
                        Path(record.root) / ".git",
                        Path(record.root) / "escape",
                        external / "read.txt",
                        external / "write.txt",
                        external / "model-new.txt",
                        external / "model-alias.txt",
                        Path(record.git_directory) / "HEAD",
                        Path(record.repository) / "config",
                    )
                )
                report["files_before"] = _observe(observed)
                _save(report_path, report)
                adapter = _make_adapter(provider, workspace=record, log_dir=log_dir)
                if case == "codex-native":
                    async with asyncio.timeout(180):
                        await _run_native(
                            adapter, record, external, log_dir, report, report_path
                        )
                else:
                    await _run_model(
                        adapter, case, record, external, log_dir, report, report_path
                    )
            finally:
                report["files_after"] = _observe(tuple(report["files_before"]))
                if report["cleanup"]["process_stopped"] is True:
                    try:
                        remove_workspace(lease, record)
                        report["cleanup"]["workspace_removed"] = True
                    except (OSError, ValueError) as error:
                        report["cleanup"]["error"] = str(error)
                else:
                    report["cleanup"]["error"] = (
                        report["cleanup"]["error"]
                        or "process stop unconfirmed; workspace retained"
                    )
    except asyncio.CancelledError:
        report["status"] = "cancelled"
        report["error"] = "probe caller cancelled"
        raise
    except (OSError, ValueError, RuntimeError, RuntimeFailure, TimeoutError) as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        report["error_notes"] = list(getattr(error, "__notes__", ()))
    finally:
        if report["cleanup"]["error"] and report["status"] in (
            "passed",
            "review_required",
        ):
            report["status"] = "incomplete"
        _save(report_path, report)
    return object_value(json.loads(_encoded(report)), "probe report")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--output",
        type=Path,
        help="prepare a plan in a new directory; does not run a runtime",
    )
    mode.add_argument(
        "--execute-plan",
        type=Path,
        help="explicitly execute one previously prepared plan case",
    )
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--codex-model", default="gpt-6.1-terra")
    parser.add_argument("--codex-effort", default="medium")
    parser.add_argument("--claude-model", default="claude-sonnet-5-5")
    parser.add_argument("--claude-effort", default="medium")
    args = parser.parse_args(argv)
    try:
        if args.output is not None:
            if args.case is not None:
                raise ValueError("--case is only valid with explicit execution")
            plan = prepare_plan(
                args.output,
                codex_model=args.codex_model,
                codex_effort=args.codex_effort,
                claude_model=args.claude_model,
                claude_effort=args.claude_effort,
            )
            print(
                json.dumps(
                    {"plan": str(Path(plan["root"]) / "plan.json"), "executed": False},
                    ensure_ascii=False,
                )
            )
            return 0
        if args.case is None:
            raise ValueError("explicit execution requires exactly one --case")
        report = asyncio.run(execute_plan(args.execute_plan, case=args.case))
        print(
            json.dumps(
                {
                    "case": report["case"],
                    "status": report["status"],
                    "report": str(args.execute_plan.parent / args.case / "report.json"),
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["status"] == "passed" else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"probe rejected: {error}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
