"""Structural checks for a complete turn's normalized events."""

from collections.abc import Sequence

from multi_agent.core.events import AdapterEvent, Completed, Error, ProcessExited


def validate_event_sequence(
    events: Sequence[AdapterEvent], *, cancelled_before_exit: bool
) -> None:
    """Reject incomplete or contradictory streams, raising ValueError.

    The caller supplies whether cancellation was requested before process exit.
    This function cannot infer cancellation from adapter events alone.
    """
    terminal_count = sum(isinstance(event, (Completed, Error)) for event in events)
    exit_positions = [
        index for index, event in enumerate(events) if isinstance(event, ProcessExited)
    ]
    if terminal_count > 1:
        raise ValueError("a turn must have at most one terminal event")
    if len(exit_positions) > 1:
        raise ValueError("a turn must have at most one process exit")
    if exit_positions and exit_positions[0] != len(events) - 1:
        raise ValueError("process exit must be the last event")
    if terminal_count == 0 and not (cancelled_before_exit and exit_positions):
        raise ValueError(
            "a finished stream needs a terminal or a cancelled process exit"
        )
