"""Project the chosen Git workspace and the six workbench model presets."""

import argparse
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multi_agent.cli.project_stage import add_stage_parser
from multi_agent.cli.snapshot import _git, _resolve_commit
from multi_agent.cli.task_inputs import RULES_PATH, freeze_project_rules
from multi_agent.core.dispatch import resolve_dispatch
from multi_agent.core.tasks import FrozenProjectRules, ImplementationConfig, TaskCard
from multi_agent.store.database import ArchivedDatabase

_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MODELS = (
    ("Astra", "codex", "gpt-6.2-astra", None),
    ("Sol", "codex", "gpt-6.2-sol", None),
    ("Terra", "codex", "gpt-6.1-terra", None),
    ("Fable", "claude", "claude-fable-5-2", _CLAUDE_EFFORTS),
    ("Opus", "claude", "claude-opus-5-5", _CLAUDE_EFFORTS),
    ("Sonnet", "claude", "claude-sonnet-5-5", _CLAUDE_EFFORTS),
)
_IMPLEMENTATION = ("Sol", "Terra", "Sonnet")
_ROLES = (
    ("advisor", "需求顾问", ("Astra", "Fable")),
    ("planner", "项目规划", ("Astra", "Fable")),
    ("stage_lead", "阶段负责人", ("Sol",)),
    ("implementation", "实施", _IMPLEMENTATION),
    ("fallback", "实施备选", _IMPLEMENTATION),
    ("review", "独立审核", tuple(family for family, _, _, _ in _MODELS)),
)

_MODEL_PATTERNS = {
    "codex": re.compile(
        r"gpt-(?P<version>[0-9]+(?:\.[0-9]+)*)-(?P<family>astra|sol|terra)"
    ),
    "claude": re.compile(
        r"claude-(?P<family>fable|opus|sonnet)-(?P<version>[0-9]+(?:-[0-9]+)*)"
    ),
}


def private_directory(path: Path, *, parents: bool = False) -> None:
    """Create a private directory or require that the existing path is private."""
    path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError(
            "workspace data paths must be private directories owned by the current user"
        )


@dataclass(frozen=True, slots=True)
class Workspace:
    repository: Path
    data_dir: Path
    archive: ArchivedDatabase | None = None

    @classmethod
    def open(
        cls, repository: Path, data_dir: Path, *, read_only_database: Path | None = None
    ) -> "Workspace":
        repo = repository.expanduser().resolve(strict=True)
        git_dir = Path(
            _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode("utf-8")
            .removesuffix("\n")
        ).resolve(strict=True)
        if _git(repo, "rev-parse", "--is-bare-repository").strip() == b"false":
            repo = Path(
                _git(repo, "rev-parse", "--show-toplevel")
                .decode("utf-8")
                .removesuffix("\n")
            ).resolve(strict=True)
        directory = data_dir.expanduser().resolve()
        worktrees = [
            Path(entry[9:].decode("utf-8")).resolve()
            for entry in _git(repo, "worktree", "list", "--porcelain", "-z").split(
                b"\0"
            )
            if entry.startswith(b"worktree ")
        ]
        if any(directory.is_relative_to(path) for path in (repo, git_dir, *worktrees)):
            raise ValueError(
                "data directory must be outside the repository and all worktrees"
            )
        archive = (
            ArchivedDatabase.open(read_only_database)
            if read_only_database is not None
            else None
        )
        if archive is not None and (
            directory.is_relative_to(archive.path.parent)
            or archive.path.is_relative_to(directory)
        ):
            raise ValueError("archive viewing requires a separate data directory")
        private_directory(directory, parents=True)
        return cls(repo, directory, archive)

    @property
    def database(self) -> Path:
        return (
            self.archive.path
            if self.archive is not None
            else self.data_dir / "reviews.sqlite3"
        )

    def view(self, environments: dict[str, Any]) -> dict[str, Any]:
        branch = (
            _git(self.repository, "branch", "--show-current")
            .decode("utf-8")
            .removesuffix("\n")
        )
        try:
            head = _resolve_commit(self.repository, "HEAD")
        except ValueError:
            if not branch:
                raise
            branch_ref = f"refs/heads/{branch}"
            refs = _git(
                self.repository, "for-each-ref", "--format=%(refname)", branch_ref
            )
            if branch_ref.encode("utf-8") in refs.splitlines():
                raise
            head = None
        return {
            "repository": str(self.repository),
            "target_branch": branch or None,
            "head": head,
            "branches": _branches(self.repository),
            "defaults": _stage_defaults(),
            "read_only": self.archive is not None,
            "presets": {
                "source": "workbench_model_presets",
                "models": [
                    {
                        "family": family,
                        "provider": provider,
                        "model": model,
                        "efforts": efforts,
                    }
                    for family, provider, model, efforts in _MODELS
                ],
                "roles": _role_options(environments),
                "rules": _rules(self.repository, head),
            },
            "environments": environments,
        }


def _branches(repository: Path) -> list[dict[str, str]]:
    prefix = "refs/heads/"
    refs = _git(
        repository,
        "for-each-ref",
        "--sort=refname",
        "--format=%(refname)%00%(objectname)",
        prefix,
    )
    return [
        {
            "name": name.decode("utf-8").removeprefix(prefix),
            "head": head.decode("ascii"),
        }
        for name, head in (line.split(b"\0") for line in refs.splitlines())
    ]


