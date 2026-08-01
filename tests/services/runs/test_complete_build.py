"""Service-level tests for external-build finalization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, LiteralString, NoReturn

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import HeadResult, chunk_key
from kdive.artifacts.uploads import ChunkEntry, ManifestEntry
from kdive.build_artifacts.results import BuildOutput, ValidatedUpload
from kdive.config.core_settings import UPLOAD_WINDOW_MAX_TTL_MULTIPLE
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.audit import args_digest
from kdive.services.runs import complete_build
from kdive.services.runs.complete_build import (
    CompleteBuildConfigurationError,
    CompleteBuildExpiredWindowError,
    CompleteBuildFinalizer,
)
from kdive.services.runs.steps import BuildStepResult
from tests.clock import STORE_MTIME
from tests.mcp.complete_build_support import (
    FakeValidator,
    seed_external_run,
    seed_external_run_with_manifest,
)
from tests.mcp.complete_build_support import ctx as _ctx
from tests.mcp.complete_build_support import pool as _pool

_CHUNKED_KERNEL = ManifestEntry(
    "kernel", "whole", 8, chunks=(ChunkEntry("c0", 5), ChunkEntry("c1", 3))
)


class _ChunkedStore:
    def __init__(self, *, bad_head: bool = False, delete_raises: str | None = None) -> None:
        self.bad_head = bad_head
        self.delete_raises = delete_raises
        self.events: list[tuple[str, object]] = []
        self.deleted_versions: list[tuple[str, str]] = []

    def head(self, key: str) -> HeadResult | None:
        if key.endswith(".part0001"):
            checksum = "wrong" if self.bad_head else "c0"
            return HeadResult(
                5, checksum, "e1", last_modified=STORE_MTIME, version_id="test-version"
            )
        if key.endswith(".part0002"):
            return HeadResult(3, "c1", "e2", last_modified=STORE_MTIME, version_id="test-version")
        return HeadResult(8, None, "final", last_modified=STORE_MTIME, version_id="test-version")

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        del key
        return (b"x" * 8)[start : start + length]

    def delete_version(self, key: str, version_id: str) -> None:
        if self.delete_raises is not None and key.endswith(self.delete_raises):
            raise CategorizedError("delete failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        self.events.append(("delete_version", key))
        self.deleted_versions.append((key, version_id))

    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str:
        del sensitivity, retention_class
        self.events.append(("create", key))
        return "upload"

    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str:
        del key, upload_id
        self.events.append(("copy", source_key))
        return f"etag-{part_number}"

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> str:
        del upload_id
        self.events.append(("complete", (key, tuple(parts))))
        return "final"

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        del upload_id
        self.events.append(("abort", key))


class _PeerPutChunkedStore(_ChunkedStore):
    """Writes replacement chunk versions immediately before exact cleanup runs."""

    def __init__(self) -> None:
        super().__init__()
        self.current_versions: dict[str, str] = {}

    def delete_version(self, key: str, version_id: str) -> None:
        self.current_versions[key] = "peer-version-2"
        super().delete_version(key, version_id)


class _VersionDeleteStore:
    """Records exact post-commit convergence deletes for single-PUT candidates."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.deleted: list[tuple[str, str]] = []

    def delete_version(self, key: str, version_id: str) -> None:
        self.deleted.append((key, version_id))
        if self.fail:
            raise CategorizedError("delete failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    def head(self, key: str) -> HeadResult | None:
        del key
        raise AssertionError("single-PUT cleanup never reads objects")

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        del key, start, length
        raise AssertionError("single-PUT cleanup never reads objects")

    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str:
        del key, sensitivity, retention_class
        raise AssertionError("single-PUT cleanup never reassembles")

    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str:
        del key, upload_id, part_number, source_key
        raise AssertionError("single-PUT cleanup never reassembles")

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> str:
        del key, upload_id, parts
        raise AssertionError("single-PUT cleanup never reassembles")

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        del key, upload_id
        raise AssertionError("single-PUT cleanup never reassembles")


async def _run_by_id(pool: AsyncConnectionPool, run_id: Any):
    async with pool.connection() as conn:
        run = await RUNS.get(conn, run_id)
    assert run is not None
    return run


async def _manifest_present(pool: AsyncConnectionPool, run_id: Any) -> bool:
    async with pool.connection() as conn:
        return await upload_manifest.get_manifest(conn, "runs", run_id) is not None


async def _fetchall(pool: AsyncConnectionPool, query: LiteralString, params: tuple) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def _fetchone(pool: AsyncConnectionPool, query: LiteralString, params: tuple) -> tuple:
    rows = await _fetchall(pool, query, params)
    assert len(rows) == 1
    return rows[0]


async def _record_build_step(
    pool: AsyncConnectionPool, run_id: Any, result: BuildStepResult
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s)",
            (run_id, Jsonb(result.dump())),
        )


