"""Freeze, run and read independent reviews through the real local bridge."""

import base64
import hashlib
import shutil
from pathlib import Path

import test_task_workflow_cli as helpers
from test_web_archive import _serve_archive
from test_web_project_flow import FABLE, _get, _job, _request
from test_web_workspace import _serve

from multi_agent.cli.snapshot import freeze_diff
from multi_agent.core.events import thaw_json
from multi_agent.store.database import ReviewStore

workflow = helpers.workflow

PATCH = (
    "--- a/frontend/status.js\r\n"
    "+++ b/frontend/status.js\r\n"
    "@@ -1 +1 @@\r\n"
    '-export const status = "initial";\r\n'
    '+export const status = "candidate";\r\n'
)
INPUT = {
    "requirements": "核对页面状态。\r\n保留完整输入。\n",
    "acceptance": ["The status is shown.", "--a criterion is literal text"],
    "diff": PATCH,
}


def test_pasted_review_scope_and_registered_session_bytes_across_workspaces(workflow):
    repository = workflow.repository
    data = workflow.base / "local-data"
    programs = {provider: str(path) for provider, path in workflow.executables.items()}
    helpers.configure(
        workflow.executables["claude"],
        role="review",
        report={"summary": "已审阅此补丁。", "findings": []},
    )

    with _serve(repository, data, programs) as service:
        freeze = _job(service, "review.freeze", **INPUT)
        frozen = freeze["result"]
        identifier = frozen["snapshot_id"]
        assert frozen["report_only"]
        assert frozen["repository"] is None
        assert frozen["bound_repository"] == str(repository / ".git")
        assert freeze["args"]["diff"] == {"length": len(PATCH)}
        assert freeze["args"]["requirements"] == {"length": len(INPUT["requirements"])}
        snapshot_url = f"/api/resources/snapshot/{identifier}"
        snapshot = _get(service, snapshot_url.removeprefix("/api/"))["data"]["input"]
        assert snapshot["diff"] == PATCH
        assert snapshot["requirements"] == INPUT["requirements"]
        assert snapshot["acceptance"] == INPUT["acceptance"]
        assert not snapshot["files"] and not snapshot["tests"]
        assert any(
            item["id"] == identifier and item["scope"] == "independent_review"
            for item in _get(service, "runs?kind=snapshot")["items"]
        )

        round_view = _job(service, "review.run", snapshot_id=identifier, seats=[FABLE])[
            "result"
        ]
        round_id = round_view["round_id"]
        round_url = f"/api/resources/review/{round_id}?session_logs=1"
        assert round_view["snapshot_id"] == identifier
        assert round_view["seats"][0]["attempts"][0]["status"] == "succeeded"
        assert round_view["assessment"]["status"] == "user_decision"
        session = _get(service, round_url.removeprefix("/api/"))
        assert session["data"]["independent_review"]
        assert not session["data"]["can_confirm"]
        assert _get(service, f"resources/review/{round_id}")["artifacts"] == []
        (artifact,) = session["artifacts"]
        assert artifact["key"] == "session-review-1-1-1"
        assert artifact["coordinates"] == {
            "seat": 1,
            "review_attempt": 1,
            "log_index": 1,
        }
        assert artifact["stream"] == "session"
        assert any(
            item["id"] == round_id and item["scope"] == "independent_review"
            for item in _get(service, "runs?kind=review")["items"]
        )
        artifact_url = f"/api/artifacts/{artifact['artifact_id']}"

    database = data / "reviews.sqlite3"
    with ReviewStore(database) as store:
        evidence = thaw_json(store.load_execution(round_id))
        [log] = evidence["reviewer-1"]["1"]["session_logs"]
        legacy = freeze_diff(PATCH, INPUT["requirements"], tuple(INPUT["acceptance"]))
        store.save_snapshot(legacy.snapshot_id, legacy.to_dict())
    wire = Path(log["path"])
    expected = wire.read_bytes()
    boundary = expected.index("已".encode()) + 1
    digest = hashlib.sha256(expected).hexdigest()

    before = database.read_bytes()
    with _serve_archive(
        repository, workflow.base / "archive-data", database
    ) as service:
        assert _request(service, "GET", snapshot_url)[0] == 200
        assert _request(service, "GET", round_url)[1]["artifacts"] == [artifact]
        assert (
            _request(service, "GET", f"/api/resources/snapshot/{legacy.snapshot_id}")[0]
            == 404
        )

        prior = _get(service, f"{artifact_url.removeprefix('/api/')}?limit={boundary}")
        after = _get(service, f"{artifact_url.removeprefix('/api/')}?offset={boundary}")
        assert prior["next_offset"] == after["offset"] == boundary
        assert after["next_offset"] is None
        assert prior["sha256"] == after["sha256"] == artifact["sha256"] == digest
        assert prior["byte_count"] == after["byte_count"] == len(expected)
        assert (
            base64.b64decode(prior["raw_base64"])
            + base64.b64decode(after["raw_base64"])
            == expected
        )
        for action, args in (
            ("review.freeze", INPUT),
            ("review.run", {"snapshot_id": identifier, "seats": [FABLE]}),
            ("review.retry", {"round_id": round_id}),
            ("review.confirm", {"round_id": round_id}),
        ):
            status, refused = _request(
                service, "POST", "/api/jobs", {"action": action, "args": args}
            )
            assert status == 403
            assert refused["error"]["code"] == "archive_read_only"
    assert database.read_bytes() == before and wire.read_bytes() == expected

    foreign = workflow.base / "foreign"
    helpers.git(workflow.base, "clone", "--quiet", str(repository), str(foreign))
    with _serve_archive(
        foreign, workflow.base / "other-archive-data", database
    ) as service:
        for url in (
            snapshot_url,
            f"/api/diffs/snapshot/{identifier}",
            round_url,
            artifact_url,
        ):
            assert _request(service, "GET", url)[0] == 404

    other = workflow.base / "other-live-data"
    other.mkdir(mode=0o700)
    shutil.copyfile(database, other / "reviews.sqlite3")
    with _serve(foreign, other, programs) as service:
        for action, args in (
            ("review.run", {"snapshot_id": identifier, "seats": [FABLE]}),
            ("review.retry", {"round_id": round_id}),
        ):
            assert (
                _request(
                    service, "POST", "/api/jobs", {"action": action, "args": args}
                )[0]
                == 404
            )
        other_frozen = _job(service, "review.freeze", **INPUT)["result"]
        assert other_frozen["snapshot_id"] != identifier
        assert other_frozen["bound_repository"] == str(foreign / ".git")
        assert (
            _get(service, f"resources/snapshot/{other_frozen['snapshot_id']}")["data"][
                "input"
            ]["diff"]
            == PATCH
        )
        assert _request(service, "GET", snapshot_url)[0] == 404

    with _serve(repository, data, programs) as service:
        assert _request(service, "GET", artifact_url)[0] == 200
        wire.write_bytes(b"!" + expected[1:])
        assert _request(service, "GET", artifact_url)[0] == 404
        wire.unlink()
        assert _request(service, "GET", artifact_url)[0] == 404
