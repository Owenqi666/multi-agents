"""Review output is checked against frozen sources and explicit test evidence."""

from copy import deepcopy
from dataclasses import replace

import pytest

from multi_agent.adapters.report_validation import REPORT_SCHEMA, validate_report
from multi_agent.core.events import Completed
from multi_agent.core.reports import assess, check_report
from multi_agent.core.snapshot import FrozenFile, FrozenSnapshot
from multi_agent.core.snapshot import TestEvidence as Evidence

BASE = "a" * 40
CANDIDATE = "b" * 40
COMMAND = ("python", "-m", "pytest")


def snapshot() -> FrozenSnapshot:
    return FrozenSnapshot(
        repository="/repo/.git",
        baseline=BASE,
        candidate=CANDIDATE,
        diff="fixed diff",
        requirements="Handle zero input.",
        acceptance=("Zero input returns zero.",),
        tests=(Evidence(CANDIDATE, COMMAND, 0, "1 passed"),),
        files=(
            FrozenFile(
                "candidate",
                "value.py",
                "def f(x):\n    return 1 / x\n",
                "c" * 40,
                "100644",
            ),
            FrozenFile(
                "baseline", "deleted.py", "removed = True\n", "d" * 40, "100644"
            ),
            FrozenFile("candidate", "binary.dat", None, "e" * 40, "100644"),
        ),
        required_tests=(COMMAND,),
    )


def finding(**changes):
    result = {
        "id": "division",
        "severity": "P1",
        "category": "correctness",
        "side": "candidate",
        "path": "value.py",
        "line_start": 2,
        "line_end": 2,
        "quote": "    return 1 / x",
        "title": "Zero input raises",
        "impact": "The documented zero-input case raises an exception.",
        "rationale": "The denominator is zero when x is zero.",
    }
    result.update(changes)
    return result


def report(*findings):
    return {"summary": "Review completed.", "findings": list(findings)}


def test_valid_report_schema_and_frozen_runtime_containers() -> None:
    original = report(finding())
    validated = validate_report(Completed(original).result)
    assert validated == original
    assert isinstance(validated["findings"], list)
    validated["findings"][0]["title"] = "changed"
    assert original["findings"][0]["title"] == "Zero input raises"
    assert REPORT_SCHEMA["additionalProperties"] is False
    assert set(REPORT_SCHEMA["required"]) == {"summary", "findings"}
    item_schema = REPORT_SCHEMA["properties"]["findings"]["items"]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == set(finding())
    assert set(item_schema["properties"]) == set(finding())
    assert validate_report(report()) == report()


@pytest.mark.parametrize(
    "value",
    (
        None,
        [],
        {"summary": "ok"},
        {"summary": "ok", "findings": [], "extra": 1},
        {"summary": " ", "findings": []},
        {"summary": "ok", "findings": {}},
        report(finding(extra=1)),
        report(finding(severity="critical")),
        report(finding(side="HEAD")),
        report(finding(line_start=True)),
        report(finding(line_start=1.0)),
        report(finding(line_end=0)),
        report(finding(line_start=4, line_end=2)),
        report(finding(quote=None)),
        report(finding(path="")),
        report(finding(impact=" ")),
        report(finding(title="\ud800")),
        report(finding(), finding(title="Same ID, different finding")),
    ),
)
def test_report_boundary_rejects_malformed_output(value) -> None:
    with pytest.raises(ValueError):
        validate_report(value)


