"""Durable rowless System-object version cleanup (ADR-0524, #1751)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg import AsyncConnection

from kdive.artifacts.storage import VersionBatch, VersionPage
from kdive.db.locks import LockScope, require_top_level_transaction, try_advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.console_hosting import CollectorRegistry
from kdive.reconciler.repairs.systems import gone_system_state_values

_log = logging.getLogger(__name__)

LOCAL_SYSTEM_ROOT = "local/systems/"
REMOTE_SYSTEM_ROOT = "remote-libvirt/systems/"
MAX_TARGETS_PER_LANE = 200
MAX_TARGETS_PER_KEY = 20
_LOCAL_NAME = "console-rotation-state.json"
_REMOTE_NAME_PREFIX = "console-parts-"
_CANONICAL_INDEX = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class SystemObjectVersionStore(Protocol):
    """The bounded version inventory and exact-delete surface used by both lanes."""

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = 1000,
    ) -> VersionPage: ...

    def capture_exact_versions(self, key: str, limit: int) -> VersionBatch: ...
    def delete_batch(self, batch: VersionBatch) -> bool: ...


class SystemObjectHostingGate(Protocol):
    """Current local console-hosting leadership and collector presence."""

    @property
    def is_leader(self) -> bool: ...

    @property
    def registry(self) -> CollectorRegistry: ...

    def reserve_system_object_cleanup(
        self, system_id: UUID
    ) -> AbstractAsyncContextManager[bool]: ...


@dataclass(frozen=True, slots=True)
class _Lane:
    name: str
    root: str
    parser: Callable[[str], UUID | None]


@dataclass(slots=True)
class _Tally:
    lane: str
    deleted: int = 0
    failed: int = 0

    def report(self) -> int:
        if not self.failed:
            return self.deleted
        _log.error(
            "reconciler: %s System-object sweep confirmed %d version target(s) deleted and "
            "encountered %d failed key operation(s); survivors remain in version inventory",
            self.lane,
            self.deleted,
            self.failed,
        )
        raise CategorizedError(
            f"{self.lane} System-object version sweep encountered {self.failed} failed "
            f"operation(s); {self.deleted} targets were confirmed deleted",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )


def _canonical_uuid(raw: str) -> UUID | None:
    try:
        parsed = UUID(raw)
    except ValueError:
        return None
    return parsed if str(parsed) == raw else None


def parse_local_system_object_key(key: str) -> UUID | None:
    """Return the System id for one exact local rotation sidecar key."""
    parts = key.split("/")
    if len(parts) != 4 or parts[:2] != ["local", "systems"] or parts[3] != _LOCAL_NAME:
        return None
    return _canonical_uuid(parts[2])


def parse_remote_system_object_key(key: str) -> UUID | None:
    """Return the System id for one exact remote internal console-part key."""
    parts = key.split("/")
    if len(parts) != 4 or parts[:2] != ["remote-libvirt", "systems"]:
        return None
    name = parts[3]
    if not name.startswith(_REMOTE_NAME_PREFIX):
        return None
    index = name[len(_REMOTE_NAME_PREFIX) :]
    if _CANONICAL_INDEX.fullmatch(index) is None:
        return None
    return _canonical_uuid(parts[2])


_LOCAL_LANE = _Lane("local", LOCAL_SYSTEM_ROOT, parse_local_system_object_key)
_REMOTE_LANE = _Lane("remote", REMOTE_SYSTEM_ROOT, parse_remote_system_object_key)


async def sweep_local_system_object_versions(
    conn: AsyncConnection,
    store: SystemObjectVersionStore,
) -> int:
    """Sweep one bounded local sidecar inventory page."""
    return await _sweep_lane(conn, store, _LOCAL_LANE, None)


async def sweep_remote_system_object_versions(
    conn: AsyncConnection,
    store: SystemObjectVersionStore,
    gate: SystemObjectHostingGate | None,
) -> int:
    """Sweep one bounded remote part page only for the current hosting leader."""
    if gate is None or not gate.is_leader:
        return 0
    return await _sweep_lane(conn, store, _REMOTE_LANE, gate)


async def _sweep_lane(
    conn: AsyncConnection,
    store: SystemObjectVersionStore,
    lane: _Lane,
    gate: SystemObjectHostingGate | None,
) -> int:
    observed = await _read_cursor(conn, lane.name)
    page = await asyncio.to_thread(
        store.list_version_page,
        lane.root,
        key_marker=observed,
        version_id_marker=None,
        max_keys=1000,
    )
    keys = _page_keys(page)
    if page.is_truncated and not keys:
        raise CategorizedError(
            f"{lane.name} System-object version listing returned an empty truncated page",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    tally = _Tally(lane.name)
    last_considered, consumed = await _consider_keys(conn, store, lane, gate, keys, tally)
    next_cursor = _next_cursor(observed, page, last_considered, consumed)
    if next_cursor != observed or not page.is_truncated:
        await _compare_and_set_cursor(conn, lane.name, observed, next_cursor)
    return tally.report()


def _page_keys(page: VersionPage) -> tuple[str, ...]:
    keys: list[str] = []
    for entry in page.entries:
        if not keys or keys[-1] != entry.key:
            keys.append(entry.key)
    return tuple(keys)


async def _consider_keys(
    conn: AsyncConnection,
    store: SystemObjectVersionStore,
    lane: _Lane,
    gate: SystemObjectHostingGate | None,
    keys: tuple[str, ...],
    tally: _Tally,
) -> tuple[str | None, bool]:
    charged = 0
    last_considered: str | None = None
    for index, key in enumerate(keys):
        if charged >= MAX_TARGETS_PER_LANE:
            return last_considered, False
        last_considered = key
        system_id = lane.parser(key)
        if system_id is None:
            continue
        limit = min(MAX_TARGETS_PER_KEY, MAX_TARGETS_PER_LANE - charged)
        batch = await _capture_or_count_failure(store, key, limit, tally)
        if batch is None:
            charged += limit
            continue
        charged += max(1, len(batch.targets))
        if batch.targets:
            await _delete_or_count_failure(conn, store, system_id, batch, gate, tally)
        if charged >= MAX_TARGETS_PER_LANE and index != len(keys) - 1:
            return last_considered, False
    return last_considered, True


async def _capture_or_count_failure(
    store: SystemObjectVersionStore,
    key: str,
    limit: int,
    tally: _Tally,
) -> VersionBatch | None:
    try:
        return await asyncio.to_thread(store.capture_exact_versions, key, limit)
    except CategorizedError as exc:
        tally.failed += 1
        _log.warning("reconciler: System-object version capture failed for %s: %s", key, exc)
        return None


async def _delete_or_count_failure(
    conn: AsyncConnection,
    store: SystemObjectVersionStore,
    system_id: UUID,
    batch: VersionBatch,
    gate: SystemObjectHostingGate | None,
    tally: _Tally,
) -> None:
    try:
        tally.deleted += await _delete_if_fenced(conn, store, system_id, batch, gate)
    except (CategorizedError, psycopg.Error) as exc:
        tally.failed += 1
        _log.warning("reconciler: System-object version delete failed for %s: %s", batch.key, exc)


async def _delete_if_fenced(
    conn: AsyncConnection,
    store: SystemObjectVersionStore,
    system_id: UUID,
    batch: VersionBatch,
    gate: SystemObjectHostingGate | None,
) -> int:
    if gate is not None and (not gate.is_leader or gate.registry.has(system_id)):
        return 0
    require_top_level_transaction(conn, "the System-object sweep's per-key delete")
    async with conn.transaction():
        if not await try_advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            return 0
        if not await _system_key_is_retired(conn, system_id, batch.key):
            return 0
    if gate is None:
        return await _delete_batch(store, batch)
    async with gate.reserve_system_object_cleanup(system_id) as permitted:
        if not permitted:
            return 0
        return await _delete_batch(store, batch)


async def _delete_batch(store: SystemObjectVersionStore, batch: VersionBatch) -> int:
    complete = await asyncio.to_thread(store.delete_batch, batch)
    deleted = batch.targets if complete else tuple(t for t in batch.targets if not t.is_latest)
    return len(deleted)


async def _system_key_is_retired(conn: AsyncConnection, system_id: UUID, key: str) -> bool:
    row = await (
        await conn.execute(
            "SELECT s.state, NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.object_key = %s) "
            "FROM systems s WHERE s.id = %s",
            (key, system_id),
        )
    ).fetchone()
    return bool(row is not None and row[0] in gone_system_state_values() and row[1])


async def _read_cursor(conn: AsyncConnection, lane: str) -> str | None:
    require_top_level_transaction(conn, f"reading the {lane} System-object sweep cursor")
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT after_key FROM system_object_sweep_cursors WHERE lane = %s", (lane,)
            )
        ).fetchone()
    if row is None:
        raise RuntimeError(f"system_object_sweep_cursors has no {lane!r} lane")
    return row[0]


def _next_cursor(
    observed: str | None,
    page: VersionPage,
    last_considered: str | None,
    consumed: bool,
) -> str | None:
    if consumed and not page.is_truncated:
        return None
    return last_considered if last_considered is not None else observed


async def _compare_and_set_cursor(
    conn: AsyncConnection,
    lane: str,
    observed: str | None,
    next_cursor: str | None,
) -> bool:
    require_top_level_transaction(conn, f"advancing the {lane} System-object sweep cursor")
    async with conn.transaction():
        result = await conn.execute(
            "UPDATE system_object_sweep_cursors "
            "SET after_key = %s, updated_at = now() "
            "WHERE lane = %s AND after_key IS NOT DISTINCT FROM %s",
            (next_cursor, lane, observed),
        )
    return result.rowcount == 1
