"""Service-level tests for external-build finalization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, NoReturn

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads import ChunkEntry, ManifestEntry
from kdive.build_artifacts.results import BuildOutput
from kdive.db.repositories import RUNS
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.services.runs import complete_build
from kdive.services.runs.complete_build import (
    CompleteBuildConfigurationError,
    CompleteBuildFinalizer,
)
from kdive.services.runs.steps import BuildStepResult
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

    def head(self, key: str) -> HeadResult | None:
        if key.endswith(".part0001"):
            checksum = "wrong" if self.bad_head else "c0"
            return HeadResult(5, checksum, "e1")
        if key.endswith(".part0002"):
            return HeadResult(3, "c1", "e2")
        return HeadResult(8, None, "final")

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        del key
        return (b"x" * 8)[start : start + length]

    def delete(self, key: str) -> None:
        if self.delete_raises is not None and key.endswith(self.delete_raises):
            raise CategorizedError("delete failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        self.events.append(("delete", key))

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


async def _run_by_id(pool: AsyncConnectionPool, run_id: Any):
    async with pool.connection() as conn:
        run = await RUNS.get(conn, run_id)
    assert run is not None
    return run


async def _manifest_present(pool: AsyncConnectionPool, run_id: Any) -> bool:
    async with pool.connection() as conn:
        return await upload_manifest.get_manifest(conn, "runs", run_id) is not None


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
            run_id = await seed_external_run(pool)
            async with pool.connection() as conn:
                await upload_manifest.replace_manifest(
                    conn,
                    upload_manifest.UploadManifestReplaceRequest(
                        owner_kind="runs",
                        owner_id=run_id,
                        prefix=f"local/runs/{run_id}/",
                        entries=[_CHUNKED_KERNEL],
                        ttl=timedelta(seconds=-1),
                    ),
                )
            store = _ChunkedStore()
            finalizer = CompleteBuildFinalizer(
                validate_complete_build=FakeValidator(_output(run_id)),
                object_store_factory=lambda: store,
            )
            error = await _complete_config_error(pool, run_id, finalizer)

        assert error.data == {"reason": "upload_window_expired"}
        assert store.events == []

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

            def unexpected_validator(*args: object) -> NoReturn:
                del args
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


def test_complete_build_finalizer_keeps_manifest_when_chunk_cleanup_fails(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ChunkedStore(delete_raises=".part0001")
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
        assert any("manifest cleanup failed" in record.message for record in caplog.records)

    asyncio.run(_run())
