"""Replay-safe coordination for retained systemd worker incarnations (ADR-0574, ADR-0657)."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.processes.lifecycle.systemd.systemd_diagnostics import SystemdDiagnostics
from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    LifecycleRequest,
    LifecycleResponse,
    ResponseCode,
    RetryAction,
    SlotPhase,
    SlotResult,
    WorkerSettings,
)
from kdive.processes.lifecycle.systemd.systemd_worker_runtime import (
    BootObservation,
    CommandDeadlineExceeded,
    Deadline,
    SystemdConflict,
    SystemdUnavailable,
    UnitObservation,
    UnmanagedWorker,
    load_slot_redaction_values,
)
from kdive.processes.lifecycle.systemd.systemd_worker_state import (
    SlotInspection,
    SlotState,
    StateConflict,
)
from kdive.worker_lifecycle.authority_store import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    IncarnationConflict,
    LocalWorkerIncarnation,
    recoverable_worker_incarnations,
    register_worker_incarnation,
    terminate_worker_incarnation,
)
from kdive.worker_lifecycle.contracts import TerminationOutcome

_REQUEST_SECONDS = 120.0
_STOP_SECONDS = 45.0
_DIAGNOSTIC_SECONDS = 30.0
_POLL_SECONDS = 0.1
_RECOVERY_REFUSED = "recovery_refused"
# ADR-0657 (`docs/adr/0657-a-successor-invocation-is-terminal-evidence.md`, lines 62-66, the
# "clears facts, never evidence" paragraph) forbids running for a slot whose invocation identity
# is unreadable: recovery may not fabricate a `TerminationOutcome` nor attribute one invocation's
# exit facts to another. ADR-0574 makes absence within the retained boot a non-event, so systemd
# reporting no invocation on that boot leaves nothing able to prove the registered invocation
# ended. That slot is refused, with its own code so an operator and a log filter can tell it from
# a live-process refusal, which is transient. Relaxing this takes an ADR amendment; the operator's
# remedy is a reboot, which yields a different boot ID and so real evidence.
_RECOVERY_REFUSED_IDENTITY = "recovery_refused_unreadable_identity"
# A row whose stored binding does not name an invocation on this slot's own unit. It is not the
# unreadable-identity case and must not share its code: that one is cleared by a reboot, which
# yields a fresh boot ID, while this one is a row only an operator can reconcile.
_RECOVERY_REFUSED_INCOHERENT = "recovery_refused_incoherent_row"
_REFUSAL_MESSAGES = {
    _RECOVERY_REFUSED: "fixed worker unit still has live processes",
    _RECOVERY_REFUSED_IDENTITY: (
        "registered invocation identity is unreadable; ADR-0657 forbids recovering it"
    ),
    _RECOVERY_REFUSED_INCOHERENT: (
        "registered fence row does not name an invocation on this slot's unit"
    ),
}
_REFUSALS = frozenset(_REFUSAL_MESSAGES)
# Only these phases still have to derive an outcome from a current observation. `_retire_slot`
# discards a prepared generation and cleans an already-terminated one without consulting one, so
# neither can meet the unreadable invocation identity the refusal above exists for.
_OBSERVED_PHASES = frozenset(SlotPhase) - {SlotPhase.PREPARED, SlotPhase.TERMINATED}
_log = logging.getLogger(__name__)


class EvidenceRejected(RuntimeError):
    """PostgreSQL rejected purported terminal evidence for an exact incarnation."""


class LifecycleConflict(RuntimeError):
    """Retained and observed lifecycle facts cannot be reconciled safely."""


class LifecycleDeadlineExceeded(RuntimeError):
    """A lifecycle operation exhausted its shared absolute monotonic deadline."""


# The two failures #2533 names for cases 3 and 4: the retained binding no longer matches the row,
# so the evidenced path cannot commit and recovery falls back to the row's own binding.
# Deliberately broad -- every `StateConflict` out of the evidenced path means the retained facts
# cannot be trusted, which is exactly when the row should be believed instead.
_RESIDUAL_FALLBACK: tuple[type[Exception], ...] = (EvidenceRejected, StateConflict)


class _AuthorityUnavailable(RuntimeError):
    """The exact-incarnation database authority did not complete."""


class _ActivationFailure(RuntimeError):
    """A fleet activation failed after bounded rollback of earlier slots."""

    def __init__(self, cause: Exception, cleaned: tuple[SlotState, ...]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.cleaned = cleaned


@dataclass(frozen=True, slots=True)
class _Recovery:
    """What retiring one slot produced, before it is rendered as a result."""

    state: SlotState | None = None
    cleared: bool = False
    refusal: str | None = None


class IncarnationAuthority(Protocol):
    """Register and terminate exact immutable worker-incarnation facts."""

    async def register(self, state: SlotState, credential_hash: bytes) -> None:
        """Register one exact local-systemd incarnation and binding."""
        ...

    async def terminate(self, state: SlotState, outcome: TerminationOutcome) -> None:
        """Commit terminal evidence only for the same exact registered binding."""
        ...

    async def recoverable(self, unit: str) -> tuple[LocalWorkerIncarnation, ...]:
        """Return the active local rows one fixed slot still holds."""
        ...

    async def release(self, record: LocalWorkerIncarnation, outcome: TerminationOutcome) -> None:
        """Commit terminal evidence using the row's own stored binding."""
        ...


