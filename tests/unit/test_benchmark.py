"""Fixed benchmark fixtures and evidence-gated scoring; no model calls are made."""

import copy
import hashlib
import json
import runpy
from pathlib import Path

import pytest

from multi_agent.adapters.report_validation import validate_report
from multi_agent.cli import benchmark
from multi_agent.cli.main import main
from multi_agent.core.events import thaw_json
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.store import ReviewStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "benchmark"
MODES = {
    "codex-only": ("codex",),
    "claude-only": ("claude",),
    "dual": ("codex", "claude"),
    "official-plugin": ("plugin",),
}
METRICS = {
    "valid_discoveries",
    "known_found",
    "additional_valid",
    "missed",
    "false_positives",
    "duplicates",
}


def oracle():
    return json.loads((FIXTURES / "oracle.json").read_text())


def visible_case(case_id):
    return next(case for case in benchmark.catalog() if case["case_id"] == case_id)


def finding(case_id="B01", identifier="finding-1", line=2):
    source = visible_case(case_id)["candidate"].splitlines()
    return {
        "id": identifier,
        "severity": "P1",
        "category": "contract",
        "side": "candidate",
        "path": "case.py",
        "line_start": line,
        "line_end": line,
        "quote": source[line - 1],
        "title": "Reported defect",
        "impact": "A documented caller gets the wrong result",
        "rationale": "The reviewed line changes the required behavior",
    }


def report(*findings):
    return {
        "summary": "Synthetic imported report for scoring tests",
        "findings": list(findings),
    }


def judgments(*records):
    return {"schema_version": 1, "source": "user_supplied", "records": list(records)}


def judgment(
    case_id="B01",
    mode="codex-only",
    seat="codex",
    finding_id="finding-1",
    verdict="known",
    issue_id="D01",
):
    return {
        "case_id": case_id,
        "mode": mode,
        "seat": seat,
        "finding_id": finding_id,
        "verdict": verdict,
        "issue_id": issue_id,
        "reason": "The human review matched the reported diagnosis to the stated issue",
    }


def row(records, case_id="B01", mode="codex-only"):
    return next(
        item for item in records if item["case_id"] == case_id and item["mode"] == mode
    )


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("benchmark")
    output = directory / "visible"
    data = directory / "data"
    manifest = benchmark.prepare(output, data)
    return output, data, manifest


@pytest.fixture(scope="module")
def second_prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("benchmark-second")
    output = directory / "visible"
    data = output / "data"
    return output, data, benchmark.prepare(output, data)


