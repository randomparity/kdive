"""Completion-owned remote-module offload and deadline checks."""

from __future__ import annotations

import pytest

from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    CompletionDeadlineExecutor,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1.0

    def __call__(self) -> float:
        return self.now


def test_deadline_executor_rejects_without_starting_after_deadline() -> None:
    clock = Clock()
    called = False

    def operation() -> None:
        nonlocal called
        called = True

    with pytest.raises(TimeoutError, match="before start"):
        CompletionDeadlineExecutor(clock).call(operation, 1.0)
    assert called is False


def test_deadline_executor_waits_for_completion_then_reports_expiry() -> None:
    clock = Clock()
    completed = False

    def operation() -> None:
        nonlocal completed
        clock.now = 3.0
        completed = True

    with pytest.raises(TimeoutError, match="during completion"):
        CompletionDeadlineExecutor(clock).call(operation, 2.0)
    assert completed is True


def test_deadline_executor_returns_completed_result_within_same_deadline() -> None:
    clock = Clock()
    assert CompletionDeadlineExecutor(clock).call(lambda: "done", 2.0) == "done"