class SlotStorage(Protocol):
    """Crash-safe operations consumed from one fixed slot store."""

    slot: int
    unit: str
    root: Path

    def prepare(self, settings: WorkerSettings | None) -> SlotState: ...

    def load(self) -> SlotState | None: ...

    def persist(self, state: SlotState) -> None: ...

    def publish_release(self, state: SlotState) -> None: ...

    def discard_prepared(self, state: SlotState) -> None: ...

    def cleanup_terminated(self, state: SlotState) -> None: ...

    def inspect(self) -> SlotInspection: ...

    def discard_unrecoverable(self) -> bool: ...


class SystemdControl(Protocol):
    """Exact retained-unit operations consumed by the coordinator."""

    def require_inactive(self, unit: str, deadline: Deadline) -> None: ...

    def start(self, unit: str, deadline: Deadline) -> None: ...

    def observe(self, unit: str, deadline: Deadline) -> UnitObservation | BootObservation: ...

    def signal_terminate(self, unit: str, deadline: Deadline) -> None: ...

    def stop_retained(self, unit: str, deadline: Deadline) -> None: ...

    def reset_failed(self, unit: str, deadline: Deadline) -> None: ...

    def unmanaged_workers(self) -> tuple[UnmanagedWorker, ...]: ...

    def public_properties(self, unit: str, invocation_id: str, deadline: Deadline) -> str: ...

    def journal(
        self, invocation_id: str, byte_limit: int, deadline: Deadline
    ) -> str | Sequence[str]: ...


class _BudgetDeadline:
    """A child budget clipped to one caller-owned absolute deadline."""

    def __init__(self, parent: Deadline, seconds: float) -> None:
        self._parent = parent
        self._seconds = seconds
        self._parent_at_start = parent.remaining()

    def remaining(self) -> float:
        parent_remaining = self._parent.remaining()
        elapsed = max(0.0, self._parent_at_start - parent_remaining)
        return min(parent_remaining, max(0.0, self._seconds - elapsed))


class PostgresAuthority:
    """Thin witness-role adapter over the existing worker-incarnation services."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def register(self, state: SlotState, credential_hash: bytes) -> None:
        """Register the slot's unchanged local authority binding and current protocol."""
        binding = state.authority_binding()
        async with self.pool.connection() as connection:
            await register_worker_incarnation(
                connection,
                state.incarnation,
                "local",
                binding,
                credential_hash,
                CURRENT_WORKER_FENCE_PROTOCOL,
            )

    async def terminate(self, state: SlotState, outcome: TerminationOutcome) -> None:
        """Require PostgreSQL to accept the exact binding before reporting evidence."""
        async with self.pool.connection() as connection:
            accepted = await terminate_worker_incarnation(
                connection,
                state.incarnation,
                "local",
                state.authority_binding(),
                outcome,
            )
        if not accepted:
            raise EvidenceRejected(f"database rejected termination evidence for slot {state.slot}")

    async def recoverable(self, unit: str) -> tuple[LocalWorkerIncarnation, ...]:
        """Return the active local rows one fixed slot still holds."""
        async with self.pool.connection() as connection:
            return await recoverable_worker_incarnations(connection, unit)

    async def release(self, record: LocalWorkerIncarnation, outcome: TerminationOutcome) -> None:
        """Commit terminal evidence using the row's own stored binding (ADR-0667)."""
        async with self.pool.connection() as connection:
            accepted = await terminate_worker_incarnation(
                connection, record.incarnation, "local", record.authority_binding, outcome
            )
        if not accepted:
            raise EvidenceRejected(
                f"database rejected termination evidence for {record.incarnation}"
            )


