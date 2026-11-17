"""Pre-T51 canonical snapshots and v1 rendering remain independent goldens."""

import hashlib
import json

import pytest
import test_task_candidate_store as old_goldens
import test_task_review_cli as reviews

from multi_agent.cli.main import main
from multi_agent.core.reports import assess
from multi_agent.core.snapshot import FrozenSnapshot
from multi_agent.store import ReviewStore
from multi_agent.store.task_test_store import TaskTestStore

feature = reviews.feature
reviewed_feature = reviews.reviewed_feature


def test_executed_review_legacy_bytes_prompts_and_user_supplied_semantics(tmp_path):
    from multi_agent.cli.prompt import (
        CONFIRM_PROMPT,
        CONFIRM_VERSION,
        REVIEW_PROMPT,
        REVIEW_VERSION,
    )
    from multi_agent.cli.review_inputs import decode_review_input
    from multi_agent.cli.snapshot import materialize_review_input

    # These literals were independently captured before T46, not recomputed from
    # the new loader. Prompt hashes come from the committed T50 Git blob.
    raw = old_goldens.OLD_GOLDENS["snapshot"]
    value = FrozenSnapshot.from_dict(json.loads(raw))
    assert (
        value.snapshot_id
        == "6cbd44fd7b1b28bd2d97a0e1946e864e8a11668fd903310e742a700dcfecd329"
    )
    assert (
        json.dumps(
            value.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        == raw
    )
    assert decode_review_input(json.loads(raw), expected_id=value.snapshot_id) == value
    assert REVIEW_VERSION == "review-v1" and CONFIRM_VERSION == "confirmation-v1"
    assert (
        hashlib.sha256(REVIEW_PROMPT.encode()).hexdigest()
        == "8af7a9e4bca3d1451e784355c9f5adf9146d00fbd15380bfb00ed0422cc30135"
    )
    assert (
        hashlib.sha256(CONFIRM_PROMPT.encode()).hexdigest()
        == "5838f23c52018a8cd5b94012b78c8d48acddd33e58bca9f8649049754b39c27d"
    )
    rendered = tmp_path / "old-view"
    materialize_review_input(value, rendered)
    stored = json.loads(raw)
    # T50's to_dict()/run_seat used insertion order, not canonical storage order.
    # Both the outer fields and each TestEvidence item follow that Git baseline.
    expected = {
        "repository": stored["repository"],
        "baseline": stored["baseline"],
        "candidate": stored["candidate"],
        "requirements": stored["requirements"],
        "acceptance": stored["acceptance"],
        "tests": [
            {
                "commit": record["commit"],
                "command": record["command"],
                "exit_code": record["exit_code"],
                "log": record["log"],
            }
            for record in stored["tests"]
        ],
        "required_tests": stored["required_tests"],
    }
    assert (rendered / "review-input.json").read_bytes() == json.dumps(
        expected, ensure_ascii=False
    ).encode()
    assert (rendered / "diff.patch").read_bytes() == value.diff.encode()
    assert (rendered / "source/candidate/src/新增.py").read_bytes() == b"value = 1\r\n"
    manifest = {
        "snapshot_id": value.snapshot_id,
        "repository": value.repository,
        "baseline": value.baseline,
        "candidate": value.candidate,
        "omitted_files": [
            {
                "side": "baseline",
                "path": "binary.bin",
                "mode": "100644",
                "oid": "d" * 40,
            }
        ],
        "symlinks_are_inert_text": True,
        "tests_are_user_supplied": True,
    }
    assert (rendered / "source/snapshot.json").read_bytes() == (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    result = assess(value, {"seat": {"summary": "ok", "findings": []}}, ("seat",))
    assert result["tests"]["source"] == "user_supplied"
    assert result["tests"]["executed_by_workbench"] is False
    assert result["status"] == "ready"


def test_executed_review_old_execution_entries_cannot_bypass_source_gate(
    reviewed_feature, capsys
):
    from multi_agent.cli.main import load_snapshot
    from multi_agent.cli.review_inputs import load_review_input

    feature = reviewed_feature
    process = reviews.review_process(
        reviews.run_arguments(feature, feature.reviewers, dual=False)
    )
    assert process.returncode == 0, (process.stdout, process.stderr)
    output = json.loads(process.stdout)
    legacy_directory = feature.base / "legacy-cli-data"
    legacy_directory.mkdir()
    with (
        TaskTestStore(feature.database) as source,
        ReviewStore(legacy_directory / "reviews.sqlite3") as copied,
    ):
        source._connection.backup(copied._connection)
        assert (
            load_review_input(copied, output["snapshot_id"]).snapshot_id
            == output["snapshot_id"]
        )
        with pytest.raises(TypeError):
            load_snapshot(copied, output["snapshot_id"])
        before_rounds = copied.list_reviews()
    before_wire = (feature.base / "review-transport.jsonl").read_bytes()
    for arguments in (
        ["run", output["snapshot_id"], "--seat", "codex", "model-1", "high"],
        ["retry", output["round_id"]],
        ["confirm", output["round_id"]],
    ):
        assert main(["--data-dir", str(legacy_directory), *arguments]) == 2
        assert capsys.readouterr().err
    with ReviewStore(legacy_directory / "reviews.sqlite3") as copied:
        assert copied.list_reviews() == before_rounds
    assert (feature.base / "review-transport.jsonl").read_bytes() == before_wire
