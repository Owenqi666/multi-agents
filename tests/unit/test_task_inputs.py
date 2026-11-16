"""Product task validation uses committed rules, never executable model proposals."""

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from multi_agent.cli.task_inputs import (
    freeze_project_rules,
    load_task_schema,
    parse_project_rules,
    parse_task_card,
    validate_path_pattern,
    validate_task_graph,
    validate_task_references,
)
from multi_agent.core.tasks import FrozenProjectRules, TaskCard

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "a" * 40
RULES = """version: 1
commands:
  frontend-unit:
    executable: pnpm
    args: ["--filter", frontend, test]
protected_paths: [".multi-agent/**", ".claude/**"]
implementation:
  - paths: ["frontend/**"]
    provider: claude-code
    default_model: claude-sonnet-5-5
    default_effort: medium
"""


def task_data(**changes) -> dict:
    value = {
        "schema_version": 1,
        "id": "T-0100",
        "goal": "Implement the specified change.",
        "baseline_commit": BASELINE,
        "write_scope": ["frontend/**"],
        "depends_on": [],
        "interface_contracts": [{"name": "input", "definition": "Accept a string."}],
        "deliverables": ["The documented behavior."],
        "acceptance_criteria": ["The configured test passes."],
        "test_refs": ["frontend-unit"],
        "open_questions": [],
    }
    value.update(changes)
    return value


def task(**changes) -> TaskCard:
    return parse_task_card(json.dumps(task_data(**changes)))


def frozen_rules() -> FrozenProjectRules:
    return FrozenProjectRules(
        "/repo", BASELINE, "b" * 40, RULES, parse_project_rules(RULES)
    )


def git(repo: Path, *args: str) -> str:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Task Rules Test",
            "-c",
            "user.email=task-rules@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        env=environment,
        check=True,
        timeout=30,
    )
    return result.stdout.decode("utf-8").strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    (repo / ".multi-agent").mkdir()
    (repo / ".multi-agent/rules.yaml").write_text(RULES, encoding="utf-8", newline="")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "baseline")
    return repo, git(repo, "rev-parse", "HEAD")


def test_rules_are_read_from_baseline_not_worktree(repository) -> None:
    repo, baseline = repository
    initial = freeze_project_rules(repo, baseline)
    path = repo / ".multi-agent/rules.yaml"
    path.write_text(RULES.replace("pnpm", "candidate-command"))
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "candidate changes rules")
    path.write_text("not: valid: yaml: [")
    git(repo, "add", ".")
    path.write_text("uncommitted working tree")
    actual = freeze_project_rules(repo, baseline)
    assert actual == initial
    assert actual.baseline_commit == baseline
    assert actual.raw_text == RULES
    assert actual.blob_oid == git(
        repo, "rev-parse", f"{baseline}:.multi-agent/rules.yaml"
    )
    assert actual.rules.commands[0].argv == ("pnpm", "--filter", "frontend", "test")


def test_rules_hash_changes_with_committed_bytes(repository) -> None:
    repo, baseline = repository
    initial = freeze_project_rules(repo, baseline)
    changed = "# 中文 comment changes identity\r\n" + RULES
    (repo / ".multi-agent/rules.yaml").write_bytes(changed.encode("utf-8"))
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "comment changes")
    actual = freeze_project_rules(repo, git(repo, "rev-parse", "HEAD"))
    assert actual.rules == initial.rules
    assert actual.raw_text == changed
    assert actual.rules_hash == hashlib.sha256(changed.encode("utf-8")).hexdigest()
    assert actual.rules_hash != initial.rules_hash
    assert actual.blob_oid != initial.blob_oid


def test_rules_from_subdirectory_use_the_repository_root(repository) -> None:
    repo, _ = repository
    subdirectory = repo / "nested"
    (subdirectory / ".multi-agent").mkdir(parents=True)
    (subdirectory / ".multi-agent/rules.yaml").write_text(
        RULES.replace("pnpm", "incorrect-nested-command")
    )
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "nested rules are not project rules")
    baseline = git(repo, "rev-parse", "HEAD")
    expected = freeze_project_rules(repo, baseline)
    actual = freeze_project_rules(subdirectory, baseline)
    assert actual == expected
    assert actual.raw_text == RULES
    assert actual.rules.commands[0].executable == "pnpm"