def bound_judgments(prepared, results, labels):
    """Bind synthetic manual labels, preserving explicitly supplied stale bindings."""
    bound = copy.deepcopy(labels)
    for label in bound["records"]:
        cases = [
            case for case in prepared[2]["cases"] if case["case_id"] == label["case_id"]
        ]
        selected = [
            item
            for item in results["records"]
            if item["case_id"] == label["case_id"] and item["mode"] == label["mode"]
        ]
        source = selected[0]["reports"].get(label["seat"]) if selected else None
        digest = (
            hashlib.sha256(
                json.dumps(
                    validate_report(source),
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if source is not None
            else "0" * 64
        )
        label.setdefault("snapshot_id", cases[0]["snapshot_id"] if cases else "0" * 64)
        label.setdefault("report_sha256", digest)
    return bound


def score(prepared, results, labels=None):
    _, data, manifest = prepared
    labels = bound_judgments(
        prepared, results, judgments() if labels is None else labels
    )
    with ReviewStore(data / "reviews.sqlite3") as store:
        return benchmark.score(manifest, results, labels, store)


def completed(prepared, *, case_id="B01", mode="codex-only", reports=None):
    values = benchmark.template(prepared[2])
    target = row(values["records"], case_id, mode)
    target["status"] = "completed"
    target["reports"] = (
        reports if reports is not None else {seat: report() for seat in MODES[mode]}
    )
    return values


def assert_unknown_metrics(value):
    assert set(value["metrics"]) == METRICS
    assert all(metric is None for metric in value["metrics"].values())


def test_fixed_cases_have_six_defects_two_clean_and_heldout(tmp_path):
    cases = benchmark.catalog()
    expected = oracle()["cases"]
    assert [case["case_id"] for case in cases] == [f"B{i:02}" for i in range(1, 9)]
    assert [case["case_id"] for case in cases if case["split"] == "heldout"] == [
        "B07",
        "B08",
    ]
    assert sum(case["split"] == "development" for case in cases) == 6
    assert sum(bool(item["truths"]) for item in expected.values()) == 6
    assert any(item["possible_unilateral"] for item in expected.values())
    for case in cases:
        for side in ("baseline", "candidate"):
            # Only committed fixed programs are written to this test directory;
            # score() never executes imported reports or target repositories.
            source = tmp_path / f"{case['case_id']}-{side}.py"
            source.write_text(case[side], encoding="utf-8")
            namespace = runpy.run_path(str(source))
            function = next(
                value
                for name, value in namespace.items()
                if not name.startswith("__") and callable(value)
            )
            checks = []
            for check in expected[case["case_id"]]["checks"]:
                try:
                    value = function(*copy.deepcopy(check["args"]))
                except ZeroDivisionError:
                    checks.append(False)
                else:
                    checks.append(value == check["expected"])
            if side == "baseline" or not expected[case["case_id"]]["truths"]:
                assert all(checks), (case["case_id"], side, checks)
            else:
                assert not all(checks), (case["case_id"], checks)


def test_prepare_freezes_visible_inputs_without_oracle(prepared):
    output, data, manifest = prepared
    assert len(manifest["cases"]) == 8
    assert benchmark.load_manifest(output / "manifest.json") == manifest
    assert (output / "review-prompt.txt").is_file()
    assert not list(output.rglob("oracle.json"))
    hidden = oracle()["cases"]
    with ReviewStore(data / "reviews.sqlite3") as store:
        for case in manifest["cases"]:
            payload = thaw_json(store.load_snapshot(case["snapshot_id"]))
            serialized = json.dumps(payload)
            assert '"truths"' not in serialized
            assert '"checks"' not in serialized
            assert '"possible_unilateral"' not in serialized
            assert '"issue_id"' not in serialized
            for truth in hidden[case["case_id"]]["truths"]:
                assert truth["description"] not in serialized
                assert truth["issue_id"] not in serialized
            frozen = FrozenSnapshot.from_dict(payload)
            assert frozen.snapshot_id == case["snapshot_id"]
            assert {item.path for item in frozen.files} == {"case.py"}
            visible = visible_case(case["case_id"])
            assert frozen.requirements == visible["requirements"]
            assert (
                next(item.text for item in frozen.files if item.side == "candidate")
                == visible["candidate"]
            )
            assert frozen.tests == ()
            bundle = output / "review-inputs" / case["case_id"]
            assert {
                str(path.relative_to(bundle))
                for path in bundle.rglob("*")
                if path.is_file()
            } == {
                "source/baseline/case.py",
                "source/candidate/case.py",
                "source/snapshot.json",
                "review-input.json",
                "diff.patch",
            }
            review_input = frozen.to_dict()
            review_input.pop("files")
            review_input.pop("diff")
            assert (bundle / "review-input.json").read_bytes() == json.dumps(
                review_input, ensure_ascii=False
            ).encode("utf-8")
            assert (bundle / "diff.patch").read_bytes() == frozen.diff.encode("utf-8")
            assert (
                json.loads((bundle / "source" / "snapshot.json").read_text())[
                    "snapshot_id"
                ]
                == frozen.snapshot_id
            )
            for side in ("baseline", "candidate"):
                assert (bundle / "source" / side / "case.py").read_bytes() == visible[
                    side
                ].encode("utf-8")
            for path in (bundle, *bundle.rglob("*")):
                assert path.stat().st_mode & 0o222 == 0
                if path.is_file():
                    content = path.read_text()
                    for truth in hidden[case["case_id"]]["truths"]:
                        assert truth["description"] not in content
                        assert truth["issue_id"] not in content
    prompt = (output / "review-prompt.txt").read_text()
    for item in hidden.values():
        for truth in item["truths"]:
            assert truth["description"] not in prompt
            assert truth["issue_id"] not in prompt


def test_prepare_stable_commits_and_visible_hashes_but_distinct_repository_identity(
    prepared, second_prepared
):
    _, original_data, original = prepared
    second_output, second_data, second = second_prepared
    assert second_data == second_output / "data"
    assert (second_data / "reviews.sqlite3").is_file()
    with (
        ReviewStore(original_data / "reviews.sqlite3") as first_store,
        ReviewStore(second_data / "reviews.sqlite3") as second_store,
    ):
        for left, right in zip(original["cases"], second["cases"], strict=True):
            assert left["case_id"] == right["case_id"]
            assert left["visible_input_hash"] == right["visible_input_hash"]
            assert left["snapshot_id"] != right["snapshot_id"]
            first = first_store.load_snapshot(left["snapshot_id"])
            other = second_store.load_snapshot(right["snapshot_id"])
            assert first["baseline"] == other["baseline"]
            assert first["candidate"] == other["candidate"]
            assert first["repository"] != other["repository"]
    assert original["prompt_sha256"] == second["prompt_sha256"]


@pytest.mark.parametrize(
    "relative", ["review-inputs", "review-inputs/B01", "review-inputs/B01/data"]
)
def test_prepare_rejects_database_inside_model_inputs_before_creating_output(
    tmp_path, relative
):
    output = tmp_path / "visible"
    with pytest.raises(ValueError, match="outside review input bundles"):
        benchmark.prepare(output, output / relative)
    assert not output.exists()


def test_template_has_four_identical_input_modes_and_no_fabricated_runs(prepared):
    manifest = prepared[2]
    values = benchmark.template(manifest)
    assert values["schema_version"] == 1 and values["source"] == "user_supplied"
    assert len(values["records"]) == 32
    for case in manifest["cases"]:
        selected = [
            item for item in values["records"] if item["case_id"] == case["case_id"]
        ]
        assert {item["mode"] for item in selected} == set(MODES)
        assert {item["snapshot_id"] for item in selected} == {case["snapshot_id"]}
        assert {item["visible_input_hash"] for item in selected} == {
            case["visible_input_hash"]
        }
        assert {item["prompt_template_version"] for item in selected} == {
            manifest["prompt_template_version"]
        }
        assert {item["prompt_sha256"] for item in selected} == {
            manifest["prompt_sha256"]
        }
        assert all(
            item["status"] == "not_run" and item["reports"] == {} for item in selected
        )
        assert all(
            item["duration_seconds"] is None and item["usage"] is None
            for item in selected
        )
    scored = score(prepared, values)
    assert scored["status"] == "not_run"
    assert scored["executed_models"] is False and scored["recommendation"] is None
    assert set(scored["summaries"]) == {"development", "heldout", "all"}
    for item in scored["cases"]:
        assert item["status"] == "not_run"
        assert_unknown_metrics(item)


@pytest.mark.parametrize("mode", MODES)
def test_all_modes_accept_only_manual_judged_verified_discoveries(prepared, mode):
    findings = {
        seat: report(finding(identifier=f"{seat}-finding")) for seat in MODES[mode]
    }
    values = completed(prepared, mode=mode, reports=findings)
    before = copy.deepcopy(values)
    labels = judgments(
        *(
            judgment(mode=mode, seat=seat, finding_id=f"{seat}-finding")
            for seat in MODES[mode]
        )
    )
    scored = score(prepared, values, labels)
    result = row(scored["cases"], mode=mode)
    assert result["status"] == "scored"
    assert result["metrics"] == {
        "valid_discoveries": 1,
        "known_found": 1,
        "additional_valid": 0,
        "missed": 0,
        "false_positives": 0,
        "duplicates": len(MODES[mode]) - 1,
    }
    assert scored["status"] == "imported_user_evidence"
    assert scored["executed_models"] is False and scored["recommendation"] is None
    assert result["duration_seconds"] is None and result["usage"] is None
    assert values == before


def test_scoring_uses_semantic_issue_ids_not_position_as_validity(prepared):
    values = completed(
        prepared,
        mode="dual",
        reports={
            "codex": report(finding(identifier="known"), finding(identifier="extra")),
            "claude": report(
                finding(identifier="duplicate", line=3),
                finding(identifier="noise", line=1),
            ),
        },
    )
    labels = judgments(
        judgment(mode="dual", finding_id="known"),
        judgment(
            mode="dual", finding_id="extra", verdict="additional_valid", issue_id="X01"
        ),
        judgment(mode="dual", seat="claude", finding_id="duplicate"),
        judgment(
            mode="dual",
            seat="claude",
            finding_id="noise",
            verdict="false_positive",
            issue_id="FP01",
        ),
    )
    result = row(score(prepared, values, labels)["cases"], mode="dual")
    assert result["status"] == "scored"
    assert result["metrics"] == {
        "valid_discoveries": 2,
        "known_found": 1,
        "additional_valid": 1,
        "missed": 0,
        "false_positives": 1,
        "duplicates": 1,
    }
    assert len(result["reports"]["codex"]["findings"]) == 2
    assert len(result["reports"]["claude"]["findings"]) == 2


def test_empty_completed_report_counts_known_miss_not_unrun(prepared):
    result = row(score(prepared, completed(prepared))["cases"])
    assert result["status"] == "scored"
    assert result["metrics"]["missed"] == 1
    assert result["metrics"]["valid_discoveries"] == 0
    clean = row(
        score(prepared, completed(prepared, case_id="B05"))["cases"], case_id="B05"
    )
    assert clean["status"] == "scored"
    assert clean["metrics"]["missed"] == 0


@pytest.mark.parametrize(
    "reason", ["no-judgment", "unresolved", "unverified-reference", "missing-seat"]
)
def test_incomplete_evidence_stays_pending_with_null_metrics(prepared, reason):
    reported = finding()
    labels = judgments(judgment())
    mode = "codex-only"
    if reason == "no-judgment":
        labels = judgments()
    elif reason == "unresolved":
        labels = judgments(judgment(verdict="unresolved", issue_id=None))
    elif reason == "unverified-reference":
        reported["quote"] = "not the frozen source line"
    else:
        mode = "dual"
        labels = judgments(judgment(mode=mode))
    values = completed(prepared, mode=mode, reports={"codex": report(reported)})
    result = row(score(prepared, values, labels)["cases"], mode=mode)
    assert result["status"] == "pending"
    assert_unknown_metrics(result)


def test_failed_run_retains_reported_duration_without_metrics(prepared):
    values = benchmark.template(prepared[2])
    target = row(values["records"])
    target["status"] = "failed"
    target["duration_seconds"] = 1.25
    result = row(score(prepared, values)["cases"])
    assert result["status"] == "failed"
    assert result["duration_seconds"] == 1.25
    assert result["usage"] is None
    assert_unknown_metrics(result)


def test_reported_usage_and_unknown_actual_configuration_are_preserved(prepared):
    values = completed(prepared)
    target = row(values["records"])
    target["usage"] = {
        "codex": {
            "reported_model": None,
            "reported_effort": None,
            "counts": {"input_tokens": 12, "output_tokens": 4},
        }
    }
    result = row(score(prepared, values)["cases"])
    assert result["usage"] == target["usage"]
    assert result["duration_seconds"] is None


@pytest.mark.parametrize(
    "field,bad",
    [
        ("case_id", "UNKNOWN"),
        ("mode", "unknown-mode"),
        ("snapshot_id", "0" * 64),
        ("visible_input_hash", "0" * 64),
        ("prompt_template_version", "wrong-template"),
        ("prompt_sha256", "0" * 64),
    ],
)
def test_unknown_or_mismatched_import_identity_is_rejected(prepared, field, bad):
    values = completed(prepared)
    row(values["records"])[field] = bad
    with pytest.raises(ValueError):
        score(prepared, values)


def test_duplicate_case_mode_result_is_rejected(prepared):
    values = completed(prepared)
    values["records"].append(copy.deepcopy(values["records"][0]))
    with pytest.raises(ValueError):
        score(prepared, values)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("case_id", "UNKNOWN"),
        ("mode", "unknown"),
        ("seat", "claude"),
        ("finding_id", "missing"),
        ("issue_id", "NOT-A-KNOWN-ISSUE"),
    ],
)
def test_unknown_judgment_identity_is_rejected(prepared, field, bad):
    values = completed(prepared, reports={"codex": report(finding())})
    label = judgment()
    label[field] = bad
    with pytest.raises(ValueError):
        score(prepared, values, judgments(label))


