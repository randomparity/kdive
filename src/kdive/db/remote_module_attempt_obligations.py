"""Durable remote-module attempt obligations (ADR-0588, amending ADR-0585).

One row per module attempt, keyed on ``(system_id, run_id, operation_nonce)`` and carrying no
volume kind: an obligation is a property of the attempt, and one attempt owns several volumes.
The row is opened before any of that attempt's volumes are created, so the reconciler sweep can
never observe a volume whose attempt has no row.

Callers own transaction boundaries, as they do for the sibling activation repository. Nothing here
translates a database failure: the migration's constraints and its write-once trigger are the
enforcement, and their messages name what was violated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock

type MutationDischargeReason = Literal["restored", "baseline_committed", "terminal_escape"]

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DISCHARGE_REASONS: frozenset[str] = frozenset(
    ("restored", "baseline_committed", "terminal_escape")
)


class ModuleAttemptObligationError(RuntimeError):
    """A durable obligation was addressed in a state that cannot satisfy the request."""


@dataclass(frozen=True, slots=True)
class ModuleAttempt:
    """The attempt tuple ADR-0588 encodes into every one of the attempt's volume names."""

    system_id: UUID
    run_id: UUID
    operation_nonce: str

    def __post_init__(self) -> None:
        if _NONCE_RE.fullmatch(self.operation_nonce) is None:
            raise ValueError(
                "module attempt operation_nonce must be 32 lowercase hex characters, got "
                f"{self.operation_nonce!r}"
            )

    @property
    def key(self) -> tuple[UUID, UUID, str]:
        """The parameter triple every statement in this module binds, in column order."""
        return (self.system_id, self.run_id, self.operation_nonce)


@dataclass(frozen=True, slots=True)
class ModuleAttemptTerminalEvidence:
    """The payload the discarded ``attempt-reap`` volume metadata element used to carry.

    The two payloads are the opaque canonical documents the provider validates; this layer stores
    and returns them without knowing their field sets, so the provider's document models stay the
    single place their shapes are defined. ``recovery_reference`` is the
    ``remote-module-recovery-ref-v1`` document the reap readers rebuild the attempt from, and the
    baseline identities and installed counts are the ones the marker took from the *original*
    recovery reference rather than the terminal one.
    """

    terminal_operation: dict[str, Any]
    terminal_operation_identity: str
    terminal_result: dict[str, Any]
    terminal_result_identity: str
    baseline_operation_identity: str
    baseline_result_identity: str
    installed_entry_count: int
    installed_content_bytes: int
    recovery_reference: dict[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "terminal_operation_identity",
            "terminal_result_identity",
            "baseline_operation_identity",
            "baseline_result_identity",
        ):
            value = getattr(self, name)
            if _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a sha256:<64 hex> digest, got {value!r}")
        if not 0 <= self.installed_entry_count <= 200_000:
            raise ValueError(
                f"installed_entry_count must be between 0 and 200000, got "
                f"{self.installed_entry_count}"
            )
        if not 0 <= self.installed_content_bytes <= 8_589_934_592:
            raise ValueError(
                f"installed_content_bytes must be between 0 and 8589934592, got "
                f"{self.installed_content_bytes}"
            )


@dataclass(frozen=True, slots=True)
class RetainedModuleAttempt:
    """One attempt with at least one un-discharged obligation.

    The two flags are separate because the obligations govern different volume kinds: the mutation
    obligation covers ``source.ext4`` and ``scratch.ext4``, the reap obligation covers
    ``reaping.journal`` and ``reaped.journal``. Collapsing them into one flag is the shared rule
    ADR-0588 rejects, under which the sweep deletes the resume marker on the ordinary path.
    """

    attempt: ModuleAttempt
    mutation_retained: bool
    reap_retained: bool


def _evidence(row: dict[str, Any]) -> ModuleAttemptTerminalEvidence | None:
    if row["terminal_operation"] is None:
        return None
    return ModuleAttemptTerminalEvidence(
        terminal_operation=row["terminal_operation"],
        terminal_operation_identity=row["terminal_operation_identity"],
        terminal_result=row["terminal_result"],
        terminal_result_identity=row["terminal_result_identity"],
        baseline_operation_identity=row["baseline_operation_identity"],
        baseline_result_identity=row["baseline_result_identity"],
        installed_entry_count=row["installed_entry_count"],
        installed_content_bytes=row["installed_content_bytes"],
        recovery_reference=row["recovery_reference"],
    )


