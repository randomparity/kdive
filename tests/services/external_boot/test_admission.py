"""The closed external-boot admission matrix (ADR-0583, #2117).

The expected table is written transposed against the design's table — states admitting each
operation, rather than operations admitted in each state — so a transcription slip in the
implementation cannot be mirrored here.
"""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import AsyncConnection

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.services.external_boot import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)
from tests.services.external_boot.conftest import SeedActivation, build_activation

_STATE = ExternalBootActivationState
_OP = ExternalBootOperation

_EVERY_RESTRICTED_STATE = frozenset(_STATE)
_ACTIVE_ONLY = frozenset({_STATE.ACTIVE})

_ADMITTING_STATES: dict[ExternalBootOperation, frozenset[ExternalBootActivationState]] = {
    _OP.SYSTEM_TEARDOWN: _EVERY_RESTRICTED_STATE,
    _OP.EXTERNAL_BOOT_RESOLVE_CONFLICT: frozenset({_STATE.RECOVERY_CONFLICT}),
    _OP.EXTERNAL_BOOT_RELEASE: _ACTIVE_ONLY,
    _OP.FORCE_CRASH: _ACTIVE_ONLY,
    _OP.SYSTEM_WATCH_CRASH: _ACTIVE_ONLY,
    _OP.CAPTURE_VMCORE: _ACTIVE_ONLY,
    _OP.CAPTURE_TRAFFIC: _ACTIVE_ONLY,
    _OP.DEBUG_ATTACH: _ACTIVE_ONLY,
    _OP.DEBUG_DETACH: _ACTIVE_ONLY,
}

_NEVER_ADMITTED = frozenset(
    {
        _OP.RUN_CREATE,
        _OP.RUN_BIND,
        _OP.RUN_CANCEL,
        _OP.RUN_INSTALL,
        _OP.RUN_BOOT,
        _OP.SYSTEM_REPROVISION,
        _OP.SYSTEM_POWER,
        _OP.SYSTEM_SNAPSHOT,
        _OP.SYSTEM_SYSRQ,
        _OP.SYSTEM_AUTHORIZE_SSH_KEY,
    }
)

_OWNING_RUN_SCOPED = frozenset(
    {
        _OP.EXTERNAL_BOOT_RELEASE,
        _OP.CAPTURE_VMCORE,
        _OP.CAPTURE_TRAFFIC,
        _OP.DEBUG_ATTACH,
        _OP.DEBUG_DETACH,
    }
)

# Every (state, cleanup_complete) pair a restricting row can hold: no state is fully cleaned
# while it still restricts, except the two teardown states, whose cleanup does not release the
# System. `recovered` and `abandoned` with `cleanup_complete=true` stop restricting entirely and
# are covered by the no-activation case instead.
_RESTRICTING_CASES = [
    *((state, False) for state in _STATE),
    (_STATE.RECOVERY_CONFLICT, True),
    (_STATE.RECOVERY_FAILED, True),
]

_CALLERS = ("owning_run", "other_run", "no_run")


def _restricted_by(
    monkeypatch: pytest.MonkeyPatch, activation: ExternalBootActivation | None
) -> None:
    async def _get_restricting_for_system(
        _self: ExternalBootActivationRepository, _conn: AsyncConnection, _system_id: UUID
    ) -> ExternalBootActivation | None:
        return activation

    monkeypatch.setattr(
        ExternalBootActivationRepository,
        "get_restricting_for_system",
        _get_restricting_for_system,
    )


def _check(system_id: UUID, operation: ExternalBootOperation, run_id: UUID | None = None) -> None:
    asyncio.run(
        check_external_boot_admission(
            cast("AsyncConnection", None), system_id, operation, run_id=run_id
        )
    )


def _denial(
    monkeypatch: pytest.MonkeyPatch,
    state: ExternalBootActivationState,
    operation: ExternalBootOperation,
) -> ExternalBootDenied:
    system_id, run_id = uuid4(), uuid4()
    activation = build_activation(
        activation_id=uuid4(), system_id=system_id, run_id=run_id, state=state
    )
    _restricted_by(monkeypatch, activation)
    with pytest.raises(ExternalBootDenied) as raised:
        _check(system_id, operation)
    assert raised.value.details == {
        "activation_id": str(activation.id),
        "activation_state": state.value,
        "owning_run_id": str(run_id),
    }
    return raised.value


