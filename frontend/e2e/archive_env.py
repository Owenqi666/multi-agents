"""Create a version-five archive whose snapshot exists only in committed WAL."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from multi_agent.cli.snapshot import freeze_repository
from multi_agent.store.database import _migration_scripts

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/unit/web_support"))

import offline_env
from offline_fixture import create_fixture

_VERSION = 5
_DATABASE = "old-workbench.sqlite3"


def _git(repository, env, *arguments):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Offline Archive",
            "-c",
            "user.email=archive@example.invalid",
            *arguments,
        ],
        cwd=repository,
        env=env,
        text=True,
    ).strip()


def _seed_archive(root, env):
    repository = root / "repository"
    baseline = _git(repository, env, "rev-parse", "HEAD")
    (repository / "frontend" / "status.js").write_text(
        'export const status = "frozen-wal-only";\n', encoding="utf-8"
    )
    _git(repository, env, "commit", "-qam", "Freeze archived candidate")
    candidate = _git(repository, env, "rev-parse", "HEAD")
    frozen = freeze_repository(
        repository,
        baseline,
        candidate,
        "Retain the archived requirement.",
        ("The frozen candidate includes the archived value.",),
    )
    archive = root / "archive"
    archive.mkdir()
    seed = root / _DATABASE
    connection = sqlite3.connect(seed)
    try:
        for version, migration in _migration_scripts():
            if version > _VERSION:
                break
            connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(f"PRAGMA user_version={version}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute(
            "INSERT INTO snapshots (snapshot_id, payload) VALUES (?, ?)",
            (frozen.snapshot_id, json.dumps(frozen.to_dict())),
        )
        connection.commit()
        for name in (_DATABASE, _DATABASE + "-wal"):
            shutil.copyfile(root / name, archive / name)
    finally:
        connection.close()
    uri = (archive / _DATABASE).as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as base:
        if base.execute("SELECT count(*) FROM snapshots").fetchone() != (0,):
            raise RuntimeError("the fixture snapshot reached the checkpointed database")
    return frozen.snapshot_id


def _inventory(root):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "archive").iterdir())
    }


def _launch(root, env, port, frontend_dist):
    arguments = [
        sys.executable,
        "-m",
        "multi_agent.web",
        "--repository",
        str(root / "repository"),
        "--data-dir",
        str(root / "data"),
        "--read-only-database",
        str(root / "archive" / _DATABASE),
        "--port",
        str(port),
        "--codex-bin",
        str(root / "bin" / "codex"),
        "--claude-bin",
        str(root / "bin" / "claude"),
    ]
    if frontend_dist is not None:
        arguments.extend(("--frontend-dist", str(frontend_dist)))
    return subprocess.Popen(
        arguments,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("archive",), required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--frontend-dist", type=Path)
    args = parser.parse_args()
    offline_env._launch = _launch
    with TemporaryDirectory(prefix="multi-agent-offline-archive-") as directory:
        root = Path(directory).resolve()
        env = create_fixture(root, args.scenario)
        snapshot_id = _seed_archive(root, env)
        print(
            json.dumps(
                {
                    "snapshotId": snapshot_id,
                    "version": _VERSION,
                    "files": _inventory(root),
                }
            ),
            flush=True,
        )
        try:
            offline_env._serve(root, env, args.port, args.frontend_dist)
        finally:
            print(json.dumps(_inventory(root)), flush=True)


if __name__ == "__main__":
    main()
