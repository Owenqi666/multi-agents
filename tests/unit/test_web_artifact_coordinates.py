"""Keep project log coordinates aligned with archived artifact handles."""

import base64
import hashlib
import json

import test_web_project_resources as resource_helpers
from test_web_project_flow import _get
from test_web_workspace import _serve

from multi_agent.cli.snapshot import freeze_repository
from multi_agent.core.planning import PlanningRequest
from multi_agent.core.project_plan import ConfirmedGoal
from multi_agent.store.project_store import ProjectStore

bound = resource_helpers.bound


def legacy_locator(kind, identifier, selection, key, digest):
    raw = json.dumps([kind, identifier, selection, key, digest], separators=(",", ":"))
    return "art-" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def test_planner_and_consultation_seat_logs_keep_their_receipt_positions(bound):
    workspace, database, baseline, _ = bound
    repository = str(workspace.repository)
    common = str(workspace.repository / ".git")
    log = workspace.data_dir / "stderr.bin"
    raw = b"original log\n"
    log.write_bytes(raw)
    info = log.stat()
    digest = hashlib.sha256(raw).hexdigest()
    receipt = {
        "path": str(log),
        "byte_count": len(raw),
        "sha256": digest,
        "identity": [info.st_dev, info.st_ino],
        "sealed": True,
        "healthy": True,
        "closed": True,
    }
    codex = PlanningRequest("codex", "model", "high")
    claude = PlanningRequest("claude", "another-model", "high")
    source = freeze_repository(
        workspace.repository,
        baseline,
        baseline,
        "consultation for log check",
        ("file",),
    )
    with ProjectStore(database) as store:
        store.save_snapshot(source.snapshot_id, source.to_dict())
        store.confirm_goal(
            ConfirmedGoal(
                "project-1",
                "goal-1",
                repository,
                common,
                baseline,
                "goal",
                "goal",
                "then",
            ),
            new_project=True,
        )
        store.start_plan("plan-1", "goal-1", codex, source.snapshot_id, "v1", "then")
        store.finish_plan(
            "plan-1",
            "later",
            document=None,
            error={"detail": "example"},
            evidence={"logs": [receipt]},
        )
        store.start_consultation(
            "consultation-1",
            repository,
            common,
            baseline,
            "goal",
            "project-1",
            source.snapshot_id,
            (codex, claude),
            "then",
        )
        for seat, logs in ((1, []), (2, [receipt | {"sealed": False}, receipt])):
            store.reserve_consultation_seat("consultation-1", seat, "v1", "then")
            store.finish_consultation_seat(
                "consultation-1",
                seat,
                "later",
                advice={"draft_goal": "goal"} if seat == 1 else None,
                error={"detail": "example"} if seat == 2 else None,
                evidence={"logs": logs},
            )

    with _serve(workspace.repository, workspace.data_dir, {}) as service:
        for kind, identifier, key, coordinates in (
            ("project-plan", "plan-1", "planner-1", {"log_index": 1}),
            ("consultation", "consultation-1", "seat-2-2", {"seat": 2, "log_index": 2}),
        ):
            (artifact,) = _get(service, f"resources/{kind}/{identifier}")["artifacts"]
            artifact_id = legacy_locator(kind, identifier, {}, key, digest)
            assert artifact == {
                "artifact_id": artifact_id,
                "key": key,
                "coordinates": coordinates,
                "stream": "stderr",
                "sha256": digest,
                "byte_count": len(raw),
            }
            assert _get(service, f"artifacts/{artifact_id}")["text"] == raw.decode()