class SystemdWorkerLifecycle:
    """Converge the fixed worker fleet without discarding uncertain evidence."""

    def __init__(
        self,
        *,
        stores: Sequence[SlotStorage],
        runtime: SystemdControl,
        authority: IncarnationAuthority,
        wait: Callable[[float], None] = time.sleep,
        load_redaction_values: Callable[[Path, int], tuple[str, ...]] = (
            load_slot_redaction_values
        ),
    ) -> None:
        if tuple(store.slot for store in stores) != tuple(range(1, 9)):
            raise ValueError("lifecycle requires the eight ordered fixed slot stores")
        self._stores = tuple(stores)
        self._runtime = runtime
        self._authority = authority
        self._wait = wait
        self._load_redaction_values = load_redaction_values
        self._diagnostics = SystemdDiagnostics(
            stores=self._stores,
            runtime=self._runtime,
            load_redaction_values=load_redaction_values,
            systemd_call=self._systemd_call,
            store_call=self._store_call,
            state_failures=(LifecycleDeadlineExceeded, StateConflict, OSError),
            acquisition_failures=(
                LifecycleDeadlineExceeded,
                CommandDeadlineExceeded,
                StateConflict,
                OSError,
            ),
        )

    async def start(self, request: LifecycleRequest, deadline: Deadline) -> LifecycleResponse:
        """Replace the current fleet, replaying retained generations before activation."""
        operation_deadline = _BudgetDeadline(deadline, _REQUEST_SECONDS)
        if request.operation != "start" or request.worker_count is None:
            return _invalid_start_response()
        try:
            unmanaged = self._systemd_call(operation_deadline, self._runtime.unmanaged_workers)
            if unmanaged:
                raise LifecycleConflict("unmanaged worker processes require operator recovery")
            await self._replace_current_fleet(operation_deadline)
            states = await self._activate_fleet(request, operation_deadline)
        except _ActivationFailure as exc:
            return self._failure_response(exc.cause, operation_deadline, additional=exc.cleaned)
        except Exception as exc:
            return self._failure_response(exc, operation_deadline)
        return _ok_response("worker fleet started", tuple(_result(state) for state in states))

    async def status(self, deadline: Deadline) -> LifecycleResponse:
        """Record exact terminal observations while retaining every diagnostic source."""
        operation_deadline = _BudgetDeadline(deadline, _REQUEST_SECONDS)
        states: list[SlotState] = []
        try:
            for store in self._stores:
                state = self._store_call(operation_deadline, store.load)
                if state is not None:
                    states.append(await self._status_slot(store, state, operation_deadline))
        except Exception as exc:
            return self._failure_response(exc, operation_deadline)
        return _ok_response("worker fleet status", tuple(_result(state) for state in states))

    async def stop(self, deadline: Deadline) -> LifecycleResponse:
        """Stop the current fleet and clean only database-evidenced incarnations."""
        operation_deadline = _BudgetDeadline(deadline, _REQUEST_SECONDS)
        try:
            terminated = await self._stop_current_fleet(operation_deadline)
        except Exception as exc:
            return self._failure_response(exc, operation_deadline)
        return _ok_response("worker fleet stopped", tuple(_result(state) for state in terminated))

    async def diagnostics(self, deadline: Deadline) -> LifecycleResponse:
        """Delegate bounded, non-mutating diagnostic capture."""
        operation_deadline = _BudgetDeadline(deadline, _REQUEST_SECONDS)
        diagnostic_deadline = _BudgetDeadline(operation_deadline, _DIAGNOSTIC_SECONDS)
        return self._diagnostics.capture(diagnostic_deadline)

    async def recover(self, deadline: Deadline) -> LifecycleResponse:
        """Retire every slot proven dead and release the identity its failed unit retains.

        ADR-0657 permits clearing the on-disk slot facts and releasing the fence for a slot
        proven dead, and forbids fabricating a ``TerminationOutcome``. Every outcome published
        here is derived by ``_terminal_observation`` from a current systemd observation, exactly
        as ``stop`` derives it; nothing is synthesized for a slot systemd cannot account for.
        What ``stop`` cannot reach is the unit itself: it is left ``failed`` holding the
        ``InvocationID`` that ``require_inactive`` refuses, and only ``reset-failed`` clears it.
        """
        operation_deadline = _BudgetDeadline(deadline, _REQUEST_SECONDS)
        stop_deadline = _BudgetDeadline(operation_deadline, _STOP_SECONDS)
        results: list[SlotResult] = []
        try:
            for store in self._stores:
                result = await self._recover_slot(store, operation_deadline, stop_deadline)
                if result is not None:
                    results.append(result)
        except Exception as exc:
            return _with_completed_slots(
                self._failure_response(exc, operation_deadline), tuple(results)
            )
        if any(result.code in _REFUSALS for result in results):
            return LifecycleResponse(
                ok=False,
                code="conflict",
                message="recovery refused one or more fixed worker slots",
                retry_action="operator_recovery",
                slots=tuple(results),
            )
        return _ok_response("worker slots recovered", tuple(results))

    async def _recover_slot(
        self, store: SlotStorage, deadline: Deadline, stop_deadline: Deadline
    ) -> SlotResult | None:
        observation = self._systemd_call(
            stop_deadline, self._runtime.observe, store.unit, stop_deadline
        )
        if observation.unit != store.unit:
            raise LifecycleConflict("systemd returned a foreign unit observation")
        retained_identity = isinstance(observation, UnitObservation)
        if retained_identity:
            if observation.membership == "unknown":
                raise SystemdUnavailable("worker cgroup membership is unavailable")
            if observation.membership == "populated":
                return SlotResult(
                    slot=store.slot,
                    unit=store.unit,
                    code=_RECOVERY_REFUSED,
                    message="fixed worker unit still has live processes",
                )
        inspection = self._store_call(stop_deadline, store.inspect)
        recovery = await self._retire_inspected_slot(
            store, inspection, observation, deadline, stop_deadline
        )
        if recovery.refusal is not None:
            return SlotResult(
                slot=store.slot,
                unit=store.unit,
                code=recovery.refusal,
                message=_REFUSAL_MESSAGES[recovery.refusal],
            )
        if retained_identity:
            # Only a unit systemd still accounts for can be holding an identity to release; a
            # BootObservation is already the inactive, empty-identity state `require_inactive`
            # wants, so resetting it would be a no-op that hides which slots this call touched.
            self._systemd_call(stop_deadline, self._runtime.reset_failed, store.unit, stop_deadline)
        if recovery.state is not None:
            return _result(recovery.state)
        if recovery.cleared:
            return SlotResult(
                slot=store.slot, unit=store.unit, message="retired the residual worker slot"
            )
        if retained_identity:
            return SlotResult(
                slot=store.slot, unit=store.unit, message="cleared the retained unit identity"
            )
        return None

    async def _retire_inspected_slot(
        self,
        store: SlotStorage,
        inspection: SlotInspection,
        observation: UnitObservation | BootObservation,
        deadline: Deadline,
        stop_deadline: Deadline,
    ) -> _Recovery:
        if inspection.state is not None:
            identity = (
                _state_identity(inspection.state)
                if inspection.state.phase in _OBSERVED_PHASES
                else None
            )
            if identity is not None and _identity_is_unreadable(identity, observation):
                return _Recovery(refusal=_RECOVERY_REFUSED_IDENTITY)
            try:
                retired = await self._retire_slot(
                    store, inspection.state, observation, deadline, stop_deadline
                )
            except _RESIDUAL_FALLBACK as exc:
                # #2533 cases 3 and 4: the retained binding no longer matches the row, so the
                # evidenced path cannot commit. The row is the fence holder, so fall back to
                # proving *its* identity dead and releasing it with its own stored binding.
                _log.warning(
                    "recovery falling back to the registered binding unit=%s slot=%d cause=%s",
                    store.unit,
                    store.slot,
                    type(exc).__name__,
                )
            else:
                return _Recovery(state=retired)
        return await self._retire_residual_slot(
            store, inspection, observation, deadline, stop_deadline
        )

    async def _retire_residual_slot(
        self,
        store: SlotStorage,
        inspection: SlotInspection,
        observation: UnitObservation | BootObservation,
        deadline: Deadline,
        stop_deadline: Deadline,
    ) -> _Recovery:
        releasable: list[tuple[LocalWorkerIncarnation, TerminationOutcome]] = []
        for record in await self._authority_records(store, deadline):
            if record.authority_binding["host"] != socket.gethostname():
                # Another host's fence. The incarnation prefix carries no host, so a shared
                # database can surface one; it is not this host's to release (ADR-0667).
                _log.warning(
                    "recovery skipped a foreign-host fence unit=%s slot=%d", store.unit, store.slot
                )
                continue
            identity = _registered_identity(store, record)
            if identity is None:
                return _Recovery(refusal=_RECOVERY_REFUSED_INCOHERENT)
            if _identity_is_unreadable(identity, observation):
                return _Recovery(refusal=_RECOVERY_REFUSED_IDENTITY)
            outcome = _identity_outcome(identity, observation)
            if outcome is None:
                return _Recovery(refusal=_RECOVERY_REFUSED)
            releasable.append((record, outcome))
        for record, outcome in releasable:
            await self._release(record, outcome, deadline)
        removed = self._store_call(stop_deadline, store.discard_unrecoverable)
        cleared = bool(releasable) or removed
        if cleared:
            # The residue is why this slot needed the residual path at all, and it is the only
            # place the absent-versus-malformed distinction reaches an operator. Recovery must not
            # branch on it; reporting it is what it is for.
            _log.warning(
                "recovery retired a residual slot unit=%s slot=%d residue=%s released=%d",
                store.unit,
                store.slot,
                inspection.residue,
                len(releasable),
            )
        return _Recovery(cleared=cleared)

    async def _authority_records(
        self, store: SlotStorage, deadline: Deadline
    ) -> tuple[LocalWorkerIncarnation, ...]:
        try:
            return await self._authority_call(
                deadline, lambda: self._authority.recoverable(store.unit)
            )
        except LifecycleDeadlineExceeded:
            raise
        except Exception as exc:
            raise _AuthorityUnavailable("worker recovery authority unavailable") from exc

    async def _release(
        self, record: LocalWorkerIncarnation, outcome: TerminationOutcome, deadline: Deadline
    ) -> None:
        try:
            await self._authority_call(deadline, lambda: self._authority.release(record, outcome))
        except EvidenceRejected:
            raise
        except LifecycleDeadlineExceeded:
            raise
        except IncarnationConflict:
            raise
        except Exception as exc:
            raise _AuthorityUnavailable("worker termination authority unavailable") from exc

    async def _retire_slot(
        self,
        store: SlotStorage,
        state: SlotState,
        observation: UnitObservation | BootObservation,
        deadline: Deadline,
        stop_deadline: Deadline,
    ) -> SlotState:
        if state.phase is SlotPhase.PREPARED:
            # A prepared generation is registered only at the gated-to-registered step, so it
            # holds no fence and has no evidence to reconcile; its files are simply discarded.
            # `stop` discards it too where the unit is already inactive and empty, and adopts it
            # as GATED for the audit record only where `require_inactive` still refuses the unit;
            # recovery has no reason to re-open a generation nothing is fenced against.
            self._store_call(stop_deadline, store.discard_prepared, state)
            return state
        if state.phase is not SlotPhase.TERMINATED:
            outcome = _terminal_observation(state, observation)
            if outcome is None:
                # `_recover_slot` refuses a populated cgroup over this same observation before
                # reaching here, and that is the only condition under which the observation
                # declines to yield an outcome. Kept so a reordering fails closed instead of
                # retiring a live worker's binding.
                raise LifecycleConflict("recovery observed a live worker after proving it dead")
            state = await self._terminate_for_stop(store, state, outcome, deadline)
        self._post_evidence_cleanup(store, state, stop_deadline)
        return state

    async def _replace_current_fleet(self, deadline: Deadline) -> None:
        bound: list[tuple[SlotStorage, SlotState]] = []
        terminated: list[tuple[SlotStorage, SlotState]] = []
        for store in self._stores:
            state = self._store_call(deadline, store.load)
            if state is None:
                continue
            state = await self._reconcile_for_start(store, state, deadline)
            if state.phase is SlotPhase.STARTED:
                bound.append((store, state))
            elif state.phase is SlotPhase.TERMINATED:
                terminated.append((store, state))
        stop_deadline = _BudgetDeadline(deadline, _STOP_SECONDS)
        newly_terminated = await self._stop_bound_states(tuple(bound), deadline, stop_deadline)
        terminated.extend(zip((store for store, _ in bound), newly_terminated, strict=True))
        for store, state in terminated:
            self._post_evidence_cleanup(store, state, stop_deadline)

    async def _activate_fleet(
        self, request: LifecycleRequest, deadline: Deadline
    ) -> tuple[SlotState, ...]:
        if request.worker_count is None:
            raise ValueError("validated start request has no worker count")
        activated: list[tuple[SlotStorage, SlotState]] = []
        for store in self._stores[: request.worker_count]:
            try:
                self._systemd_call(deadline, self._runtime.require_inactive, store.unit, deadline)
                prepared = self._store_call(deadline, store.prepare, request.settings)
                state = await self._reconcile_prepared(store, prepared, deadline)
                if state.phase is not SlotPhase.STARTED:
                    raise LifecycleConflict("new worker invocation exited before activation")
                activated.append((store, state))
            except Exception as exc:
                cleaned = await self._rollback_activated(activated, deadline)
                raise _ActivationFailure(exc, cleaned) from exc
        return tuple(state for _, state in activated)

    async def _rollback_activated(
        self, activated: list[tuple[SlotStorage, SlotState]], deadline: Deadline
    ) -> tuple[SlotState, ...]:
        if not activated:
            return ()
        cleaned: list[SlotState] = []
        try:
            stop_deadline = _BudgetDeadline(deadline, _STOP_SECONDS)
            terminated = await self._stop_bound_states(tuple(activated), deadline, stop_deadline)
            for (store, _), state in zip(activated, terminated, strict=True):
                self._post_evidence_cleanup(store, state, stop_deadline)
                cleaned.append(state)
        except Exception as exc:
            _log.error(
                "systemd activation rollback failed cause=%s cleaned_slots=%s",
                type(exc).__name__,
                [state.slot for state in cleaned],
            )
            return tuple(cleaned)
        return tuple(cleaned)

    async def _reconcile_for_start(
        self, store: SlotStorage, state: SlotState, deadline: Deadline
    ) -> SlotState:
        if state.phase is SlotPhase.PREPARED:
            return await self._reconcile_prepared(store, state, deadline)
        if state.phase is SlotPhase.GATED:
            return await self._reconcile_gated(store, state, deadline)
        if state.phase is SlotPhase.REGISTERED:
            return await self._reconcile_registered(store, state, deadline)
        if state.phase is SlotPhase.STARTED:
            return await self._reconcile_started(store, state, deadline)
        return self._reconcile_terminated(state)

    async def _reconcile_prepared(
        self, store: SlotStorage, state: SlotState, deadline: Deadline
    ) -> SlotState:
        self._systemd_call(deadline, self._runtime.start, state.unit, deadline)
        observation = self._systemd_call(deadline, self._runtime.observe, state.unit, deadline)
        _require_prepared_observation(state, observation)
        gated = state.model_copy(
            update={
                "phase": SlotPhase.GATED,
                "boot_id": observation.boot_id,
                "invocation_id": observation.invocation_id,
            }
        )
        self._store_call(deadline, store.persist, gated)
        return await self._reconcile_gated(store, gated, deadline, observation=observation)

    async def _reconcile_gated(
        self,
        store: SlotStorage,
        state: SlotState,
        deadline: Deadline,
        *,
        observation: UnitObservation | None = None,
    ) -> SlotState:
        observed = observation or self._systemd_call(
            deadline, self._runtime.observe, state.unit, deadline
        )
        terminal = _terminal_observation(state, observed)
        await self._register(state, deadline)
        registered = state.model_copy(update={"phase": SlotPhase.REGISTERED})
        self._store_call(deadline, store.persist, registered)
        if terminal is not None:
            return await self._publish_termination(store, registered, terminal, deadline)
        return await self._reconcile_registered(store, registered, deadline)

    async def _reconcile_registered(
        self, store: SlotStorage, state: SlotState, deadline: Deadline
    ) -> SlotState:
        observation = self._systemd_call(deadline, self._runtime.observe, state.unit, deadline)
        terminal = _terminal_observation(state, observation)
        if terminal is not None:
            return await self._publish_termination(store, state, terminal, deadline)
        self._store_call(deadline, store.publish_release, state)
        released = self._systemd_call(deadline, self._runtime.observe, state.unit, deadline)
        terminal = _terminal_observation(state, released)
        if terminal is not None:
            return await self._publish_termination(store, state, terminal, deadline)
        started = state.model_copy(update={"phase": SlotPhase.STARTED})
        self._store_call(deadline, store.persist, started)
        return started

    async def _reconcile_started(
        self, store: SlotStorage, state: SlotState, deadline: Deadline
    ) -> SlotState:
        observation = self._systemd_call(deadline, self._runtime.observe, state.unit, deadline)
        terminal = _terminal_observation(state, observation)
        if terminal is None:
            return state
        return await self._publish_termination(store, state, terminal, deadline)

    @staticmethod
    def _reconcile_terminated(state: SlotState) -> SlotState:
        if state.phase is not SlotPhase.TERMINATED:
            raise LifecycleConflict("terminated reconciliation requires terminated state")
        return state

    async def _status_slot(
        self, store: SlotStorage, state: SlotState, deadline: Deadline
    ) -> SlotState:
        if state.phase in {SlotPhase.PREPARED, SlotPhase.TERMINATED}:
            return state
        observation = self._systemd_call(deadline, self._runtime.observe, state.unit, deadline)
        terminal = _terminal_observation(state, observation)
        if terminal is None:
            return state
        if state.phase is SlotPhase.GATED:
            await self._register(state, deadline)
            state = state.model_copy(update={"phase": SlotPhase.REGISTERED})
            self._store_call(deadline, store.persist, state)
        return await self._publish_termination(store, state, terminal, deadline)

    async def _stop_current_fleet(self, deadline: Deadline) -> tuple[SlotState, ...]:
        bound: list[tuple[SlotStorage, SlotState]] = []
        terminated: list[tuple[SlotStorage, SlotState]] = []
        discarded: list[SlotState] = []
        stop_deadline = _BudgetDeadline(deadline, _STOP_SECONDS)
        for store in self._stores:
            state = self._store_call(stop_deadline, store.load)
            if state is None:
                continue
            if state.phase is SlotPhase.PREPARED:
                adopted = self._resolve_prepared_for_stop(store, state, stop_deadline)
                if adopted is None:
                    discarded.append(state)
                    continue
                state = adopted
            if state.phase is SlotPhase.TERMINATED:
                terminated.append((store, state))
                continue
            observation = self._systemd_call(
                stop_deadline, self._runtime.observe, state.unit, stop_deadline
            )
            outcome = _terminal_observation(state, observation)
            if outcome is not None:
                state = await self._terminate_for_stop(store, state, outcome, deadline)
                terminated.append((store, state))
            else:
                bound.append((store, state))
        newly_terminated = await self._stop_bound_states(tuple(bound), deadline, stop_deadline)
        terminated.extend(zip((store for store, _ in bound), newly_terminated, strict=True))
        for store, state in terminated:
            self._post_evidence_cleanup(store, state, stop_deadline)
        return tuple(
            sorted((*discarded, *(state for _, state in terminated)), key=lambda state: state.slot)
        )

    def _resolve_prepared_for_stop(
        self, store: SlotStorage, state: SlotState, deadline: Deadline
    ) -> SlotState | None:
        try:
            self._systemd_call(deadline, self._runtime.require_inactive, state.unit, deadline)
        except SystemdConflict:
            pass
        else:
            self._store_call(deadline, store.discard_prepared, state)
            return None
        observation = self._systemd_call(deadline, self._runtime.observe, state.unit, deadline)
        _require_prepared_observation(state, observation)
        gated = state.model_copy(
            update={
                "phase": SlotPhase.GATED,
                "boot_id": observation.boot_id,
                "invocation_id": observation.invocation_id,
            }
        )
        self._store_call(deadline, store.persist, gated)
        return gated

    async def _stop_bound_states(
        self,
        states: tuple[tuple[SlotStorage, SlotState], ...],
        deadline: Deadline,
        stop_deadline: Deadline,
    ) -> tuple[SlotState, ...]:
        for _, state in states:
            self._systemd_call(
                stop_deadline,
                self._runtime.signal_terminate,
                state.unit,
                stop_deadline,
            )
        terminated: list[SlotState] = []
        for store, state in states:
            outcome = self._wait_for_terminal(state, stop_deadline)
            terminated.append(await self._terminate_for_stop(store, state, outcome, deadline))
        return tuple(terminated)

    def _wait_for_terminal(self, state: SlotState, deadline: Deadline) -> TerminationOutcome:
        while True:
            observation = self._systemd_call(deadline, self._runtime.observe, state.unit, deadline)
            outcome = _terminal_observation(state, observation)
            if outcome is not None:
                return outcome
            remaining = deadline.remaining()
            if remaining <= 0:
                raise LifecycleDeadlineExceeded("worker stop exceeded its monotonic ceiling")
            self._wait(min(_POLL_SECONDS, remaining))

    async def _terminate_for_stop(
        self,
        store: SlotStorage,
        state: SlotState,
        outcome: TerminationOutcome,
        deadline: Deadline,
    ) -> SlotState:
        if state.phase is SlotPhase.GATED:
            await self._register(state, deadline)
            state = state.model_copy(update={"phase": SlotPhase.REGISTERED})
            self._store_call(deadline, store.persist, state)
        return await self._publish_termination(store, state, outcome, deadline)

    async def _publish_termination(
        self,
        store: SlotStorage,
        state: SlotState,
        outcome: TerminationOutcome,
        deadline: Deadline,
    ) -> SlotState:
        await self._terminate(state, outcome, deadline)
        terminated = state.model_copy(update={"phase": SlotPhase.TERMINATED, "outcome": outcome})
        self._store_call(deadline, store.persist, terminated)
        return terminated

    def _post_evidence_cleanup(
        self, store: SlotStorage, state: SlotState, deadline: Deadline
    ) -> None:
        if state.phase is not SlotPhase.TERMINATED:
            raise LifecycleConflict("cleanup requires persisted terminal evidence")
        self._systemd_call(deadline, self._runtime.stop_retained, state.unit, deadline)
        self._store_call(deadline, store.cleanup_terminated, state)

    async def _register(self, state: SlotState, deadline: Deadline) -> None:
        try:
            await self._authority_call(
                deadline,
                lambda: self._authority.register(state, bytes.fromhex(state.credential_hash)),
            )
        except EvidenceRejected:
            raise
        except LifecycleDeadlineExceeded:
            raise
        except IncarnationConflict:
            raise
        except Exception as exc:
            raise _AuthorityUnavailable("worker registration authority unavailable") from exc

    async def _terminate(
        self, state: SlotState, outcome: TerminationOutcome, deadline: Deadline
    ) -> None:
        try:
            await self._authority_call(deadline, lambda: self._authority.terminate(state, outcome))
        except EvidenceRejected:
            raise
        except LifecycleDeadlineExceeded:
            raise
        except IncarnationConflict:
            # No current `IncarnationAuthority.terminate` backend raises this — only
            # `register`'s unique-violation path does today. Kept for symmetry with
            # `_register` and as forward defense for the recovery-termination path
            # issue #2488 adds, so a future fence-checked terminate does not fall
            # through to `_AuthorityUnavailable` the way registration once did.
            raise
        except Exception as exc:
            raise _AuthorityUnavailable("worker termination authority unavailable") from exc

    async def _authority_call[T](
        self,
        deadline: Deadline,
        operation: Callable[[], Coroutine[Any, Any, T]],
    ) -> T:
        remaining = _require_time(deadline)
        try:
            result = await asyncio.wait_for(operation(), timeout=remaining)
        except TimeoutError as exc:
            raise LifecycleDeadlineExceeded("database operation exceeded request deadline") from exc
        _require_time(deadline)
        return result

    @staticmethod
    def _systemd_call(deadline: Deadline, operation: Callable[..., Any], *args: object) -> Any:
        _require_time(deadline)
        try:
            result = operation(*args)
        except Exception as exc:
            if deadline.remaining() <= 0:
                raise LifecycleDeadlineExceeded(
                    "systemd operation exceeded request deadline"
                ) from exc
            raise
        _require_time(deadline)
        return result

    @staticmethod
    def _store_call(deadline: Deadline, operation: Callable[..., Any], *args: object) -> Any:
        _require_time(deadline)
        try:
            result = operation(*args)
        except Exception as exc:
            if deadline.remaining() <= 0:
                raise LifecycleDeadlineExceeded("slot operation exceeded request deadline") from exc
            raise
        _require_time(deadline)
        return result

    def _failure_response(
        self,
        error: Exception,
        deadline: Deadline,
        *,
        diagnostic: bool = False,
        additional: tuple[SlotState, ...] = (),
    ) -> LifecycleResponse:
        code, action, message = _map_failure(error, diagnostic=diagnostic)
        return LifecycleResponse(
            ok=False,
            code=code,
            message=message,
            retry_action=action,
            slots=self._retained_results(code, deadline, additional=additional),
        )

    def _retained_results(
        self,
        code: str,
        deadline: Deadline,
        *,
        additional: tuple[SlotState, ...] = (),
    ) -> tuple[SlotResult, ...]:
        results = {state.slot: _result(state, code=code) for state in additional}
        for store in self._stores:
            if deadline.remaining() <= 0:
                break
            try:
                state = self._store_call(deadline, store.load)
            except LifecycleDeadlineExceeded:
                break
            except Exception:
                results[store.slot] = SlotResult(
                    slot=store.slot, unit=store.unit, code=code, message="unreadable"
                )
            else:
                if state is not None:
                    results[store.slot] = _result(state, code=code)
        return tuple(results[slot] for slot in sorted(results))


