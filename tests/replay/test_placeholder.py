"""Temporary check until the state machine has replay scenarios."""

from pathlib import Path


def test_replay_scenarios_directory_exists() -> None:
    assert (Path(__file__).parent / "scenarios").is_dir()