def test_duplicate_manual_judgment_is_rejected(prepared):
    values = completed(prepared, reports={"codex": report(finding())})
    label = judgment()
    with pytest.raises(ValueError):
        score(prepared, values, judgments(label, copy.deepcopy(label)))


@pytest.mark.parametrize(
    "malformed", ["duplicate-case", "unknown-case", "wrong-dataset", "wrong-prompt"]
)
def test_manifest_validation_rejects_changed_identity(prepared, tmp_path, malformed):
    manifest = copy.deepcopy(prepared[2])
    if malformed == "duplicate-case":
        manifest["cases"][1] = copy.deepcopy(manifest["cases"][0])
    elif malformed == "unknown-case":
        manifest["cases"][0]["case_id"] = "UNKNOWN"
    elif malformed == "wrong-dataset":
        manifest["dataset_version"] = "other-dataset"
    else:
        manifest["prompt_template_version"] = "other-prompt"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        benchmark.load_manifest(path)


def test_cross_manifest_snapshot_cannot_be_mixed_even_with_same_visible_hash(
    prepared, second_prepared
):
    _, _, second = second_prepared
    values = completed(prepared)
    row(values["records"])["snapshot_id"] = second["cases"][0]["snapshot_id"]
    with pytest.raises(ValueError):
        score(prepared, values)


