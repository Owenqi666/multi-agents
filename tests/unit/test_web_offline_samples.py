"""Rebuild the checked-in samples by running each offline HTTP workflow."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from difflib import unified_diff
from itertools import islice
from pathlib import Path

import pytest
from web_support.sample_comparison import comparison_value

REPOSITORY = Path(__file__).resolve().parents[2]
RECORDER = REPOSITORY / "tests" / "unit" / "web_support" / "record_samples.py"
SAMPLES = REPOSITORY / "docs" / "web-api" / "samples"


def _record_and_compare(tmp_path, scenario):
    completed = subprocess.run(
        [
            sys.executable,
            str(RECORDER),
            "--scenario",
            scenario,
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, (scenario, completed.stderr)
    known = SAMPLES / scenario
    recorded = tmp_path / scenario
    files = {file.name for file in known.glob("*.json")}
    assert "index.json" in files, scenario
    assert {file.name for file in recorded.glob("*.json")} == files, scenario
    for name in sorted(files):
        actual = _comparison_text(recorded / name)
        expected = _comparison_text(known / name)
        if actual != expected:
            diff = unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile="checked in",
                tofile="re-recorded",
                n=2,
            )
            pytest.fail(f"{scenario}/{name}\n" + "".join(islice(diff, 30)))


def _comparison_text(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    return (
        json.dumps(
            comparison_value(value), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )


def test_offline_http_samples_match_recorded_responses(tmp_path):
    scenarios = ("accept", "rework", "dual-partial", "cancel")
    with ThreadPoolExecutor(max_workers=len(scenarios)) as executor:
        futures = [
            executor.submit(_record_and_compare, tmp_path, scenario)
            for scenario in scenarios
        ]
        for future in futures:
            future.result()


def test_recorded_log_comparison_keeps_fixed_output_and_pages_exact():
    artifact = {
        "artifact_id": "art-test",
        "stream": "wire",
        "sha256": "hash",
        "byte_count": 31,
    }
    changed = artifact | {"byte_count": 32}
    assert comparison_value(artifact) == comparison_value(changed)
    assert comparison_value(artifact | {"byte_count": 0}) != comparison_value(artifact)

    stdout = artifact | {"stream": "stdout"}
    assert comparison_value(stdout) == stdout
    page = artifact | {"text": "line\n", "offset": 0, "next_offset": 5}
    assert comparison_value(page) == page


def test_offline_fixture_keeps_login_and_homes_inside_disposable_directory(tmp_path):
    from web_support.offline_fixture import create_fixture

    env = create_fixture(tmp_path, "accept")
    assert Path(env["HOME"]).resolve().parent == tmp_path
    assert Path(env["CODEX_HOME"]).resolve().parent == tmp_path
    login = json.loads((Path(env["CODEX_HOME"]) / "auth.json").read_text())
    assert login["tokens"]["access_token"] == "offline-fixture-access_token"