class RemoteModuleAttemptObligationRepository:
    """Own the durable attempt obligations; callers own transaction boundaries."""

    async def _state(self, conn: AsyncConnection, attempt: ModuleAttempt) -> dict[str, Any] | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT mutation_discharged_at, mutation_discharge_reason, reap_opened_at, "
                "reap_discharged_at, terminal_operation IS NOT NULL AS has_evidence "
                "FROM remote_module_attempt_obligations "
                "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                attempt.key,
            )
            return await cur.fetchone()

    async def _require_state(
        self, conn: AsyncConnection, attempt: ModuleAttempt, action: str
    ) -> dict[str, Any]:
        state = await self._state(conn, attempt)
        if state is None:
            raise ModuleAttemptObligationError(
                f"cannot {action}: no obligation row for attempt "
                f"{attempt.system_id}/{attempt.run_id}/{attempt.operation_nonce}; "
                "open_mutation_obligation must run before the attempt's first volume is created"
            )
        return state

    async def open_mutation_obligation(self, conn: AsyncConnection, attempt: ModuleAttempt) -> bool:
        """Open the attempt's row before any of its volumes exist.

        Idempotent, so a crash-resume that replays the open is harmless. Returns whether this call
        inserted the row; ``False`` means the attempt already had one, whatever state it is in.
        """
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO remote_module_attempt_obligations "
                "(system_id, run_id, operation_nonce) VALUES (%s, %s, %s) "
                "ON CONFLICT ON CONSTRAINT remote_module_attempt_obligations_pkey DO NOTHING",
                attempt.key,
            )
            return cur.rowcount == 1

    async def mutation_obligation_is_open(
        self, conn: AsyncConnection, attempt: ModuleAttempt
    ) -> bool:
        """Return whether the exact attempt has an open mutation obligation."""
        state = await self._state(conn, attempt)
        return state is not None and state["mutation_discharged_at"] is None

    async def discharge_mutation_obligation(
        self, conn: AsyncConnection, attempt: ModuleAttempt, *, reason: MutationDischargeReason
    ) -> bool:
        """Discharge the mutation obligation, releasing ``source.ext4`` and ``scratch.ext4``.

        The first discharge wins: a second call, with the same reason or a different one, is a
        no-op and returns ``False``. Reversing that would let a later reason overwrite the durable
        record of why the recovery point stopped being needed.
        """
        if reason not in _DISCHARGE_REASONS:
            raise ValueError(
                f"mutation discharge reason must be one of {sorted(_DISCHARGE_REASONS)}, "
                f"got {reason!r}"
            )
        async with (
            advisory_xact_lock(conn, LockScope.SYSTEM, attempt.system_id),
            conn.cursor() as cur,
        ):
            await cur.execute(
                "UPDATE remote_module_attempt_obligations "
                "SET mutation_discharged_at = now(), mutation_discharge_reason = %s "
                "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s "
                "AND mutation_discharged_at IS NULL",
                (reason, *attempt.key),
            )
            if cur.rowcount == 1:
                return True
        await self._require_state(conn, attempt, "discharge the mutation obligation")
        return False

    async def record_terminal_evidence(
        self,
        conn: AsyncConnection,
        attempt: ModuleAttempt,
        evidence: ModuleAttemptTerminalEvidence,
    ) -> None:
        """Write the terminal evidence the reap readers have no other source for.

        Write-once in the database: replaying identical evidence succeeds, and evidence that
        differs from what is already stored raises rather than silently winning or silently
        losing.
        """
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE remote_module_attempt_obligations SET "
                "terminal_operation = %s, terminal_operation_identity = %s, "
                "terminal_result = %s, terminal_result_identity = %s, "
                "baseline_operation_identity = %s, baseline_result_identity = %s, "
                "installed_entry_count = %s, installed_content_bytes = %s, "
                "recovery_reference = %s "
                "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                (
                    Jsonb(evidence.terminal_operation),
                    evidence.terminal_operation_identity,
                    Jsonb(evidence.terminal_result),
                    evidence.terminal_result_identity,
                    evidence.baseline_operation_identity,
                    evidence.baseline_result_identity,
                    evidence.installed_entry_count,
                    evidence.installed_content_bytes,
                    Jsonb(evidence.recovery_reference),
                    *attempt.key,
                ),
            )
            if cur.rowcount == 1:
                return
        await self._require_state(conn, attempt, "record the terminal evidence")

    async def read_terminal_evidence(
        self, conn: AsyncConnection, attempt: ModuleAttempt
    ) -> ModuleAttemptTerminalEvidence | None:
        """Read the terminal evidence, or ``None`` when the attempt has none or no row."""
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT terminal_operation, terminal_operation_identity, terminal_result, "
                "terminal_result_identity, baseline_operation_identity, "
                "baseline_result_identity, installed_entry_count, installed_content_bytes, "
                "recovery_reference FROM remote_module_attempt_obligations "
                "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                attempt.key,
            )
            row = await cur.fetchone()
        return None if row is None else _evidence(row)

    async def open_reap_obligation(self, conn: AsyncConnection, attempt: ModuleAttempt) -> bool:
        """Open the reap obligation before the first journal volume is created.

        Idempotent: a replay after the obligation is open returns ``False``. The terminal evidence
        must already be stored, because the marker the journal volumes stand for cannot be built
        without it and the crash-resume path treats that marker as authoritative.
        """
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE remote_module_attempt_obligations SET reap_opened_at = now() "
                "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s "
                "AND reap_opened_at IS NULL AND terminal_operation IS NOT NULL",
                attempt.key,
            )
            if cur.rowcount == 1:
                return True
        state = await self._require_state(conn, attempt, "open the reap obligation")
        if not state["has_evidence"]:
            raise ModuleAttemptObligationError(
                "cannot open the reap obligation for attempt "
                f"{attempt.system_id}/{attempt.run_id}/{attempt.operation_nonce}: no terminal "
                "evidence is stored; call record_terminal_evidence first"
            )
        return False

    async def discharge_reap_obligation(
        self, conn: AsyncConnection, attempt: ModuleAttempt
    ) -> bool:
        """Discharge the reap obligation, releasing the two journal volumes.

        Idempotent on a second discharge. Discharging an obligation that was never opened is a
        caller ordering error and raises rather than inventing a discharge.
        """
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE remote_module_attempt_obligations SET reap_discharged_at = now() "
                "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s "
                "AND reap_opened_at IS NOT NULL AND reap_discharged_at IS NULL",
                attempt.key,
            )
            if cur.rowcount == 1:
                return True
        state = await self._require_state(conn, attempt, "discharge the reap obligation")
        if state["reap_opened_at"] is None:
            raise ModuleAttemptObligationError(
                "cannot discharge the reap obligation for attempt "
                f"{attempt.system_id}/{attempt.run_id}/{attempt.operation_nonce}: it was never "
                "opened"
            )
        return False

    async def retained_owners(self, conn: AsyncConnection) -> tuple[RetainedModuleAttempt, ...]:
        """Read every attempt with an un-discharged obligation, for the sweep's callable to wrap.

        Deliberately unbounded and deliberately not filtered by host or pool: an attempt missing
        from this set is one the sweep is entitled to delete the storage of, so truncating the read
        destroys a live attempt's recovery point. A discharged attempt drops out of the partial
        index this reads through, so the set tracks outstanding work rather than total history.
        """
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT system_id, run_id, operation_nonce, "
                "mutation_discharged_at IS NULL AS mutation_retained, "
                "(reap_opened_at IS NOT NULL AND reap_discharged_at IS NULL) AS reap_retained "
                "FROM remote_module_attempt_obligations "
                "WHERE mutation_discharged_at IS NULL "
                "OR (reap_opened_at IS NOT NULL AND reap_discharged_at IS NULL) "
                "ORDER BY system_id, run_id, operation_nonce"
            )
            rows = await cur.fetchall()
        return tuple(
            RetainedModuleAttempt(
                attempt=ModuleAttempt(
                    system_id=row["system_id"],
                    run_id=row["run_id"],
                    operation_nonce=row["operation_nonce"],
                ),
                mutation_retained=row["mutation_retained"],
                reap_retained=row["reap_retained"],
            )
            for row in rows
        )
