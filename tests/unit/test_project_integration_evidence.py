"""Read stage integration evidence against real SQLite rows and pinned Git commits."""

from dataclasses import asdict, replace

import pytest
from test_task_workflow_cli import git

from multi_agent.cli import project_integration
from multi_agent.core.integration import (
    IntegrationCandidate,
    IntegrationConflict,
    IntegrationMember,
    IntegrationRecord,
)
from multi_agent.core.task_state import TaskInput
from multi_agent.core.tasks import FrozenProjectRules, ProjectRules, TaskCard
from multi_agent.core.workspaces import WorkspaceDelivery
from multi_agent.store.project_integration import ProjectIntegrationStore


@pytest.fixture
def stage(tmp_path):
    repository = tmp_path.resolve() / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main", "--quiet", "--template=")
    git(repository, "commit", "--allow-empty", "--quiet", "-m", "base")
    baseline = git(repository, "rev-parse", "HEAD")
    tree = git(repository, "rev-parse", "HEAD^{tree}")
    common = repository / ".git"
    database = tmp_path.resolve() / "evidence.sqlite3"
    rules = FrozenProjectRules(
        str(common), baseline, baseline, "rules", ProjectRules(1, (), (), (), None)
    )
    members = tuple(
        IntegrationMember(
            f"leaf-{number}",
            f"plan-{number}",
            1,
            TaskInput(
                TaskCard(
                    1, f"T-{number:04}", "leaf", baseline, (), (), (), (), (), (), ()
                ),
                rules,
                "requirements",
                "then",
            ),
            WorkspaceDelivery(
                baseline,
                f"{number:x}" * 40,
                (),
                "digest",
                f"refs/leaves/{number}",
                tree,
            ),
            (),
        )
        for number in (1, 2)
    )
    coordinates = tuple(
        (member.task_run_id, member.attempt, member.delivery.candidate)
        for member in members
    )

    def record(identifier, *, selected=members, base=baseline, conflict=False):
        if conflict:
            result = IntegrationConflict(("src/ledger.py",))
        else:
            ref = f"refs/multi-agent/integrations/{identifier}"
            commit = git(repository, "commit-tree", tree, "-p", base, "-m", identifier)
            git(repository, "update-ref", ref, commit)
            result = IntegrationCandidate(ref, commit, tree)
        value = IntegrationRecord(
            identifier,
            str(common),
            "refs/heads/main",
            base,
            selected,
            0 if conflict else len(selected),
            result,
        )
        with ProjectIntegrationStore(database) as store:
            # Seed completed integration rows without launching unrelated models.
            store._insert_integration(value)
        return value

    def show(selected=coordinates, *, integration_id=None, repository=common):
        return project_integration.view_delegated_integration(
            database, repository, baseline, selected, integration_id=integration_id
        )

    return repository, baseline, members, coordinates, record, show


def _recorded_evidence(
    record, *, verdict="ready", test_status="succeeded", superseded=None
):
    return {
        "test": {
            "attempt": 2,
            "candidate": asdict(record.result),
            "status": test_status,
            "evidence_hash": "recorded-test-hash",
        },
        "test_evidence_verification": "verified"
        if test_status == "succeeded"
        else "unverified",
        "reviews": [
            {
                "test_attempt": 2,
                "round_id": "old-review",
                "snapshot_id": "old-snapshot",
                "verification": "verified",
                "assessment": {"status": "ready"},
            },
            {
                "test_attempt": 2,
                "round_id": "latest-review",
                "snapshot_id": "latest-snapshot",
                "verification": "verified",
                "assessment": {"status": verdict},
            },
        ],
        "current_eligible": False,
        "superseded_by": superseded,
    }


def test_delegated_view_requires_current_exact_member_versions(stage, monkeypatch):
    _, _, members, coordinates, record, show = stage
    exact = record("exact")
    record("missing-leaf", selected=members[:1])
    record("stale-attempt", selected=(replace(members[0], attempt=2), members[1]))
    record(
        "stale-candidate",
        selected=(
            replace(
                members[0], delivery=replace(members[0].delivery, candidate="f" * 40)
            ),
            members[1],
        ),
    )
    monkeypatch.setattr(
        project_integration,
        "verification_view",
        lambda *args, **kwargs: _recorded_evidence(exact),
    )

    result = show(coordinates[::-1])
    assert result["integration_id"] == "exact"
    assert result["candidate"] == exact.result.commit
    assert result["tests_passed"] and result["review_satisfied"]
    assert show((*coordinates, coordinates[0])) is None


