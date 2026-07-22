"""Service-level tests for external-build finalization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, LiteralString, NoReturn

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import HeadResult, chunk_key
from kdive.artifacts.uploads import ChunkEntry, ManifestEntry
from kdive.build_artifacts.results import BuildOutput
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.audit import args_digest
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
    entries = [ManifestEntry("kernel", "ck", 1), ManifestEntry("initrd", "ci", 1)]
    provenance: dict[str, str | bool | list[str]] = {"source_url": "https://x", "verified": True}

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, entries=entries)
            kernel = f"{_prefix(run_id)}kernel"
            debuginfo = f"{_prefix(run_id)}debuginfo"
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
                "FROM artifacts WHERE owner_id = %s ORDER BY object_key",
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
        # Only the two uploaded manifest entries become artifact rows (debuginfo is a
        # validator-reported ref, not an uploaded object), ordered by object_key.
        assert artifacts == [
            ("runs", "build", "sensitive", f"{_prefix(run_id)}initrd"),
            ("runs", "build", "sensitive", kernel),
        ]
        assert audit == (
            "runs.complete_build",
            "runs",
            "created->succeeded",
            args_digest({"run_id": str(run_id)}),
        )
        assert manifest_gone

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


def test_complete_build_chunked_cleanup_deletes_chunks_and_manifest(migrated_url: str) -> None:
    """After a chunked finalize, every chunk key is deleted and the manifest is removed."""

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
        copied = sorted(src for op, src in store.events if op == "copy")
        assert copied == sorted(part_keys)
        # Cleanup then deletes every chunk key and removes the manifest.
        deleted = sorted(key for op, key in store.events if op == "delete")
        assert deleted == sorted(part_keys)
        assert manifest_gone

    asyncio.run(_run())
