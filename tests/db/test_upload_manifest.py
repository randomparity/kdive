"""Tests for owner-scoped upload-manifest storage (ADR-0048 §4)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from kdive.artifacts.uploads.upload_manifest import (
    UploadManifestReplaceRequest,
    delete_manifest,
    get_manifest,
    get_manifest_sync,
    refresh_deadline,
    replace_manifest,
)
from kdive.artifacts.uploads.uploads import ChunkEntry, ManifestEntry

_HOUR = timedelta(hours=1)
#: Far wider than any TTL a test refreshes with, so the cap cannot bind and the test is measuring
#: the extension itself.
_UNBOUNDED = timedelta(days=365)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def _request(
    owner_id: UUID,
    entries: list[ManifestEntry],
    *,
    prefix: str | None = None,
    ttl: timedelta = timedelta(hours=1),
) -> UploadManifestReplaceRequest:
    return UploadManifestReplaceRequest(
        owner_kind="runs",
        owner_id=owner_id,
        prefix=prefix or f"local/runs/{owner_id}/",
        entries=entries,
        ttl=ttl,
    )


def test_round_trip(migrated_url: str) -> None:
    """replace_manifest then get_manifest returns the entries, prefix, and a deadline."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10), ManifestEntry("vmlinux", "YmFy", 20)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert got.entries == tuple(entries)
        assert got.prefix == f"local/runs/{owner_id}/"
        assert got.deadline is not None

    asyncio.run(_run_test())


def test_round_trips_chunks(migrated_url: str) -> None:
    """A chunked entry persists and reloads its ordered chunk list through the JSONB column."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [
            ManifestEntry(
                "vmlinux",
                "whole",
                10,
                chunks=(ChunkEntry("c0", 6), ChunkEntry("c1", 4)),
            ),
            ManifestEntry("kernel", "Zm9v", 3),
        ]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        by_name = {e.name: e for e in got.entries}
        assert by_name["vmlinux"].chunks == (ChunkEntry("c0", 6), ChunkEntry("c1", 4))
        assert by_name["kernel"].chunks is None

    asyncio.run(_run_test())


def test_round_trips_encoding(migrated_url: str) -> None:
    """An encoded entry persists encoding + uncompressed_size; a plain entry stays identity."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [
            ManifestEntry("rootfs", "whole", 4096, encoding="gzip", uncompressed_size=6 * 1024**3),
            ManifestEntry("kernel", "Zm9v", 3),
        ]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        by_name = {e.name: e for e in got.entries}
        assert by_name["rootfs"].encoding == "gzip"
        assert by_name["rootfs"].uncompressed_size == 6 * 1024**3
        # A plain (no-encoding) entry deserializes as identity.
        assert by_name["kernel"].encoding is None
        assert by_name["kernel"].uncompressed_size is None

    asyncio.run(_run_test())


def test_preexisting_payload_without_encoding_defaults_to_identity() -> None:
    """A manifest payload written before ADR-0437 (no encoding keys) deserializes as identity."""
    from kdive.artifacts.uploads.upload_manifest import _entry_from_payload

    entry = _entry_from_payload({"name": "rootfs", "sha256": "a", "size_bytes": 10})
    assert entry.encoding is None
    assert entry.uncompressed_size is None


def test_get_manifest_sync_round_trips(migrated_url: str) -> None:
    """The sync manifest read returns the same entries an async replace persisted (ADR-0438)."""

    async def _seed(owner_id: UUID) -> None:
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn,
                UploadManifestReplaceRequest(
                    owner_kind="investigations",
                    owner_id=owner_id,
                    prefix=f"local/investigations/{owner_id}/",
                    entries=[
                        ManifestEntry(
                            "rootfs", "whole", 4096, encoding="gzip", uncompressed_size=6 * 1024**3
                        )
                    ],
                    ttl=timedelta(hours=1),
                ),
            )

    owner_id = uuid4()
    asyncio.run(_seed(owner_id))
    with psycopg.connect(migrated_url) as conn:
        got = get_manifest_sync(conn, "investigations", owner_id)
        absent = get_manifest_sync(conn, "investigations", uuid4())
    assert got is not None
    assert got.entries[0].encoding == "gzip"
    assert got.entries[0].uncompressed_size == 6 * 1024**3
    assert absent is None


