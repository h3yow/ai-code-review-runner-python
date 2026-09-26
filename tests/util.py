"""Small polling helper shared by lifecycle tests."""
from __future__ import annotations

import time

from review_runner.models import Task, TaskState
from review_runner.service import ReviewService


def wait_for_state(
    service: ReviewService,
    task_id: str,
    *states: TaskState,
    timeout: float = 5.0,
    interval: float = 0.01,
) -> Task:
    """Poll until the task reaches one of the expected states."""
    deadline = time.time() + timeout
    task = None
    while time.time() < deadline:
        task = service.store.get(task_id)
        assert task is not None
        if task.state in states:
            return task
        time.sleep(interval)
    state = task.state.value if task else "missing"
    raise AssertionError(
        f"task {task_id} did not reach {[s.value for s in states]} "
        f"within {timeout}s (state={state})"
    )
