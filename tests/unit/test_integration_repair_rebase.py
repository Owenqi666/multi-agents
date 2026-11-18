"""Carry a verified repair forward through the real integration Git route."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
import test_task_workflow_cli as helpers
from test_integration_cli import create_args, implement

from multi_agent.cli.integration_cli import create_integration
from multi_agent.core.integration import IntegrationCandidate
from multi_agent.core.integration_rework import IntegrationRepairDelta
from multi_agent.store.integration_store import IntegrationStore

workflow = helpers.workflow
REPAIR_PATH = "frontend/style.js"
OLD = b'export const theme = "default";\n'
REPAIRED = b'export const theme = "accepted";\n'


def _commit(workflow, base, label, files):
    repo = workflow.repository
    checkout = workflow.base / label
    helpers.git(repo, "worktree", "add", "--quiet", "--detach", str(checkout), base)
    try:
        for path, contents in files.items():
            (checkout / path).write_bytes(contents)
        helpers.git(checkout, "add", *files)
        helpers.git(checkout, "commit", "--quiet", "-m", label)
        return helpers.git(checkout, "rev-parse", "HEAD")
    finally:
        helpers.git(repo, "worktree", "remove", str(checkout))


def test_rebase_preserves_the_approved_repair_or_refuses_publication(
    workflow, monkeypatch
):
    repo = workflow.repository
    target = helpers.git(repo, "symbolic-ref", "HEAD")
    (repo / REPAIR_PATH).write_bytes(OLD)
    helpers.git(repo, "add", REPAIR_PATH)
    helpers.git(repo, "commit", "--quiet", "-m", "Add theme source")
    baseline = helpers.git(repo, "rev-parse", "HEAD")
    workflow.baseline = baseline
    first, _ = implement(
        workflow, "T-0280", "frontend/**", {"frontend/status.js": helpers.FIXED_BYTES}
    )
    second, _ = implement(
        workflow, "T-0281", "frontend/banner.js", {"frontend/banner.js": b"banner\n"}
    )
    integrated = helpers.successful(*create_args(workflow, first, second))
    with IntegrationStore.open_readonly(workflow.database) as store:
        original = store.load_integration(integrated["integration_id"])
    repaired_id = uuid4().hex
    repair_base = integrated["candidate"]["commit"]
    repaired_commit = _commit(
        workflow, repair_base, "approved-repair", {REPAIR_PATH: REPAIRED}
    )
    repaired_tree = helpers.git(repo, "rev-parse", f"{repaired_commit}^{{tree}}")
    repaired_ref = f"refs/multi-agent/integrations/{repaired_id}"
    helpers.git(repo, "update-ref", repaired_ref, repaired_commit)
    repaired = replace(
        original,
        integration_id=repaired_id,
        result=IntegrationCandidate(repaired_ref, repaired_commit, repaired_tree),
    )
    with IntegrationStore(workflow.database) as store:
        store.create_integration(repaired)

    # Exercise the Git application boundary with the same frozen tuple the
    # repair store supplies, independently of its implementation reservation.
    lineage = (
        original,
        repaired,
        original.members[0],
        IntegrationRepairDelta(
            repair_base, repaired_commit, repaired_tree, (REPAIR_PATH,)
        ),
    )
    looked_up = []

    def repair_for_rebase(_store, source_id):
        looked_up.append(source_id)
        return lineage

    monkeypatch.setattr(
        IntegrationStore, "integration_repair_for_rebase", repair_for_rebase
    )
    task_ids = [first["task_run_id"], second["task_run_id"]]

    def rebase(source_id):
        return create_integration(
            workflow.database, repo, target, task_ids, rebased_from=source_id
        )

    advanced = _commit(
        workflow, baseline, "unrelated-target", {"target.txt": b"advance\n"}
    )
    helpers.git(repo, "update-ref", target, advanced, baseline)
    rebased = rebase(repaired_id)
    assert isinstance(rebased.result, IntegrationCandidate)
    assert (
        helpers.git(repo, "show", f"{rebased.result.commit}:{REPAIR_PATH}")
        == REPAIRED.decode().strip()
    )
    assert (
        helpers.git(repo, "show", f"{rebased.result.commit}:frontend/banner.js")
        == "banner"
    )
    assert helpers.git(repo, "show", f"{rebased.result.commit}:target.txt") == "advance"
    with IntegrationStore.open_readonly(workflow.database) as store:
        assert store.integration_parent(rebased.integration_id) == repaired_id

    saved_refs = helpers.git(repo, "for-each-ref", "refs/multi-agent/integrations")
    rules = json.loads(workflow.rules_bytes)
    rules["protected_paths"].append(REPAIR_PATH)
    protected_target = _commit(
        workflow,
        advanced,
        "protect-target-path",
        {".multi-agent/rules.yaml": json.dumps(rules).encode()},
    )
    helpers.git(repo, "update-ref", target, protected_target, advanced)
    with pytest.raises(ValueError, match=r"repair for T-0280.*frontend/style\.js"):
        rebase(rebased.integration_id)
    assert (
        helpers.git(repo, "for-each-ref", "refs/multi-agent/integrations") == saved_refs
    )

    conflict_target = _commit(
        workflow,
        advanced,
        "conflicting-target",
        {REPAIR_PATH: b'export const theme = "target";\n'},
    )
    helpers.git(repo, "update-ref", target, conflict_target, protected_target)
    with pytest.raises(
        ValueError, match=r"repair for T-0280.*Git conflict.*frontend/style\.js"
    ):
        rebase(rebased.integration_id)
    assert (
        helpers.git(repo, "for-each-ref", "refs/multi-agent/integrations") == saved_refs
    )
    assert looked_up == [repaired_id, rebased.integration_id, rebased.integration_id]