def _require_time(deadline: Deadline) -> float:
    remaining = deadline.remaining()
    if remaining <= 0:
        raise LifecycleDeadlineExceeded("lifecycle request deadline exceeded")
    return remaining


def _require_prepared_observation(
    state: SlotState, observation: UnitObservation | BootObservation
) -> None:
    if observation.unit != state.unit:
        raise LifecycleConflict("systemd returned a foreign unit observation")
    if isinstance(observation, BootObservation):
        raise SystemdUnavailable("prepared worker has no exact systemd invocation")
    if observation.membership == "unknown":
        raise SystemdUnavailable("worker cgroup membership is unavailable")


@dataclass(frozen=True, slots=True)
class _InvocationIdentity:
    """One exact systemd invocation, from retained state or from the registered binding."""

    unit: str
    slot: int
    boot_id: str
    invocation_id: str


def _state_identity(state: SlotState) -> _InvocationIdentity | None:
    """Return the retained invocation identity, or ``None`` for an unbound phase."""
    if state.boot_id is None or state.invocation_id is None:
        return None
    return _InvocationIdentity(state.unit, state.slot, state.boot_id, state.invocation_id)


def _identity_is_unreadable(
    identity: _InvocationIdentity, observation: UnitObservation | BootObservation
) -> bool:
    """Report the one case ADR-0657:62-66 forbids recovering.

    A ``BootObservation`` on the *retained* boot means systemd has no invocation identity for this
    unit, and ADR-0574 forbids reading that absence as termination. Nothing can prove the
    registered invocation ended, so recovery refuses rather than clearing. A different boot ID is
    not this case: that is real evidence, and ``_identity_outcome`` maps it to ``killed``.
    """
    return isinstance(observation, BootObservation) and observation.boot_id == identity.boot_id


