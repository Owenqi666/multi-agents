"""An integration review keeps executed logs and selected current reviewer evidence."""

import json
from dataclasses import replace

import pytest
import test_executed_review_input as task_reviews

from multi_agent.cli import runner
from multi_agent.cli.integration_autorework import check_confirmation_decisions
from multi_agent.cli.integration_review import (
    _test_change_paths,
    check_integration_confirmation_link,
    check_rework_paths,
    inspect_integration_review,
    select_rework_member,
)
from multi_agent.cli.prompt import INTEGRATION_REVIEW_VERSION
from multi_agent.cli.review_inputs import decode_review_input, load_review_input
from multi_agent.cli.snapshot import materialize_review_input
from multi_agent.core import SeatConfig
from multi_agent.core.events import AttemptStarted, Completed, Envelope, freeze_json
from multi_agent.core.integration import (
    IntegrationCandidate,
    IntegrationMember,
    IntegrationRecord,
)
from multi_agent.core.review_input import IntegrationReviewInput
from multi_agent.core.snapshot import FrozenFile
from multi_agent.core.task_execution import task_execution_value
from multi_agent.core.tasks import InterfaceContract
from multi_agent.store.integration_store import IntegrationStore

feature = task_reviews.feature
frozen = task_reviews.frozen


def _frozen_integration_review(frozen, source, member):
    suite = frozen.value.executed_suite
    commands = tuple(
        replace(
            command,
            manifest={
                **task_execution_value(command.manifest),
                "integration_id": "integration-example",
                "candidate": source.candidate,
                "command": task_execution_value(declared),
            },
        )
        for declared, command in zip(
            suite.prepared.checkout.input.commands, suite.commands, strict=True
        )
    )
    value = IntegrationReviewInput(
        source=source,
        integration_id="integration-example",
        test_attempt=suite.test_attempt,
        test_evidence_hash="a" * 64,
        commands=commands,
        members=(member,),
        test_change_paths=_test_change_paths(source, commands),
    )
    frozen.store.save_snapshot(value.snapshot_id, freeze_json(value.to_dict()))
    return value


def test_integration_review_reopens_actual_logs_and_gate_uses_all_selected_seats(
    frozen,
):
    original = frozen.value.source
    old_path, new_path = "tests/test_Foo.py", "tests/test_foo.py"
    source = replace(
        original,
        files=(
            *(
                replace(item, text="print('changed build script')\n", oid="c" * 40)
                if item.side == "candidate" and item.path == "src/build.py"
                else item
                for item in original.files
            ),
            FrozenFile("baseline", old_path, "pass\n", "b" * 40, "100644"),
            FrozenFile("candidate", new_path, "pass\n", "b" * 40, "100644"),
            FrozenFile("candidate", "conftest.py", "pass\n", "b" * 40, "100644"),
        ),
    )
    suite = frozen.value.executed_suite
    declaration = suite.prepared.checkout.input
    contract = InterfaceContract("interface", "Preserve the versioned wire cursor")
    member = IntegrationMember(
        task_run_id=declaration.task_run_id,
        planning_run_id="initial-plan",
        attempt=declaration.implementation_attempt,
        delivery=declaration.candidate.delivery,
        task_input=replace(
            suite.task_input,
            card=replace(suite.task_input.card, interface_contracts=(contract,)),
        ),
        external_dependencies=(),
    )
    value = _frozen_integration_review(frozen, source, member)
    commands = value.commands
    store = frozen.store
    assert load_review_input(store, value.snapshot_id) == value

    destination = frozen.feature.base / "integration-review-source"
    materialize_review_input(value, destination)
    rendered = json.loads((destination / "review-input.json").read_bytes())
    assert rendered["snapshot_id"] == value.snapshot_id
    assert rendered["source"]["candidate"] == source.candidate
    assert rendered["integration_id"] == value.integration_id
    assert rendered["test_change_paths"] == [
        "conftest.py",
        "src/build.py",
        old_path,
        new_path,
    ]
    assert (
        rendered["members"][0]["task_input"]["card"]["interface_contracts"][0][
            "definition"
        ]
        == contract.definition
    )
    assert rendered["commands"][0]["stdout"]["sha256"] == commands[0].stdout.sha256
    assert (destination / "logs/1/stdout.bin").read_bytes() == commands[
        0
    ].stdout.content
    assert (destination / "diff.patch").read_bytes() == source.diff.encode()
    raw = value.to_dict()
    raw["commands"][0]["manifest"]["candidate"] = "f" * 40
    with pytest.raises(ValueError, match="frozen integration suite"):
        decode_review_input(raw)

    round_id = runner.new_review(
        store,
        value.snapshot_id,
        (
            SeatConfig("first", "codex", "one", "high"),
            SeatConfig("second", "claude", "two", "high"),
        ),
        timeout=10,
        grace=1,
        executables={"codex": "unused", "claude": "unused"},
    )
    assert (
        store.load_round(round_id).prompt_template_version == INTEGRATION_REVIEW_VERSION
    )
    for key in ("first", "second"):
        store.apply(round_id, Envelope(key, 1, AttemptStarted()))
        store.apply(
            round_id, Envelope(key, 1, Completed({"summary": "ok", "findings": []}))
        )
        if key == "first":
            store.set_execution(round_id, key, 1, {"workspace_unchanged": True})
            with pytest.raises(ValueError, match="complete executed review"):
                inspect_integration_review(store, round_id, value)
    with pytest.raises(ValueError, match="seat second"):
        inspect_integration_review(store, round_id, value)
    store.set_execution(round_id, "second", 1, {"workspace_unchanged": True})
    assessment = inspect_integration_review(store, round_id, value)["assessment"]
    assert assessment["status"] == "ready"
    assert assessment["tests"]["executed_by_workbench"] is True
    assert assessment["tests"]["test_evidence_hash"] == value.test_evidence_hash
    with pytest.raises(ValueError, match="current integration test evidence"):
        inspect_integration_review(
            store, round_id, replace(value, test_evidence_hash="b" * 64)
        )