def test_the_expected_table_decides_every_operation() -> None:
    assert _ADMITTING_STATES.keys().isdisjoint(_NEVER_ADMITTED)
    assert set(_ADMITTING_STATES) | _NEVER_ADMITTED == set(ExternalBootOperation)


@pytest.mark.parametrize("caller", _CALLERS)
@pytest.mark.parametrize("operation", list(ExternalBootOperation))
@pytest.mark.parametrize(("state", "cleanup_complete"), _RESTRICTING_CASES)
def test_a_restricting_activation_admits_only_the_table(
    monkeypatch: pytest.MonkeyPatch,
    state: ExternalBootActivationState,
    cleanup_complete: bool,
    operation: ExternalBootOperation,
    caller: str,
) -> None:
    system_id, owning_run_id = uuid4(), uuid4()
    activation = build_activation(
        activation_id=uuid4(),
        system_id=system_id,
        run_id=owning_run_id,
        state=state,
        cleanup_complete=cleanup_complete,
    )
    _restricted_by(monkeypatch, activation)
    caller_run = {"owning_run": owning_run_id, "other_run": uuid4(), "no_run": None}[caller]
    admitted = state in _ADMITTING_STATES.get(operation, frozenset()) and (
        operation not in _OWNING_RUN_SCOPED or caller_run == owning_run_id
    )
    if admitted:
        assert _check(system_id, operation, caller_run) is None
        return
    with pytest.raises(ExternalBootDenied) as raised:
        _check(system_id, operation, caller_run)
    assert raised.value.category is ErrorCategory.CONFLICT
    assert raised.value.terminal


@pytest.mark.parametrize("operation", list(ExternalBootOperation))
def test_no_restricting_activation_admits_every_operation(
    monkeypatch: pytest.MonkeyPatch, operation: ExternalBootOperation
) -> None:
    _restricted_by(monkeypatch, None)
    assert _check(uuid4(), operation) is None


@pytest.mark.parametrize(
    ("state", "operation", "next_actions"),
    [
        (_STATE.ACTIVE, _OP.RUN_INSTALL, ["runs.get", "runs.release_external_boot"]),
        (_STATE.RECOVERY_CONFLICT, _OP.SYSTEM_POWER, ["runs.get", "systems.teardown"]),
        (_STATE.RECOVERY_FAILED, _OP.RUN_CREATE, ["runs.get", "systems.teardown"]),
        (_STATE.PREPARING, _OP.RUN_BOOT, ["runs.get"]),
    ],
)
def test_denial_carries_the_state_s_next_actions_off_details(
    monkeypatch: pytest.MonkeyPatch,
    state: ExternalBootActivationState,
    operation: ExternalBootOperation,
    next_actions: list[str],
) -> None:
    denied = _denial(monkeypatch, state, operation)
    assert denied.next_actions == next_actions


def test_get_restricting_for_system_sees_only_uncleaned_activations(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            uncleaned = await seeded_activation(conn, state=_STATE.RECOVERED)
            cleaned = await seeded_activation(conn, state=_STATE.RECOVERED, cleanup_complete=True)
            torn_down = await seeded_activation(
                conn, state=_STATE.RECOVERY_FAILED, cleanup_complete=True
            )
            await conn.commit()

            found = await repo.get_restricting_for_system(conn, uncleaned.system_id)
            assert found is not None
            assert (found.id, found.state, found.run_id) == (
                uncleaned.activation.id,
                _STATE.RECOVERED,
                uncleaned.run_id,
            )
            assert await repo.get_restricting_for_system(conn, cleaned.system_id) is None
            still_restricting = await repo.get_restricting_for_system(conn, torn_down.system_id)
            assert still_restricting is not None
            assert still_restricting.id == torn_down.activation.id
            assert await repo.get_restricting_for_system(conn, uuid4()) is None

    asyncio.run(_run())