def _registered_identity(
    store: SlotStorage, record: LocalWorkerIncarnation
) -> _InvocationIdentity | None:
    """Return the invocation identity the fence claims, or ``None`` if the row is incoherent.

    ``_validated_binding`` has already rejected a binding with missing or empty members, so the
    only inconsistency left is a stored ``unit`` disagreeing with the incarnation prefix the row
    was found by. Such a row cannot be trusted to name an invocation, so it is refused.
    """
    binding = record.authority_binding
    if binding["unit"] != store.unit:
        return None
    return _InvocationIdentity(store.unit, store.slot, binding["boot_id"], binding["invocation_id"])


def _terminal_observation(
    state: SlotState, observation: UnitObservation | BootObservation
) -> TerminationOutcome | None:
    # The foreign-unit check stays ahead of `_state_identity`, even though `_identity_outcome`
    # repeats it: the original checked the unit first, so moving it would swap which
    # `LifecycleConflict` a foreign observation of an unbound state raises.
    if observation.unit != state.unit:
        raise LifecycleConflict("systemd returned a foreign unit observation")
    identity = _state_identity(state)
    if identity is None:
        raise LifecycleConflict("bound lifecycle phase has no exact invocation")
    return _identity_outcome(identity, observation)


def _identity_outcome(
    identity: _InvocationIdentity, observation: UnitObservation | BootObservation
) -> TerminationOutcome | None:
    if observation.unit != identity.unit:
        raise LifecycleConflict("systemd returned a foreign unit observation")
    if observation.boot_id != identity.boot_id:
        return "killed"
    if isinstance(observation, BootObservation):
        raise SystemdUnavailable("worker invocation is absent on the retained boot")
    if observation.invocation_id != identity.invocation_id:
        # A unit carries one invocation at a time and is assigned a new INVOCATION_ID only when it
        # leaves an inactive state, so a successor identity on the retained boot proves the
        # retained invocation ended. Its own exit facts went with it, and the observed result and
        # membership describe the successor, so neither is mapped here (ADR-0657, amending
        # ADR-0574; absence, which ADR-0574's same-boot rule governs, is still refused above).
        # Logged because this is the only trace an out-of-band restart leaves: the outcome is the
        # same `killed` any unobservable termination gets, and the same request deletes the slot
        # files that would otherwise carry the timeline.
        _log.warning(
            "retained worker invocation was replaced out of band unit=%s slot=%d "
            "retained_invocation=%s observed_invocation=%s",
            identity.unit,
            identity.slot,
            identity.invocation_id,
            observation.invocation_id,
        )
        return "killed"
    if observation.membership == "unknown":
        raise SystemdUnavailable("worker cgroup membership is unavailable")
    if observation.membership == "populated":
        return None
    return _outcome(observation)