def test_reference_verification_preserves_original_opinion() -> None:
    deleted = finding(
        id="deletion",
        side="baseline",
        path="deleted.py",
        line_start=1,
        line_end=1,
        quote="removed = True",
        title="A baseline-only line",
    )
    original = report(finding(), deleted)
    untouched = deepcopy(original)
    checked = check_report(validate_report(original), snapshot())
    assert original == untouched
    for supplied, verified in zip(
        original["findings"], checked["findings"], strict=True
    ):
        assert all(verified[key] == value for key, value in supplied.items())
        assert verified["reference_status"] == "verified"
        assert verified["reference_reason"] == "complete_lines_match"


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"side": "candidate", "path": "deleted.py"}, "file_not_in_frozen_side"),
        ({"path": "/value.py"}, "file_not_in_frozen_side"),
        ({"path": "../value.py"}, "file_not_in_frozen_side"),
        ({"path": "binary.dat"}, "source_text_unavailable"),
        ({"line_end": 3}, "line_range_out_of_bounds"),
        ({"line_start": 3, "line_end": 3}, "line_range_out_of_bounds"),
        ({"quote": "return 1 / x"}, "quote_does_not_match_complete_lines"),
        ({"quote": "    return 1 / x "}, "quote_does_not_match_complete_lines"),
        ({"quote": "    return 1 / x\n\n"}, "quote_does_not_match_complete_lines"),
    ),
)
def test_unmatched_reference_is_retained_but_unverified(changes, reason) -> None:
    original = finding(**changes)
    checked = check_report(report(original), snapshot())["findings"][0]
    assert checked["reference_status"] == "unverified"
    assert checked["reference_reason"] == reason
    assert checked["quote"] == original["quote"]


def test_quotes_compare_complete_lines_preserving_whitespace() -> None:
    value = snapshot()
    source = replace(value.files[0], text="def f(x):\r\n    return 1 / x\r\n")
    value = replace(value, files=(source,))
    multiline = finding(line_start=1, quote="def f(x):\n    return 1 / x\n")
    assert (
        check_report(report(multiline), value)["findings"][0]["reference_status"]
        == "verified"
    )
    empty = replace(value, files=(replace(source, text=""),))
    empty_claim = finding(line_start=1, line_end=1, quote="")
    assert (
        check_report(report(empty_claim), empty)["findings"][0]["reference_status"]
        == "unverified"
    )
    blank_line = replace(value, files=(replace(source, text="\n"),))
    assert (
        check_report(report(empty_claim), blank_line)["findings"][0]["reference_status"]
        == "verified"
    )


def test_missing_report_prevents_any_overall_assessment() -> None:
    output = assess(snapshot(), {"one": report(finding())}, ("one", "two"))
    assert output["status"] == "incomplete"
    assert output["missing_seats"] == ["two"]
    assert output["groups"] == []
    assert output["reasons"] == ["selected_reports_missing"]
    assert output["reports"]["one"]["findings"][0]["reference_status"] == "verified"


@pytest.mark.parametrize("separator", ("\v", "\f", "\r", "\x85", "\u2028", "\u2029"))
def test_unicode_and_control_separators_do_not_change_git_line_numbers(
    separator,
) -> None:
    value = snapshot()
    source = replace(value.files[0], text=f"left{separator}right\n")
    value = replace(value, files=(source,))
    false_line = finding(line_start=2, line_end=2, quote="right")
    partial_line = finding(line_start=1, line_end=1, quote="left")
    full_line = finding(line_start=1, line_end=1, quote=f"left{separator}right")
    assert (
        check_report(report(false_line), value)["findings"][0]["reference_status"]
        == "unverified"
    )
    assert (
        check_report(report(partial_line), value)["findings"][0]["reference_status"]
        == "unverified"
    )
    assert (
        check_report(report(full_line), value)["findings"][0]["reference_status"]
        == "verified"
    )


def test_clean_single_or_dual_report_with_complete_tests_is_ready() -> None:
    for seats in (("one",), ("one", "two")):
        output = assess(snapshot(), {seat: report() for seat in seats}, seats)
        assert output["status"] == "ready"
        assert output["tests"]["status"] == "passed"
        assert output["tests"]["source"] == "user_supplied"
        assert output["tests"]["executed_by_workbench"] is False


@pytest.mark.parametrize(
    "value",
    (
        replace(snapshot(), required_tests=()),
        replace(snapshot(), tests=()),
        replace(snapshot(), tests=(Evidence(BASE, COMMAND, 0, "old"),)),
        replace(snapshot(), required_tests=(COMMAND, ("other",))),
        replace(snapshot(), tests=(Evidence(CANDIDATE, ("unrelated",), 0, "pass"),)),
        replace(
            snapshot(), tests=snapshot().tests + (Evidence(BASE, COMMAND, 0, "old"),)
        ),
    ),
)
def test_missing_or_mismatched_test_evidence_needs_user_decision(value) -> None:
    output = assess(value, {"one": report()}, ("one",))
    assert output["status"] == "user_decision"
    assert output["tests"]["status"] == "incomplete"
    assert "complete_candidate_test_evidence_missing" in output["reasons"]