def test_full_set_replacement(migrated_url: str) -> None:
    """A second replace_manifest with fewer entries replaces, not merges, the prior set."""

    async def _run_test() -> None:
        owner_id = uuid4()
        first_entries = [
            ManifestEntry("kernel", "Zm9v", 10),
            ManifestEntry("vmlinux", "YmFy", 20),
        ]
        second_entries = [ManifestEntry("kernel", "bmV3", 30)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, first_entries))
            await replace_manifest(
                conn, _request(owner_id, second_entries, prefix=f"local/runs/{owner_id}/v2/")
            )
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert got.entries == tuple(second_entries)
        assert got.prefix == f"local/runs/{owner_id}/v2/"

    asyncio.run(_run_test())


def test_remint_updates_deadline(migrated_url: str) -> None:
    """A re-mint with a longer ttl moves the deadline forward (proves EXCLUDED.deadline)."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)]))
            got1 = await get_manifest(conn, "runs", owner_id)
            assert got1 is not None
            first_deadline = got1.deadline
            await replace_manifest(
                conn,
                _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=timedelta(hours=5)),
            )
            got2 = await get_manifest(conn, "runs", owner_id)
        assert got2 is not None
        assert got2.deadline > first_deadline

    asyncio.run(_run_test())


def test_replace_returns_stamp_matching_persisted_deadline(migrated_url: str) -> None:
    """replace_manifest returns a (server_time, deadline) stamp read from the same
    transaction, so deadline - server_time == ttl exactly and deadline equals the value
    a later get_manifest reads (the reaper's contract). Both are timezone-aware (#1336)."""

    async def _run_test() -> None:
        owner_id = uuid4()
        ttl = timedelta(hours=1)
        async with await _connect(migrated_url) as conn:
            stamp = await replace_manifest(
                conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=ttl)
            )
            got = await get_manifest(conn, "runs", owner_id)
        assert got is not None
        assert stamp.server_time.tzinfo is not None
        assert stamp.deadline.tzinfo is not None
        assert stamp.deadline - stamp.server_time == ttl
        assert stamp.deadline == got.deadline

    asyncio.run(_run_test())


async def _window_row(conn: psycopg.AsyncConnection, owner_id: UUID) -> tuple[datetime, datetime]:
    """Return the owner's ``(window_started_at, deadline)`` as Postgres holds them."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT window_started_at, deadline FROM upload_manifests "
            "WHERE owner_kind = 'runs' AND owner_id = %s",
            (owner_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def _age_window(conn: psycopg.AsyncConnection, owner_id: UUID, mint_age: timedelta) -> None:
    """Backdate the mint by ``mint_age`` and leave the window open for one more minute.

    Simulates a window that has already absorbed extensions without waiting for wall-clock time.
    The deadline is restamped too, so every assertion below is against values this statement's
    ``now()`` fixed — no test depends on how long the previous statement took.
    """
    await conn.execute(
        "UPDATE upload_manifests SET window_started_at = now() - %s, "
        "    deadline = now() + interval '1 minute' "
        "WHERE owner_kind = 'runs' AND owner_id = %s",
        (mint_age, owner_id),
    )


def test_refresh_extends_an_open_window(migrated_url: str) -> None:
    """A refresh with budget to spare moves the deadline to a full ttl out and reports uncapped."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=_HOUR)
            )
            _, before = await _window_row(conn, owner_id)
            refreshed = await refresh_deadline(
                conn, "runs", owner_id, timedelta(hours=2), max_window=_UNBOUNDED
            )
            _, persisted = await _window_row(conn, owner_id)
        assert refreshed is not None
        assert refreshed.capped is False
        assert refreshed.deadline > before
        assert refreshed.deadline == persisted

    asyncio.run(_run_test())


def test_refresh_declines_an_absent_manifest(migrated_url: str) -> None:
    """No row means no window to extend — the caller must not read that as a spent budget."""

    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            refreshed = await refresh_deadline(conn, "runs", uuid4(), _HOUR, max_window=_UNBOUNDED)
        assert refreshed is None

    asyncio.run(_run_test())