def test_product_test_refs_use_rules_commands_not_verify_steps() -> None:
    validate_task_references(task(), frozen_rules())
    with pytest.raises(ValueError):
        validate_task_references(task(test_refs=["unit"]), frozen_rules())


def test_unknown_product_test_ref_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_task_references(task(test_refs=["missing-test"]), frozen_rules())


def test_proposed_tests_do_not_become_approved_commands(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run"
    proposal = {
        "name": "new-check",
        "executable": sys.executable,
        "args": ["-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        "reason": "Suggested only.",
    }
    card = task(proposed_tests=[proposal])
    validate_task_references(card, frozen_rules())
    assert card.proposed_tests[0].args == tuple(proposal["args"])
    with pytest.raises(ValueError):
        validate_task_references(
            replace(card, test_refs=("new-check",)), frozen_rules()
        )
    assert not marker.exists()
    assert [item.name for item in frozen_rules().rules.commands] == ["frontend-unit"]


def test_unresolved_blocking_question_prevents_dispatch() -> None:
    question = {"question": "Which behavior?", "blocks_dispatch": True}
    with pytest.raises(ValueError):
        validate_task_references(task(open_questions=[question]), frozen_rules())
    question["resolution"] = "Use the stated requirement."
    validate_task_references(task(open_questions=[question]), frozen_rules())
    validate_task_references(
        task(
            open_questions=[{"question": "Optional detail?", "blocks_dispatch": False}]
        ),
        frozen_rules(),
    )


def test_unknown_dependency_and_dependency_cycle_are_rejected() -> None:
    first = task(depends_on=["T-0101"])
    second = task(id="T-0101", depends_on=["T-0100"])
    with pytest.raises(ValueError):
        validate_task_graph((first,))
    with pytest.raises(ValueError):
        validate_task_graph((first, second))
    with pytest.raises(ValueError):
        validate_task_graph((first, second), frozenset({"T-0101"}))
    with pytest.raises(ValueError):
        validate_task_graph((task(depends_on=["T-0100"]),))


def test_task_graph_checks_duplicate_ids_and_accepts_known_dependencies() -> None:
    with pytest.raises(ValueError):
        validate_task_graph((task(), task(goal="Another task with the same id.")))
    dependent = task(depends_on=["T-0099"])
    validate_task_graph((dependent,), frozenset({"T-0099"}))
    validate_task_graph((dependent, task(id="T-0099")))


def test_task_baseline_must_match_frozen_rules() -> None:
    with pytest.raises(ValueError):
        validate_task_references(task(baseline_commit="c" * 40), frozen_rules())


@pytest.mark.parametrize(
    "text",
    [
        RULES + "version: 1\n",
        RULES.replace(
            "    executable: pnpm", "    executable: pnpm\n    executable: false"
        ),
        RULES.replace('["--filter", frontend, test]', '"pnpm test"'),
        RULES.replace('["--filter", frontend, test]', "[true]"),
        RULES.replace('["--filter", frontend, test]', "[1]"),
        RULES.replace('["--filter", frontend, test]', '["\\0"]'),
        RULES.replace("executable: pnpm", 'executable: " "'),
        RULES.replace("executable: pnpm", 'executable: "pnpm\\0"'),
    ],
)
def test_rules_reject_duplicate_yaml_keys_and_invalid_argv(text: str) -> None:
    with pytest.raises(ValueError):
        parse_project_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        RULES + "unknown: true\n",
        RULES.replace("provider: claude-code", "provider: unsupported"),
        RULES.replace("version: 1", "version: true"),
        RULES.replace("version: 1", "version: 2"),
        RULES.replace("    executable: pnpm", "    executable: pnpm\n    shell: true"),
        RULES.replace(
            "    default_effort: medium", "    default_effort: medium\n    unknown: 1"
        ),
        RULES.replace("  frontend-unit:", "  Not-A-Test:"),
        RULES.replace("protected_paths:", "unexpected_paths:"),
        "[]",
    ],
)
def test_rules_reject_unknown_fields_providers_and_malformed_structure(
    text: str,
) -> None:
    with pytest.raises(ValueError):
        parse_project_rules(text)