def _stage_defaults() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    add_stage_parser(parser.add_subparsers(required=True))
    args = parser.parse_args(["stage", "start", "plan", "stage"])
    model, effort = args.sol
    provider, review_model, review_effort = args.card_review
    return {
        "source": "cli_default",
        "stage_mode": args.mode,
        "sol": {"model": model, "effort": effort},
        "card_review": {
            "provider": provider,
            "model": review_model,
            "effort": review_effort,
        },
    }


def _latest_models(provider: str, catalog: list[str]) -> dict[str, str]:
    newest: dict[str, tuple[tuple[int, ...], str]] = {}
    for model in catalog:
        match = _MODEL_PATTERNS[provider].fullmatch(model)
        if match is None:
            continue
        family = match["family"].capitalize()
        version = tuple(int(part) for part in re.split(r"[.-]", match["version"]))
        entry = (version, model)
        if family not in newest or entry > newest[family]:
            newest[family] = entry
    return {family: model for family, (_, model) in newest.items()}


def _role_options(environments: dict[str, Any]) -> list[dict[str, Any]]:
    selected = {
        provider: _latest_models(provider, observation["models"])
        for provider, observation in environments.items()
        if observation is not None and observation["models"] is not None
    }
    candidates = {}
    effort_source: str | None
    for family, provider, preset_model, suggested_efforts in _MODELS:
        if provider not in selected:
            if provider == "codex":
                continue
            model = preset_model
            source = "workbench_preset"
            efforts = suggested_efforts
            effort_source = source
        else:
            if family not in selected[provider]:
                continue
            model = selected[provider][family]
            source = "runtime_catalog"
            model_efforts = environments[provider]["model_efforts"]
            efforts = model_efforts.get(model) if model_efforts is not None else None
            effort_source = source if efforts is not None else None
        candidates[family] = {
            "family": family,
            "provider": provider,
            "model": model,
            "efforts": efforts,
            "source": source,
            "effort_source": effort_source,
        }

    return [
        {
            "id": role,
            "label": label,
            "families": families,
            "candidates": [
                candidates[family] for family in families if family in candidates
            ],
        }
        for role, label, families in _ROLES
    ]


def _provider(provider: str) -> str:
    # Rules use the implementation provider spelling, whereas the catalog and
    # HTTP environment action use RuntimeAdapter's provider names.
    return {"codex": "codex", "claude-code": "claude"}[provider]


def _config(config: ImplementationConfig) -> dict[str, Any]:
    return {
        "provider": _provider(config.provider),
        "implementation_provider": config.provider,
        "allowed_models": config.allowed_models,
        "default_model": config.default_model,
        "default_effort": config.default_effort,
    }


def _scope(paths: tuple[str, ...], frozen: FrozenProjectRules) -> dict[str, Any]:
    # Run the actual dispatch-policy intersection for this preview. This
    # transient card is never persisted or submitted for implementation.
    card = TaskCard(
        1,
        "T-0000",
        "Preview implementation scope",
        frozen.baseline_commit,
        paths,
        (),
        (),
        (),
        (),
        (),
        (),
    )
    try:
        decision = resolve_dispatch(card, frozen)
    except ValueError as error:
        return {
            "paths": paths,
            "provider": None,
            "allowed_models": [],
            "matched_paths": None,
            "error": str(error),
        }
    return {
        "paths": paths,
        "provider": _provider(decision.choice.provider),
        "allowed_models": decision.allowed_models,
        "matched_paths": tuple(
            path
            for index in decision.matched_rule_indices
            for path in frozen.rules.implementation[index].paths
        ),
        "error": None,
    }


def _rules(repository: Path, head: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unavailable",
        "source": RULES_PATH,
        "baseline_commit": head,
        "rules_hash": None,
        "protected_paths": [],
        "implementation": [],
        "default_implementation": None,
        "task_types": [],
        "applies_to": ["implementation", "fallback"],
        "error": None,
    }
    if head is None:
        return result
    # Missing configuration is normal on repositories that haven't registered
    # commands yet. A present but invalid Git blob has a visible error instead.
    if not _git(repository, "ls-tree", "--full-tree", "-z", head, "--", RULES_PATH):
        return result
    try:
        frozen = freeze_project_rules(repository, head)
    except (TypeError, ValueError):
        return {
            **result,
            "status": "error",
            "error": "The committed implementation rules could not be validated.",
        }
    return {
        **result,
        "status": "available",
        "rules_hash": frozen.rules_hash,
        "protected_paths": frozen.rules.protected_paths,
        "implementation": [
            {
                "paths": rule.paths,
                **_config(rule.config),
                "effective": _scope(rule.paths, frozen),
            }
            for rule in frozen.rules.implementation
        ],
        "default_implementation": (
            _config(frozen.rules.default_implementation)
            if frozen.rules.default_implementation is not None
            else None
        ),
        "task_types": [
            {
                "name": item.name,
                "provider": _provider(item.provider),
                "implementation_provider": item.provider,
                "allowed_models": item.allowed_models,
            }
            for item in frozen.rules.task_types
        ],
    }