def test_refresh_declines_an_already_expired_window(migrated_url: str) -> None:
    """A lapsed deadline is not resurrected: the ``deadline >= now()`` arm matches nothing."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn,
                _request(
                    owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=timedelta(seconds=-30)
                ),
            )
            refreshed = await refresh_deadline(conn, "runs", owner_id, _HOUR, max_window=_UNBOUNDED)
        assert refreshed is None

    asyncio.run(_run_test())


def test_refresh_cannot_extend_past_the_cap(migrated_url: str) -> None:
    """The regression #1553 filed: repeated refreshes cannot roll one window forward forever.

    The window's mint is 90 minutes old and the cap is two hours, so a one-hour extension has only
    30 minutes of budget left. The refresh grants exactly that — the cap, to the microsecond — and
    every further refresh grants nothing at all.

    Without the ``LEAST(..., window_started_at + max_window)`` clamp each call would stamp
    ``now() + ttl`` afresh, and both assertions below fail.
    """

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=_HOUR)
            )
            await _age_window(conn, owner_id, timedelta(minutes=90))
            mint, _ = await _window_row(conn, owner_id)

            first = await refresh_deadline(
                conn, "runs", owner_id, _HOUR, max_window=timedelta(hours=2)
            )
            assert first is not None
            assert first.deadline == mint + timedelta(hours=2)
            assert first.capped is True

            for _ in range(4):
                again = await refresh_deadline(
                    conn, "runs", owner_id, _HOUR, max_window=timedelta(hours=2)
                )
                assert again is not None
                assert again.deadline == first.deadline
                assert again.capped is True

    asyncio.run(_run_test())


def test_refresh_never_shortens_an_open_window(migrated_url: str) -> None:
    """A spent budget leaves the deadline alone rather than pulling it backward.

    A refresh that moved the deadline into the past would hand the upload reaper the chunk objects
    the refresh exists to protect — a retention bound turned into data loss. ``GREATEST`` forbids
    it, and the caller still gets a deadline (not ``None``), so an exhausted budget is never
    mistaken for a reaped window.
    """

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=_HOUR)
            )
            await _age_window(conn, owner_id, timedelta(hours=10))
            _, before = await _window_row(conn, owner_id)
            refreshed = await refresh_deadline(
                conn, "runs", owner_id, _HOUR, max_window=timedelta(hours=2)
            )
            _, after = await _window_row(conn, owner_id)
        assert refreshed is not None
        assert refreshed.deadline == before
        assert after == before
        assert refreshed.capped is True

    asyncio.run(_run_test())


def test_refresh_reports_capped_when_the_standing_deadline_outruns_the_ttl(
    migrated_url: str,
) -> None:
    """A TTL lowered after the mint leaves every refresh granting nothing, and it says so (#1724).

    The window was minted at the old, larger ``KDIVE_UPLOAD_TTL_SECONDS``, so its standing deadline
    is already further out than the ``now() + ttl`` a refresh under the new, smaller one computes.
    ``GREATEST`` keeps the standing deadline — correctly, since shortening it would hand the reaper
    an in-flight reassembly's chunk objects — but the refresh granted the caller nothing, which is
    a capped outcome and not the uncapped one the old ``deadline < now() + ttl`` predicate reported.
    """

    async def _run_test() -> None:
        owner_id = uuid4()
        # An hour at the mint, a minute at the refresh: the operator lowered the knob in between.
        shrunk = timedelta(minutes=1)
        async with await _connect(migrated_url) as conn:
            await replace_manifest(
                conn, _request(owner_id, [ManifestEntry("kernel", "Zm9v", 10)], ttl=_HOUR)
            )
            _, before = await _window_row(conn, owner_id)
            refreshed = await refresh_deadline(
                conn, "runs", owner_id, shrunk, max_window=shrunk * 3
            )
            _, after = await _window_row(conn, owner_id)
        assert refreshed is not None
        assert refreshed.deadline == before
        assert after == before
        assert refreshed.capped is True

    asyncio.run(_run_test())


def test_refresh_reports_capped_on_a_backfilled_row_minted_under_a_larger_ttl(
    migrated_url: str,
) -> None:
    """The same outcome for a row migration ``0085`` backfilled (#1724, ADR-0511 §2).

    ``window_started_at DEFAULT now()`` gives the backfilled row a fresh budget, but its deadline
    was stamped by a mint that predates the column, under whatever TTL was live then. Six hours out
    against a one-hour refresh, the clamped grant is never the greater of the two and the deadline
    stands untouched — a capped refresh, reported as one.
    """

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await conn.execute(
                "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
                "VALUES ('runs', %s, %s, %s, now() + interval '6 hours')",
                (
                    owner_id,
                    f"local/runs/{owner_id}/",
                    Jsonb([{"name": "kernel", "sha256": "Zm9v", "size_bytes": 10}]),
                ),
            )
            _, before = await _window_row(conn, owner_id)
            refreshed = await refresh_deadline(
                conn, "runs", owner_id, _HOUR, max_window=timedelta(hours=3)
            )
            _, after = await _window_row(conn, owner_id)
        assert refreshed is not None
        assert refreshed.deadline == before
        assert after == before
        assert refreshed.capped is True

    asyncio.run(_run_test())


def test_a_remint_restarts_the_extension_budget(migrated_url: str) -> None:
    """The cap bounds extension, not re-minting (ADR-0511, ADR-0448 §4).

    ``artifacts.create_run_upload`` is the recovery every "your window is gone" rejection points
    at, and it must keep granting a full fresh window on demand. A cap measured from the row's
    ``created_at`` — which the upsert deliberately does not touch — would have bound it too; this
    is the trap the separate ``window_started_at`` column exists to avoid.
    """

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10)]
        cap = timedelta(hours=2)
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries, ttl=_HOUR))
            await _age_window(conn, owner_id, timedelta(hours=10))
            spent = await refresh_deadline(conn, "runs", owner_id, _HOUR, max_window=cap)
            assert spent is not None and spent.capped is True

            stamp = await replace_manifest(conn, _request(owner_id, entries, ttl=_HOUR))
            mint, deadline = await _window_row(conn, owner_id)
            refreshed = await refresh_deadline(conn, "runs", owner_id, _HOUR, max_window=cap)

        # The re-mint itself is unbounded by the cap: a full ttl, measured from the same
        # statement's clock, on a window whose predecessor's budget was exhausted.
        assert stamp.deadline - stamp.server_time == _HOUR
        assert deadline > spent.deadline
        assert mint == stamp.server_time
        # And the fresh window has a fresh budget, so the next extension is granted in full.
        assert refreshed is not None
        assert refreshed.capped is False
        assert refreshed.deadline > deadline

    asyncio.run(_run_test())


def test_refresh_works_on_a_row_that_predates_the_mint_column(migrated_url: str) -> None:
    """A row inserted without ``window_started_at`` is still refreshable (migration 0085).

    The column's ``DEFAULT now()`` backfills in-flight windows at migrate time with a fresh budget
    rather than a retroactively exhausted one, so an upgrade cannot clamp a live reassembly's
    window on its first refresh.
    """

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await conn.execute(
                "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
                "VALUES ('runs', %s, %s, %s, now() + interval '1 hour')",
                (
                    owner_id,
                    f"local/runs/{owner_id}/",
                    Jsonb([{"name": "kernel", "sha256": "Zm9v", "size_bytes": 10}]),
                ),
            )
            _, before = await _window_row(conn, owner_id)
            refreshed = await refresh_deadline(
                conn, "runs", owner_id, timedelta(hours=2), max_window=timedelta(hours=6)
            )
        assert refreshed is not None
        assert refreshed.capped is False
        assert refreshed.deadline > before

    asyncio.run(_run_test())


def test_absent_returns_none(migrated_url: str) -> None:
    """get_manifest returns None when no manifest exists for the owner."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())


def test_delete_removes_row(migrated_url: str) -> None:
    """delete_manifest removes the row; subsequent get_manifest returns None."""

    async def _run_test() -> None:
        owner_id = uuid4()
        entries = [ManifestEntry("kernel", "Zm9v", 10)]
        async with await _connect(migrated_url) as conn:
            await replace_manifest(conn, _request(owner_id, entries))
            await delete_manifest(conn, "runs", owner_id)
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())


def test_delete_is_idempotent(migrated_url: str) -> None:
    """delete_manifest on an absent owner does not raise; get_manifest stays None."""

    async def _run_test() -> None:
        owner_id = uuid4()
        async with await _connect(migrated_url) as conn:
            await delete_manifest(conn, "runs", owner_id)
            got = await get_manifest(conn, "runs", owner_id)
        assert got is None

    asyncio.run(_run_test())