def test_semantically_duplicate_false_positives_are_counted_once(prepared):
    values = completed(
        prepared,
        mode="dual",
        reports={
            "codex": report(finding(identifier="left-noise")),
            "claude": report(finding(identifier="right-noise", line=3)),
        },
    )
    labels = judgments(
        judgment(
            mode="dual",
            finding_id="left-noise",
            verdict="false_positive",
            issue_id="FP01",
        ),
        judgment(
            mode="dual",
            seat="claude",
            finding_id="right-noise",
            verdict="false_positive",
            issue_id="FP01",
        ),
    )
    result = row(score(prepared, values, labels)["cases"], mode="dual")
    assert result["status"] == "scored"
    assert result["metrics"]["false_positives"] == 1
    assert result["metrics"]["known_found"] == 0
    assert result["metrics"]["missed"] == 1


def test_omitted_results_remain_not_run_without_zero_quality_metrics(prepared):
    result = score(
        prepared, {"schema_version": 1, "source": "user_supplied", "records": []}
    )
    assert result["status"] == "not_run"
    assert len(result["cases"]) == 32
    for case in result["cases"]:
        assert case["status"] == "not_run"
        assert_unknown_metrics(case)
    for split in result["summaries"].values():
        for summary in split.values():
            assert all(value is None for value in summary["metrics"].values())
            assert summary["duration_seconds"] is None


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "12"])
def test_invalid_measurements_are_not_accepted_as_runtime_evidence(prepared, value):
    values = completed(prepared)
    row(values["records"])["duration_seconds"] = value
    with pytest.raises((TypeError, ValueError)):
        score(prepared, values)
    row(values["records"])["duration_seconds"] = None
    row(values["records"])["usage"] = {
        "codex": {
            "reported_model": None,
            "reported_effort": None,
            "counts": {"tokens": value},
        }
    }
    with pytest.raises((TypeError, ValueError)):
        score(prepared, values)