def test_integration_rework_requires_one_owner_and_qualified_confirmation(frozen):
    suite = frozen.value.executed_suite
    declaration = suite.prepared.checkout.input
    member = IntegrationMember(
        task_run_id=declaration.task_run_id,
        planning_run_id="initial-plan",
        attempt=declaration.implementation_attempt,
        delivery=declaration.candidate.delivery,
        task_input=suite.task_input,
        external_dependencies=(),
    )
    source = frozen.value.source
    value = _frozen_integration_review(frozen, source, member)
    record = IntegrationRecord(
        value.integration_id,
        source.repository,
        "refs/heads/main",
        source.baseline,
        value.members,
        1,
        IntegrationCandidate(
            "refs/integration-example", source.candidate, member.delivery.tree
        ),
    )
    seats = (
        SeatConfig("first", "codex", "one", "high"),
        SeatConfig("second", "claude", "two", "high"),
    )
    store = frozen.store
    parent = runner.new_review(
        store,
        value.snapshot_id,
        seats,
        timeout=10,
        grace=1,
        executables={"codex": "unused", "claude": "unused"},
    )
    blocker = {
        "id": "one-blocker",
        "severity": "P1",
        "category": "correctness",
        "side": "candidate",
        "path": "src/code.py",
        "line_start": 1,
        "line_end": 1,
        "quote": "CANDIDATE = True",
        "title": "incorrect literal",
        "impact": "incorrect behavior",
        "rationale": "literal contradicts the contract",
    }
    for seat, findings in ((seats[0], [blocker]), (seats[1], [])):
        store.apply(parent, Envelope(seat.key, 1, AttemptStarted()))
        store.apply(
            parent,
            Envelope(
                seat.key, 1, Completed({"summary": "review", "findings": findings})
            ),
        )
        store.set_execution(parent, seat.key, 1, {"workspace_unchanged": True})
    review = inspect_integration_review(store, parent, value, require_ready=False)
    owner, findings = select_rework_member(record, review)
    assert owner == member and [item["seat"] for item in findings] == ["first"]
    with pytest.raises(ValueError, match="src/code.py.*protected"):
        select_rework_member(record, review, target_protected=("src/code.py",))
    shared = replace(
        record,
        applied_count=2,
        members=(
            member,
            replace(
                member,
                task_run_id="another",
                task_input=replace(
                    member.task_input, card=replace(member.task_input.card, id="T-9001")
                ),
                delivery=replace(
                    member.delivery, changed_paths=("src/code.py", "src/build.py")
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="no unique member.*T-9001"):
        select_rework_member(shared, review)
    with pytest.raises(ValueError, match="src/build.py.*T-9001"):
        check_rework_paths(shared, owner, ("src/build.py",))
    with pytest.raises(ValueError, match="src/code.py.*T-9001"):
        check_rework_paths(shared, owner, ("src/code.py",))
    with pytest.raises(ValueError, match="protected"):
        check_rework_paths(shared, owner, (".agents/private.md",))
    check_rework_paths(shared, owner, ("src/new-helper.py",))

    child = runner.new_confirmation(store, parent)
    pending = check_integration_confirmation_link(store, parent, value, child)
    assert [seat.config for seat in pending.seats] == [seats[1]]
    with pytest.raises(ValueError, match="complete executed confirmation"):
        inspect_integration_review(store, parent, value, require_ready=False)
    store.apply(child, Envelope("second", 1, AttemptStarted()))
    store.apply(
        child,
        Envelope(
            "second",
            1,
            Completed(
                {
                    "summary": "disputed",
                    "confirmations": [
                        {
                            "target_seat": "first",
                            "finding_id": "one-blocker",
                            "decision": "disagree",
                            "reason": "The contract does permit this literal.",
                        }
                    ],
                }
            ),
        ),
    )
    with pytest.raises(ValueError, match="confirmation seat second"):
        inspect_integration_review(store, parent, value, require_ready=False)
    store.set_execution(child, "second", 1, {"workspace_unchanged": True})
    review = inspect_integration_review(store, parent, value, require_ready=False)
    assert review["assessment"]["status"] == "user_decision"
    with pytest.raises(ValueError, match="blocking_confirmation_unresolved"):
        select_rework_member(record, review)
    # A rebased SHA with the same tree cannot discard the unresolved review.
    same = replace(
        record,
        integration_id="same-tree",
        result=replace(record.result, ref="refs/integration-same", commit="a" * 40),
    )
    changed = replace(
        same,
        integration_id="changed-tree",
        result=replace(
            same.result, ref="refs/integration-changed", commit="b" * 40, tree="c" * 40
        ),
    )
    with IntegrationStore(frozen.feature.database) as integrations:
        for item in (record, same, changed):
            integrations._insert_integration(item)
        integrations._connection.executemany(
            "INSERT INTO integration_rebases VALUES (?, ?)",
            [(item.integration_id, record.integration_id) for item in (same, changed)],
        )
        integrations._connection.execute(
            """INSERT INTO integration_test_attempts (integration_id, attempt, request_payload)
               VALUES (?, 1, '{}')""",
            (record.integration_id,),
        )
        integrations._connection.execute(
            "INSERT INTO integration_review_links VALUES (?, ?, 1, 'fixture', '[1,2]', 0)",
            (parent, record.integration_id),
        )
        for item in (record, same):
            with pytest.raises(ValueError, match="still requires resolution"):
                check_confirmation_decisions(integrations, item)
        check_confirmation_decisions(integrations, changed)
