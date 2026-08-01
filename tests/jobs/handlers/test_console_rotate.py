"""Tests for the console_rotate worker job handler (local rotation, #892).

Drives ``console_rotate_handler`` directly with an in-memory object store (the handler's
object-store boundary) and a migrated Postgres connection (the artifacts row boundary), so the
behaviors verified are: redacted gzip parts stored + rows registered, sidecar advanced, idempotent
retry (insert-if-absent), best-effort degrade on a console-read permission wall, and a boot-id
change starting a new part generation.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    FetchedArtifact,
    HeadResult,
    StoredArtifact,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import JobState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.console import console_rotate
from kdive.providers.console_parts.rotation import RotationState, rotate
from kdive.providers.console_parts.sidecar import read_sidecar
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore
from tests.clock import STORE_MTIME

_CONSOLE = b"console-line payload bytes\n" * 6000  # ~158 KiB -> several rotation parts (64 KiB)


class _FakeStore:
    """In-memory object store recording every put so idempotency can be asserted by key."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, Sensitivity, str, str | None]] = {}
        self.put_calls: list[str] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        key = request.key()
        self.objects[key] = (
            request.data,
            request.sensitivity,
            request.retention_class,
            request.content_encoding,
        )
        self.put_calls.append(key)
        etag = hashlib.sha256(request.data).hexdigest()
        return StoredArtifact(
            key, etag, request.sensitivity, request.retention_class, version_id="test-version"
        )

    def get_artifact(self, key: str, _etag: str | None) -> FetchedArtifact:
        if key not in self.objects:
            raise KeyError(key)
        data, sensitivity, retention, _enc = self.objects[key]
        return FetchedArtifact(data, sensitivity, retention)

    def head(self, key: str) -> HeadResult | None:
        if key not in self.objects:
            return None
        data, sensitivity, _retention, enc = self.objects[key]
        return HeadResult(
            size_bytes=len(data),
            # The real store's etag identifies the bytes, and ADR-0519's compensating delete
            # compares it against what the attempt wrote — a constant here would make that
            # fence untestable, so derive it the same way ``put_artifact`` does.
            checksum_sha256=None,
            etag=hashlib.sha256(data).hexdigest(),
            sensitivity=sensitivity,
            content_encoding=enc,
            last_modified=STORE_MTIME,
            version_id="test-version",
        )

    def part_puts(self) -> list[str]:
        return [k for k in self.put_calls if "console-part-" in k]


def _job(system_id: UUID, boot_id: str) -> Job:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.CONSOLE_ROTATE,
        payload={"system_id": str(system_id), "boot_id": boot_id},
        state=JobState.RUNNING,
        max_attempts=1,
        authorizing={"principal": "reconciler", "agent_session": None, "project": "local"},
        dedup_key=f"console_rotate:{system_id}",
    )


def _write_console(tmp_path: Path, system_id: UUID, data: bytes) -> Path:
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(data)
    return log


