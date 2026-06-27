"""Cover the pure bind-precondition helper of the run-bind service (ADR-0169).

The async, Postgres-locked bind flow stays a Postgres-backed (bucket-1) target; this is the
connectionless ``_run_bindable_error`` reject helper.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from kdive.domain.capacity.state import RunState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.services.runs.bind import _run_bindable_error


def _run(*, system_id: object, state: RunState) -> Run:
    return cast("Run", SimpleNamespace(id=uuid4(), system_id=system_id, state=state))


def test_already_bound_run_is_transport_conflict() -> None:
    run = _run(system_id=uuid4(), state=RunState.CREATED)
    err = _run_bindable_error(run)
    assert err is not None
    assert err.category is ErrorCategory.TRANSPORT_CONFLICT
    assert err.object_id == str(run.id)
    assert str(err) == "run is already bound to a system"
    assert err.details == {"reason": "run_already_bound"}


def test_build_terminal_run_is_stale() -> None:
    run = _run(system_id=None, state=RunState.FAILED)
    err = _run_bindable_error(run)
    assert err is not None
    assert err.category is ErrorCategory.STALE_HANDLE
    assert err.object_id == str(run.id)
    assert err.details == {"current_status": RunState.FAILED.value}


def test_canceled_run_is_stale() -> None:
    run = _run(system_id=None, state=RunState.CANCELED)
    err = _run_bindable_error(run)
    assert err is not None and err.category is ErrorCategory.STALE_HANDLE
    assert err.details == {"current_status": RunState.CANCELED.value}


def test_bindable_run_returns_none() -> None:
    for state in (RunState.CREATED, RunState.RUNNING, RunState.SUCCEEDED):
        assert _run_bindable_error(_run(system_id=None, state=state)) is None


def test_already_bound_wins_over_terminal_state() -> None:
    # the most-specific reason wins: a bound + failed run reports already-bound, not stale
    run = _run(system_id=uuid4(), state=RunState.FAILED)
    err = _run_bindable_error(run)
    assert err is not None and err.details == {"reason": "run_already_bound"}