def test_not_run_cannot_contain_fabricated_measurements(prepared):
    values = benchmark.template(prepared[2])
    row(values["records"])["duration_seconds"] = 0
    with pytest.raises(ValueError):
        score(prepared, values)


def test_imports_must_state_user_supplied_provenance(prepared):
    values = completed(prepared)
    values["source"] = "verified_runtime"
    with pytest.raises(ValueError):
        score(prepared, values)
    values["source"] = "user_supplied"
    labels = judgments()
    labels["source"] = "model_generated"
    with pytest.raises(ValueError):
        score(prepared, values, labels)


def test_model_metadata_without_counters_is_not_complete_usage(prepared):
    values = benchmark.template(prepared[2])
    for target in values["records"]:
        target["status"] = "completed"
        target["reports"] = {seat: report() for seat in MODES[target["mode"]]}
        target["usage"] = {
            seat: {
                "reported_model": "reported-model",
                "reported_effort": None,
                "counts": {},
            }
            for seat in MODES[target["mode"]]
        }
    result = score(prepared, values)
    for split in result["summaries"].values():
        assert all(summary["usage_complete"] is False for summary in split.values())
    assert row(result["cases"])["usage"]["codex"]["counts"] == {}
    for target in values["records"]:
        for evidence in target["usage"].values():
            evidence["counts"] = {"tokens": 0}
    reported_zero = score(prepared, values)
    for split in reported_zero["summaries"].values():
        assert all(summary["usage_complete"] is True for summary in split.values())