def test_delegated_view_prefers_stage_baseline_and_supports_exact_id(
    stage, monkeypatch
):
    repository, _, _, coordinates, record, show = stage
    git(repository, "commit", "--allow-empty", "--quiet", "-m", "different baseline")
    other = git(repository, "rev-parse", "HEAD")
    wrong = record("wrong-baseline", base=other)
    mismatch = show()
    assert mismatch["integration_id"] == wrong.integration_id
    assert mismatch["candidate"] is None and "differs" in mismatch["source_error"]
    assert show(integration_id=wrong.integration_id) is None

    correct = record("selected")
    newer = record("newer-wrong-baseline", base=other)

    def observed(_store, actual, **kwargs):
        assert actual == correct
        return _recorded_evidence(correct)

    monkeypatch.setattr(project_integration, "verification_view", observed)
    preferred = show()
    assert preferred["integration_id"] == correct.integration_id
    assert preferred["review_satisfied"]
    record("newer-correct-conflict", conflict=True)
    assert not show()["review_satisfied"]
    assert show(integration_id=correct.integration_id) == preferred
    assert show(integration_id=newer.integration_id) is None
    assert show(integration_id="unknown-integration") is None
    assert show(coordinates[:1], integration_id=correct.integration_id) is None
    assert show(integration_id=correct.integration_id, repository=repository) is None


@pytest.mark.parametrize("blocker", ["conflict", "pin"])
def test_delegated_view_does_not_fall_back_from_newest(stage, monkeypatch, blocker):
    repository, baseline, _, _, record, show = stage
    record("old-ready")
    latest = record("newest", conflict=blocker == "conflict")
    if blocker == "pin":
        git(repository, "update-ref", latest.result.ref, baseline)

    def unexpected_verification(*args, **kwargs):
        pytest.fail(
            "an invalid selected integration cannot read or replace its evidence"
        )

    monkeypatch.setattr(
        project_integration, "verification_view", unexpected_verification
    )
    result = show()
    assert result["integration_id"] == "newest"
    assert result["candidate"] is None and result["evidence"] is None
    assert result["source_error"]
    assert not result["tests_passed"] and not result["review_satisfied"]


@pytest.mark.parametrize(
    ("verdict", "test_status", "superseded", "expected"),
    [
        ("ready", "succeeded", None, (True, True)),
        ("needs_rework", "succeeded", None, (True, False)),
        ("ready", "failed", None, (False, False)),
        ("ready", "succeeded", "repair", (False, False)),
    ],
)
def test_delegated_view_reports_latest_evidence_even_when_blocked(
    stage, monkeypatch, verdict, test_status, superseded, expected
):
    _, _, _, _, record, show = stage
    candidate = record("selected")
    evidence = _recorded_evidence(
        candidate, verdict=verdict, test_status=test_status, superseded=superseded
    )
    monkeypatch.setattr(
        project_integration, "verification_view", lambda *args, **kwargs: evidence
    )

    result = show()
    assert result["test_attempt"] == 2
    assert result["test_evidence_hash"] == "recorded-test-hash"
    assert result["review_round_id"] == "latest-review"
    assert result["review_snapshot_id"] == "latest-snapshot"
    assert result["evidence"] == evidence
    assert (result["tests_passed"], result["review_satisfied"]) == expected


def test_delegated_view_does_not_invent_missing_or_reused_evidence(stage, monkeypatch):
    _, _, _, _, record, show = stage
    candidate = record("new-candidate")
    initial = show()
    assert initial["candidate"] == candidate.result.commit
    assert initial["test_attempt"] is None and initial["test_evidence_hash"] is None
    assert initial["review_round_id"] is None and initial["review_snapshot_id"] is None
    assert not initial["tests_passed"] and not initial["review_satisfied"]

    evidence = _recorded_evidence(candidate)
    evidence["test"]["attempt"] = 3
    monkeypatch.setattr(
        project_integration, "verification_view", lambda *args, **kwargs: evidence
    )
    stale = show()
    assert stale["test_attempt"] == 3 and stale["tests_passed"]
    assert stale["review_round_id"] is None and stale["review_snapshot_id"] is None
    assert not stale["review_satisfied"]

    evidence["test"]["candidate"]["commit"] = "b" * 40
    swapped = show()
    assert swapped["test_attempt"] is None and swapped["test_evidence_hash"] is None
    assert not swapped["tests_passed"] and not swapped["review_satisfied"]