async def _complete(
    pool: AsyncConnectionPool,
    run_id: Any,
    finalizer: CompleteBuildFinalizer,
) -> BuildStepResult:
    run = await _run_by_id(pool, run_id)
    async with pool.connection() as conn:
        return await finalizer.complete(conn, _ctx(), run, build_id=None, cmdline="console=ttyS0")


async def _complete_config_error(
    pool: AsyncConnectionPool,
    run_id: Any,
    finalizer: CompleteBuildFinalizer,
) -> CompleteBuildConfigurationError:
    run = await _run_by_id(pool, run_id)
    async with pool.connection() as conn:
        try:
            await finalizer.complete(conn, _ctx(), run, build_id=None, cmdline="console=ttyS0")
        except CompleteBuildConfigurationError as exc:
            return exc
    raise AssertionError("complete_build did not raise CompleteBuildConfigurationError")


async def _complete_expired_window_error(
    pool: AsyncConnectionPool,
    run_id: Any,
    finalizer: CompleteBuildFinalizer,
) -> CompleteBuildExpiredWindowError:
    run = await _run_by_id(pool, run_id)
    async with pool.connection() as conn:
        try:
            await finalizer.complete(conn, _ctx(), run, build_id=None, cmdline="console=ttyS0")
        except CompleteBuildExpiredWindowError as exc:
            return exc
    raise AssertionError("complete_build did not raise CompleteBuildExpiredWindowError")


def _output(run_id: Any) -> BuildOutput:
    return BuildOutput(f"local/runs/{run_id}/kernel", "", "build-id")


def test_complete_build_finalizer_rejects_missing_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run(pool)
            error = await _complete_config_error(
                pool,
                run_id,
                CompleteBuildFinalizer(validate_complete_build=FakeValidator(_output(run_id))),
            )

        assert error.data == {"reason": "no_upload_manifest"}

    asyncio.run(_run())


def test_complete_build_finalizer_rejects_expired_chunk_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(
                pool, entries=[_CHUNKED_KERNEL], ttl=timedelta(seconds=-1)
            )
            store = _ChunkedStore()
            finalizer = CompleteBuildFinalizer(
                validate_complete_build=FakeValidator(_output(run_id)),
                object_store_factory=lambda: store,
            )
            error = await _complete_expired_window_error(pool, run_id, finalizer)

        assert error.stamp.deadline < error.stamp.server_time
        assert store.events == []

    asyncio.run(_run())


def test_complete_build_finalizer_rejects_expired_single_put_manifest(migrated_url: str) -> None:
    """A non-chunked finalize past the deadline is rejected, not silently committed (#1534)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(seconds=-1))
            validator = FakeValidator(_output(run_id))
            error = await _complete_expired_window_error(
                pool, run_id, CompleteBuildFinalizer(validate_complete_build=validator)
            )
            run = await _run_by_id(pool, run_id)
            manifest_kept = await _manifest_present(pool, run_id)
            artifact_rows = await _fetchall(
                pool, "SELECT id FROM artifacts WHERE owner_id = %s", (run_id,)
            )

        assert error.stamp.deadline < error.stamp.server_time
        assert validator.calls == 0  # rejected before the payload is read
        assert run.state is RunState.CREATED
        assert manifest_kept  # left for the reaper, exactly as the chunked path leaves it
        assert artifact_rows == []

    asyncio.run(_run())


def test_complete_build_finalizer_declines_when_reaper_wins_mid_validation(
    migrated_url: str,
) -> None:
    """A window reaped while this finalize validated must not commit rows for deleted keys.

    The validator seam stands in for the unlocked stretch: the reaper takes the same RUN lock,
    deletes the uncommitted objects, and drops the manifest while the payload is being read.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool)

            def reap_then_validate(*args: Any, **kwargs: Any) -> ValidatedUpload:
                with psycopg.connect(migrated_url) as reaper:
                    reaper.execute(
                        "DELETE FROM upload_manifests WHERE owner_kind = 'runs' AND owner_id = %s",
                        (run_id,),
                    )
                return FakeValidator(_output(run_id))(*args, **kwargs)

            error = await _complete_config_error(
                pool,
                run_id,
                CompleteBuildFinalizer(validate_complete_build=reap_then_validate),
            )
            run = await _run_by_id(pool, run_id)
            artifact_rows = await _fetchall(
                pool, "SELECT id FROM artifacts WHERE owner_id = %s", (run_id,)
            )
            steps = await _fetchall(pool, "SELECT step FROM run_steps WHERE run_id = %s", (run_id,))

        assert error.data == {"reason": "no_upload_manifest"}
        assert run.state is RunState.CREATED
        assert artifact_rows == []
        assert steps == []

    asyncio.run(_run())


