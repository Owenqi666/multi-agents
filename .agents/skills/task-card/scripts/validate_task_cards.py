# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.18"]
# ///
"""Validate task cards against schemas/task-card.schema.json.

usage: validate_task_cards.py [--tests NAMES] [--dispatchable] CARD.json [...]

Exits 0 when every card passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:
    sys.exit(
        "validate_task_cards: jsonschema is missing; "
        "run this script with `uv run`, or install jsonschema>=4.18"
    )

repo_root = Path(__file__).resolve().parents[4]
schema_path = repo_root / "schemas" / "task-card.schema.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate task cards.")
    parser.add_argument("cards", nargs="+", type=Path, metavar="CARD.json")
    parser.add_argument(
        "--tests",
        metavar="NAMES",
        help="whitespace-separated names test_refs may use; "
        'pass "$(scripts/verify.sh --list)"',
    )
    parser.add_argument(
        "--dispatchable",
        action="store_true",
        help="also require resolved blocking questions and an existing baseline_commit",
    )
    return parser.parse_args()


def load_card(
    path: Path, validator: Draft202012Validator
) -> tuple[dict | None, list[str]]:
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return None, [f"cannot read file: {error}"]
    except json.JSONDecodeError as error:
        return None, [f"not valid JSON: {error}"]

    found = sorted(
        validator.iter_errors(card),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if found:
        return None, [
            f"{pointer(error.absolute_path)}: {describe(error)}" for error in found
        ]
    return card, []


def describe(error) -> str:
    # A regex is a poor explanation; prefer the schema's own wording.
    hint = error.schema.get("description") if isinstance(error.schema, dict) else None
    if error.validator == "pattern" and hint:
        return f"{error.instance!r} is not allowed: {hint}"
    return error.message


def pointer(parts) -> str:
    return "/" + "/".join(str(part) for part in parts) if parts else "(root)"


def find_cycle(start: str, edges: dict[str, list[str]]) -> list[str] | None:
    stack = [(start, [start])]
    seen: set[str] = set()
    while stack:
        node, trail = stack.pop()
        for dependency in edges.get(node, []):
            if dependency == start:
                return trail + [start]
            if dependency not in seen:
                seen.add(dependency)
                stack.append((dependency, trail + [dependency]))
    return None


def card_exists(card_id: str, directory: Path) -> bool:
    return (directory / f"{card_id}.json").exists() or any(
        directory.glob(f"{card_id}-*.json")
    )


def commit_exists(commit: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def cross_check(
    cards: dict[Path, dict], known_tests: set[str] | None, dispatchable: bool
) -> dict[Path, list[str]]:
    errors: dict[Path, list[str]] = {path: [] for path in cards}
    paths_by_id: dict[str, list[Path]] = {}
    for path, card in cards.items():
        paths_by_id.setdefault(card["id"], []).append(path)
    edges = {card["id"]: card["depends_on"] for card in cards.values()}

    for path, card in cards.items():
        card_id = card["id"]
        if path.name != f"{card_id}.json" and not path.name.startswith(f"{card_id}-"):
            errors[path].append(f"file name must start with the card id '{card_id}'")
        if len(paths_by_id[card_id]) > 1:
            others = ", ".join(
                str(other) for other in paths_by_id[card_id] if other != path
            )
            errors[path].append(f"id '{card_id}' is also used by {others}")

        for dependency in card["depends_on"]:
            if dependency not in paths_by_id and not card_exists(
                dependency, path.parent
            ):
                errors[path].append(f"depends_on: no card found for '{dependency}'")
        cycle = find_cycle(card_id, edges)
        if cycle:
            errors[path].append("depends_on: dependency cycle " + " -> ".join(cycle))

        if known_tests is not None:
            for name in card["test_refs"]:
                if name not in known_tests:
                    errors[path].append(f"test_refs: '{name}' is not a known check")

        if dispatchable:
            for item in card["open_questions"]:
                if item["blocks_dispatch"] and "resolution" not in item:
                    errors[path].append(
                        f"blocking question is unresolved: {item['question']}"
                    )
            commit = card["baseline_commit"]
            if not commit_exists(commit):
                errors[path].append(
                    f"baseline_commit {commit} is not in this repository"
                )
    return errors


def main() -> int:
    args = parse_args()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    report: dict[Path, list[str]] = {}
    valid: dict[Path, dict] = {}
    for path in args.cards:
        card, errors = load_card(path, validator)
        report[path] = errors
        if card is not None:
            valid[path] = card

    known_tests = set(args.tests.split()) if args.tests is not None else None
    for path, errors in cross_check(valid, known_tests, args.dispatchable).items():
        report[path].extend(errors)

    failed = 0
    for path, errors in report.items():
        if errors:
            failed += 1
            print(f"{path}: FAILED")
            for error in errors:
                print(f"  {error}")
        else:
            print(f"{path}: ok")
    print(f"{len(report)} card(s) checked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