async def _seed_system(pool: AsyncConnectionPool, system_id: UUID, state: str) -> None:
    """Insert an allocation + System row so the handler's live-state guard has a row to read."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO allocations (state, principal, project) "
            "VALUES ('requested', 'tester', 'local') RETURNING id"
        )
        row = await cur.fetchone()
        assert row is not None
        await cur.execute(
            "INSERT INTO systems (id, allocation_id, state, provisioning_profile, "
            "principal, project) VALUES (%s, %s, %s, '{}'::jsonb, 'tester', 'local')",
            (system_id, row[0], state),
        )


async def _run_handler(pool: AsyncConnectionPool, store: _FakeStore, job: Job) -> str | None:
    async with pool.connection() as conn:
        return await console_rotate.console_rotate_handler(
            conn,
            job,
            secret_registry=SecretRegistry(),
            artifact_store=cast(ObjectStore, store),
        )


async def _row_keys(pool: AsyncConnectionPool, system_id: UUID) -> list[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT object_key FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s "
            "ORDER BY object_key",
            (system_id,),
        )
        return [row[0] for row in await cur.fetchall()]


def _expected_parts(boot_id: str) -> list[tuple[str, bytes]]:
    """Independent oracle: the pure rotate() parts the handler must store, keyed by object name."""
    redact = console_rotate._make_redactor(SecretRegistry())
    state = RotationState(plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None)
    result = rotate(state, _CONSOLE, boot_id, redact)
    return [(console_rotate.part_object_name(p.gen, p.index), p.redacted) for p in result.parts]


def test_growing_console_seals_redacted_gzip_parts_and_advances_sidecar(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)
    expected = _expected_parts("boot-A")
    assert expected, "fixture console must exceed the 64 KiB rotation threshold"

    async def _run() -> tuple[_FakeStore, list[str], RotationState]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _FakeStore()
            await _run_handler(pool, store, _job(system_id, "boot-A"))
            keys = await _row_keys(pool, system_id)
            state = read_sidecar(cast(ObjectStore, store), "local", system_id)
        return store, keys, state

    store, row_keys, state = asyncio.run(_run())

    for name, redacted in expected:
        key = f"local/systems/{system_id}/{name}"
        assert key in store.objects, f"missing part object {name}"
        data, sensitivity, retention, encoding = store.objects[key]
        assert sensitivity is Sensitivity.REDACTED
        assert retention == "console"
        assert encoding == "gzip"
        head = store.head(key)
        assert head is not None and head.content_encoding == "gzip"
        assert gzip.decompress(data) == redacted
        assert key in row_keys
    assert state.plaintext_offset == len(_CONSOLE)
    assert state.boot_id == "boot-A"
    assert state.next_index == len(expected)


def test_idempotent_retry_after_crash_before_sidecar_write(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)
    sidecar_key = f"local/systems/{system_id}/console-rotation-state.json"

    async def _run() -> tuple[list[str], list[str], list[str]]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _FakeStore()
            await _run_handler(pool, store, _job(system_id, "boot-A"))
            first_parts = list(store.part_puts())
            # Simulate a crash before the sidecar advanced: drop the cursor so the retry
            # re-rotates from the pre-run (ZERO) state and must seal nothing new.
            store.objects.pop(sidecar_key, None)
            await _run_handler(pool, store, _job(system_id, "boot-A"))
            all_parts = list(store.part_puts())
            rows = await _row_keys(pool, system_id)
        return first_parts, all_parts, rows

    first_parts, all_parts, rows = asyncio.run(_run())

    assert first_parts, "first run must seal at least one part"
    assert sorted(first_parts) == sorted(set(first_parts)), "no duplicate part puts within a run"
    assert all_parts == first_parts, "retry must not re-store any part object"
    assert sorted(rows) == sorted(set(rows)), "no duplicate artifact rows on retry"
    assert len(rows) == len(first_parts)


def test_best_effort_when_console_unreadable_registers_no_parts(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()

    def _raise(_path: Path) -> bytes:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"operation": "read_console_log"},
        )

    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: Path("/nonexistent.log"))
    monkeypatch.setattr(console_rotate, "read_console_log", _raise)

    async def _run() -> tuple[str | None, list[str], list[str]]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _FakeStore()
            result = await _run_handler(pool, store, _job(system_id, "boot-A"))
            rows = await _row_keys(pool, system_id)
        return result, rows, store.part_puts()

    result, rows, part_puts = asyncio.run(_run())

    assert result is None
    assert rows == []
    assert part_puts == []


def test_boot_id_change_starts_new_part_generation(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    async def _run() -> list[str]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _FakeStore()
            await _run_handler(pool, store, _job(system_id, "boot-A"))
            await _run_handler(pool, store, _job(system_id, "boot-B"))
            return await _row_keys(pool, system_id)

    rows = asyncio.run(_run())

    assert any("console-part-0-" in key for key in rows), "first boot seals generation 0"
    assert any("console-part-1-" in key for key in rows), "boot-id change seals generation 1"


def test_rotate_decodes_invalid_utf8_and_locates_log_by_system_id(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two invariants in one round-trip:
    #  1. The console log is resolved from THIS system_id — a None/other id finds an absent log
    #     and the handler seals nothing (so a non-empty part set proves the id reached the path).
    #  2. A raw console carrying an invalid UTF-8 byte decodes via the "replace" handler (U+FFFD),
    #     never "strict" (raises) or a bogus error-handler name (LookupError) — the log is
    #     arbitrary worker-local bytes.
    system_id = uuid4()
    monkeypatch.setattr(console_rotate, "console_log_path", lambda sid: tmp_path / f"{sid}.log")
    bad = b"console-line payload \xff bytes\n" * 6000  # invalid UTF-8, > 64 KiB -> seals parts
    (tmp_path / f"{system_id}.log").write_bytes(bad)

    async def _run() -> tuple[str | None, _FakeStore]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _FakeStore()
            result = await _run_handler(pool, store, _job(system_id, "boot-A"))
            return result, store

    result, store = asyncio.run(_run())

    part_keys = store.part_puts()
    assert result == str(system_id)
    assert part_keys, "invalid-utf8 console must still seal parts (log located by system_id)"
    decoded = gzip.decompress(store.objects[part_keys[0]][0]).decode("utf-8")
    assert "�" in decoded, "the invalid byte must decode to the U+FFFD replacement char"


def test_terminal_system_seals_no_parts_after_teardown(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A console_rotate job that runs after teardown must seal nothing (teardown-race guard).

    Teardown reclaims the parts/sidecar and sets the System ``torn_down`` under the per-System
    advisory lock; a console_rotate job swept while the System was ``ready`` can still run after
    that. Without the live-state guard it would re-seal gen-0 parts from the still-present console
    log (the sidecar is gone, so it resumes from ZERO) and orphan them past teardown.
    """
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    async def _run() -> tuple[str | None, list[str], list[str]]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "torn_down")
            store = _FakeStore()
            result = await _run_handler(pool, store, _job(system_id, "boot-A"))
            rows = await _row_keys(pool, system_id)
        return result, rows, store.part_puts()

    result, rows, part_puts = asyncio.run(_run())

    assert result is None
    assert rows == [], "no console-part rows for a torn-down System"
    assert part_puts == [], "no part objects stored for a torn-down System"