def test_failed_candidate_tests_require_rework_even_with_clean_reports() -> None:
    value = replace(snapshot(), tests=(Evidence(CANDIDATE, COMMAND, 1, "failed"),))
    output = assess(value, {"one": report()}, ("one",))
    assert output["status"] == "needs_rework"
    assert output["tests"]["failed_records"] == [0]
    assert "candidate_tests_failed" in output["reasons"]


def test_later_passing_evidence_does_not_erase_a_failed_record() -> None:
    value = replace(
        snapshot(), tests=(Evidence(CANDIDATE, COMMAND, 1, "failed"), *snapshot().tests)
    )
    assert assess(value, {"one": report()}, ("one",))["status"] == "needs_rework"


def test_diff_only_is_report_only_and_cannot_verify_references() -> None:
    value = replace(
        snapshot(),
        repository=None,
        baseline=None,
        candidate=None,
        files=(),
        tests=(),
        required_tests=(),
    )
    output = assess(value, {"one": report(finding())}, ("one",))
    assert output["status"] == "user_decision"
    assert "report_only_input" in output["reasons"]
    assert (
        output["reports"]["one"]["findings"][0]["reference_reason"]
        == "repository_unavailable"
    )


def test_single_sided_verified_blocker_is_not_discarded() -> None:
    output = assess(
        snapshot(), {"one": report(finding()), "two": report()}, ("one", "two")
    )
    assert output["status"] == "needs_rework"
    assert output["groups"][0]["independent_reports"] == {
        "one": "reported",
        "two": "unconfirmed",
    }


def test_positional_duplicates_keep_each_seat_original_and_evidence() -> None:
    original = {
        "one": report(finding()),
        "two": report(finding(id="other", title="Independent wording")),
    }
    untouched = deepcopy(original)
    output = assess(snapshot(), original, ("one", "two"))
    assert original == untouched
    assert len(output["groups"]) == 1
    members = output["groups"][0]["members"]
    assert [member["seat"] for member in members] == ["one", "two"]
    assert [member["finding"]["title"] for member in members] == [
        "Zero input raises",
        "Independent wording",
    ]
    assert all(
        member["finding"]["reference_status"] == "verified" for member in members
    )
    different = report(finding(id="different", category="security"))
    assert (
        len(
            assess(
                snapshot(), {"one": original["one"], "two": different}, ("one", "two")
            )["groups"]
        )
        == 2
    )


@pytest.mark.parametrize(
    ("decision", "status"),
    (
        ("agree", "needs_rework"),
        ("disagree", "user_decision"),
        ("uncertain", "user_decision"),
    ),
)
def test_cross_confirmation_preserves_reason_and_never_erases_blocker(
    decision, status
) -> None:
    confirmation = {
        "seat": "two",
        "target_seat": "one",
        "finding_id": "division",
        "decision": decision,
        "reason": "Independent assessment of the cited zero-input path.",
    }
    output = assess(
        snapshot(),
        {"one": report(finding()), "two": report()},
        ("one", "two"),
        (confirmation,),
    )
    assert output["status"] == status
    assert output["groups"][0]["confirmations"] == [confirmation]
    assert len(output["groups"][0]["members"]) == 1
    output["groups"][0]["confirmations"][0]["reason"] = "mutated output"
    assert confirmation["reason"].startswith("Independent")


def test_agreement_cannot_promote_an_unverified_reference() -> None:
    confirmation = {
        "seat": "two",
        "target_seat": "one",
        "finding_id": "division",
        "decision": "agree",
        "reason": "Agrees with claim.",
    }
    output = assess(
        snapshot(),
        {"one": report(finding(quote="invented")), "two": report()},
        ("one", "two"),
        (confirmation,),
    )
    assert output["status"] == "user_decision"
    assert "blocking_references_unverified" in output["reasons"]


def test_nonblocking_findings_do_not_become_blockers_by_deduplication() -> None:
    output = assess(
        snapshot(),
        {
            "one": report(finding(severity="P2")),
            "two": report(finding(id="minor", severity="P3")),
        },
        ("one", "two"),
    )
    assert output["status"] == "ready"
    assert output["groups"][0]["blocking"] is False
