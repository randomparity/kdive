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
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    FetchedArtifact,
    HeadResult,
    StoredArtifact,
)
from kdive.domain.capacity.state import JobState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.console import console_rotate
from kdive.providers.console_parts.rotation import RotationState, rotate
from kdive.providers.console_parts.sidecar import read_sidecar
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore

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
        return StoredArtifact(key, etag, request.sensitivity, request.retention_class)

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
            checksum_sha256=None,
            etag="etag",
            sensitivity=sensitivity,
            content_encoding=enc,
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
