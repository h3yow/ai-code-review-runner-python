"""State machine rules: legal transitions only, no state may be skipped."""
from __future__ import annotations

import pytest

from review_runner.models import ALLOWED_TRANSITIONS, TaskState, validate_transition


def test_legal_forward_path():
    validate_transition(TaskState.PENDING, TaskState.RUNNING)
    validate_transition(TaskState.RUNNING, TaskState.SUCCEEDED)


def test_legal_cancel_paths():
    validate_transition(TaskState.PENDING, TaskState.CANCELLING)
    validate_transition(TaskState.RUNNING, TaskState.CANCELLING)
    validate_transition(TaskState.CANCELLING, TaskState.CANCELLED)


def test_terminal_states_have_no_outgoing_transitions():
    for terminal in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()


@pytest.mark.parametrize(
    "src,dst",
    [
        (TaskState.PENDING, TaskState.SUCCEEDED),     # cannot skip RUNNING
        (TaskState.PENDING, TaskState.CANCELLED),      # cannot skip CANCELLING
        (TaskState.RUNNING, TaskState.PENDING),        # no backwards jump
        (TaskState.SUCCEEDED, TaskState.RUNNING),
        (TaskState.FAILED, TaskState.RUNNING),
        (TaskState.CANCELLED, TaskState.RUNNING),
    ],
)
def test_illegal_jumps_are_rejected(src, dst):
    with pytest.raises(RuntimeError, match="illegal state transition"):
        validate_transition(src, dst)
