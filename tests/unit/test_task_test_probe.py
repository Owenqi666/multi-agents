"""The permission canary is inert until run, and fake execution proves no sandbox."""

import asyncio
import hashlib
import json
import stat
from pathlib import Path

import pytest
import test_task_test_runtime as runtime_tests

from multi_agent.adapters import test_probe as probe

runtime_setup = runtime_tests.runtime_setup


def test_task_test_probe_prepare_is_inert_and_plan_bound(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("prepare attempted runtime or authentication access")

    monkeypatch.setattr(probe, "TaskTestRuntimeAdapter", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent-credentials"))
    plan = probe.prepare_plan(tmp_path / "reviewable")
    value = json.loads(plan.read_text())
    owner = json.loads((plan.parent / ".probe-owner.json").read_text())
    assert value["case"] == "native-test-boundary"
    assert value["required_cli_version"] == "codex-cli 0.169.0"
    assert value["required_stop_scope"] == "command-request-and-owned-runtime-v1"
    assert value["unconfirmed"] == [
        "command_group_gone",
        "all_descendants_stopped",
        "producer_stdout_eof",
        "producer_stderr_eof",
    ]
    assert "unverified" in value["permission_status"]
    assert len(value["commands"]) == 2
    assert all(
        command[0] == value["python"] and command[1] == "-c"
        for command in value["commands"]
    )
    assert set(value["allowed_writes"]) == {
        str(plan.parent / probe.CASE / "checkout"),
        str(plan.parent / probe.CASE / "scratch"),
    }
    assert owner["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert {
        "src/multi_agent/adapters/test_runtime.py",
        "src/multi_agent/adapters/test_isolation.py",
        "src/multi_agent/adapters/test_recording.py",
        "src/multi_agent/adapters/test_probe.py",
        "src/multi_agent/adapters/codex_isolation.py",
        "src/multi_agent/adapters/ownership.py",
        "src/multi_agent/adapters/runtime.py",
        "src/multi_agent/adapters/runtime_decode.py",
        "src/multi_agent/adapters/probe/process.py",
        "src/multi_agent/adapters/probe/recording.py",
        "protocol/codex/config.schema.json",
        "protocol/codex/schema/v2/CommandExecParams.json",
    } <= value["source_hashes"].keys()
    assert stat.S_IMODE(plan.stat().st_mode) == 0o400
    assert not (plan.parent / probe.CASE).exists()
    assert not (tmp_path / "nonexistent-credentials").exists()


@pytest.mark.parametrize(
    "change",
    [
        "bytes",
        "sources",
        "commands",
        "version",
        "owner",
        "case",
        "python",
        "runtime_read_paths",
        "profile",
        "timeout",
        "allowed_writes",
    ],
)
def test_task_test_probe_rejects_changed_plan_before_runtime(
    tmp_path, monkeypatch, change
):
    plan = probe.prepare_plan(tmp_path / "reviewable")
    value = json.loads(plan.read_text())
    owner_path = plan.parent / ".probe-owner.json"
    owner = json.loads(owner_path.read_text())
    if change in ("bytes", "sources"):
        value["source_hashes"]["src/multi_agent/adapters/test_runtime.py"] = "0" * 64
    elif change == "commands":
        value["commands"][0].append("unreviewed")
    elif change == "version":
        value["required_cli_version"] = "codex-cli 0.168.0"
    elif change == "owner":
        owner["owner"] = "different"
    elif change == "case":
        value["case"] = "other"
    elif change == "python":
        value["python"] = "/unreviewed/interpreter"
    elif change == "runtime_read_paths":
        value["runtime_read_paths"] = ["/"]
    elif change == "profile":
        value["profile"] = "unreviewed-profile"
    elif change == "timeout":
        value["timeout"] = 600
    elif change == "allowed_writes":
        value["allowed_writes"] = ["/"]
    plan.chmod(0o600)
    plan.write_text(json.dumps(value))
    if change != "bytes":
        owner["plan_sha256"] = hashlib.sha256(plan.read_bytes()).hexdigest()
    owner_path.chmod(0o600)
    owner_path.write_text(json.dumps(owner))

    def forbidden(*args, **kwargs):
        pytest.fail("invalid plan reached command launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(probe.run_plan(plan, case=probe.CASE))
    assert not (plan.parent / probe.CASE).exists()


def test_task_test_probe_fake_run_cannot_claim_native_permissions(runtime_setup):
    setup = runtime_setup
    plan = probe.prepare_plan(setup.root / "reviewable")
    report = asyncio.run(
        probe.run_plan(plan, case=probe.CASE, executable=str(setup.executable))
    )
    assert report["passed"] is False
    assert report["all_descendants_stopped"] is None
    assert report["permission_status"] == "observed canary results only"
    root = plan.parent / probe.CASE
    assert (root / "checkout/build-artifact").read_text() == "built"
    assert (root / "scratch/cache/shared").read_text() == "cached"
    assert len(report["results"]) == 2
    first, second = report["results"]
    assert first["reported_exit_code"] == 1 and second["reported_exit_code"] == 0
    checks = json.loads((root / "logs/command-1/stdout.bin").read_bytes())
    assert checks["child_scratch_writable"] is True
    assert checks["temporary_auth_read"] is False
    assert checks["network_denied"] is False
    assert (
        root / "logs/command-2/stdout.bin"
    ).read_text() == "shared build and cache verified\n"
    for result in report["results"]:
        assert result["environment"]["cli_version"] == "codex-cli 0.169.0"
        assert all(item["removed"] for item in result["receipt"]["isolations"])
        assert all(
            not Path(item["root"]).exists() for item in result["receipt"]["isolations"]
        )
    assert json.loads((root / "summary.json").read_text())["passed"] is False
    with pytest.raises(FileExistsError):
        asyncio.run(
            probe.run_plan(plan, case=probe.CASE, executable=str(setup.executable))
        )


def test_task_test_probe_prepare_cli_never_runs_native(tmp_path, capsys):
    target = tmp_path / "prepared"
    assert probe.main(["prepare", "--output-dir", str(target)]) == 0
    assert capsys.readouterr().out.strip() == str(target / "plan.json")
    assert not (target / probe.CASE).exists()
