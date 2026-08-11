"""Replay-safe coordination for retained systemd worker incarnations."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.processes.lifecycle.systemd_worker_contract import (
    LifecycleRequest,
    LifecycleResponse,
    ResponseCode,
    RetryAction,
    SlotPhase,
    SlotResult,
    WorkerSettings,
)
from kdive.processes.lifecycle.systemd_worker_runtime import (
    BootObservation,
    CommandDeadlineExceeded,
    Deadline,
    SystemdConflict,
    SystemdUnavailable,
    UnitObservation,
    UnmanagedWorker,
    load_slot_redaction_values,
)
from kdive.processes.lifecycle.systemd_worker_state import (
    SlotState,
    StateConflict,
    TerminationOutcome,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.worker_incarnations import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    register_worker_incarnation,
    terminate_worker_incarnation,
)

_REQUEST_SECONDS = 120.0
_STOP_SECONDS = 45.0
_DIAGNOSTIC_SECONDS = 30.0
_SLOT_ACQUISITION_BYTES = 320 * 1024
_TOTAL_ACQUISITION_BYTES = 1_310_720
_PROPERTY_ACQUISITION_BYTES = 4096
_SLOT_EMISSION_BYTES = 256 * 1024
_TOTAL_EMISSION_BYTES = 1_048_576
_MAX_REDACTION_VALUES = 32
_MAX_REDACTION_VALUE_BYTES = 4096
_POLL_SECONDS = 0.1
_TRUNCATION_MARKER = "[diagnostics truncated]\n"
_AGGREGATE_TRUNCATION_MARKER = "[aggregate diagnostics truncated]\n"
_WITHHELD_TEMPLATE = "[diagnostics withheld for slot {slot}]\n"
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/@\s]+)(@)")
_SCHEMELESS_USERINFO = re.compile(r"(?<![\w/])([^/:@\s]+:[^/@\s]*)(@)(?=[^/\s]+)")
_UNTERMINATED_URL_AUTHORITY = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^\s/]*)\Z")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:password|passwd|token|api[_-]?key|access[_-]?key|secret|"
    r"credential|database_url)[A-Za-z0-9_-]*)(\s*[=:]\s*)([^\r\n]*)"
)
_MASK_SENTINELS = ("~", "^", "#", "%", "?")

type _DiagnosticEntry = tuple[SlotStorage, SlotState | None, bool]


class EvidenceRejected(RuntimeError):
    """PostgreSQL rejected purported terminal evidence for an exact incarnation."""


class LifecycleConflict(RuntimeError):
    """Retained and observed lifecycle facts cannot be reconciled safely."""


class LifecycleDeadlineExceeded(RuntimeError):
    """A lifecycle operation exhausted its shared absolute monotonic deadline."""


class _AuthorityUnavailable(RuntimeError):
    """The exact-incarnation database authority did not complete."""


class _ActivationFailure(RuntimeError):
    """A fleet activation failed after bounded rollback of earlier slots."""

    def __init__(self, cause: Exception, cleaned: tuple[SlotState, ...]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.cleaned = cleaned


class IncarnationAuthority(Protocol):
    """Register and terminate exact immutable worker-incarnation facts."""

    async def register(self, state: SlotState, credential_hash: bytes) -> None:
        """Register one exact local-systemd incarnation and binding."""
        ...

    async def terminate(self, state: SlotState, outcome: TerminationOutcome) -> None:
        """Commit terminal evidence only for the same exact registered binding."""
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


@dataclass(slots=True)
class _DiagnosticCapture:
    reports: list[str] = field(default_factory=list)
    results: list[SlotResult] = field(default_factory=list)
    withheld_slots: set[int] = field(default_factory=set)
    acquired: int = 0
    emitted: int = 0
    aggregate_truncated: bool = False

    def append(self, report: str) -> None:
        available = max(0, _TOTAL_EMISSION_BYTES - self.emitted)
        bounded = _bounded_text(report, available, truncated=False)
        self.reports.append(bounded)
        self.emitted += len(bounded.encode("utf-8"))


class SystemdControl(Protocol):
    """Exact retained-unit operations consumed by the coordinator."""

    def require_inactive(self, unit: str, deadline: Deadline) -> None: ...

    def start(self, unit: str, deadline: Deadline) -> None: ...

    def observe(self, unit: str, deadline: Deadline) -> UnitObservation | BootObservation: ...

    def signal_terminate(self, unit: str, deadline: Deadline) -> None: ...

    def stop_retained(self, unit: str, deadline: Deadline) -> None: ...

    def reset(self, unit: str, deadline: Deadline) -> None: ...

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
        """Select exact retained invocation journals without mutating lifecycle state."""
        operation_deadline = _BudgetDeadline(deadline, _REQUEST_SECONDS)
        diagnostic_deadline = _BudgetDeadline(operation_deadline, _DIAGNOSTIC_SECONDS)
        entries = tuple(
            (store, *self._diagnostic_state(store, diagnostic_deadline)) for store in self._stores
        )
        capture = self._capture_diagnostics(entries, diagnostic_deadline)
        diagnostics = "".join(capture.reports)
        if capture.withheld_slots:
            return LifecycleResponse(
                ok=False,
                code="diagnostics_withheld",
                message="diagnostics withheld for one or more slots",
                retry_action="operator_recovery",
                slots=tuple(capture.results),
                diagnostics=diagnostics,
            )
        return LifecycleResponse(
            ok=True,
            code="ok",
            message="worker diagnostics captured",
            retry_action="none",
            slots=tuple(capture.results),
            diagnostics=diagnostics,
        )

    def _capture_diagnostics(
        self, entries: tuple[_DiagnosticEntry, ...], deadline: Deadline
    ) -> _DiagnosticCapture:
        capture = _DiagnosticCapture()
        for index, (store, state, unsafe_state) in enumerate(entries):
            if unsafe_state:
                capture.withheld_slots.add(store.slot)
                capture.append(_WITHHELD_TEMPLATE.format(slot=store.slot))
                capture.results.append(
                    SlotResult(
                        slot=store.slot,
                        unit=store.unit,
                        code="diagnostics_withheld",
                        message="withheld",
                    )
                )
            elif state is not None:
                has_later = any(
                    later_state is not None or later_unsafe
                    for _, later_state, later_unsafe in entries[index + 1 :]
                )
                capture.results.append(
                    self._capture_diagnostic_slot(
                        store, state, deadline, capture, has_later=has_later
                    )
                )
        return capture

    def _capture_diagnostic_slot(
        self,
        store: SlotStorage,
        state: SlotState,
        deadline: Deadline,
        capture: _DiagnosticCapture,
        *,
        has_later: bool,
    ) -> SlotResult:
        if capture.aggregate_truncated:
            return _result(state)
        remaining_acquisition = _TOTAL_ACQUISITION_BYTES - capture.acquired
        if remaining_acquisition < _PROPERTY_ACQUISITION_BYTES:
            capture.append(_AGGREGATE_TRUNCATION_MARKER)
            capture.aggregate_truncated = True
            return _result(state)
        try:
            report, used, aggregate_truncated = self._diagnose_slot(
                store,
                state,
                deadline,
                acquisition_budget=remaining_acquisition,
                emission_budget=_TOTAL_EMISSION_BYTES - capture.emitted,
                reserve_aggregate=has_later,
            )
        except Exception:
            capture.withheld_slots.add(store.slot)
            capture.append(_WITHHELD_TEMPLATE.format(slot=store.slot))
            return _result(state, code="diagnostics_withheld")
        capture.acquired += used
        capture.append(report)
        capture.aggregate_truncated = aggregate_truncated
        return _result(state)

    def _diagnostic_state(
        self, store: SlotStorage, deadline: Deadline
    ) -> tuple[SlotState | None, bool]:
        try:
            return self._store_call(deadline, store.load), False
        except Exception:
            return None, True

    def _diagnose_slot(
        self,
        store: SlotStorage,
        state: SlotState,
        deadline: Deadline,
        *,
        acquisition_budget: int,
        emission_budget: int,
        reserve_aggregate: bool,
    ) -> tuple[str, int, bool]:
        invocation_id = _require_diagnostic_budget(state, acquisition_budget, emission_budget)
        secret_values = _validated_redaction_values(
            self._load_redaction_values(store.root, store.slot)
        )
        properties = self._systemd_call(
            deadline,
            self._runtime.public_properties,
            state.unit,
            invocation_id,
            deadline,
        )
        slot_budget = min(_SLOT_ACQUISITION_BYTES, acquisition_budget)
        public_text, _, public_truncated = _bounded_chunks(
            (properties,), _PROPERTY_ACQUISITION_BYTES
        )
        public_bytes = _PROPERTY_ACQUISITION_BYTES
        journal_budget = slot_budget - public_bytes
        journal_text, journal_bytes, journal_truncated = self._diagnostic_journal(
            invocation_id, journal_budget, deadline
        )
        raw = f"{public_text}Journal:\n{journal_text}"
        acquisition_truncated = any(
            (public_truncated, journal_truncated, slot_budget < _SLOT_ACQUISITION_BYTES)
        )
        safe, forbidden = _sanitize_diagnostics(
            raw, secret_values, acquisition_truncated=acquisition_truncated
        )
        report = f"=== slot {state.slot} ===\n{safe}"
        emit_limit = min(_SLOT_EMISSION_BYTES, emission_budget)
        report = _bounded_text(report, emit_limit, truncated=acquisition_truncated)
        aggregate_truncated = reserve_aggregate and (
            len(report.encode("utf-8")) + len(_AGGREGATE_TRUNCATION_MARKER.encode("utf-8"))
            > emission_budget
        )
        if aggregate_truncated:
            report = _aggregate_bounded_text(report, emission_budget)
        if _contains_forbidden(report, forbidden):
            raise StateConflict("sanitized diagnostics retained a secret value")
        return report, public_bytes + journal_bytes, aggregate_truncated

    def _diagnostic_journal(
        self, invocation_id: str, byte_limit: int, deadline: Deadline
    ) -> tuple[str, int, bool]:
        if byte_limit <= 0:
            return "", 0, True
        chunks = self._systemd_call(
            deadline,
            self._runtime.journal,
            invocation_id,
            byte_limit,
            deadline,
        )
        source = (chunks,) if isinstance(chunks, str) else chunks
        text, used, truncated = _bounded_chunks(source, byte_limit)
        return text, used, truncated or used >= byte_limit

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
        except Exception:
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
        self._systemd_call(deadline, self._runtime.reset, state.unit, deadline)
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
        except Exception as exc:
            raise _AuthorityUnavailable("worker termination authority unavailable") from exc

    async def _authority_call(
        self,
        deadline: Deadline,
        operation: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        remaining = _require_time(deadline)
        try:
            await asyncio.wait_for(operation(), timeout=remaining)
        except TimeoutError as exc:
            raise LifecycleDeadlineExceeded("database operation exceeded request deadline") from exc
        _require_time(deadline)

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


def _bounded_chunks(chunks: Sequence[str], limit: int) -> tuple[str, int, bool]:
    retained = bytearray()
    truncated = False
    for chunk in chunks:
        encoded = chunk.encode("utf-8")
        available = max(0, limit - len(retained))
        retained.extend(encoded[:available])
        if len(encoded) > available:
            truncated = True
            break
    return retained.decode("utf-8", errors="ignore"), len(retained), truncated


def _require_diagnostic_budget(
    state: SlotState, acquisition_budget: int, emission_budget: int
) -> str:
    if state.invocation_id is None:
        raise StateConflict("slot has no exact invocation for diagnostics")
    if acquisition_budget <= 0 or emission_budget <= 0:
        raise StateConflict("slot has no safe diagnostic acquisition budget")
    return state.invocation_id


def _validated_redaction_values(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > _MAX_REDACTION_VALUES:
        raise StateConflict("slot has unsafe diagnostic redaction sources")
    for value in values:
        if not value or len(value.encode("utf-8")) > _MAX_REDACTION_VALUE_BYTES:
            raise StateConflict("slot has unsafe diagnostic redaction sources")
    return values


def _sanitize_diagnostics(
    text: str,
    secret_values: tuple[str, ...],
    *,
    acquisition_truncated: bool,
) -> tuple[str, tuple[str, ...]]:
    registry = SecretRegistry()
    scope = object()
    for value in secret_values:
        registry.register(value, scope=scope)
    registered = tuple(sorted(registry.snapshot(), key=len, reverse=True))
    structural = _structural_secret_values(text, acquisition_truncated=acquisition_truncated)
    forbidden = tuple(dict.fromkeys((*registered, *structural)))
    for sentinel in _MASK_SENTINELS:
        if any(sentinel in value or value in sentinel for value in forbidden):
            continue
        redacted = _render_sanitized_diagnostics(
            text,
            registered,
            sentinel,
            acquisition_truncated=acquisition_truncated,
        )
        if not _contains_forbidden(redacted, forbidden):
            return redacted, forbidden
    raise StateConflict("diagnostics cannot be rendered without reproducing a secret")


def _structural_secret_values(text: str, *, acquisition_truncated: bool) -> tuple[str, ...]:
    values = [match.group(2) for match in _URL_USERINFO.finditer(text)]
    values.extend(match.group(1) for match in _SCHEMELESS_USERINFO.finditer(text))
    values.extend(match.group(3) for match in _SECRET_ASSIGNMENT.finditer(text))
    if acquisition_truncated and (match := _UNTERMINATED_URL_AUTHORITY.search(text)):
        values.append(match.group(2))
    return tuple(value for value in values if value)


def _render_sanitized_diagnostics(
    text: str,
    registered: tuple[str, ...],
    sentinel: str,
    *,
    acquisition_truncated: bool,
) -> str:
    redacted = text
    for value in registered:
        redacted = redacted.replace(value, _mask_bytes(value, sentinel))
    redacted = _URL_USERINFO.sub(
        lambda match: f"{match.group(1)}{_mask_bytes(match.group(2), sentinel)}{match.group(3)}",
        redacted,
    )
    redacted = _SCHEMELESS_USERINFO.sub(
        lambda match: f"{_mask_bytes(match.group(1), sentinel)}{match.group(2)}",
        redacted,
    )
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_mask_bytes(match.group(3), sentinel)}",
        redacted,
    )
    if acquisition_truncated:
        redacted = _UNTERMINATED_URL_AUTHORITY.sub(
            lambda match: f"{match.group(1)}{_mask_bytes(match.group(2), sentinel)}",
            redacted,
        )
    return _escape_diagnostic_controls(redacted)


def _mask_bytes(value: str, sentinel: str) -> str:
    return sentinel * len(value.encode("utf-8"))


def _contains_forbidden(text: str, forbidden: tuple[str, ...]) -> bool:
    return any(value in text for value in forbidden)


def _escape_diagnostic_controls(text: str) -> str:
    escaped: list[str] = []
    for character in text:
        value = ord(character)
        category = unicodedata.category(character)
        if character == "\n":
            escaped.append(character)
        elif category == "Cc":
            escaped.append(f"\\x{value:02x}")
        elif category in {"Cf", "Zl", "Zp"}:
            width = 4 if value <= 0xFFFF else 8
            escaped.append(f"\\{'u' if width == 4 else 'U'}{value:0{width}x}")
        else:
            escaped.append(character)
    return "".join(escaped).replace("::", "\\x3a\\x3a")


def _bounded_text(text: str, limit: int, *, truncated: bool) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit and not truncated:
        return text
    marker = _TRUNCATION_MARKER.encode()
    if limit <= len(marker):
        return ""
    prefix = encoded[: limit - len(marker) - 1].decode("utf-8", errors="ignore")
    return prefix.rstrip("\n") + "\n" + _TRUNCATION_MARKER


def _aggregate_bounded_text(text: str, limit: int) -> str:
    marker = _AGGREGATE_TRUNCATION_MARKER.encode("utf-8")
    if limit < len(marker):
        return ""
    prefix = text.encode("utf-8")[: limit - len(marker)].decode("utf-8", errors="ignore")
    return prefix + _AGGREGATE_TRUNCATION_MARKER


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


def _terminal_observation(
    state: SlotState, observation: UnitObservation | BootObservation
) -> TerminationOutcome | None:
    if observation.unit != state.unit:
        raise LifecycleConflict("systemd returned a foreign unit observation")
    if state.boot_id is None or state.invocation_id is None:
        raise LifecycleConflict("bound lifecycle phase has no exact invocation")
    if observation.boot_id != state.boot_id:
        return "killed"
    if isinstance(observation, BootObservation):
        raise SystemdUnavailable("worker invocation is absent on the retained boot")
    if observation.invocation_id != state.invocation_id:
        raise LifecycleConflict("systemd invocation does not match retained state")
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
    if isinstance(error, _AuthorityUnavailable):
        return "dependency_unavailable", "restore_database", "database authority is unavailable"
    if isinstance(error, SystemdUnavailable):
        return "dependency_unavailable", "restore_systemd", "systemd evidence is unavailable"
    if isinstance(error, (LifecycleConflict, StateConflict, SystemdConflict)):
        return "conflict", "operator_recovery", "retained lifecycle facts conflict"
    if diagnostic:
        return "diagnostics_withheld", "retry_same_operation", "diagnostics could not be acquired"
    return "internal_error", "operator_recovery", "lifecycle operation failed"