@pytest.mark.parametrize(
    "text",
    [
        '!!python/object/apply:os.system ["must-not-run"]',
        RULES.replace("protected_paths: [", "protected_paths: &paths [").replace(
            'paths: ["frontend/**"]', "paths: *paths"
        ),
        RULES.replace("    provider: claude-code", "    <<: {provider: claude-code}"),
        RULES + "---\nversion: 1\n",
    ],
)
def test_yaml_is_safe_and_rejects_aliases_merges_and_extra_documents(text: str) -> None:
    with pytest.raises(ValueError):
        parse_project_rules(text)


def test_yaml_preserves_on_off_yes_no_as_literal_strings() -> None:
    rules = parse_project_rules(
        RULES.replace('["--filter", frontend, test]', "[on, off, yes, no]")
    )
    assert rules.commands[0].args == ("on", "off", "yes", "no")


def test_command_arguments_remain_literal_and_empty_arguments_are_preserved() -> None:
    raw = yaml.safe_load(RULES)
    raw["commands"]["frontend-unit"]["args"] = [
        "",
        "a b",
        "$(touch sentinel)",
        ";",
        "*",
    ]
    rules = parse_project_rules(yaml.safe_dump(raw))
    assert rules.commands[0].argv == ("pnpm", "", "a b", "$(touch sentinel)", ";", "*")


def test_rule_defaults_and_task_type_restrictions_are_explicit() -> None:
    initial = parse_project_rules(RULES)
    assert initial.default_implementation is None
    assert initial.implementation[0].config.allowed_models == ("claude-sonnet-5-5",)
    raw = yaml.safe_load(RULES)
    raw["default_implementation"] = {
        "provider": "codex",
        "default_model": "model-one",
        "default_effort": "high",
        "allowed_models": ["model-one", "model-two"],
    }
    raw["task_types"] = {
        "frontend": {"provider": "claude-code", "allowed_models": ["m"]}
    }
    actual = parse_project_rules(yaml.safe_dump(raw))
    assert actual.default_implementation.provider == "codex"
    assert actual.default_implementation.allowed_models == ("model-one", "model-two")
    assert actual.task_types[0].name == "frontend"
    assert actual.task_types[0].allowed_models == ("m",)
    raw["default_implementation"]["default_model"] = "not-allowed"
    with pytest.raises(ValueError):
        parse_project_rules(yaml.safe_dump(raw))


@pytest.mark.parametrize("location", ["default_implementation", "task_types"])
def test_optional_rule_restrictions_reject_unknown_providers(location: str) -> None:
    raw = yaml.safe_load(RULES)
    if location == "default_implementation":
        raw[location] = {
            "provider": "unknown",
            "default_model": "m",
            "default_effort": "high",
        }
    else:
        raw[location] = {"frontend": {"provider": "unknown", "allowed_models": ["m"]}}
    with pytest.raises(ValueError):
        parse_project_rules(yaml.safe_dump(raw))


@pytest.mark.parametrize("kind", ["symlink", "gitlink"])
def test_rules_reject_non_regular_blob(repository, tmp_path: Path, kind: str) -> None:
    repo, baseline = repository
    path = repo / ".multi-agent/rules.yaml"
    path.unlink()
    if kind == "symlink":
        external = tmp_path / "external-rules.yaml"
        external.write_text(RULES)
        path.symlink_to(external)
        git(repo, "add", ".")
    else:
        git(
            repo,
            "update-index",
            "--cacheinfo",
            "160000",
            baseline,
            ".multi-agent/rules.yaml",
        )
    git(repo, "commit", "--quiet", "-m", "nonregular rules")
    with pytest.raises(ValueError):
        freeze_project_rules(repo, git(repo, "rev-parse", "HEAD"))