def _outcome(observation: UnitObservation) -> TerminationOutcome:
    if observation.result == "success" and observation.exec_main_status == 0:
        return "succeeded"
    killed_results = {"signal", "core-dump", "timeout", "watchdog", "oom-kill"}
    return "killed" if observation.result in killed_results else "failed"


def _result(state: SlotState, *, code: str = "ok") -> SlotResult:
    return SlotResult(slot=state.slot, unit=state.unit, phase=state.phase, code=code)


def _with_completed_slots(
    response: LifecycleResponse, completed: tuple[SlotResult, ...]
) -> LifecycleResponse:
    """Keep slots a failed sweep already finished visible beside the failure.

    ``_retained_results`` rebuilds its list by reloading every store, so a slot this request
    already retired -- its ``state.json`` gone -- disappears from the report. Recovery is the
    operator escape hatch and the whole point of it is knowing which fences were released, so a
    sweep that aborts at slot 5 must still say that slots 1 to 4 were retired. A reloaded result
    is the more current fact and wins where both describe one slot -- except for a refusal, whose
    slot is deliberately left loadable, so the reload would overwrite "this unit has live
    processes" with the unrelated code the sweep later failed on.
    """
    merged = {result.slot: result for result in completed}
    merged.update(
        {
            result.slot: result
            for result in response.slots
            if merged.get(result.slot) is None or merged[result.slot].code not in _REFUSALS
        }
    )
    return LifecycleResponse(
        ok=response.ok,
        code=response.code,
        message=response.message,
        retry_action=response.retry_action,
        slots=tuple(merged[slot] for slot in sorted(merged)),
        diagnostics=response.diagnostics,
    )


