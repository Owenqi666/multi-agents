"""Translate typed HTTP action arguments to the existing product CLI."""

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from multi_agent.adapters.project_planner import parse_project_plan
from multi_agent.cli.planning_inputs import _EFFORTS
from multi_agent.cli.snapshot import _string

from .resources import read_resource, require_owned
from .workspace import _MODEL_PATTERNS, Workspace

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_STAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_CARD_ID = re.compile(r"T-[0-9]{4}")
_ADVISORY = frozenset(("astra", "fable"))
_LEAF = frozenset(("terra", "sonnet"))
_IMPLEMENTATION = _LEAF | {"sol"}
_REVIEW = _ADVISORY | _IMPLEMENTATION | {"opus"}
_DRAFT = frozenset(("fable", "opus", "sonnet"))
_PLAN_REVIEW = frozenset(("astra", "sol", "terra"))


class UnknownAction(ValueError):
    """An action is outside the product registry."""


class ActionConflict(ValueError):
    """An action cannot begin in the resource's current state."""


def _fields(
    value: object, required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected an object of named arguments")
    if missing := set(required) - value.keys():
        raise ValueError(f"missing arguments: {', '.join(sorted(missing))}")
    if value.keys() - set(required + optional):
        raise ValueError("the action received unexpected arguments")
    return value


def _token(value: object, name: str) -> str:
    value = _string(value, name, nonempty=True)
    if value.startswith("-") or any(character.isspace() for character in value):
        raise ValueError(f"{name} must be a single non-option term")
    return value


def _identifier(
    value: object, name: str, pattern: re.Pattern[str] = _IDENTIFIER
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is not a valid identifier")
    return value


def _array(value: object, name: str, minimum: int = 1) -> list[object]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ValueError(f"{name} must be an array of at least {minimum} items")
    return value


def _role(
    value: object,
    families: frozenset[str],
    *,
    implementation: bool = False,
    implicit_codex: bool = False,
) -> tuple[str, str, str]:
    names = ("model", "effort") if implicit_codex else ("provider", "model", "effort")
    fields = _fields(value, names)
    provider = "codex" if implicit_codex else _token(fields["provider"], "provider")
    model = _token(fields["model"], "model")
    effort = _token(fields["effort"], "effort")
    providers = ("codex", "claude-code") if implementation else ("codex", "claude")
    if provider not in providers:
        raise ValueError("provider is unavailable for this role")
    runtime = "codex" if provider == "codex" else "claude"
    match = _MODEL_PATTERNS[runtime].fullmatch(model)
    if match is None or match["family"] not in families:
        raise ValueError("model family is unavailable for this role")
    if effort not in _EFFORTS[runtime]:
        raise ValueError("effort is unavailable for this provider")
    return provider, model, effort


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


@dataclass(slots=True)
class _Command:
    workspace: Workspace
    executables: dict[str, str]
    input_directory: Path
    test_options: list[str]
    args: dict[str, object]
    argv: list[str]
    inputs: dict[str, str] = field(default_factory=dict)

    def storage(
        self, *, repository: bool = False, runtime_directory: bool = False
    ) -> None:
        data = self.workspace.data_dir
        self.argv.extend(("--database", str(data / "reviews.sqlite3")))
        if repository:
            self.argv.extend(("--repository", str(self.workspace.repository)))
        if runtime_directory:
            run = data / f"web-run-{self.input_directory.parent.name}"
            self.argv.extend(("--directory", str(run)))

    def bins(self, *, codex_only: bool = False) -> None:
        self.argv.extend(("--codex-bin", self.executables["codex"]))
        if not codex_only:
            self.argv.extend(("--claude-bin", self.executables["claude"]))

    def owned(self, name: str, kind: str) -> str:
        identifier = _identifier(self.args[name], name)
        require_owned(self.workspace, kind, identifier)
        return identifier

    def members(self, name: str, kind: str, *, minimum: int = 1) -> None:
        values = _array(self.args[name], name, minimum)
        identifiers = [_identifier(value, name) for value in values]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError(f"{name} must contain distinct IDs")
        for identifier in identifiers:
            require_owned(self.workspace, kind, identifier)
        self.argv.extend(identifiers)

    def token(self, name: str, choices: tuple[str, ...] = ()) -> None:
        if name in self.args:
            value = _token(self.args[name], name)
            if choices and value not in choices:
                raise ValueError(f"invalid {name}")
            self.argv.extend((_flag(name), value))

    def revision(self, name: str) -> None:
        if name in self.args:
            revision = _string(self.args[name], name, nonempty=True)
            if revision.startswith("-") or "\r" in revision or "\n" in revision:
                raise ValueError(f"invalid {name} commit revision")
            self.argv.extend((_flag(name), revision))

    def attempt(self, name: str) -> None:
        if name in self.args:
            value = self.args[name]
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError(f"{name} must be a positive integer")
            self.argv.extend((_flag(name), str(value)))

    def boolean(self, name: str, *, when: bool = True, flag: str | None = None) -> bool:
        if name not in self.args:
            return False
        value = self.args[name]
        if type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
        if value == when:
            self.argv.append(flag or _flag(name))
        return value

    def input_file(self, name: str, extension: str, contents: str) -> None:
        filename = name + extension
        self.inputs[filename] = contents
        self.argv.extend((_flag(name), str(self.input_directory / filename)))

    def text_file(self, name: str) -> None:
        self.input_file(name, ".txt", _string(self.args[name], name, nonempty=True))

    def seats(self, *, advisors: bool = False) -> None:
        name = "advisors" if advisors else "seats"
        values = _array(self.args[name], name)
        if len(values) > 2:
            raise ValueError(f"{name} must contain one or two roles")
        choices = [_role(value, _ADVISORY if advisors else _REVIEW) for value in values]
        coordinate = 0 if advisors else 1
        if len({choice[coordinate] for choice in choices}) != len(choices):
            raise ValueError(f"{name} must contain distinct roles")
        for choice in choices:
            self.argv.extend(("--advisor" if advisors else "--seat", *choice))

    def retry_seats(self) -> None:
        if "seat_keys" in self.args:
            keys = [
                _identifier(key, "seat_keys")
                for key in _array(self.args["seat_keys"], "seat_keys")
            ]
            if len(set(keys)) != len(keys):
                raise ValueError("seat_keys must contain distinct keys")
            for key in keys:
                self.argv.extend(("--seat", key))


def _project_goal(command: _Command, action: str) -> None:
    command.argv.extend(("--repo", str(command.workspace.repository)))
    command.revision("baseline")
    command.text_file("original_goal")
    if "project_id" in command.args:
        command.argv.extend(("--project-id", command.owned("project_id", "project")))
    if action == "project.consult":
        command.argv.extend(
            ("--consultation-id", f"consultation-{command.input_directory.parent.name}")
        )
        command.seats(advisors=True)
        command.bins()
        return

    command.text_file("confirmed_goal")
    if "consultation_id" in command.args:
        command.argv.extend(
            ("--consultation", command.owned("consultation_id", "consultation"))
        )
    elif "adoption" in command.args:
        raise ValueError("adoption requires consultation_id")
    command.token("adoption", ("accepted", "edited"))


def _consultation_recovery(command: _Command, _action: str) -> None:
    command.argv.append(command.owned("consultation_id", "consultation"))


def _project_plan(command: _Command, action: str) -> None:
    if action == "project.plan":
        command.argv.append(command.owned("goal_id", "goal"))
        command.argv.extend(("--planner", *_role(command.args["planner"], _ADVISORY)))
        command.bins()
    else:
        command.argv.append(command.owned("plan_id", "project-plan"))
        document = parse_project_plan(command.args["document"])
        command.input_file(
            "document",
            ".json",
            json.dumps(document, ensure_ascii=False, allow_nan=False),
        )


def _stage(command: _Command, action: str) -> None:
    if action == "project.stage.start":
        command.argv.extend(
            (
                command.owned("plan_id", "project-plan"),
                _identifier(command.args["stage_id"], "stage_id", _STAGE_ID),
            )
        )
        command.token("mode", ("direct", "delegate"))
        command.revision("baseline")
        if "sol" in command.args:
            _, model, effort = _role(
                command.args["sol"], frozenset(("sol",)), implicit_codex=True
            )
            command.argv.extend(("--sol", model, effort))
        if "card_review" in command.args:
            command.argv.extend(
                ("--card-review", *_role(command.args["card_review"], _REVIEW))
            )
        command.bins()
        return

    stage_id = command.owned("stage_run_id", "stage")
    command.argv.append(stage_id)
    if action == "project.stage.resume":
        if command.boolean("prepare"):
            command.bins()
    elif action == "project.stage.leaf":
        command.argv.append(_identifier(command.args["card_id"], "card_id", _CARD_ID))
        if "implementation" in command.args:
            command.argv.extend(
                (
                    "--implementation",
                    *_role(command.args["implementation"], _LEAF, implementation=True),
                )
            )
        if "card_review" in command.args:
            command.argv.extend(
                ("--card-review", *_role(command.args["card_review"], _REVIEW))
            )
        command.revision("baseline")
        command.boolean("review_only")
        command.boolean("refreeze")
    else:
        command.token("target")
        stage = read_resource(command.workspace, "stage", stage_id, {})["data"]
        if stage["mode"] != "delegate" or not stage["can_integrate"]:
            raise ActionConflict(
                "the stage needs a verified candidate for every delegated leaf"
            )


def _planning_declarations(value: object) -> str:
    declarations = _fields(value, ("dependencies", "decisions"))
    entries = {
        "dependencies": ("task_id", "baseline_commit", "source", "detail"),
        "decisions": ("question", "resolution", "source", "detail"),
    }
    for name, fields in entries.items():
        for item in _array(declarations[name], name, minimum=0):
            declaration = _fields(item, fields)
            for key in fields:
                _string(declaration[key], key, nonempty=True)
    return json.dumps(declarations, ensure_ascii=False, allow_nan=False)


def _planning(command: _Command, action: str) -> None:
    if action == "plan.retry":
        identifier = _identifier(command.args["planning_run_id"], "planning_run_id")
        plan = read_resource(
            command.workspace, "plan", identifier, {"implementation": ["1"]}
        )["data"]
        if not plan["retry_configurable"]:
            raise ActionConflict(
                "the standalone plan cannot be retried in its current state"
            )
        command.storage()
        command.argv.extend(("--retry", identifier))
        return

    command.storage(repository=True, runtime_directory=True)
    command.revision("baseline")
    command.text_file("requirements")
    for name, provider, families in (
        ("draft", "claude", _DRAFT),
        ("plan_review", "codex", _PLAN_REVIEW),
    ):
        if name in command.args:
            role = _fields(command.args[name], ("model", "effort"))
            _, model, effort = _role({"provider": provider, **role}, families)
            command.argv.extend((_flag(name), model, effort))
    if "declarations" in command.args:
        command.input_file(
            "declarations",
            ".json",
            _planning_declarations(command.args["declarations"]),
        )
    command.bins()


def _task_run(command: _Command, action: str) -> None:
    if action == "task.run_batch":
        command.members("planning_run_ids", "plan", minimum=2)
    else:
        command.argv.append(command.owned("planning_run_id", "plan"))
    command.storage(repository=True, runtime_directory=True)
    if action == "task.retry":
        command.argv.append("--retry")
        return

    if "implementation" in command.args:
        command.argv.extend(
            (
                "--implementation",
                *_role(
                    command.args["implementation"], _IMPLEMENTATION, implementation=True
                ),
            )
        )
    if "fallbacks" in command.args:
        for item in _array(command.args["fallbacks"], "fallbacks", minimum=0):
            command.argv.extend(
                ("--fallback", *_role(item, _IMPLEMENTATION, implementation=True))
            )
    command.token("task_type")
    command.bins()


def _review_freeze(command: _Command, _action: str) -> None:
    if "diff" in command.args:
        if command.args.keys() & {"baseline", "candidate", "tests"}:
            raise ValueError("a pasted diff cannot include revisions or supplied tests")
        command.text_file("diff")
        command.argv.extend(("--diff-repository", str(command.workspace.repository)))
    else:
        if not {"baseline", "candidate"} <= command.args.keys():
            raise ValueError("freezing a commit pair requires baseline and candidate")
        command.argv.extend(("--repo", str(command.workspace.repository)))
        command.revision("baseline")
        command.revision("candidate")
        if "tests" in command.args:
            tests = _fields(command.args["tests"], ("required_commands", "results"))
            for key, value in tests.items():
                _array(value, key, minimum=0)
            command.input_file(
                "tests", ".json", json.dumps(tests, ensure_ascii=False, allow_nan=False)
            )
    command.text_file("requirements")
    for item in _array(command.args["acceptance"], "acceptance"):
        criterion = _string(item, "acceptance", nonempty=True)
        command.argv.append(f"--acceptance={criterion}")


def _independent_review(command: _Command, action: str) -> None:
    if action == "review.run":
        command.argv.append(command.owned("snapshot_id", "snapshot"))
        command.seats()
        command.bins()
        return
    command.argv.append(command.owned("round_id", "review"))
    if action == "review.retry":
        command.retry_seats()


def _task_evidence(command: _Command, action: str) -> None:
    if action in ("task.test", "task.review"):
        command.argv.append(command.owned("task_run_id", "task"))
        command.storage(repository=True, runtime_directory=True)
        command.attempt("implementation_attempt")
        if action == "task.test":
            for name in _array(command.args["commands"], "commands"):
                command.argv.extend(("--command", _token(name, "commands")))
            source = _string(
                command.args["declaration_source"], "declaration_source", nonempty=True
            )
            command.argv.append(f"--declaration-source={source}")
            command.bins(codex_only=True)
            command.argv.extend(command.test_options)
        else:
            command.attempt("test_attempt")
            command.seats()
            command.boolean("auto_rework", when=False, flag="--no-auto-rework")
            command.bins()
        return

    command.argv.append(command.owned("round_id", "task-review"))
    command.storage(runtime_directory=action == "task.rework")
    if action == "task.retry_review":
        command.retry_seats()


def _integration(command: _Command, action: str) -> None:
    if action == "integration.create":
        command.members("task_run_ids", "task")
        command.storage(repository=True)
        command.token("target")
        return

    command.argv.append(command.owned("integration_id", "integration"))
    command.storage(
        repository=True,
        runtime_directory=action in ("integration.test", "integration.review"),
    )
    if action == "integration.test":
        command.bins(codex_only=True)
        command.argv.extend(command.test_options)
    elif action == "integration.review":
        command.seats()
        command.attempt("test_attempt")
        command.boolean("auto_rework", when=False, flag="--no-auto-rework")
        command.bins()
    elif action != "integration.rebase":
        command.argv.extend(("--review", command.owned("round_id", "review")))
        if action == "integration.retry_review":
            command.retry_seats()


type _Handler = Callable[[_Command, str], None]
type _Spec = tuple[str, _Handler, tuple[str, ...], tuple[str, ...]]
_STRATEGY = ("implementation", "fallbacks", "task_type")
_ACTIONS: dict[str, _Spec] = {
    "project.consult": (
        "project consult",
        _project_goal,
        ("baseline", "original_goal", "advisors"),
        ("project_id",),
    ),
    "project.consult.reconcile": (
        "project reconcile-consultation",
        _consultation_recovery,
        ("consultation_id",),
        (),
    ),
    "project.confirm": (
        "project confirm",
        _project_goal,
        ("baseline", "original_goal", "confirmed_goal"),
        ("project_id", "consultation_id", "adoption"),
    ),
    "project.plan": ("project plan", _project_plan, ("goal_id", "planner"), ()),
    "project.revise": ("project revise", _project_plan, ("plan_id", "document"), ()),
    "project.stage.start": (
        "project stage start",
        _stage,
        ("plan_id", "stage_id"),
        ("mode", "baseline", "sol", "card_review"),
    ),
    "project.stage.resume": (
        "project stage resume",
        _stage,
        ("stage_run_id",),
        ("prepare",),
    ),
    "project.stage.leaf": (
        "project stage leaf",
        _stage,
        ("stage_run_id", "card_id"),
        ("implementation", "card_review", "baseline", "review_only", "refreeze"),
    ),
    "project.stage.integrate": (
        "project stage integrate",
        _stage,
        ("stage_run_id",),
        ("target",),
    ),
    "plan.create": (
        "task plan",
        _planning,
        ("baseline", "requirements"),
        ("draft", "plan_review", "declarations"),
    ),
    "plan.retry": ("task plan", _planning, ("planning_run_id",), ()),
    "task.run": ("task run", _task_run, ("planning_run_id",), _STRATEGY),
    "task.retry": ("task run", _task_run, ("planning_run_id",), ()),
    "task.run_batch": ("task run-batch", _task_run, ("planning_run_ids",), _STRATEGY),
    "task.test": (
        "task-test run",
        _task_evidence,
        ("task_run_id", "implementation_attempt", "commands", "declaration_source"),
        (),
    ),
    "task.review": (
        "task-review run",
        _task_evidence,
        ("task_run_id", "implementation_attempt", "test_attempt", "seats"),
        ("auto_rework",),
    ),
    "task.retry_review": (
        "task-review retry",
        _task_evidence,
        ("round_id",),
        ("seat_keys",),
    ),
    "task.confirm": ("task-review confirm", _task_evidence, ("round_id",), ()),
    "task.rework": ("task rework", _task_evidence, ("round_id",), ()),
    "review.freeze": (
        "freeze",
        _review_freeze,
        ("requirements", "acceptance"),
        ("baseline", "candidate", "diff", "tests"),
    ),
    "review.run": ("run", _independent_review, ("snapshot_id", "seats"), ()),
    "review.retry": (
        "retry",
        _independent_review,
        ("round_id",),
        ("seat_keys",),
    ),
    "review.confirm": ("confirm", _independent_review, ("round_id",), ()),
    "integration.create": (
        "integration create",
        _integration,
        ("task_run_ids", "target"),
        (),
    ),
    "integration.test": ("integration test", _integration, ("integration_id",), ()),
    "integration.review": (
        "integration review",
        _integration,
        ("integration_id", "seats"),
        ("auto_rework", "test_attempt"),
    ),
    "integration.retry_review": (
        "integration retry-review",
        _integration,
        ("integration_id", "round_id"),
        ("seat_keys",),
    ),
    "integration.confirm": (
        "integration confirm",
        _integration,
        ("integration_id", "round_id"),
        (),
    ),
    "integration.rework": (
        "integration rework",
        _integration,
        ("integration_id", "round_id"),
        (),
    ),
    "integration.rebase": ("integration rebase", _integration, ("integration_id",), ()),
    "integration.accept": (
        "integration accept",
        _integration,
        ("integration_id", "round_id"),
        (),
    ),
}


def prepare_action(
    action: str,
    args: object,
    *,
    workspace: Workspace,
    executables: dict[str, str],
    directory: Path,
    tool_directories: tuple[str, ...],
    runtime_read_paths: tuple[str, ...],
) -> list[str]:
    """Validate the request and prepare any private CLI input files."""
    if action not in _ACTIONS:
        raise UnknownAction("the requested action is unavailable")
    subcommand, handler, required, optional = _ACTIONS[action]
    test_options: list[str] = []
    for flag, paths in (
        ("--tool-directory", tool_directories),
        ("--runtime-read-path", runtime_read_paths),
    ):
        for path in paths:
            test_options.extend((flag, path))
    command = _Command(
        workspace,
        executables,
        directory,
        test_options,
        _fields(args, required, optional),
        [
            sys.executable,
            "-P",
            "-m",
            "multi_agent",
            "--data-dir",
            str(workspace.data_dir),
            "--structured-errors",
            *subcommand.split(),
        ],
    )
    handler(command, action)
    for filename, contents in command.inputs.items():
        descriptor = os.open(
            directory / filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(contents)
    return command.argv
