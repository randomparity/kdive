"""Shared run-step state vocabulary for the ledger and read models."""

from __future__ import annotations

from typing import Final, Literal, TypedDict, cast
from uuid import UUID

RunStepState = Literal["pending", "running", "succeeded"]
BootOutcome = Literal["ready", "expected_crash_observed", "crashed_halted_live"]

RUN_STEP_PENDING: RunStepState = "pending"
RUN_STEP_RUNNING: RunStepState = "running"
RUN_STEP_SUCCEEDED: RunStepState = "succeeded"
BOOT_OUTCOME_READY: Final[BootOutcome] = "ready"
BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED: Final[BootOutcome] = "expected_crash_observed"
BOOT_OUTCOME_CRASHED_HALTED_LIVE: Final[BootOutcome] = "crashed_halted_live"

RUN_STEP_STATES: tuple[RunStepState, ...] = (
    RUN_STEP_PENDING,
    RUN_STEP_RUNNING,
    RUN_STEP_SUCCEEDED,
)
PERSISTED_RUN_STEP_STATES: tuple[RunStepState, ...] = (
    RUN_STEP_RUNNING,
    RUN_STEP_SUCCEEDED,
)
BOOT_OUTCOMES: tuple[BootOutcome, ...] = (
    BOOT_OUTCOME_READY,
    BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED,
    BOOT_OUTCOME_CRASHED_HALTED_LIVE,
)


class BootStepResult(TypedDict, total=False):
    """Persisted `boot` step result fields."""

    system_id: str
    boot_outcome: BootOutcome
    expectation_matched: bool
    evidence_kind: str
    evidence_artifact_id: str
    available_capture: list[str]
    inert_capture: list[str]
    matched_line: str


def parse_persisted_run_step_state(value: object, *, run_id: UUID, step: str) -> RunStepState:
    """Return a typed persisted run-step state or raise for a corrupt ledger row."""
    if value in PERSISTED_RUN_STEP_STATES:
        return cast("RunStepState", value)
    raise RuntimeError(
        f"run_step ({run_id}, {step}) has unknown state {value!r}; "
        f"expected one of {list(PERSISTED_RUN_STEP_STATES)}"
    )


def parse_boot_outcome(value: object) -> BootOutcome | None:
    """Return a typed boot outcome, or ``None`` for a missing/unknown stored value."""
    if value in BOOT_OUTCOMES:
        return cast("BootOutcome", value)
    return None
