"""Versioned review decoding, with frozen executed evidence kept intact."""

import json
import math
import re
from dataclasses import fields
from pathlib import PurePath
from typing import Any, cast

from multi_agent.core.events import thaw_json
from multi_agent.core.review_input import (
    ExecutedCommand,
    ExecutedLog,
    ExecutedReviewInput,
    ExecutedSuite,
    IntegrationReviewInput,
    ReviewInput,
)
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.core.task_execution import task_execution_hash, task_execution_json
from multi_agent.core.task_test_events import TaskTestExecutionPrepared
from multi_agent.store import ReviewStore
from multi_agent.store.integration_store import _decode_member
from multi_agent.store.task_codec import decode_task_input
from multi_agent.store.task_test_codec import _prepared

from .snapshot import _check_paths, _path, _requirements, _string
from .task_test_evidence import (
    TaskTestCommandManifest,
    _result,
    _runtime_qualified,
    _stopped,
)

MAX_REVIEW_INPUT_BYTES = 128 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _object(value: object, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("review input requires a JSON object")
    if keys is not None and set(value) != keys:
        raise ValueError("review input fields differ from the frozen version")
    return cast(dict[str, Any], value)


def _array(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("review input requires a JSON array")
    return value


def _number(value: object, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("review input contains an invalid integer")
    return value


def _hash(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("review input contains an invalid SHA256")
    return value


def _log(value: object) -> ExecutedLog:
    content = _object(_object(value, {"content"})["content"], {"hex"})["hex"]
    if not isinstance(content, str):
        raise TypeError("executed log must use the frozen hex encoding")
    try:
        decoded = bytes.fromhex(content)
    except ValueError as error:
        raise ValueError("executed log has invalid hex bytes") from error
    if decoded.hex() != content:
        raise ValueError("executed log hex is not canonical")
    return ExecutedLog(content=decoded)


def _source(value: object) -> FrozenSnapshot:
    raw = _object(
        value,
        {
            "repository",
            "baseline",
            "candidate",
            "diff",
            "requirements",
            "acceptance",
            "tests",
            "files",
            "required_tests",
        },
    )
    for name in ("repository", "baseline", "candidate", "diff", "requirements"):
        _string(raw[name], f"review source {name}")
    _array(raw["acceptance"])
    _array(raw["files"])
    if raw["tests"] != [] or raw["required_tests"] != []:
        raise ValueError("executed review source cannot include supplied test evidence")
    for item in raw["files"]:
        entry = _object(item, {"side", "path", "text", "oid", "mode"})
        for name in ("side", "path", "oid", "mode"):
            _string(entry[name], f"review file {name}", nonempty=True)
        if entry["text"] is not None:
            _string(entry["text"], "review file text")
        if entry["side"] not in ("baseline", "candidate"):
            raise ValueError("review source has an unknown side")
    result = FrozenSnapshot.from_dict(raw)
    _requirements(result.requirements, result.acceptance)
    for side in ("baseline", "candidate"):
        _check_paths(item.path for item in result.files if item.side == side)
    return result


def _prepared_input(raw: object) -> TaskTestExecutionPrepared:
    value = _object(raw, {"kind", "checkout", "context", "policy", "scratch"})
    if value["kind"] != TaskTestExecutionPrepared.kind:
        raise ValueError("executed review has another preparation event")
    for name in ("checkout", "context", "policy", "scratch"):
        _object(value[name])
    checkout = value["checkout"]
    _object(checkout["input"])
    _array(checkout["controls"])
    _array(checkout["objects"])
    prepared = _prepared(value)
    if task_execution_json(prepared) != task_execution_json(value):
        raise ValueError("review preparation has noncanonical or unknown fields")
    declared = prepared.checkout.input
    for version in (
        declared.version,
        prepared.checkout.version,
        prepared.policy.version,
    ):
        if type(version) is not int or version != 1:
            raise ValueError("executed review has an unknown nested version")
    if prepared.checkout.layout != "standalone-shallow-git-v1":
        raise ValueError("executed review has another checkout layout")
    _number(declared.implementation_attempt)
    _number(declared.task_history_length, minimum=0)
    _hash(declared.task_history_hash)
    _number(prepared.checkout.test_history_length, minimum=0)
    _hash(prepared.checkout.test_history_hash)
    if prepared.checkout.observed_test_attempt is not None:
        _number(prepared.checkout.observed_test_attempt)
    for name in ("task_run_id", "task_id", "declaration_source", "source_kind"):
        _string(getattr(declared, name), name, nonempty=True)
    for digest in (
        declared.task_hash,
        declared.rules_hash,
        declared.task_input_hash,
        prepared.context.manifest_sha256,
        prepared.policy.executable_sha256,
    ):
        _hash(digest)
    for identity in (
        prepared.checkout.database_identity,
        prepared.checkout.database_parent_identity,
        prepared.checkout.root_identity,
        prepared.checkout.git_identity,
        prepared.context.identity,
        prepared.policy.executable_identity,
        prepared.scratch.identity,
        prepared.scratch.home_identity,
        prepared.scratch.tmp_identity,
        prepared.scratch.cache_identity,
    ):
        for coordinate in identity:
            _number(coordinate, minimum=0)
    for duration in (prepared.policy.timeout, prepared.policy.grace):
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise ValueError("review preparation has an invalid duration")
    _number(prepared.policy.output_bytes_limit)
    if not declared.commands or len({item.name for item in declared.commands}) != len(
        declared.commands
    ):
        raise ValueError("executed review commands must have unique names")
    for command in declared.commands:
        _string(command.name, "test name", nonempty=True)
        _string(command.executable, "test executable", nonempty=True)
        for argument in command.args:
            _string(argument, "test argument")
    return prepared


def _command(
    value: object,
    suite: dict[str, Any],
    prepared: TaskTestExecutionPrepared,
    sequence: int,
) -> ExecutedCommand:
    raw = _object(value, {"manifest", "stdout", "stderr"})
    manifest = _object(
        raw["manifest"], {field.name for field in fields(TaskTestCommandManifest)}
    )
    declared = prepared.checkout.input
    expected = {
        "version": 1,
        "task_run_id": declared.task_run_id,
        "implementation_attempt": declared.implementation_attempt,
        "test_attempt": suite["test_attempt"],
        "sequence": sequence,
        "input_hash": declared.input_hash,
        "prepared_hash": task_execution_hash(prepared),
        "process_stopped": True,
        "qualified": True,
        "validation_error": None,
        "stop_scope": prepared.policy.required_stop_scope,
        "output_scope": prepared.policy.output_scope,
        "command_group_gone": None,
        "all_descendants_stopped": None,
        "producer_stdout_eof": None,
        "producer_stderr_eof": None,
    }
    if any(
        task_execution_json(manifest[key]) != task_execution_json(item)
        for key, item in expected.items()
    ):
        raise ValueError("executed command manifest differs from its suite")
    _hash(manifest["sha256"])
    _hash(manifest["history_hash"])
    if _number(manifest["history_length"], minimum=0) > suite["test_history_length"]:
        raise ValueError("command archive is newer than frozen suite history")
    directory = PurePath(prepared.checkout.root).parent / "logs" / f"command-{sequence}"
    if manifest["path"] != str(directory / "execution.json"):
        raise ValueError("executed command archive path differs from preparation")
    actual, receipt = _result(manifest["actual_result"])
    code = actual["exit_code"]
    if not (
        actual["sequence"] == sequence
        and type(code) is int
        and 0 <= code <= 255
        and code != 124
        and code == actual["reported_exit_code"]
        and isinstance(actual["environment"], dict)
        and actual["environment"].get("cli_version") == "codex-cli 0.169.0"
        and isinstance(actual["exit_code_provenance"], str)
        and bool(actual["exit_code_provenance"])
        and actual["request_attempted"]
        and actual["request_settled"]
        and _stopped(actual, receipt) is True
        and receipt.resources_released
        and receipt.logs_sealed
        and receipt.outputs_complete
        and not actual["problems"]
        and not actual["timeout_observed"]
        and not actual["cancel_accepted"]
        and not actual["output_limit_reached"]
        and actual["stdout_cap_reached"] is not True
        and actual["stderr_cap_reached"] is not True
    ):
        raise ValueError("executed command lacks complete qualified runtime facts")
    logs = {PurePath(log.path).name: log for log in receipt.logs}
    if (
        set(logs) != {"stdout.bin", "stderr.bin", "wire.jsonl"}
        or len(receipt.logs) != 3
    ):
        raise ValueError("executed command must retain exactly three owned logs")
    result = ExecutedCommand(
        manifest=manifest, stdout=_log(raw["stdout"]), stderr=_log(raw["stderr"])
    )
    for name, log in logs.items():
        if log.path != str(directory / name) or log.identity is None:
            raise ValueError("executed log is outside its owned directory")
        _hash(log.sha256)
        _number(log.byte_count, minimum=0)
    for name, content in (("stdout", result.stdout), ("stderr", result.stderr)):
        log = logs[f"{name}.bin"]
        if (
            log.byte_count != content.byte_count
            or log.sha256 != content.sha256
            or actual[f"{name}_received_bytes"] != content.byte_count
        ):
            raise ValueError("frozen log bytes differ from the actual receipt")
    return result


def _integration_command(
    value: object,
    integration_id: str,
    candidate: str | None,
    test_attempt: int,
    sequence: int,
) -> ExecutedCommand:
    raw = _object(value, {"manifest", "stdout", "stderr"})
    manifest = _object(raw["manifest"])
    if (
        manifest["integration_id"] != integration_id
        or manifest["candidate"] != candidate
        or _number(manifest["test_attempt"]) != test_attempt
        or _number(manifest["sequence"]) != sequence
        or manifest["qualified"] is not True
        or manifest["process_stopped"] is not True
    ):
        raise ValueError("executed command differs from the frozen integration suite")
    declared = _object(manifest["command"], {"name", "executable", "args"})
    _string(declared["name"], "executed command name", nonempty=True)
    _string(declared["executable"], "executed command executable", nonempty=True)
    for argument in _array(declared["args"]):
        _string(argument, "executed command argument")
    actual, receipt = _result(manifest["actual_result"])
    if actual["sequence"] != sequence or not _runtime_qualified(actual, receipt):
        raise ValueError("integration command lacks complete qualified runtime facts")
    result = ExecutedCommand(
        manifest=manifest, stdout=_log(raw["stdout"]), stderr=_log(raw["stderr"])
    )
    logs = {PurePath(log.path).name: log for log in receipt.logs}
    for name, content in (("stdout", result.stdout), ("stderr", result.stderr)):
        log = logs[f"{name}.bin"]
        if log.byte_count != content.byte_count or log.sha256 != content.sha256:
            raise ValueError(
                "frozen integration log bytes differ from the actual receipt"
            )
    return result


def _decode_integration_review(
    raw: dict[str, Any], expected_id: str | None
) -> IntegrationReviewInput:
    _object(
        raw,
        {
            "version",
            "kind",
            "source",
            "integration_id",
            "test_attempt",
            "test_evidence_hash",
            "commands",
            "members",
            "test_change_paths",
        },
    )
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("unknown integration review input version")
    if len(task_execution_json(raw).encode("utf-8")) > MAX_REVIEW_INPUT_BYTES:
        raise ValueError(
            "executed integration review input exceeds the canonical byte budget"
        )
    source = _source(raw["source"])
    integration_id = _string(raw["integration_id"], "integration ID", nonempty=True)
    test_attempt = _number(raw["test_attempt"])
    commands = tuple(
        _integration_command(
            command, integration_id, source.candidate, test_attempt, index
        )
        for index, command in enumerate(_array(raw["commands"]), 1)
    )
    if not commands:
        raise ValueError("integration review requires a complete executed test suite")
    members = tuple(
        _decode_member(_object(member)) for member in _array(raw["members"])
    )
    if not members:
        raise ValueError("integration review requires the original member cards")
    # Hints name paths from both trees, which are materialized separately.
    paths = tuple(_path(path) for path in _array(raw["test_change_paths"]))
    result = IntegrationReviewInput(
        source=source,
        integration_id=integration_id,
        test_attempt=test_attempt,
        test_evidence_hash=_hash(raw["test_evidence_hash"]),
        commands=commands,
        members=members,
        test_change_paths=paths,
    )
    if task_execution_json(result.to_dict()) != task_execution_json(raw) or (
        expected_id is not None and result.snapshot_id != expected_id
    ):
        raise ValueError("integration review does not match its complete frozen hash")
    return result


def decode_review_input(
    value: object, *, expected_id: str | None = None
) -> ReviewInput:
    """Decode only stored bytes; this does not re-authorize a current source."""
    raw = _object(value)
    if "version" not in raw and "kind" not in raw:
        return FrozenSnapshot.from_dict(raw)
    if raw.get("kind") == "executed_integration_review":
        try:
            return _decode_integration_review(raw, expected_id)
        except (
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            RecursionError,
        ) as error:
            raise ValueError(
                "integration review input has an invalid nested shape"
            ) from error
    if (
        type(raw.get("version")) is not int
        or raw["version"] != 2
        or raw.get("kind") != "executed_task_review"
    ):
        raise ValueError("unknown review input kind or version")
    _object(raw, {"version", "kind", "source", "executed_suite"})
    try:
        if len(task_execution_json(raw).encode("utf-8")) > MAX_REVIEW_INPUT_BYTES:
            raise ValueError("executed review input exceeds the canonical byte budget")
        suite = _object(
            raw["executed_suite"],
            {
                "task_input",
                "prepared",
                "test_attempt",
                "test_history_length",
                "test_history_hash",
                "commands",
            },
        )
        _number(suite["test_attempt"])
        _number(suite["test_history_length"], minimum=0)
        _hash(suite["test_history_hash"])
        prepared = _prepared_input(suite["prepared"])
        task_raw = _object(
            suite["task_input"], {"card", "frozen_rules", "requirements", "created_at"}
        )
        task_input = decode_task_input(json.dumps(task_raw, ensure_ascii=False))
        if task_execution_json(task_input) != task_execution_json(task_raw):
            raise ValueError("original task input has unknown or noncanonical fields")
        declared = prepared.checkout.input
        if (
            task_execution_hash(task_input) != declared.task_input_hash
            or task_input.card.task_hash != declared.task_hash
            or task_input.frozen_rules.rules_hash != declared.rules_hash
            or task_input.card.id != declared.task_id
        ):
            raise ValueError("executed review does not contain its original task input")
        registry = {
            command.name: command for command in task_input.frozen_rules.rules.commands
        }
        if any(registry.get(command.name) != command for command in declared.commands):
            raise ValueError(
                "executed review argv differs from the frozen command registry"
            )
        source = _source(raw["source"])
        if (
            source.repository != prepared.checkout.repository
            or source.repository != task_input.frozen_rules.repository
            or source.baseline != task_input.card.baseline_commit
            or source.baseline != task_input.frozen_rules.baseline_commit
            or source.baseline != declared.candidate.delivery.baseline
            or source.candidate != declared.candidate.delivery.candidate
            or source.requirements != task_input.requirements
            or source.acceptance != task_input.card.acceptance_criteria
        ):
            raise ValueError("review source differs from the original task/candidate")
        commands = _array(suite["commands"])
        if len(commands) != len(declared.commands):
            raise ValueError("executed review must include every declared command")
        result = ExecutedReviewInput(
            source=source,
            executed_suite=ExecutedSuite(
                task_input=task_input,
                prepared=prepared,
                test_attempt=suite["test_attempt"],
                test_history_length=suite["test_history_length"],
                test_history_hash=suite["test_history_hash"],
                commands=tuple(
                    _command(item, suite, prepared, index)
                    for index, item in enumerate(commands, 1)
                ),
            ),
        )
        if task_execution_json(result.to_dict()) != task_execution_json(raw) or (
            expected_id is not None and result.snapshot_id != expected_id
        ):
            raise ValueError(
                "executed review input does not match its complete frozen hash"
            )
        return result
    except (
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise ValueError("executed review input has an invalid nested shape") from error


def load_review_input(store: ReviewStore, identifier: str) -> ReviewInput:
    return decode_review_input(
        thaw_json(store.load_snapshot(identifier)), expected_id=identifier
    )