def test_changed_report_with_reused_finding_id_rejects_old_judgment(prepared):
    values = completed(prepared, reports={"codex": report(finding())})
    labels = bound_judgments(prepared, values, judgments(judgment()))
    row(values["records"])["reports"]["codex"]["findings"][0]["title"] = (
        "Different diagnosis with reused identifier"
    )
    with pytest.raises(ValueError):
        score(prepared, values, labels)


def test_new_snapshot_rejects_old_judgment_even_when_report_matches(
    prepared, second_prepared
):
    original = completed(prepared, reports={"codex": report(finding())})
    labels = bound_judgments(prepared, original, judgments(judgment()))
    latest = completed(second_prepared, reports={"codex": report(finding())})
    assert (
        row(original["records"])["visible_input_hash"]
        == row(latest["records"])["visible_input_hash"]
    )
    with pytest.raises(ValueError):
        score(second_prepared, latest, labels)


@pytest.mark.parametrize("missing", ["snapshot_id", "report_sha256"])
def test_manual_judgment_requires_explicit_evidence_bindings(prepared, missing):
    values = completed(prepared, reports={"codex": report(finding())})
    labels = bound_judgments(prepared, values, judgments(judgment()))
    del labels["records"][0][missing]
    with (
        ReviewStore(prepared[1] / "reviews.sqlite3") as store,
        pytest.raises(ValueError),
    ):
        benchmark.score(prepared[2], values, labels, store)


def test_pending_report_exposes_copyable_evidence_bindings(prepared):
    values = completed(prepared, reports={"codex": report(finding())})
    result = row(score(prepared, values)["cases"])
    expected = bound_judgments(prepared, values, judgments(judgment()))["records"][0]
    binding = {
        key: value
        for key, value in expected.items()
        if key not in {"verdict", "issue_id", "reason"}
    }
    assert result["judgment_bindings"] == [binding]
    assert result["pending"] == [{**binding, "reason": "judgment_missing"}]


def test_benchmark_template_and_score_are_routed_through_main(
    prepared, tmp_path, capsys
):
    output, data, manifest = prepared
    arguments = ["--data-dir", str(data), "benchmark"]
    manifest_args = ["--manifest", str(output / "manifest.json")]
    assert main([*arguments, "template", *manifest_args]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    values = json.loads(captured.out)
    assert values == benchmark.template(manifest)
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(values), encoding="utf-8")
    judgments_path = tmp_path / "judgments.json"
    judgments_path.write_text(json.dumps(judgments()), encoding="utf-8")
    assert (
        main(
            [
                *arguments,
                "score",
                *manifest_args,
                "--results",
                str(results_path),
                "--judgments",
                str(judgments_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result == score(prepared, values)
    assert result["status"] == "not_run"
    assert result["executed_models"] is False
    assert len(result["cases"]) == 32
