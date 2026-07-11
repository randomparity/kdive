"""Shared run-step state vocabulary for the ledger and read models."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

RunStepState = Literal["pending", "running", "succeeded"]

RUN_STEP_PENDING: RunStepState = "pending"
RUN_STEP_RUNNING: RunStepState = "running"
RUN_STEP_SUCCEEDED: RunStepState = "succeeded"

RUN_STEP_STATES: tuple[RunStepState, ...] = (
    RUN_STEP_PENDING,
    RUN_STEP_RUNNING,
    RUN_STEP_SUCCEEDED,
)
PERSISTED_RUN_STEP_STATES: tuple[RunStepState, ...] = (
    RUN_STEP_RUNNING,
    RUN_STEP_SUCCEEDED,
)


def parse_persisted_run_step_state(value: object, *, run_id: UUID, step: str) -> RunStepState:
    """Return a typed persisted run-step state or raise for a corrupt ledger row."""
    if value in PERSISTED_RUN_STEP_STATES:
        return cast("RunStepState", value)
    raise RuntimeError(
        f"run_step ({run_id}, {step}) has unknown state {value!r}; "
        f"expected one of {list(PERSISTED_RUN_STEP_STATES)}"
    )
