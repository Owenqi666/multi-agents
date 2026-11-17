"""Reference checks and deterministic assessment of validated review reports.

Reference verification checks complete source lines, preserving their contents
and indentation. CRLF/LF and a final line terminator do not change a quote's
meaning. It does not establish that the reviewer's diagnosis is correct.

Candidate duplicate groups are positional, not semantic deduplication. All
original opinions and cross-confirmation reasons remain available.
"""

from copy import deepcopy
from typing import Any, cast

from .events import thaw_json
from .review_input import ExecutedReviewInput, ReviewInput, review_source
from .snapshot import FrozenSnapshot

_LOCATION_FIELDS = ("side", "path", "line_start", "line_end", "category")
_BLOCKING = frozenset(("P0", "P1"))


def _lines(text: str) -> list[str]:
    # Git coordinates use LF, not Python's broader Unicode line separators.
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def check_report(report: dict[str, Any], snapshot: ReviewInput) -> dict[str, Any]:
    """Annotate a validated report without mutating or discarding its fields."""
    snapshot = review_source(snapshot)
    checked = deepcopy(report)
    files = {(item.side, item.path): item for item in snapshot.files}
    for finding in checked["findings"]:
        source = files.get((finding["side"], finding["path"]))
        if snapshot.repository is None:
            reason = "repository_unavailable"
        elif source is None:
            reason = "file_not_in_frozen_side"
        elif source.text is None:
            reason = "source_text_unavailable"
        else:
            lines = _lines(source.text)
            start, end = finding["line_start"], finding["line_end"]
            quote_lines = _lines(finding["quote"]) or [""]
            if end > len(lines):
                reason = "line_range_out_of_bounds"
            elif quote_lines != lines[start - 1 : end]:
                reason = "quote_does_not_match_complete_lines"
            else:
                reason = "complete_lines_match"
        finding["reference_status"] = (
            "verified" if reason == "complete_lines_match" else "unverified"
        )
        finding["reference_reason"] = reason
    return checked


def _test_evidence(snapshot: FrozenSnapshot) -> dict[str, Any]:
    current = [
        record for record in snapshot.tests if record.commit == snapshot.candidate
    ]
    observed = {record.command for record in current}
    missing = [
        command for command in snapshot.required_tests if command not in observed
    ]
    mismatched = [
        index
        for index, record in enumerate(snapshot.tests)
        if record.commit != snapshot.candidate
    ]
    failed = [
        index
        for index, record in enumerate(snapshot.tests)
        if record.commit == snapshot.candidate and record.exit_code != 0
    ]
    if (
        snapshot.repository is None
        or snapshot.candidate is None
        or not snapshot.required_tests
        or not current
        or missing
        or mismatched
    ):
        status = "incomplete"
    elif failed:
        status = "failed"
    else:
        status = "passed"
    return {
        "source": "user_supplied",
        "executed_by_workbench": False,
        "status": status,
        "required_commands": [list(command) for command in snapshot.required_tests],
        "missing_commands": [list(command) for command in missing],
        "mismatched_records": mismatched,
        "failed_records": failed,
    }


def _executed_test_evidence(value: ExecutedReviewInput) -> dict[str, Any]:
    suite = value.executed_suite
    declared = suite.prepared.checkout.input
    required = [
        {"sequence": index, "name": command.name, "argv": list(command.argv)}
        for index, command in enumerate(declared.commands, 1)
    ]
    records = []
    failed = []
    mismatched = []
    for index, command in enumerate(suite.commands):
        manifest = cast(dict[str, Any], thaw_json(command.manifest))
        actual = cast(dict[str, Any], manifest["actual_result"])
        sequence = manifest["sequence"]
        if (
            sequence != index + 1
            or sequence > len(required)
            or manifest["test_attempt"] != suite.test_attempt
            or manifest["task_run_id"] != declared.task_run_id
            or manifest["implementation_attempt"] != declared.implementation_attempt
            or manifest["input_hash"] != declared.input_hash
            or not manifest["qualified"]
            or manifest["process_stopped"] is not True
        ):
            mismatched.append(index)
        if actual["exit_code"] != 0:
            failed.append(index)
        records.append(
            {
                "sequence": sequence,
                "name": declared.commands[index].name
                if index < len(declared.commands)
                else None,
                "reported_exit_code": actual["reported_exit_code"],
                "exit_code": actual["exit_code"],
                "stop_scope": manifest["stop_scope"],
                "output_scope": manifest["output_scope"],
                "stdout": {
                    "byte_count": command.stdout.byte_count,
                    "sha256": command.stdout.sha256,
                },
                "stderr": {
                    "byte_count": command.stderr.byte_count,
                    "sha256": command.stderr.sha256,
                },
            }
        )
    observed = {record["sequence"] for record in records}
    missing = [item for item in required if item["sequence"] not in observed]
    status = (
        "incomplete"
        if missing or mismatched or not required or len(records) != len(required)
        else "failed"
        if failed
        else "passed"
    )
    return {
        "source": "workbench_executed",
        "executed_by_workbench": True,
        "status": status,
        "task_run_id": declared.task_run_id,
        "implementation_attempt": declared.implementation_attempt,
        "test_attempt": suite.test_attempt,
        "required_commands": required,
        "commands": records,
        "missing_commands": missing,
        "mismatched_records": mismatched,
        "failed_records": failed,
    }


