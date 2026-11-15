"""Prepare fixed review inputs and score explicitly imported human judgments.

This module never launches a model or executes sample code. Oracle data is read
only by the scorer; prepare reads the visible pack and creates ordinary Git
repositories. Numeric quality metrics require complete reports and resolved
human judgments, including matching source citations for valid discoveries.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from multi_agent.adapters.report_validation import REPORT_SCHEMA, validate_report
from multi_agent.core.reports import check_report
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.store import ReviewStore

from .main import data_directory, load_snapshot, object_fields, read_json
from .prompt import REVIEW_PROMPT, REVIEW_VERSION
from .snapshot import freeze_repository, materialize

DATASET_VERSION = "review-benchmark-v1"
FIXTURES = Path(__file__).resolve().parents[3] / "tests/fixtures/benchmark"
MODE_SEATS = {
    "codex-only": ("codex",),
    "claude-only": ("claude",),
    "dual": ("codex", "claude"),
    "official-plugin": ("plugin",),
}
METRICS = (
    "valid_discoveries",
    "known_found",
    "additional_valid",
    "missed",
    "false_positives",
    "duplicates",
)
_CASE_FIELDS = {
    "case_id",
    "split",
    "requirements",
    "acceptance",
    "baseline",
    "candidate",
}
_MANIFEST_CASE = {"case_id", "split", "snapshot_id", "visible_input_hash"}
_BINDING = {
    "case_id",
    "mode",
    "snapshot_id",
    "visible_input_hash",
    "prompt_template_version",
    "prompt_sha256",
}
_RESULT_FIELDS = _BINDING | {"status", "reports", "duration_seconds", "usage"}
_JUDGMENT_FIELDS = {
    "case_id",
    "mode",
    "seat",
    "finding_id",
    "snapshot_id",
    "report_sha256",
    "verdict",
    "issue_id",
    "reason",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be nonempty UTF-8 text")
    value.encode("utf-8")
    return value


def _version(value: object) -> None:
    if type(value) is not int or value != 1:
        raise ValueError("unsupported benchmark schema_version")


def catalog() -> list[dict[str, Any]]:
    pack = object_fields(
        read_json(FIXTURES / "visible.json"),
        {"schema_version", "dataset_version", "cases"},
        "visible pack",
    )
    _version(pack["schema_version"])
    if pack["dataset_version"] != DATASET_VERSION or not isinstance(
        pack["cases"], list
    ):
        raise ValueError("invalid visible benchmark pack")
    cases = []
    for raw in pack["cases"]:
        case = object_fields(raw, _CASE_FIELDS, "visible case")
        for field in ("case_id", "requirements", "baseline", "candidate"):
            _text(case[field], field)
        if not isinstance(case["acceptance"], list) or not case["acceptance"]:
            raise ValueError("visible case needs acceptance criteria")
        for criterion in case["acceptance"]:
            _text(criterion, "criterion")
        cases.append(case)
    if [case["case_id"] for case in cases] != [
        f"B{number:02}" for number in range(1, 9)
    ]:
        raise ValueError("benchmark must contain ordered cases B01 through B08")
    if [case["split"] for case in cases] != ["development"] * 6 + ["heldout"] * 2:
        raise ValueError("benchmark needs six development and two heldout cases")
    return cases


def _input_hash(case: dict[str, Any]) -> str:
    # Repository locations and their object-database identity are intentionally
    # excluded. Four compared runs must additionally share the exact snapshot ID.
    return _digest(
        {
            "path": "case.py",
            "baseline": case["baseline"],
            "candidate": case["candidate"],
            "requirements": case["requirements"],
            "acceptance": case["acceptance"],
            "prompt_template_version": REVIEW_VERSION,
            "prompt": REVIEW_PROMPT,
            "report_schema": REPORT_SCHEMA,
        }
    )


def _git(repo: Path, *args: str) -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000",
        LC_ALL="C",
    )
    try:
        result = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "--no-lazy-fetch",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                f"core.attributesFile={os.devnull}",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "user.name=Review benchmark",
                "-c",
                "user.email=benchmark@example.invalid",
                "-C",
                str(repo),
                *args,
            ],
            env=env,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"benchmark Git preparation failed: {error}") from error
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8").strip()


def prepare(output: Path, data_dir: Path) -> dict[str, Any]:
    cases = catalog()  # Do not read the oracle here.
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("benchmark output must be a new directory")
    if output.is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError("prepare output must be outside the workbench checkout")
    resolved_data = data_dir.expanduser().resolve()
    if resolved_data.is_relative_to(output / "review-inputs"):
        raise ValueError(
            "benchmark data directory must be outside review input bundles"
        )
    if any(
        resolved_data.is_relative_to(output / "cases" / case["case_id"])
        for case in cases
    ):
        raise ValueError("benchmark data directory must be outside sample repositories")
    output.mkdir(parents=True)
    directory = data_directory(resolved_data)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "prompt_template_version": REVIEW_VERSION,
        "prompt_sha256": hashlib.sha256(REVIEW_PROMPT.encode()).hexdigest(),
        "cases": [],
    }
    with ReviewStore(directory / "reviews.sqlite3") as store:
        for case in cases:
            repo = output / "cases" / case["case_id"]
            repo.mkdir(parents=True)
            _git(
                repo,
                "init",
                "--quiet",
                "--template=",
                "--object-format=sha1",
                "--initial-branch=main",
            )
            commits = []
            for side in ("baseline", "candidate"):
                (repo / "case.py").write_text(case[side], encoding="utf-8", newline="")
                _git(repo, "add", "--", "case.py")
                _git(repo, "commit", "--quiet", "-m", side)
                commits.append(_git(repo, "rev-parse", "HEAD"))
            snapshot = freeze_repository(
                repo,
                commits[0],
                commits[1],
                case["requirements"],
                tuple(case["acceptance"]),
            )
            store.save_snapshot(snapshot.snapshot_id, snapshot.to_dict())
            bundle = output / "review-inputs" / case["case_id"]
            bundle.mkdir(parents=True)
            materialize(snapshot, bundle / "source")
            review_input = snapshot.to_dict()
            review_input.pop("files")
            review_input.pop("diff")
            for name, text in (
                ("review-input.json", json.dumps(review_input, ensure_ascii=False)),
                ("diff.patch", snapshot.diff),
            ):
                (bundle / name).write_text(text, encoding="utf-8")
                (bundle / name).chmod(0o444)
            bundle.chmod(0o555)
            manifest["cases"].append(
                {
                    "case_id": case["case_id"],
                    "split": case["split"],
                    "snapshot_id": snapshot.snapshot_id,
                    "visible_input_hash": _input_hash(case),
                }
            )
    (output / "review-prompt.txt").write_text(REVIEW_PROMPT, encoding="utf-8")
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _manifest(value: object) -> dict[str, Any]:
    result = object_fields(
        value,
        {
            "schema_version",
            "dataset_version",
            "prompt_template_version",
            "prompt_sha256",
            "cases",
        },
        "manifest",
    )
    _version(result["schema_version"])
    if (
        result["dataset_version"] != DATASET_VERSION
        or result["prompt_template_version"] != REVIEW_VERSION
    ):
        raise ValueError("manifest dataset or prompt version does not match")
    if result["prompt_sha256"] != hashlib.sha256(REVIEW_PROMPT.encode()).hexdigest():
        raise ValueError("manifest prompt content does not match its template")
    expected = {case["case_id"]: case for case in catalog()}
    if not isinstance(result["cases"], list):
        raise TypeError("manifest cases must be an array")
    seen: set[str] = set()
    for raw in result["cases"]:
        case = object_fields(raw, _MANIFEST_CASE, "manifest case")
        identifier = _text(case["case_id"], "case_id")
        if identifier not in expected or identifier in seen:
            raise ValueError("manifest case is unknown or repeated")
        seen.add(identifier)
        if case["split"] != expected[identifier]["split"] or case[
            "visible_input_hash"
        ] != _input_hash(expected[identifier]):
            raise ValueError("manifest visible input or split does not match")
        if not isinstance(case["snapshot_id"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", case["snapshot_id"]
        ):
            raise ValueError("manifest snapshot_id must be a SHA-256 digest")
    if seen != set(expected):
        raise ValueError("manifest must contain all eight cases")
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    return _manifest(read_json(path))


def _binding(
    manifest: dict[str, Any], case: dict[str, Any], mode: str
) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "mode": mode,
        "snapshot_id": case["snapshot_id"],
        "visible_input_hash": case["visible_input_hash"],
        "prompt_template_version": manifest["prompt_template_version"],
        "prompt_sha256": manifest["prompt_sha256"],
    }


def template(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = _manifest(manifest)
    return {
        "schema_version": 1,
        "source": "user_supplied",
        "records": [
            {
                **_binding(manifest, case, mode),
                "status": "not_run",
                "reports": {},
                "duration_seconds": None,
                "usage": None,
            }
            for case in manifest["cases"]
            for mode in MODE_SEATS
        ],
    }


def _number(value: object, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a nonnegative finite number or null")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise ValueError(f"{name} is outside the finite numeric range") from error
    if not finite or value < 0:
        raise ValueError(f"{name} must be a nonnegative finite number or null")
    return value


def _usage(value: object, seats: tuple[str, ...]) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not value or not set(value).issubset(seats):
        raise ValueError(
            "usage must map selected seats to reported evidence, or be null"
        )
    for raw in value.values():
        item = object_fields(
            raw, {"reported_model", "reported_effort", "counts"}, "usage evidence"
        )
        for field in ("reported_model", "reported_effort"):
            if item[field] is not None:
                _text(item[field], field)
        if not isinstance(item["counts"], dict):
            raise TypeError("usage counts must be an object")
        for name, number in item["counts"].items():
            _text(name, "usage field")
            _number(number, "usage counter")


def _records(value: object, name: str) -> list[Any]:
    document = object_fields(value, {"schema_version", "source", "records"}, name)
    _version(document["schema_version"])
    if document["source"] != "user_supplied" or not isinstance(
        document["records"], list
    ):
        raise ValueError(
            f"{name} must declare source=user_supplied and an array of records"
        )
    return document["records"]


def _results(
    manifest: dict[str, Any], value: object
) -> dict[tuple[str, str], dict[str, Any]]:
    cases = {case["case_id"]: case for case in manifest["cases"]}
    result = {}
    for raw in _records(value, "results"):
        row = object_fields(raw, _RESULT_FIELDS, "result record")
        identifier = _text(row["case_id"], "case_id")
        mode = _text(row["mode"], "mode")
        key = (identifier, mode)
        if identifier not in cases or mode not in MODE_SEATS or key in result:
            raise ValueError("result case/mode is unknown or repeated")
        if any(
            row[field] != expected
            for field, expected in _binding(manifest, cases[identifier], mode).items()
        ):
            raise ValueError(
                "result snapshot, visible input, or prompt does not match the shared manifest"
            )
        if row["status"] not in ("not_run", "completed", "failed"):
            raise ValueError("invalid imported result status")
        seats = MODE_SEATS[mode]
        if not isinstance(row["reports"], dict) or not set(row["reports"]).issubset(
            seats
        ):
            raise ValueError("reports must be keyed by the selected benchmark seats")
        if row["status"] == "not_run" and (
            row["reports"]
            or row["duration_seconds"] is not None
            or row["usage"] is not None
        ):
            raise ValueError("not_run cannot carry findings or measured evidence")
        if row["duration_seconds"] is not None:
            _number(row["duration_seconds"], "duration_seconds")
        _usage(row["usage"], seats)
        result[key] = {
            **row,
            "reports": {
                seat: validate_report(report) for seat, report in row["reports"].items()
            },
        }
    return result


def _truths() -> dict[str, set[str]]:
    oracle = object_fields(
        read_json(FIXTURES / "oracle.json"),
        {"schema_version", "dataset_version", "cases"},
        "oracle",
    )
    _version(oracle["schema_version"])
    if oracle["dataset_version"] != DATASET_VERSION or not isinstance(
        oracle["cases"], dict
    ):
        raise ValueError("oracle version or cases are invalid")
    if set(oracle["cases"]) != {case["case_id"] for case in catalog()}:
        raise ValueError("oracle does not match the visible cases")
    truths = {}
    for identifier, case in oracle["cases"].items():
        identifiers = [
            _text(truth["issue_id"], "oracle issue ID") for truth in case["truths"]
        ]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("oracle issue IDs must be unique per case")
        truths[identifier] = set(identifiers)
    return truths


def _judgments(
    value: object,
    results: dict[tuple[str, str], dict[str, Any]],
    truths: dict[str, set[str]],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    judgments = {}
    classifications: dict[tuple[str, str, str], str] = {}
    for raw in _records(value, "judgments"):
        row = object_fields(raw, _JUDGMENT_FIELDS, "judgment")
        for field in _JUDGMENT_FIELDS - {"issue_id"}:
            _text(row[field], field)
        identifier, mode, seat, finding = (
            row[field] for field in ("case_id", "mode", "seat", "finding_id")
        )
        key = (identifier, mode, seat, finding)
        result = results.get((identifier, mode))
        if (
            result is None
            or seat not in result["reports"]
            or finding
            not in {item["id"] for item in result["reports"][seat]["findings"]}
            or key in judgments
        ):
            raise ValueError("judgment target is unknown or repeated")
        if row["snapshot_id"] != result["snapshot_id"] or row[
            "report_sha256"
        ] != _digest(result["reports"][seat]):
            raise ValueError("judgment snapshot or report content no longer matches")
        verdict, issue = row["verdict"], row["issue_id"]
        if verdict not in ("known", "additional_valid", "false_positive", "unresolved"):
            raise ValueError("invalid human verdict")
        if verdict == "unresolved":
            if issue is not None:
                raise ValueError("unresolved judgments must have issue_id=null")
        else:
            issue = _text(issue, "judgment issue_id")
            if (verdict == "known") != (issue in truths[identifier]):
                raise ValueError(
                    "known verdicts must use this case's oracle ID; other verdicts must not"
                )
            issue_key = (identifier, mode, issue)
            if issue_key in classifications and classifications[issue_key] != verdict:
                raise ValueError("the same issue ID has conflicting human verdicts")
            classifications[issue_key] = verdict
        judgments[key] = dict(row)
    return judgments


def _snapshot(
    case: dict[str, Any], store: ReviewStore, manifest_case: dict[str, Any]
) -> FrozenSnapshot:
    snapshot = load_snapshot(store, manifest_case["snapshot_id"])
    if snapshot.snapshot_id != manifest_case["snapshot_id"]:
        raise ValueError("stored snapshot content does not match its identity")
    content = {(item.side, item.path, item.mode): item.text for item in snapshot.files}
    expected = {
        (side, "case.py", "100644"): case[side] for side in ("baseline", "candidate")
    }
    if (
        content != expected
        or len(snapshot.files) != 2
        or snapshot.requirements != case["requirements"]
        or snapshot.acceptance != tuple(case["acceptance"])
        or snapshot.tests
        or snapshot.required_tests
        or snapshot.repository is None
    ):
        raise ValueError("stored snapshot does not contain the fixed benchmark input")
    return snapshot


def _score_case(
    row: dict[str, Any],
    snapshot: FrozenSnapshot,
    truths: set[str],
    judgments: dict[tuple[str, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    identifier, mode = row["case_id"], row["mode"]
    reports = {
        seat: check_report(report, snapshot) for seat, report in row["reports"].items()
    }
    metrics = dict.fromkeys(METRICS)
    pending = []
    missing_seats = set(MODE_SEATS[mode]) - set(reports)
    if missing_seats and row["status"] == "completed":
        pending.append(
            {"reason": "selected_reports_missing", "seats": sorted(missing_seats)}
        )
    labels = []
    bindings = []
    issue_sets: dict[str, set[str]] = {
        verdict: set() for verdict in ("known", "additional_valid", "false_positive")
    }
    valid_count = 0
    for seat, report in reports.items():
        for finding in report["findings"]:
            binding = {
                "case_id": identifier,
                "mode": mode,
                "seat": seat,
                "finding_id": finding["id"],
                "snapshot_id": row["snapshot_id"],
                "report_sha256": _digest(row["reports"][seat]),
            }
            bindings.append(binding)
            label = judgments.get((identifier, mode, seat, finding["id"]))
            if label is None:
                pending.append(
                    {
                        **binding,
                        "reason": "judgment_missing",
                    }
                )
                continue
            labels.append(label)
            verdict = label["verdict"]
            if verdict == "unresolved" or (
                verdict in ("known", "additional_valid")
                and finding["reference_status"] != "verified"
            ):
                pending.append(
                    {
                        **binding,
                        "reason": "judgment_unresolved"
                        if verdict == "unresolved"
                        else "valid_finding_reference_unverified",
                    }
                )
                continue
            issue_sets[verdict].add(label["issue_id"])
            valid_count += verdict in ("known", "additional_valid")
    status = row["status"]
    if status == "completed":
        status = "pending" if pending else "scored"
    if status == "scored":
        found = len(issue_sets["known"])
        extra = len(issue_sets["additional_valid"])
        metrics = {
            "valid_discoveries": found + extra,
            "known_found": found,
            "additional_valid": extra,
            "missed": len(truths - issue_sets["known"]),
            "false_positives": len(issue_sets["false_positive"]),
            "duplicates": valid_count - found - extra,
        }
    return {
        **row,
        "status": status,
        "reports": reports,
        "judgments": labels,
        "judgment_bindings": bindings,
        "pending": pending,
        "metrics": metrics,
        "duration_source": "user_supplied",
        "usage_source": "user_supplied",
    }


def score(
    manifest: dict[str, Any], results: object, judgments: object, store: ReviewStore
) -> dict[str, Any]:
    manifest = _manifest(manifest)
    imported = _results(manifest, results)
    truths = _truths()
    labeled = _judgments(judgments, imported, truths)
    cases = {case["case_id"]: case for case in catalog()}
    manifest_cases = {case["case_id"]: case for case in manifest["cases"]}
    snapshots = {
        identifier: _snapshot(case, store, manifest_cases[identifier])
        for identifier, case in cases.items()
    }
    rows = []
    for blank in template(manifest)["records"]:
        identifier, mode = blank["case_id"], blank["mode"]
        row = _score_case(
            imported.get((identifier, mode), blank),
            snapshots[identifier],
            truths[identifier],
            labeled,
        )
        row["split"] = cases[identifier]["split"]
        rows.append(row)
    summaries: dict[str, Any] = {}
    for split in ("development", "heldout", "all"):
        summaries[split] = {}
        for mode, seats in MODE_SEATS.items():
            selected = [
                row
                for row in rows
                if row["mode"] == mode and (split == "all" or row["split"] == split)
            ]
            complete = all(row["status"] == "scored" for row in selected)
            summaries[split][mode] = {
                "status": "scored" if complete else "pending",
                "case_count": len(selected),
                "metrics": {
                    key: sum(row["metrics"][key] for row in selected)
                    if complete
                    else None
                    for key in METRICS
                },
                "duration_seconds": sum(row["duration_seconds"] for row in selected)
                if all(row["duration_seconds"] is not None for row in selected)
                else None,
                "usage_complete": all(
                    row["usage"] is not None
                    and set(row["usage"]) == set(seats)
                    and all(row["usage"][seat]["counts"] for seat in seats)
                    for row in selected
                ),
            }
    return {
        "status": "imported_user_evidence"
        if any(row["status"] != "not_run" for row in imported.values())
        else "not_run",
        "source": "user_supplied",
        "executed_models": False,
        "recommendation": None,
        "cases": rows,
        "summaries": summaries,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare fixed samples; import reports and human judgments without running models."
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path.home() / ".local/share/multi-agent"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--out", type=Path, required=True)
    for command in ("template", "score"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--manifest", type=Path, required=True)
        if command == "score":
            subparser.add_argument("--results", type=Path, required=True)
            subparser.add_argument("--judgments", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            output = prepare(args.out, args.data_dir)
        else:
            manifest = load_manifest(args.manifest)
            if args.command == "template":
                output = template(manifest)
            else:
                with ReviewStore(
                    data_directory(args.data_dir) / "reviews.sqlite3"
                ) as store:
                    output = score(
                        manifest,
                        read_json(args.results),
                        read_json(args.judgments),
                        store,
                    )
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
    ) as error:
        print(f"multi-agent benchmark: {error}", file=sys.stderr)
        return 2