def test_missing_rules_and_invalid_baseline_are_rejected(repository) -> None:
    repo, _ = repository
    git(repo, "rm", ".multi-agent/rules.yaml")
    git(repo, "commit", "--quiet", "-m", "remove rules")
    with pytest.raises(ValueError):
        freeze_project_rules(repo, git(repo, "rev-parse", "HEAD"))
    with pytest.raises(ValueError):
        freeze_project_rules(repo, "f" * 40)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute",
        "a//b",
        "./a",
        "a/./b",
        "a/../b",
        "a/",
        "a\\b",
        ".git/config",
        ".GiT/config",
        "a/.git/config",
        ".git./config",
        ".git /config",
        "a\0b",
        "a\nb",
        "a/[bc]",
        "a/{b,c}",
        "a/@(b)",
        "a/**suffix",
        "a/pre**fix",
    ],
)
def test_paths_reject_unsafe_or_unsupported_syntax(value: str) -> None:
    with pytest.raises(ValueError):
        validate_path_pattern(value)
    with pytest.raises(ValueError):
        task(write_scope=[value])
    raw = yaml.safe_load(RULES)
    raw["implementation"][0]["paths"] = [value]
    with pytest.raises(ValueError):
        parse_project_rules(yaml.safe_dump(raw))
    raw["implementation"][0]["paths"] = ["frontend/**"]
    raw["protected_paths"] = [value]
    with pytest.raises(ValueError):
        parse_project_rules(yaml.safe_dump(raw))


@pytest.mark.parametrize(
    "value", ["a.py", "frontend/**", "a/*/b?.py", "**", "中文/文件.py"]
)
def test_supported_path_patterns_remain_verbatim(value: str) -> None:
    assert validate_path_pattern(value) == value
    assert task(write_scope=[value]).write_scope == (value,)


def test_task_json_rejects_duplicate_keys_unknown_fields_and_invalid_schema() -> None:
    text = json.dumps(task_data())
    with pytest.raises(ValueError):
        parse_task_card(text.replace('"goal":', '"goal": "duplicate", "goal":'))
    with pytest.raises(ValueError):
        parse_task_card(
            text.replace('"name": "input"', '"name": "input", "name": "other"')
        )
    for changes in (
        {"task_type": "frontend"},
        {"schema_version": 2},
        {"baseline_commit": "HEAD"},
        {
            "open_questions": [
                {"question": "Q?", "blocks_dispatch": True, "resolution": " "}
            ]
        },
    ):
        with pytest.raises(ValueError):
            task(**changes)
    invalid = task_data()
    del invalid["open_questions"]
    with pytest.raises(ValueError):
        parse_task_card(json.dumps(invalid))
    for raw in ("[]", "null", "{", '{"goal": NaN}'):
        with pytest.raises(ValueError):
            parse_task_card(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "T-0100\n"},
        {"depends_on": ["T-0099\n"]},
        {"baseline_commit": BASELINE + "\n"},
        {"test_refs": ["frontend-unit\n"]},
        {
            "proposed_tests": [
                {
                    "name": "new-check\n",
                    "executable": "python",
                    "args": ["check.py"],
                    "reason": "Suggested only.",
                }
            ]
        },
    ],
)
def test_task_identifiers_reject_trailing_newlines(changes: dict) -> None:
    with pytest.raises(ValueError):
        task(**changes)


def test_load_task_schema_uses_the_canonical_schema() -> None:
    source = json.loads((ROOT / "schemas/task-card.schema.json").read_text())
    assert load_task_schema() == source


def test_installed_package_can_load_task_schema(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--offline", str(ROOT), "--out-dir", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = (ROOT / "schemas/task-card.schema.json").read_bytes()
    wheel = next(output.glob("*.whl"))
    source = next(output.glob("*.tar.gz"))
    resource = "multi_agent/task-card.schema.json"
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read(resource) == expected
        assert (archive.getinfo(resource).external_attr >> 16) & 0o170000 != 0o120000
    with tarfile.open(source) as archive:
        members = [member for member in archive if member.name.endswith("/" + resource)]
        assert len(members) == 1
        assert members[0].isfile()
        assert archive.extractfile(members[0]).read() == expected
    installed = tmp_path / "installed"
    result = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, sys.argv[1]); "
        "from importlib.resources import files; import multi_agent; "
        "assert Path(multi_agent.__file__).is_relative_to(Path(sys.argv[1])); "
        "sys.stdout.buffer.write(files('multi_agent').joinpath('task-card.schema.json').read_bytes())"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(installed)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == expected