def _groups(
    reports: dict[str, dict[str, Any]],
    expected_seats: tuple[str, ...],
    confirmations: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for seat in expected_seats:
        for finding in reports[seat]["findings"]:
            key = tuple(finding[field] for field in _LOCATION_FIELDS)
            group = grouped.setdefault(
                key,
                {
                    "location": {field: finding[field] for field in _LOCATION_FIELDS},
                    "members": [],
                    "confirmations": [],
                    "blocking": False,
                },
            )
            group["members"].append({"seat": seat, "finding": finding})
            group["blocking"] |= finding["severity"] in _BLOCKING
            group["confirmations"].extend(
                deepcopy(confirmation)
                for confirmation in confirmations
                if confirmation["target_seat"] == seat
                and confirmation["finding_id"] == finding["id"]
            )
    result = list(grouped.values())
    for group in result:
        present = {member["seat"] for member in group["members"]}
        group["independent_reports"] = {
            seat: "reported" if seat in present else "unconfirmed"
            for seat in expected_seats
        }
    return result


def assess(
    snapshot: ReviewInput,
    reports: dict[str, dict[str, Any]],
    expected_seats: tuple[str, ...],
    confirmations: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Assess only complete selected reports and explicit, validated evidence.

    The CLI binds confirmation identities to their runtime and validates their
    targets. This pure function trusts that boundary and never changes reports.
    Unverified blocking claims or substantive confirmation disagreement require
    a user decision. Verified unilateral blockers remain actionable even when
    another reviewer did not independently mention them.
    """
    checked = {
        seat: check_report(reports[seat], snapshot)
        for seat in expected_seats
        if seat in reports
    }
    missing = [seat for seat in expected_seats if seat not in checked]
    tests = (
        _executed_test_evidence(snapshot)
        if isinstance(snapshot, ExecutedReviewInput)
        else _test_evidence(snapshot)
    )
    result: dict[str, Any] = {
        "status": "incomplete",
        "reasons": [],
        "missing_seats": missing,
        "reports": checked,
        "groups": [],
        "tests": tests,
    }
    if missing:
        result["reasons"] = ["selected_reports_missing"]
        return result

    groups = _groups(checked, expected_seats, confirmations)
    result["groups"] = groups
    blockers = [
        member["finding"]
        for group in groups
        for member in group["members"]
        if member["finding"]["severity"] in _BLOCKING
    ]
    uncertain = any(finding["reference_status"] != "verified" for finding in blockers)
    disputed = any(
        confirmation["decision"] in ("disagree", "uncertain")
        for group in groups
        if group["blocking"]
        for confirmation in group["confirmations"]
    )
    reasons: list[str] = []
    if review_source(snapshot).repository is None:
        reasons.append("report_only_input")
    if tests["status"] == "incomplete":
        reasons.append("complete_candidate_test_evidence_missing")
    if uncertain:
        reasons.append("blocking_references_unverified")
    if disputed:
        reasons.append("blocking_confirmation_unresolved")
    if reasons:
        result["status"] = "user_decision"
    elif blockers or tests["status"] == "failed":
        result["status"] = "needs_rework"
    else:
        result["status"] = "ready"
    if blockers:
        reasons.append("blocking_findings_present")
    if tests["failed_records"]:
        reasons.append("candidate_tests_failed")
    result["reasons"] = reasons
    return result
