"""Exercise the frontend build through the loopback service."""

import json
import subprocess
import sys
from contextlib import contextmanager
from http.client import HTTPConnection
from threading import Thread

import pytest

import multi_agent
from multi_agent.web import __main__ as web_main
from multi_agent.web.server import WorkspaceServer
from multi_agent.web.workspace import Workspace

_HTML = b"<!doctype html><title>Fixture workbench</title>"
_ASSET = b"\x00\xff\x01"


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repository"
    subprocess.run(["git", "init", "-qb", "main", str(repo)], check=True)
    return repo


@pytest.fixture
def frontend_dist(tmp_path):
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(_HTML)
    (dist / "assets" / "fixture image.png").write_bytes(_ASSET)
    return dist


@contextmanager
def _serve(repository, data_dir, dist):
    workspace = Workspace.open(repository, data_dir)
    binaries = {"codex": "codex", "claude": "claude"}
    with WorkspaceServer(0, workspace, binaries, frontend_dist=dist) as service:
        thread = Thread(target=service.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield service
        finally:
            service.shutdown()
            thread.join()


def _get(service, path, **headers):
    connection = HTTPConnection("127.0.0.1", service.server_port, timeout=3)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def test_web_static_serves_spa_and_assets_without_shadowing_api(
    tmp_path, repository, frontend_dist
):
    (frontend_dist / "api").mkdir()
    (frontend_dist / "api" / "workspace").write_bytes(b"static API lookalike")
    with _serve(repository, tmp_path / "data", frontend_dist) as service:
        for path in ("/", "/index.html", "/project/stage/123?resume"):
            status, headers, body = _get(service, path)
            assert (status, body) == (200, _HTML)
            assert headers["Content-Type"] == "text/html; charset=utf-8"
            assert headers["Cache-Control"] == "no-cache"
            assert headers["Access-Control-Allow-Origin"] is None

        status, headers, body = _get(service, "/assets/fixture%20image.png?v=1")
        assert (status, body) == (200, _ASSET)
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert _get(service, "/assets/not-built.js")[0] == 404
        assert _get(service, "/api/missing")[0] == 404

        status, headers, body = _get(service, "/api/workspace")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert json.loads(body)["repository"] == str(repository.resolve())

        status, _, body = _get(service, "/", Host=f"localhost:{service.server_port}")
        assert status == 403
        assert json.loads(body)["error"]["code"] == "invalid_host"


def test_web_static_missing_directory_keeps_api_available(tmp_path, repository):
    with _serve(repository, tmp_path / "data", tmp_path / "missing") as service:
        status, headers, body = _get(service, "/")
        assert status == 404
        assert headers["Content-Type"] == "text/plain; charset=utf-8"
        assert b"pnpm build" in body
        assert _get(service, "/api/workspace")[0] == 200


def test_web_static_rejects_paths_outside_build(tmp_path, repository, frontend_dist):
    outside = frontend_dist.with_name("dist-other")
    outside.mkdir()
    secret = outside / "private.txt"
    secret.write_bytes(b"outside the build")
    (frontend_dist / "shortcut").symlink_to(outside, target_is_directory=True)
    (frontend_dist / "assets" / "link.txt").symlink_to(secret)
    with _serve(repository, tmp_path / "data", frontend_dist) as service:
        for path in (
            "/%2e%2e/dist-other/private.txt",
            "/shortcut/private.txt",
            "/assets/link.txt",
        ):
            assert _get(service, path)[0] == 404

        index = frontend_dist / "index.html"
        index.unlink()
        index.symlink_to(secret)
        assert _get(service, "/")[0] == 404
        assert _get(service, "/project/stage/123")[0] == 404


@pytest.mark.parametrize("override", [False, True])
def test_web_static_cli_uses_workbench_source_or_explicit_dist(
    monkeypatch, tmp_path, repository, frontend_dist, override
):
    package_file = tmp_path / "workbench" / "src" / "multi_agent" / "__init__.py"
    monkeypatch.setattr(multi_agent, "__file__", str(package_file))
    source_dist = tmp_path / "workbench" / "frontend" / "dist"
    source_dist.mkdir(parents=True)
    (source_dist / "index.html").write_text("workbench source build")
    target_dist = repository / "frontend" / "dist"
    target_dist.mkdir(parents=True)
    (target_dist / "index.html").write_text("target repository build")
    argv = [
        "multi_agent.web",
        "--repository",
        str(repository),
        "--data-dir",
        str(tmp_path / "data"),
    ]
    if override:
        argv += ["--frontend-dist", str(frontend_dist)]
    monkeypatch.setattr(sys, "argv", argv)

    directories = []
    monkeypatch.setattr(
        WorkspaceServer,
        "serve_forever",
        lambda service: directories.append(service.frontend_dist),
    )
    assert web_main.main() == 0
    assert directories == [(frontend_dist if override else source_dist).resolve()]