def test_complete_build_finalizer_declines_when_the_window_is_reminted_mid_validation(
    migrated_url: str,
) -> None:
    """Presence is not identity: a reap plus a re-mint must not commit the validated window.

    The object keys are owner-addressed and identical across re-mints, so nothing downstream
    would notice the swap — the commit would register rows carrying the deleted objects' etags.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool)

            def reap_and_remint_then_validate(*args: Any, **kwargs: Any) -> ValidatedUpload:
                with psycopg.connect(migrated_url, autocommit=True) as other:
                    other.execute(
                        "DELETE FROM upload_manifests WHERE owner_kind = 'runs' AND owner_id = %s",
                        (run_id,),
                    )
                    other.execute(
                        "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, "
                        "deadline) VALUES ('runs', %s, %s, %s, now() + interval '1 hour')",
                        (
                            run_id,
                            f"local/runs/{run_id}/",
                            Jsonb([{"name": "kernel", "sha256": "c", "size_bytes": 1}]),
                        ),
                    )
                return FakeValidator(_output(run_id))(*args, **kwargs)

            error = await _complete_config_error(
                pool,
                run_id,
                CompleteBuildFinalizer(validate_complete_build=reap_and_remint_then_validate),
            )
            run = await _run_by_id(pool, run_id)
            artifact_rows = await _fetchall(
                pool, "SELECT id FROM artifacts WHERE owner_id = %s", (run_id,)
            )
            remint_kept = await _manifest_present(pool, run_id)

        assert error.data == {"reason": "upload_window_replaced"}
        assert run.state is RunState.CREATED
        assert artifact_rows == []
        assert remint_kept  # the agent's fresh window is not deleted out from under it

    asyncio.run(_run())


def test_complete_build_finalizer_declines_chunked_reassembly_of_a_reaped_window(
    migrated_url: str,
) -> None:
    """A refresh that finds no row means the reaper collected it; do not reassemble (ADR-0448).

    The store factory runs between the open-window check and the refresh, which is exactly the
    gap the reaper can land in.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ChunkedStore()

            def reap_then_build_store() -> _ChunkedStore:
                with psycopg.connect(migrated_url) as reaper:
                    reaper.execute(
                        "DELETE FROM upload_manifests WHERE owner_kind = 'runs' AND owner_id = %s",
                        (run_id,),
                    )
                return store

            error = await _complete_config_error(
                pool,
                run_id,
                CompleteBuildFinalizer(
                    validate_complete_build=FakeValidator(_output(run_id)),
                    object_store_factory=reap_then_build_store,
                ),
            )
            run = await _run_by_id(pool, run_id)

        assert error.data == {"reason": "no_upload_manifest"}
        assert store.events == []  # no multipart copy against reaped chunk objects
        assert run.state is RunState.CREATED

    asyncio.run(_run())


