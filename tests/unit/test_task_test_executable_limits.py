"""Real sparse-file size/hash boundaries; these files are never executed."""

import asyncio
import hashlib
import stat
import subprocess
from dataclasses import replace

import pytest
import test_task_test_runtime as runtime_helpers

from multi_agent.adapters import test_isolation as isolation
from multi_agent.adapters import test_probe as probe
from multi_agent.adapters import test_runtime as runtime
from multi_agent.cli.task_test_context import capture_task_test_policy

runtime_setup = runtime_helpers.runtime_setup

SUPPORTED_LIMIT = 536_870_912
OBSERVED_CODEX_SIZE = 280_704_736
PREFIX = b"T53 non-executable sparse fixture: beginning\n"
SUFFIX = b"\nT53 non-executable sparse fixture: end\x00"
BOUNDARIES = ("product", "probe", "runtime")


@pytest.fixture
def inert_setup(runtime_setup, monkeypatch):
    # The reused fixture creates synthetic types/auth only. Its fake executable
    # is not run, and neither is the larger non-executable file below.
    def forbidden(*args, **kwargs):
        pytest.fail("file inspection attempted process or authentication access")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(runtime, "create_test_isolation", forbidden)
    monkeypatch.setattr(isolation, "_minimal_auth", forbidden)
    return runtime_setup


def _sparse_executable(setup, size):
    path = setup.root / "never-executed-sparse-codex"
    with path.open("wb") as stream:
        stream.write(PREFIX)
        stream.truncate(size)
        stream.seek(size - len(SUFFIX))
        stream.write(SUFFIX)
    path.chmod(0o755)
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o755
    assert info.st_size == size
    return path, (info.st_dev, info.st_ino)


def _expected_digest(size):
    # Compute from the known construction, independently of the file and all
    # three production readers. Only a fixed-size zero chunk is held in memory.
    digest = hashlib.sha256(PREFIX)
    remaining = size - len(PREFIX) - len(SUFFIX)
    zeros = bytes(1024 * 1024)
    while remaining:
        count = min(remaining, len(zeros))
        digest.update(zeros[:count])
        remaining -= count
    digest.update(SUFFIX)
    return digest.hexdigest()


def _inspect(boundary, setup, path, identity, expected_hash):
    if boundary == "product":
        policy = capture_task_test_policy(path, timeout=1.0, grace=0.1)
        assert policy.executable == str(path)
        assert policy.executable_identity == identity
        assert policy.executable_sha256 == expected_hash
    elif boundary == "probe":
        actual_identity, actual_hash = probe._binary_hash(path)
        assert actual_identity == identity
        assert actual_hash == expected_hash
    else:
        assert boundary == "runtime"
        policy = replace(
            setup.policy,
            executable=str(path),
            executable_identity=identity,
            executable_sha256=expected_hash,
        )
        adapter = runtime_helpers.adapter(setup, policy=policy)
        before = adapter.snapshot()
        try:
            adapter._check_executable()
        finally:
            assert adapter.snapshot() == before
            assert not before.request_attempted
            assert not before.receipt.processes
            assert not before.receipt.isolations
            assert not adapter.log_dir.exists()


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize(
    "size",
    [OBSERVED_CODEX_SIZE, SUPPORTED_LIMIT],
    ids=["observed-codex-280704736", "exact-512-MiB"],
)
def test_task_test_executable_limits_accept_supported_sizes(
    inert_setup, size, boundary
):
    path, identity = _sparse_executable(inert_setup, size)
    expected_hash = _expected_digest(size)

    _inspect(boundary, inert_setup, path, identity, expected_hash)

    assert (path.stat().st_dev, path.stat().st_ino) == identity
    assert not inert_setup.capture.exists()


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_task_test_executable_limits_reject_overflow(inert_setup, boundary):
    path, identity = _sparse_executable(inert_setup, SUPPORTED_LIMIT + 1)
    error_type = runtime.TestProtocolError if boundary == "runtime" else ValueError
    message = "inspection byte limit" if boundary == "runtime" else "bounded regular"

    # Oversized files must fail before hashing; the runtime's supplied hash is
    # deliberately irrelevant, rather than pretending this is an accepted policy.
    with pytest.raises(error_type, match=message) as caught:
        _inspect(boundary, inert_setup, path, identity, "0" * 64)

    assert type(caught.value) is error_type
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    assert not inert_setup.capture.exists()