def _ok_response(message: str, slots: tuple[SlotResult, ...]) -> LifecycleResponse:
    return LifecycleResponse(
        ok=True,
        code="ok",
        message=message,
        retry_action="none",
        slots=slots,
    )


def _invalid_start_response() -> LifecycleResponse:
    return LifecycleResponse(
        ok=False,
        code="invalid_request",
        message="start requires a validated start request",
        retry_action="correct_request",
    )


def _map_failure(error: Exception, *, diagnostic: bool) -> tuple[ResponseCode, RetryAction, str]:
    if isinstance(error, (LifecycleDeadlineExceeded, CommandDeadlineExceeded)):
        return "deadline_exceeded", "retry_same_operation", "lifecycle deadline exceeded"
    if isinstance(error, EvidenceRejected):
        return "evidence_rejected", "retry_same_operation", "termination evidence was rejected"
    if isinstance(error, IncarnationConflict):
        # A unique violation on the fence table, not a database outage: the database answered
        # and refused the write because a still-active fence disputes it (issue #2487).
        return (
            "conflict",
            "operator_recovery",
            "worker incarnation conflicts with an active fence",
        )
    if isinstance(error, _AuthorityUnavailable):
        return "dependency_unavailable", "restore_database", "database authority is unavailable"
    if isinstance(error, SystemdUnavailable):
        return "dependency_unavailable", "restore_systemd", "systemd evidence is unavailable"
    if isinstance(error, LifecycleConflict):
        return (
            "conflict",
            "operator_recovery",
            "retained lifecycle facts conflict with the observed unit",
        )
    if isinstance(error, StateConflict):
        return "conflict", "operator_recovery", "retained slot state conflicts with lifecycle rules"
    if isinstance(error, SystemdConflict):
        return (
            "conflict",
            "operator_recovery",
            "systemd observation conflicts with the retained lifecycle contract",
        )
    if diagnostic:
        return "diagnostics_withheld", "retry_same_operation", "diagnostics could not be acquired"
    return "internal_error", "operator_recovery", "lifecycle operation failed"