def test_complete_build_finalizer_recovers_on_remint_without_reupload(migrated_url: str) -> None:
    """The advertised recovery: reject, re-mint, finalize — same keys, nothing re-uploaded."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(seconds=-1))
            finalizer = CompleteBuildFinalizer(
                validate_complete_build=FakeValidator(_output(run_id))
            )
            await _complete_expired_window_error(pool, run_id, finalizer)

            async with pool.connection() as conn:
                await upload_manifest.replace_manifest(
                    conn,
                    upload_manifest.UploadManifestReplaceRequest(
                        owner_kind="runs",
                        owner_id=run_id,
                        prefix=f"local/runs/{run_id}/",
                        entries=[ManifestEntry("kernel", "c", 1)],
                        ttl=timedelta(hours=1),
                    ),
                )
            result = await _complete(pool, run_id, finalizer)
            keys = await _fetchall(
                pool,
                "SELECT object_key FROM artifacts WHERE owner_kind = 'investigations' "
                "AND owner_id = (SELECT investigation_id FROM runs WHERE id = %s)",
                (run_id,),
            )
            run = await _run_by_id(pool, run_id)

        assert run.state is RunState.SUCCEEDED
        # The re-mint addressed the very key the lapsed window used: no re-upload was needed.
        assert keys == [(f"local/runs/{run_id}/kernel",)]
        assert result.kernel_ref == f"local/runs/{run_id}/kernel"

    asyncio.run(_run())


def test_complete_build_finalizer_accepts_single_put_inside_window(migrated_url: str) -> None:
    """The deadline is a wall, not a floor: a still-open window finalizes (#1534)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(seconds=30))
            result = await _complete(
                pool,
                run_id,
                CompleteBuildFinalizer(validate_complete_build=FakeValidator(_output(run_id))),
            )
            run = await _run_by_id(pool, run_id)

        assert result.kernel_ref == f"local/runs/{run_id}/kernel"
        assert run.state is RunState.SUCCEEDED

    asyncio.run(_run())


def test_complete_build_finalizer_returns_recorded_success_after_reassembly_failure(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            recorded = BuildStepResult(
                kernel_ref="recorded/kernel",
                debuginfo_ref=None,
                build_id="recorded-build",
            )
            await _record_build_step(pool, run_id, recorded)

            def unexpected_validator(*args: object, **kwargs: object) -> NoReturn:
                del args, kwargs
                raise AssertionError("recorded success must bypass validation")

            result = await _complete(
                pool,
                run_id,
                CompleteBuildFinalizer(
                    validate_complete_build=unexpected_validator,
                    object_store_factory=lambda: _ChunkedStore(bad_head=True),
                ),
            )

        assert result == recorded

    asyncio.run(_run())


async def _complete_swallowing_failure(
    pool: AsyncConnectionPool,
    run_id: Any,
    finalizer: CompleteBuildFinalizer,
) -> None:
    """Finalize, swallowing the failure *inside* the connection block as the MCP handler does.

    `mcp/tools/lifecycle/runs/complete_build.py` catches every finalize failure and returns a
    `ToolResponse`, so its `async with pool.connection()` exits cleanly and psycopg **commits** —
    including the deadline extension `_reassemble_chunked_artifacts` already landed in its own
    savepoint before reassembly ran. The other helpers here re-raise out of the block, which rolls
    that extension back; reproducing #1553 requires the production shape, not theirs.
    """
    run = await _run_by_id(pool, run_id)
    async with pool.connection() as conn:
        try:
            await finalizer.complete(conn, _ctx(), run, build_id=None, cmdline="console=ttyS0")
        except CategorizedError:
            return
    raise AssertionError("the chunked finalize was expected to fail in reassembly")


async def _window_deadline(pool: AsyncConnectionPool, run_id: Any) -> Any:
    row = await _fetchone(
        pool,
        "SELECT deadline FROM upload_manifests WHERE owner_kind = 'runs' AND owner_id = %s",
        (run_id,),
    )
    return row[0]


def test_repeated_failing_finalizes_cannot_extend_the_window_past_the_cap(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The #1553 regression, end to end through the finalize service (ADR-0511).

    A chunked finalize refreshes the window before reassembly and commits that refresh in its own
    savepoint; the failure that follows is swallowed at the MCP layer, so the extension survives.
    The first failing attempt therefore still buys a full TTL — that is the behavior ADR-0448 §4
    kept, and this asserts it is unchanged. What is new is the second attempt: once the window has
    reached `KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE` TTLs from its mint, a failing retry buys
    nothing, and the retry loop can no longer hold uncommitted objects indefinitely.

    Remove the `LEAST(..., window_started_at + max_window)` clamp from `refresh_deadline` and the
    second attempt stamps `now() + ttl` again — `after_second == after_first` fails.
    """
    monkeypatch.setenv("KDIVE_UPLOAD_TTL_SECONDS", "3600")
    monkeypatch.setenv("KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE", "2")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            finalizer = CompleteBuildFinalizer(
                validate_complete_build=FakeValidator(_output(run_id)),
                object_store_factory=lambda: _ChunkedStore(bad_head=True),
            )
            minted = await _window_deadline(pool, run_id)

            await _complete_swallowing_failure(pool, run_id, finalizer)
            after_first = await _window_deadline(pool, run_id)

            # Spend the budget without waiting for it: backdate the mint past the cap, leaving
            # the deadline (and so the window) untouched and open.
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE upload_manifests SET window_started_at = now() - interval '10 hours' "
                    "WHERE owner_kind = 'runs' AND owner_id = %s",
                    (run_id,),
                )

            with caplog.at_level(logging.WARNING, logger=complete_build.__name__):
                await _complete_swallowing_failure(pool, run_id, finalizer)
            after_second = await _window_deadline(pool, run_id)

            run = await _run_by_id(pool, run_id)

        assert after_first > minted  # a failing finalize still commits its first extension
        assert after_second == after_first  # and the cap stops every later one
        assert "upload window extension capped" in caplog.text
        assert run.state is RunState.CREATED

    asyncio.run(_run())


def test_a_ttl_lowered_after_the_mint_reports_a_capped_refresh(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refresh that grants nothing tells the operator so, whatever spent the budget (#1724).

    The window is minted at an hour and the operator then lowers `KDIVE_UPLOAD_TTL_SECONDS` to a
    minute — the case ADR-0511 §2 records, and the shape a row migration `0085` backfilled arrives
    in. The standing deadline already outruns the `now() + ttl` this refresh computes, so `GREATEST`
    keeps it and the finalize buys nothing. The old predicate compared the surviving deadline
    against `now() + ttl`, found it larger, and called that uncapped: a retry loop was told its
    window was still growing while every extension was a no-op.
    """
    monkeypatch.setenv("KDIVE_UPLOAD_TTL_SECONDS", "3600")
    monkeypatch.setenv("KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE", "3")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            minted = await _window_deadline(pool, run_id)

            monkeypatch.setenv("KDIVE_UPLOAD_TTL_SECONDS", "60")
            with caplog.at_level(logging.WARNING, logger=complete_build.__name__):
                await _complete_swallowing_failure(
                    pool,
                    run_id,
                    CompleteBuildFinalizer(
                        validate_complete_build=FakeValidator(_output(run_id)),
                        object_store_factory=lambda: _ChunkedStore(bad_head=True),
                    ),
                )
            after = await _window_deadline(pool, run_id)

        assert after == minted  # the refresh granted nothing: no shortening, no extension
        assert "upload window extension capped" in caplog.text

    asyncio.run(_run())


def test_the_cap_multiple_is_read_from_configuration(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multiple of 1 makes the mint's own deadline the whole budget (ADR-0511 decision 3).

    This is what pins the knob to the behavior. A fresh window's deadline is already
    `window_started_at + ttl`, so at a multiple of 1 the clamp lands on the value already there and
    the very first refresh is a no-op — an operator choice that forbids extension outright. Ignore
    the setting and fall back to the built-in 3 and the refresh grants a full TTL instead, so the
    assertion below is the one that would catch an unwired knob.
    """
    monkeypatch.setenv("KDIVE_UPLOAD_TTL_SECONDS", "3600")
    monkeypatch.setenv("KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE", "1")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            minted = await _window_deadline(pool, run_id)
            await _complete_swallowing_failure(
                pool,
                run_id,
                CompleteBuildFinalizer(
                    validate_complete_build=FakeValidator(_output(run_id)),
                    object_store_factory=lambda: _ChunkedStore(bad_head=True),
                ),
            )
            after = await _window_deadline(pool, run_id)

        assert after == minted

    asyncio.run(_run())


def test_the_cap_multiple_rejects_a_value_at_or_below_zero() -> None:
    """Zero or negative puts the cap at or before the mint, making every refresh a no-op.

    That does not merely tighten the bound — it silently removes the extension the chunked finalize
    relies on to keep the reaper off an in-flight reassembly's chunk objects. Rejecting it in the
    parser puts the failure at `config validate` rather than at the first reassembly.
    """
    assert UPLOAD_WINDOW_MAX_TTL_MULTIPLE.parse("1") == 1
    for raw in ("0", "-1"):
        with pytest.raises(ValueError, match="must be >= 1"):
            UPLOAD_WINDOW_MAX_TTL_MULTIPLE.parse(raw)


def test_complete_build_finalizer_keeps_manifest_when_chunk_cleanup_fails(
    migrated_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ChunkedStore(delete_raises=".part0001")
            with caplog.at_level(logging.WARNING, logger=complete_build.__name__):
                result = await _complete(
                    pool,
                    run_id,
                    CompleteBuildFinalizer(
                        validate_complete_build=FakeValidator(_output(run_id)),
                        object_store_factory=lambda: store,
                    ),
                )
            present = await _manifest_present(pool, run_id)

        assert result.kernel_ref == f"local/runs/{run_id}/kernel"
        assert present is True
        failed_key = chunk_key(f"local/runs/{run_id}/", "kernel", 1)
        expected = f"chunk cleanup failed for {failed_key}: delete failed"
        assert any(record.getMessage() == expected for record in caplog.records)

    asyncio.run(_run())


def test_complete_build_finalizer_logs_manifest_cleanup_failure(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_delete_manifest(*args: object) -> None:
        del args
        raise CategorizedError(
            "manifest delete failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE
        )

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            monkeypatch.setattr(
                complete_build.upload_manifest, "delete_manifest", fail_delete_manifest
            )
            with caplog.at_level(logging.WARNING, logger=complete_build.__name__):
                result = await _complete(
                    pool,
                    run_id,
                    CompleteBuildFinalizer(
                        validate_complete_build=FakeValidator(_output(run_id)),
                        object_store_factory=_ChunkedStore,
                    ),
                )

        assert result.kernel_ref == f"local/runs/{run_id}/kernel"
        expected = f"manifest cleanup failed for run {run_id}: manifest delete failed"
        assert any(record.getMessage() == expected for record in caplog.records)

    asyncio.run(_run())


def _prefix(run_id: Any) -> str:
    return f"local/runs/{run_id}/"


def test_complete_build_success_persists_run_step_artifacts_and_audit(migrated_url: str) -> None:
    """A successful non-chunked finalize persists the run/step/artifact/audit rows verbatim.

    Pins the BuildStepResult fields carried back, the SUCCEEDED run row (kernel + debuginfo),
    the run_steps result JSON, both artifact rows (owner_kind/retention/sensitivity/key), the
    complete_build audit event, and manifest deletion.
    """
    entries = [
        ManifestEntry("kernel", "ck", 1),
        ManifestEntry("vmlinux", "cv", 1),
        ManifestEntry("initrd", "ci", 1),
    ]
    provenance: dict[str, str | bool | list[str]] = {"source_url": "https://x", "verified": True}

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=entries)
            kernel = f"{_prefix(run_id)}kernel"
            debuginfo = f"{_prefix(run_id)}vmlinux"
            output = BuildOutput(kernel, debuginfo, "build-id")
            run = await _run_by_id(pool, run_id)
            async with pool.connection() as conn:
                result = await CompleteBuildFinalizer(
                    validate_complete_build=FakeValidator(output)
                ).complete(
                    conn,
                    _ctx(),
                    run,
                    build_id=None,
                    cmdline="console=ttyS0",
                    source_provenance=provenance,
                )
            state, run_kernel, run_debuginfo = await _fetchone(
                pool, "SELECT state, kernel_ref, debuginfo_ref FROM runs WHERE id = %s", (run_id,)
            )
            step_result = await _fetchone(
                pool, "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
            )
            artifacts = await _fetchall(
                pool,
                "SELECT owner_kind, retention_class, sensitivity, object_key "
                "FROM artifacts WHERE owner_kind = 'investigations' "
                "AND owner_id = (SELECT investigation_id FROM runs WHERE id = %s) "
                "ORDER BY object_key",
                (run_id,),
            )
            audit = await _fetchone(
                pool,
                "SELECT tool, object_kind, transition, args_digest "
                "FROM audit_log WHERE object_id = %s",
                (run_id,),
            )
            manifest_gone = not await _manifest_present(pool, run_id)

        assert result.kernel_ref == kernel
        assert result.debuginfo_ref == debuginfo
        assert result.initrd_ref == f"{_prefix(run_id)}initrd"
        assert result.build_id == "build-id"
        assert result.cmdline == "console=ttyS0"
        assert result.build_provenance == provenance
        assert state == RunState.SUCCEEDED.value
        assert run_kernel == kernel
        assert run_debuginfo == debuginfo
        assert step_result[0] == result.dump()
        # Every reusable uploaded build artifact becomes Investigation-owned.
        assert artifacts == [
            ("investigations", "build", "sensitive", f"{_prefix(run_id)}initrd"),
            ("investigations", "build", "sensitive", kernel),
            ("investigations", "build", "sensitive", debuginfo),
        ]
        assert audit == (
            "runs.complete_build",
            "runs",
            "created->succeeded",
            args_digest({"run_id": str(run_id)}),
        )
        assert manifest_gone

    asyncio.run(_run())


def test_complete_build_publishes_winner_under_investigation_then_run_lock(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reusable artifact set is published atomically under the ordered locks."""
    entries = [
        ManifestEntry("kernel", "ck", 1),
        ManifestEntry("vmlinux", "cv", 1),
        ManifestEntry("initrd", "ci", 1),
    ]
    trace: list[str] = []
    original_lock = complete_build.advisory_xact_lock

    @asynccontextmanager
    async def traced_lock(conn, scope, key):
        trace.append(scope.value)
        async with original_lock(conn, scope, key):
            yield

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=entries)
            prefix = _prefix(run_id)
            result = await _complete(
                pool,
                run_id,
                CompleteBuildFinalizer(
                    validate_complete_build=FakeValidator(
                        BuildOutput(f"{prefix}kernel", f"{prefix}vmlinux", "build-id")
                    )
                ),
            )
            run_build_ref = await _fetchone(
                pool, "SELECT build_ref FROM runs WHERE id = %s", (run_id,)
            )
            catalog = await _fetchone(
                pool,
                "SELECT artifacts FROM investigation_builds "
                "WHERE investigation_id = (SELECT investigation_id FROM runs WHERE id = %s)",
                (run_id,),
            )
            artifacts = await _fetchall(
                pool,
                "SELECT owner_kind, object_key FROM artifacts ORDER BY object_key",
                (),
            )

        assert result.build_ref is not None
        assert result.expires_at is not None
        assert run_build_ref == (result.build_ref,)
        assert catalog == (
            {
                "initrd": {"key": f"{prefix}initrd", "version_id": "test-version"},
                "kernel": {"key": f"{prefix}kernel", "version_id": "test-version"},
                "vmlinux": {"key": f"{prefix}vmlinux", "version_id": "test-version"},
            },
        )
        assert artifacts == [
            ("investigations", f"{prefix}initrd"),
            ("investigations", f"{prefix}kernel"),
            ("investigations", f"{prefix}vmlinux"),
        ]

    with monkeypatch.context() as patched:
        patched.setattr(complete_build, "advisory_xact_lock", traced_lock)
        asyncio.run(_run())
    assert trace == ["investigation", "run"]


def test_concurrent_identical_completions_reuse_winner_and_delete_loser_versions(
    migrated_url: str,
) -> None:
    """A converged Run stores winner refs and removes only its own validated version."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            first_id = await seed_external_run_with_manifest(pool)
            second_id = await seed_external_run_with_manifest(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET investigation_id = "
                    "(SELECT investigation_id FROM runs WHERE id = %s) WHERE id = %s",
                    (first_id, second_id),
                )
            store = _VersionDeleteStore()
            first = CompleteBuildFinalizer(
                validate_complete_build=FakeValidator(_output(first_id)),
                object_store_factory=lambda: store,
            )
            second = CompleteBuildFinalizer(
                validate_complete_build=FakeValidator(_output(second_id)),
                object_store_factory=lambda: store,
            )
            results = await asyncio.gather(
                _complete(pool, first_id, first), _complete(pool, second_id, second)
            )
            artifacts = await _fetchall(
                pool,
                "SELECT object_key FROM artifacts WHERE owner_kind = 'investigations' "
                "AND owner_id = (SELECT investigation_id FROM runs WHERE id = %s)",
                (first_id,),
            )

        assert results[0].build_ref == results[1].build_ref
        assert results[0].kernel_ref == results[1].kernel_ref
        assert artifacts == [(results[0].kernel_ref,)]
        candidates = {f"{_prefix(first_id)}kernel", f"{_prefix(second_id)}kernel"}
        assert store.deleted == [(next(iter(candidates - {results[0].kernel_ref})), "test-version")]

    asyncio.run(_run())


def test_loser_delete_failure_leaves_the_version_for_orphan_repair(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Delete errors do not roll back a completed loser or target the winning object."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            winner_id = await seed_external_run_with_manifest(pool)
            loser_id = await seed_external_run_with_manifest(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET investigation_id = "
                    "(SELECT investigation_id FROM runs WHERE id = %s) WHERE id = %s",
                    (winner_id, loser_id),
                )
            await _complete(
                pool,
                winner_id,
                CompleteBuildFinalizer(validate_complete_build=FakeValidator(_output(winner_id))),
            )
            store = _VersionDeleteStore(fail=True)
            with caplog.at_level(logging.WARNING, logger=complete_build.__name__):
                result = await _complete(
                    pool,
                    loser_id,
                    CompleteBuildFinalizer(
                        validate_complete_build=FakeValidator(_output(loser_id)),
                        object_store_factory=lambda: store,
                    ),
                )

        assert result.kernel_ref == f"{_prefix(winner_id)}kernel"
        assert store.deleted == [(f"{_prefix(loser_id)}kernel", "test-version")]
        assert any(
            "losing build cleanup failed" in record.getMessage() for record in caplog.records
        )

    asyncio.run(_run())


def test_complete_build_propagates_target_arch_to_validator(migrated_url: str) -> None:
    """The persisted build-profile arch is passed to the upload validator (ADR-0343)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(
                pool, build_profile={"schema_version": 1, "arch": "aarch64"}
            )
            validator = FakeValidator(_output(run_id))
            await _complete(pool, run_id, CompleteBuildFinalizer(validate_complete_build=validator))
            assert validator.last_arch == "aarch64"

    asyncio.run(_run())


def test_complete_build_defaults_missing_arch_to_x86_64(migrated_url: str) -> None:
    """A build profile without an arch validates as x86_64 (the documented default)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(
                pool, build_profile={"schema_version": 1}
            )
            validator = FakeValidator(_output(run_id))
            await _complete(pool, run_id, CompleteBuildFinalizer(validate_complete_build=validator))
            assert validator.last_arch == "x86_64"

    asyncio.run(_run())


def test_complete_build_chunked_cleanup_deletes_selected_chunk_versions_and_manifest(
    migrated_url: str,
) -> None:
    """A peer replacement cannot turn post-commit cleanup into a delete of new chunks."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ChunkedStore()
            await _complete(
                pool,
                run_id,
                CompleteBuildFinalizer(
                    validate_complete_build=FakeValidator(_output(run_id)),
                    object_store_factory=lambda: store,
                ),
            )
            manifest_gone = not await _manifest_present(pool, run_id)

        prefix = _prefix(run_id)
        final_key = f"{prefix}kernel"
        part_keys = [chunk_key(prefix, "kernel", 1), chunk_key(prefix, "kernel", 2)]
        # Reassembly targets the final key and copies exactly the chunk source keys built from
        # the manifest prefix (pins _reassemble_artifacts prefix/final_key).
        assert ("create", final_key) in store.events
        copied = sorted(str(src) for op, src in store.events if op == "copy")
        assert copied == sorted(part_keys)
        # Cleanup then deletes the HEAD identities verified before the commit fence.
        assert sorted(store.deleted_versions) == sorted((key, "test-version") for key in part_keys)
        assert manifest_gone

    asyncio.run(_run())


def test_complete_build_chunk_cleanup_does_not_target_peer_chunk_versions(
    migrated_url: str,
) -> None:
    """The post-commit delete targets the pre-fence HEAD, not a peer replacement."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _PeerPutChunkedStore()
            await _complete(
                pool,
                run_id,
                CompleteBuildFinalizer(
                    validate_complete_build=FakeValidator(_output(run_id)),
                    object_store_factory=lambda: store,
                ),
            )

        assert {version for _, version in store.deleted_versions} == {"test-version"}
        assert set(store.current_versions.values()) == {"peer-version-2"}

    asyncio.run(_run())


def test_complete_build_does_not_delete_chunks_when_the_commit_fails(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact chunk cleanup cannot begin until the Run finalization is durable."""

    async def _reject_commit(_: psycopg.AsyncConnection) -> None:
        raise psycopg.OperationalError("commit failed")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ChunkedStore()
            finalizer = CompleteBuildFinalizer(
                validate_complete_build=FakeValidator(_output(run_id)),
                object_store_factory=lambda: store,
            )
            run = await _run_by_id(pool, run_id)
            async with pool.connection() as conn:
                with monkeypatch.context() as patched:
                    patched.setattr(psycopg.AsyncConnection, "commit", _reject_commit)
                    with pytest.raises(psycopg.OperationalError, match="commit failed"):
                        await finalizer.complete(
                            conn, _ctx(), run, build_id=None, cmdline="console=ttyS0"
                        )

        assert store.deleted_versions == []

    asyncio.run(_run())