# --- Run correlation (ADR-0279, #935) -------------------------------------------------


async def _seed_booted_run(pool: AsyncConnectionPool, system_id: UUID) -> UUID:
    """Insert an Investigation + a Run bound to ``system_id`` with a succeeded boot step."""
    run_id, investigation_id = uuid4(), uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'tester', 'local', 't', 'open')",
            (investigation_id,),
        )
        await conn.execute(
            "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
            "principal, project) "
            "VALUES (%s, %s, %s, 'local-libvirt', 'succeeded', '{}'::jsonb, 'tester', 'local')",
            (run_id, investigation_id, system_id),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state) VALUES (%s, 'boot', 'succeeded')",
            (run_id,),
        )
    return run_id


async def _part_run_ids(pool: AsyncConnectionPool, system_id: UUID) -> list[UUID | None]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT run_id FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s "
            "AND object_key LIKE '%%console-part-%%' ORDER BY object_key",
            (system_id,),
        )
        return [row[0] for row in await cur.fetchall()]


def test_parts_attributed_to_booted_run(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    async def _run() -> tuple[UUID, list[UUID | None], str | None]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            run_id = await _seed_booted_run(pool, system_id)
            result = await _run_handler(pool, _FakeStore(), _job(system_id, "boot-A"))
            return run_id, await _part_run_ids(pool, system_id), result

    run_id, part_run_ids, result = asyncio.run(_run())
    assert result == str(system_id)
    assert part_run_ids, "fixture console must seal at least one part"
    assert all(rid == run_id for rid in part_run_ids), part_run_ids


def test_parts_uncorrelated_when_no_boot_step(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    async def _run() -> tuple[list[UUID | None], str | None]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")  # no Run / no boot step
            result = await _run_handler(pool, _FakeStore(), _job(system_id, "boot-A"))
            return await _part_run_ids(pool, system_id), result

    part_run_ids, result = asyncio.run(_run())
    assert result == str(system_id), "rotation still succeeds with no resolvable Run"
    assert part_run_ids, "parts are still sealed"
    assert all(rid is None for rid in part_run_ids), part_run_ids


def test_resolver_failure_degrades_to_null_and_advances_sidecar(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    async def _boom(_conn: object, _system_id: object) -> UUID | None:
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(console_rotate, "latest_booted_run_id", _boom)

    async def _run() -> tuple[list[UUID | None], str | None, int]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            await _seed_booted_run(pool, system_id)
            store = _FakeStore()
            result = await _run_handler(pool, store, _job(system_id, "boot-A"))
            state = read_sidecar(cast(ObjectStore, store), "local", system_id)
            return await _part_run_ids(pool, system_id), result, state.plaintext_offset

    part_run_ids, result, offset = asyncio.run(_run())
    assert result == str(system_id), "a resolver failure must not fail the rotation job"
    assert part_run_ids and all(rid is None for rid in part_run_ids), part_run_ids
    assert offset == len(_CONSOLE), "the sidecar still advances so rotation does not stall"


# --- Lock span over the part PUTs (#1725, ADR-0519) ------------------------------------


def _advisory_locks_held_by(url: str, backend_pid: int) -> int:
    """Count advisory locks ``backend_pid`` holds, probed from a **second**, sync connection.

    Synchronous because it is called from inside ``put_artifact``, which the handler runs on a
    worker thread via ``asyncio.to_thread``; and from a second backend because probing on the
    connection under test would perturb the transaction being measured.
    """
    with psycopg.connect(url, autocommit=True) as probe, probe.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s",
            (backend_pid,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


async def _locks_visible_while_one_is_held(url: str) -> int:
    """Control for :func:`_advisory_locks_held_by`: what it reports while a lock IS held.

    Without this, an all-zeros assertion would pass just as happily if the probe could never
    see an advisory lock at all.
    """
    async with (
        await psycopg.AsyncConnection.connect(url) as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, uuid4()),
    ):
        return await asyncio.to_thread(_advisory_locks_held_by, url, conn.info.backend_pid)


class _LockProbingStore(_FakeStore):
    """A store that records the handler's own advisory-lock count at each part PUT."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url
        self.backend_pid: int | None = None
        self.locks_at_part_put: list[int] = []
        self.deleted: list[str] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        if "console-part-" in request.key():
            assert self.backend_pid is not None, "the test must publish the handler's backend pid"
            self.locks_at_part_put.append(_advisory_locks_held_by(self._url, self.backend_pid))
        return super().put_artifact(request)

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


class _TearingDownStore(_LockProbingStore):
    """Sets the System terminal from a second backend as the first part PUT lands."""

    def __init__(self, url: str, system_id: UUID) -> None:
        super().__init__(url)
        self._system_id = system_id
        self.torn_down = False

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        if "console-part-" in request.key() and not self.torn_down:
            self.torn_down = True
            with psycopg.connect(self._url, autocommit=True) as teardown:
                teardown.execute(
                    "UPDATE systems SET state = 'torn_down' WHERE id = %s", (self._system_id,)
                )
        return super().put_artifact(request)


async def _run_probing(pool: AsyncConnectionPool, store: _LockProbingStore, job: Job) -> str | None:
    """Drive the handler the way the worker dispatches it, exposing its backend pid to the store.

    ``set_autocommit(True)`` mirrors ``JobWorker._run_handler`` and is load-bearing, not
    incidental: on a pooled non-autocommit connection the handler's ``conn.transaction()`` blocks
    are savepoints inside one implicit transaction that the pool ends, so a
    ``pg_advisory_xact_lock`` would outlive every block regardless of where the PUTs sit
    (ADR-0506/ADR-0516). Only under the worker's dispatch does releasing the lock mean anything.
    """
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        store.backend_pid = conn.info.backend_pid
        try:
            return await console_rotate.console_rotate_handler(
                conn,
                job,
                secret_registry=SecretRegistry(),
                artifact_store=cast(ObjectStore, store),
            )
        finally:
            await conn.set_autocommit(False)


def test_part_puts_hold_no_advisory_lock(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every console-part PUT runs with the per-System lock released (#1725).

    A rotation seals an unbounded number of parts, and ``SYSTEM`` is the scope teardown, boot and
    revert also serialize on, so this is the widest of the three lock spans the issue covers. The
    control probe pins that the same query *does* report a lock when one is held.
    """
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    async def _run():
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _LockProbingStore(migrated_url)
            result = await _run_probing(pool, store, _job(system_id, "boot-A"))
            return (
                result,
                store,
                await _row_keys(pool, system_id),
                await _locks_visible_while_one_is_held(migrated_url),
            )

    result, store, rows, held = asyncio.run(_run())
    assert held >= 1  # the probe can see an advisory lock when one is held
    assert len(store.locks_at_part_put) > 1, "the fixture must seal several parts"
    assert set(store.locks_at_part_put) == {0}  # ...and none was held at any part PUT
    assert result == str(system_id)  # the rotation still completed
    assert len(rows) == len(store.locks_at_part_put)  # every PUT part got its row


def test_teardown_during_part_puts_discards_the_unregistered_objects(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teardown landing while the part objects are in flight registers none and deletes them.

    ``_reclaim_console_artifacts`` selects the keys to delete *from the artifacts rows*, so parts
    written after that sweep has run would survive it permanently. The sidecar must also stay
    where it was, so the next rotation re-derives these parts rather than skipping them.
    """
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)
    sidecar_key = f"local/systems/{system_id}/console-rotation-state.json"

    async def _run():
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _TearingDownStore(migrated_url, system_id)
            result = await _run_probing(pool, store, _job(system_id, "boot-A"))
            return result, store, await _row_keys(pool, system_id)

    result, store, rows = asyncio.run(_run())
    assert result is None
    assert store.torn_down  # the race actually fired
    assert len(store.deleted) > 1  # every part this attempt wrote was deleted
    assert store.deleted == store.part_puts()  # ...and exactly those, in the order written
    assert store.objects == {}  # nothing survived, sidecar included
    assert sidecar_key not in store.objects  # the cursor was not advanced past unsealed parts
    assert rows == []


def test_discard_failure_does_not_mask_the_teardown_outcome(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store fault on the compensating delete is swallowed: the rotation degrades to ``None``."""
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    class _UndeletableStore(_TearingDownStore):
        def delete(self, key: str) -> None:
            self.deleted.append(key)
            raise CategorizedError(
                "delete_object failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )

    async def _run():
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _UndeletableStore(migrated_url, system_id)
            result = await _run_probing(pool, store, _job(system_id, "boot-A"))
            return result, store, await _row_keys(pool, system_id)

    result, store, rows = asyncio.run(_run())
    assert store.deleted  # the compensating delete was attempted on every part
    assert result is None  # ...and its failure did not become the handler's result
    assert rows == []


def test_part_objects_are_byte_deterministic(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-derived part must compress to the same bytes, so its etag is a stable identity.

    A wall-clock stamp in the gzip header would give the same ``(gen, index)`` a different object
    and a different etag on every rotation — defeating the insert-if-absent identity these parts
    are keyed on, and manufacturing the etag drift ADR-0519's repair exists to correct.

    CPython's ``gzip.compress`` default for ``mtime`` is 0 from 3.13 on (it was the current time
    before), and this project pins ``requires-python = "==3.14.*"``, so today the handler would
    be deterministic even without pinning it. That is exactly why this assertion is on the object
    rather than on the call: it holds the property whoever supplies it, and it is the thing that
    notices if the call or the runtime default moves again.

    Asserted on the header's MTIME field rather than by compressing twice — two calls inside one
    wall-clock second agree even when the stamp is live, so a "compress twice" test would pass
    vacuously.
    """
    system_id = uuid4()
    log = _write_console(tmp_path, system_id, _CONSOLE)
    monkeypatch.setattr(console_rotate, "console_log_path", lambda _sid: log)

    async def _run() -> _FakeStore:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            await _seed_system(pool, system_id, "ready")
            store = _FakeStore()
            await _run_handler(pool, store, _job(system_id, "boot-A"))
            return store

    store = asyncio.run(_run())
    part_keys = store.part_puts()
    assert part_keys, "the fixture console must seal at least one part"
    for key in part_keys:
        data = store.objects[key][0]
        assert data[:2] == b"\x1f\x8b", f"{key} is not a gzip member"
        # Bytes 4..8 of a gzip member are its MTIME field, little-endian (RFC 1952 §2.3.1).
        assert struct.unpack("<I", data[4:8])[0] == 0, f"{key} carries a wall-clock gzip stamp"
