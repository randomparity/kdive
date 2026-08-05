"""runs.complete_build + the symmetric source gate (ADR-0048 §4/§6)."""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts import upload_manifest
from kdive.artifacts.storage import (
    HeadResult,
    MultipartCompletion,
    PresignedUpload,
    PresignPutRequest,
)
from kdive.artifacts.uploads import ChunkEntry, ManifestEntry
from kdive.build_artifacts.results import BuildOutput
from kdive.config.core_settings import BUILD_ARTIFACT_RETENTION_DAYS
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import RequestContext
from kdive.mcp.resources.external_build_contract import EXTERNAL_BUILD_CONTRACT_URI
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_RUN_UPLOAD_TOOL,
    ArtifactDeclaration,
)
from kdive.mcp.tools.catalog.artifacts.uploads import (
    create_run_upload as _create_run_upload,
)
from kdive.mcp.tools.lifecycle.runs.complete_build import CompleteBuildHandlers
from kdive.mcp.tools.lifecycle.runs.view import get_run as _get_run
from kdive.security.audit import args_digest
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.clock import STORE_MTIME
from tests.mcp.complete_build_support import (
    FakeValidator as _FakeValidator,
)
from tests.mcp.complete_build_support import (
    ctx as _ctx,
)
from tests.mcp.complete_build_support import (
    pool as _pool,
)
from tests.mcp.complete_build_support import (
    seed_external_run as _seed_external_run,
)
from tests.mcp.complete_build_support import (
    seed_external_run_with_manifest as _seed_external_run_with_manifest,
)
from tests.mcp.complete_build_support import (
    seed_run as _seed_run,
)
from tests.mcp.systems_support import provider_resolver


def _combined_kernel_tar() -> bytes:
    """The unified `kernel` artifact: gzip tar of boot/vmlinuz (a bzImage) + lib/modules/<ver>/."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (
            ("boot/vmlinuz", b"\x00" * 0x202 + b"HdrS" + b"\x00" * 16),
            ("lib/modules/6.9.0/modules.dep", b""),
            ("lib/modules/6.9.0/kernel/drivers/foo.ko", b"\x7fELFmod"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_KERNEL_TAR = _combined_kernel_tar()
_EXTERNAL_PROFILE = {"schema_version": 1}
_DEFAULT_BUILD_HANDLERS = CompleteBuildHandlers()


async def create_run_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    artifacts: list[ArtifactDeclaration],
    store: Any = None,
) -> ToolResponse:
    return await _create_run_upload(
        pool,
        ctx,
        run_id=run_id,
        artifacts=artifacts,
        resolver=provider_resolver(),
        store=store,
    )


def _build_handlers(validator) -> CompleteBuildHandlers:
    return CompleteBuildHandlers(validate_complete_build=validator)


class _DelayedValidator(_FakeValidator):
    """Makes a response-clock regression observable without a real large upload."""

    def __call__(self, manifest, keys, declared_build_id, *, arch: str = "x86_64"):
        time.sleep(0.5)
        return super().__call__(manifest, keys, declared_build_id, arch=arch)


class _UploadStore:
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        return PresignedUpload(
            url=f"https://store/{request.key}",
            required_headers={"x-amz-checksum-sha256": request.sha256},
        )


class _ValidationStore:
    """A head + get_range store fake for the single-PUT validation path.

    The multipart/delete members exist only so the fake satisfies ``ExternalBuildStore``; the
    single-PUT lane never calls them, so they raise if a regression starts routing here.
    """

    def __init__(self, blobs: dict[str, bytes], heads: dict[str, HeadResult]) -> None:
        self._blobs = blobs
        self._heads = heads

    def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None:
        return self._heads.get(key)

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        return self._blobs[key][start : start + length]

    def delete_version(self, key: str, version_id: str) -> None:
        raise AssertionError("single-PUT path must not delete")

    def create_multipart_upload(
        self, key: str, *, sensitivity: object, retention_class: str
    ) -> str:
        raise AssertionError("single-PUT path must not reassemble")

    def upload_part_copy(
        self,
        key: str,
        upload_id: str,
        *,
        part_number: int,
        source_key: str,
        source_version_id: str,
    ) -> str:
        raise AssertionError("single-PUT path must not reassemble")

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: object
    ) -> MultipartCompletion:
        raise AssertionError("single-PUT path must not reassemble")

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        raise AssertionError("single-PUT path must not reassemble")


async def _artifact_keys(pool, run_id) -> set[str]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT object_key FROM artifacts WHERE owner_kind='runs' AND owner_id=%s",
            (run_id,),
        )
        return {row["object_key"] for row in await cur.fetchall()}


async def _get_run_data(pool, run_id) -> dict[str, Any]:
    resp = await _get_run(
        pool, _ctx(), str(run_id), resolver=provider_resolver(), secret_registry=SecretRegistry()
    )
    assert resp.status == RunState.SUCCEEDED.value, resp
    return resp.data


async def _complete_build_audit_args_digest(pool, run_id) -> str | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT args_digest FROM audit_log "
            "WHERE tool = 'runs.complete_build' AND object_id = %s",
            (str(run_id),),
        )
        row = await cur.fetchone()
    return row["args_digest"] if row is not None else None


async def _build_step_result(pool, run_id) -> dict[str, object]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'",
            (run_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row["result"]


def test_complete_build_finalizes_external_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                cmdline="dhash_entries=1",
            )
            assert resp.status == "succeeded"
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert run is not None and run.state is RunState.SUCCEEDED
        assert run.kernel_ref is not None and run.kernel_ref.endswith("/kernel")

    asyncio.run(_run())


def test_complete_build_returns_reusable_build_deadline_contract(migrated_url: str) -> None:
    """Completion and replay expose the persisted generation and database-clock deadline."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            first = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None
            )
            replay = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None
            )

        for response in (first, replay):
            assert isinstance(response.data["build_ref"], str)
            expires_at = datetime.fromisoformat(str(response.data["expires_at"]))
            server_time = datetime.fromisoformat(str(response.data["server_time"]))
            assert expires_at.tzinfo is UTC
            assert server_time.tzinfo is UTC
            assert expires_at > server_time
        assert first.data["build_ref"] == replay.data["build_ref"]
        assert first.data["expires_at"] == replay.data["expires_at"]

    asyncio.run(_run())


def test_complete_build_response_server_time_is_current_after_validation(
    migrated_url: str,
) -> None:
    """The completion response clock and generation deadline begin after validation work.

    Two Postgres clock reads bracket the call rather than a wall-clock budget measured after
    it: acquiring a second pool connection under `pytest -n auto` against one Postgres is
    itself unbounded, so a budget narrows exactly when the suite is busiest and ends up
    measuring scheduling latency instead of ordering (#1862). Every bound below widens under
    load. Both reads stay in Postgres — comparing against Python's clock would make the result
    depend on the database session's time zone.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            async with pool.connection() as conn:
                before_row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
            assert before_row is not None
            response = await _build_handlers(
                _DelayedValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            ).complete_build(pool, _ctx(), str(run_id), build_id=None)
            async with pool.connection() as conn:
                after_row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
            assert after_row is not None

        before, after = before_row[0], after_row[0]
        expires_at = datetime.fromisoformat(str(response.data["expires_at"]))
        server_time = datetime.fromisoformat(str(response.data["server_time"]))
        retention = timedelta(days=config.require(BUILD_ARTIFACT_RETENTION_DAYS))
        validation = timedelta(milliseconds=450)  # _DelayedValidator sleeps 0.5 s
        assert before < server_time <= after
        assert server_time - before >= validation
        assert expires_at - before >= retention + validation

    asyncio.run(_run())


def test_complete_build_threads_profile_arch_to_validator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(
                pool, build_profile={"schema_version": 1, "arch": "ppc64le"}
            )
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None
            )
            assert resp.status == "succeeded"
        assert validator.last_arch == "ppc64le"

    asyncio.run(_run())


def test_complete_build_defaults_arch_x86_64_when_profile_omits_it(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)  # {"schema_version": 1}
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None
            )
        assert validator.last_arch == "x86_64"

    asyncio.run(_run())


def test_complete_build_rejects_corrupt_non_string_arch(migrated_url: str) -> None:
    # A present-but-non-string persisted arch (unreachable via runs.create) fails loudly rather
    # than being silently masked as x86_64.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(
                pool, build_profile={"schema_version": 1, "arch": 123}
            )
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "invalid_build_profile_arch"
        assert validator.calls == 0  # rejected before any finalize work

    asyncio.run(_run())


def test_complete_build_without_cmdline_records_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
            )
            assert resp.status == "succeeded"
            result = await _build_step_result(pool, run_id)
        assert "cmdline" not in result

    asyncio.run(_run())


def test_complete_build_blank_cmdline_records_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                cmdline="   ",
            )
            assert resp.status == "succeeded"
            result = await _build_step_result(pool, run_id)
        assert "cmdline" not in result

    asyncio.run(_run())


def test_complete_build_records_client_attested_provenance(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                source_label="linux-6.9 worktree",
                source_ref="abc1234",
            )
            assert resp.status == "succeeded"
            result = await _build_step_result(pool, run_id)
            data = await _get_run_data(pool, run_id)
        expected = {
            "client_attested": True,
            "label": "linux-6.9 worktree",
            "source_ref": "abc1234",
        }
        assert result["build_provenance"] == expected
        assert data["build_provenance"] == expected

    asyncio.run(_run())


def test_complete_build_without_provenance_records_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None
            )
            assert resp.status == "succeeded"
            result = await _build_step_result(pool, run_id)
            data = await _get_run_data(pool, run_id)
        assert "build_provenance" not in result
        assert "build_provenance" not in data

    asyncio.run(_run())


def test_complete_build_blank_provenance_treated_as_absent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                source_label="   ",
                source_ref="",
            )
            assert resp.status == "succeeded"
            result = await _build_step_result(pool, run_id)
        assert "build_provenance" not in result

    asyncio.run(_run())


def test_complete_build_rejects_invalid_provenance_without_finalizing(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                source_label="a" * 257,
            )
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "invalid_source_provenance"
        assert resp.data["field"] == "source_label"
        assert validator.calls == 0  # rejected before any finalize work
        assert run is not None and run.state is RunState.CREATED

    asyncio.run(_run())


def test_complete_build_provenance_verbatim_and_not_audited(migrated_url: str) -> None:
    # AC7: a credential-like source_ref is echoed verbatim in runs.get and never lands in the
    # complete_build audit record (its args carry only run_id).
    secret_like = "https://user:tok3n@host/linux.git#deadbeef"  # pragma: allowlist secret

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                source_ref=secret_like,
            )
            assert resp.status == "succeeded"
            data = await _get_run_data(pool, run_id)
            recorded_digest = await _complete_build_audit_args_digest(pool, run_id)
        assert data["build_provenance"]["source_ref"] == secret_like
        assert recorded_digest == args_digest({"run_id": str(run_id)})

    asyncio.run(_run())


def test_provenance_is_bound_on_first_completion(migrated_url: str) -> None:
    # AC6: a replay cannot add or change provenance — the first completion binds it.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            handlers = _build_handlers(validator)
            first = await handlers.complete_build(pool, _ctx(), str(run_id), build_id=None)
            second = await handlers.complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                source_label="late-claim",
                source_ref="late-ref",
            )
            result = await _build_step_result(pool, run_id)
        assert first.status == "succeeded" and second.status == "succeeded"
        assert validator.calls == 1  # the replay short-circuits before finalize
        assert "build_provenance" not in result  # the replay's claim is not recorded

    asyncio.run(_run())


def test_contributor_completes_external_build(migrated_url: str) -> None:
    # ADR-0234 end-to-end: a contributor (not operator) can finalize an external build it
    # uploaded. Proves the re-gated ingest core admits the role and drives the Run to SUCCEEDED,
    # not just that an isolated require_role passes.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool,
                _ctx(role=Role.CONTRIBUTOR),
                str(run_id),
                build_id=None,
                cmdline="dhash_entries=1",
            )
            assert resp.status == "succeeded"
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert run is not None and run.state is RunState.SUCCEEDED

    asyncio.run(_run())


def test_complete_build_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            handlers = _build_handlers(validator)
            r1 = await handlers.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
            r2 = await handlers.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert r1.status == "succeeded" and r2.status == "succeeded"
        assert validator.calls == 1  # the short-read short-circuits the second

    asyncio.run(_run())


def test_complete_build_maps_validation_build_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(
                CategorizedError("bad", category=ErrorCategory.BUILD_FAILURE)
            )
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.error_category == ErrorCategory.BUILD_FAILURE.value
        # A format/shape rejection is self-correcting: it points back at the contract (#769).
        assert resp.suggested_next_actions == [CREATE_RUN_UPLOAD_TOOL]
        assert resp.refs["external_build_contract"] == EXTERNAL_BUILD_CONTRACT_URI

    asyncio.run(_run())


def test_complete_build_success_does_not_carry_the_format_advisory(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.status == "succeeded"
        assert resp.suggested_next_actions != [CREATE_RUN_UPLOAD_TOOL]

    asyncio.run(_run())


def test_complete_build_nudges_effective_config_upload_when_absent(migrated_url: str) -> None:
    # No effective_config was uploaded, so the EXT4_FS/VIRTIO_BLK boot-config advisory could never
    # fire; the success envelope carries a non-blocking nudge pointing at the upload tool (#1342).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.status == "succeeded"
        advisory = resp.data["missing_effective_config"]
        assert isinstance(advisory, dict)
        assert advisory["reason"] == "no_effective_config_uploaded"
        assert CREATE_RUN_UPLOAD_TOOL in resp.suggested_next_actions
        assert "missing_boot_config" not in resp.data  # nudge and warning are mutually exclusive

    asyncio.run(_run())


def test_complete_build_omits_nudge_when_effective_config_present(migrated_url: str) -> None:
    # A complete effective_config was uploaded (EXT4_FS + VIRTIO_BLK): neither the absent-config
    # nudge nor the missing-symbol boot warning fires, and the upload tool is not suggested (#1342).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _EXTERNAL_PROFILE)
            config = b"CONFIG_EXT4_FS=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_VIRTIO_PCI=y\n"
            responses = await create_run_upload(
                pool,
                _ctx(),
                run_id=str(run_id),
                artifacts=[
                    {"name": "kernel", "sha256": "ck", "size_bytes": len(_KERNEL_TAR)},
                    {"name": "effective_config", "sha256": "cc", "size_bytes": len(config)},
                ],
                store=_UploadStore(),
            )
            assert {response.status for response in responses.items} == {"upload_ready"}
            kernel_key = f"local/runs/{run_id}/kernel"
            config_key = f"local/runs/{run_id}/effective_config"
            store = _ValidationStore(
                {kernel_key: _KERNEL_TAR, config_key: config},
                {
                    kernel_key: HeadResult(
                        len(_KERNEL_TAR),
                        "ck",
                        "e-k",
                        last_modified=STORE_MTIME,
                        version_id="test-version",
                    ),
                    config_key: HeadResult(
                        len(config),
                        "cc",
                        "e-c",
                        last_modified=STORE_MTIME,
                        version_id="test-version",
                    ),
                },
            )
            resp = await CompleteBuildHandlers(
                object_store_factory=lambda: store,
            ).complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="x")

        assert resp.status == "succeeded", resp
        assert "missing_effective_config" not in resp.data
        assert CREATE_RUN_UPLOAD_TOOL not in resp.suggested_next_actions

    asyncio.run(_run())


def test_complete_build_malformed_stored_profile_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, {"source": "bogus"})
            resp = await _DEFAULT_BUILD_HANDLERS.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.status == "error"  # a structured failure, not a raised ToolError
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_rejects_run_with_no_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run(pool)
            resp = await _DEFAULT_BUILD_HANDLERS.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_complete_build_rejects_non_created_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)
            async with pool.connection() as conn:
                await conn.execute("UPDATE runs SET state='failed' WHERE id=%s", (run_id,))
            resp = await _DEFAULT_BUILD_HANDLERS.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["current_status"] == RunState.FAILED.value

    asyncio.run(_run())


def test_complete_build_writes_artifact_rows_and_deletes_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            entries = [
                ManifestEntry("kernel", "c", 1),
                ManifestEntry("vmlinux", "c", 1),
                ManifestEntry("initrd", "c", 1),
            ]
            run_id = await _seed_external_run_with_manifest(pool, entries=entries)
            kernel_key = f"local/runs/{run_id}/kernel"
            vmlinux_key = f"local/runs/{run_id}/vmlinux"
            validator = _FakeValidator(BuildOutput(kernel_key, vmlinux_key, "abcd"))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id="abcd", cmdline="x"
            )
            assert resp.status == "succeeded"
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT object_key FROM artifacts WHERE owner_kind = 'investigations' "
                        "AND owner_id = (SELECT investigation_id FROM runs WHERE id = %s)",
                        (run_id,),
                    )
                    rows = await cur.fetchall()
                manifest = await upload_manifest.get_manifest(conn, "runs", run_id)
        keys = {r["object_key"] for r in rows}
        assert keys == {kernel_key, vmlinux_key, f"local/runs/{run_id}/initrd"}
        assert manifest is None

    asyncio.run(_run())


def test_complete_build_writes_effective_config_artifact(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _EXTERNAL_PROFILE)
            config = b"CONFIG_SERIAL_8250_CONSOLE=y\nCONFIG_VIRTIO_BLK=y\nCONFIG_VIRTIO_PCI=y\n"
            responses = await create_run_upload(
                pool,
                _ctx(),
                run_id=str(run_id),
                artifacts=[
                    {"name": "kernel", "sha256": "ck", "size_bytes": len(_KERNEL_TAR)},
                    {"name": "effective_config", "sha256": "cc", "size_bytes": len(config)},
                ],
                store=_UploadStore(),
            )
            assert {response.status for response in responses.items} == {"upload_ready"}
            assert await _artifact_keys(pool, run_id) == set()
            kernel_key = f"local/runs/{run_id}/kernel"
            config_key = f"local/runs/{run_id}/effective_config"
            store = _ValidationStore(
                {kernel_key: _KERNEL_TAR, config_key: config},
                {
                    kernel_key: HeadResult(
                        len(_KERNEL_TAR),
                        "ck",
                        "e-k",
                        last_modified=STORE_MTIME,
                        version_id="test-version",
                    ),
                    config_key: HeadResult(
                        len(config),
                        "cc",
                        "e-c",
                        last_modified=STORE_MTIME,
                        version_id="test-version",
                    ),
                },
            )

            resp = await CompleteBuildHandlers(
                object_store_factory=lambda: store,
            ).complete_build(
                pool,
                _ctx(),
                str(run_id),
                build_id=None,
                cmdline="x",
            )
            keys = await _artifact_keys(pool, run_id)

        assert resp.status == "succeeded", resp
        assert keys == {config_key}

    asyncio.run(_run())


# --- Chunked reassembly at finalize (ADR-0104) ------------------------------------------

_CHUNKED_KERNEL = ManifestEntry(
    "kernel", "whole", 8, chunks=(ChunkEntry("c0", 5), ChunkEntry("c1", 3))
)


class _ReassemblyStore:
    """An ExternalBuildStore fake recording multipart + delete calls for one chunked kernel."""

    def __init__(self, *, delete_raises: str | None = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self._delete_raises = delete_raises

    def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None:
        if version_id is not None:
            assert version_id == "final-version"
        if key.endswith(".part0001"):
            return HeadResult(5, "c0", "e", last_modified=STORE_MTIME, version_id="test-version")
        if key.endswith(".part0002"):
            return HeadResult(3, "c1", "e", last_modified=STORE_MTIME, version_id="test-version")
        if key.endswith("/kernel"):
            return HeadResult(
                8,
                None,
                "final-etag",
                last_modified=STORE_MTIME,
                version_id="test-version",
            )  # reassembled: composite/None checksum
        return None

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        return _KERNEL_TAR[start : start + length]

    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str:
        self.events.append(("create", key))
        return "uid"

    def upload_part_copy(
        self,
        key: str,
        upload_id: str,
        *,
        part_number: int,
        source_key: str,
        source_version_id: str,
    ) -> str:
        assert source_version_id in {"test-version", "chunk-version"}
        self.events.append(("copy", part_number, source_key))
        return f"etag-{part_number}"

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> MultipartCompletion:
        self.events.append(("complete", tuple(parts)))
        return MultipartCompletion("final-etag", "final-version")

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self.events.append(("abort", key))

    def delete_version(self, key: str, version_id: str) -> None:
        if self._delete_raises is not None and key.endswith(self._delete_raises):
            raise CategorizedError("delete boom", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        self.events.append(("delete_version", key))


def _chunked_handlers(store: _ReassemblyStore, output: BuildOutput) -> CompleteBuildHandlers:
    return CompleteBuildHandlers(
        validate_complete_build=_FakeValidator(output),
        object_store_factory=lambda: store,
    )


async def _manifest_present(pool, run_id) -> bool:
    async with pool.connection() as conn:
        return await upload_manifest.get_manifest(conn, "runs", run_id) is not None


def test_chunked_complete_build_reassembles_and_succeeds(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ReassemblyStore()
            output = BuildOutput(f"local/runs/{run_id}/kernel", "", "")
            resp = await _chunked_handlers(store, output).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
            assert resp.status == "succeeded"
            assert [e[0] for e in store.events[:4]] == ["create", "copy", "copy", "complete"]
            keys = await _artifact_keys(pool, run_id)
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert run is not None and run.state is RunState.SUCCEEDED
        assert keys == set()

    asyncio.run(_run())


def test_production_validator_publishes_chunked_build_without_final_head_checksum(
    migrated_url: str,
) -> None:
    """Verified chunk checksums remain the integrity evidence for a multipart final object."""
    import hashlib

    split = len(_KERNEL_TAR) // 2
    chunks = (_KERNEL_TAR[:split], _KERNEL_TAR[split:])
    entry = ManifestEntry(
        "kernel",
        hashlib.sha256(_KERNEL_TAR).hexdigest(),
        len(_KERNEL_TAR),
        chunks=tuple(ChunkEntry(hashlib.sha256(chunk).hexdigest(), len(chunk)) for chunk in chunks),
    )

    class _ProductionChunkStore(_ReassemblyStore):
        def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None:
            if key.endswith("/kernel"):
                assert version_id == "final-version"
            if key.endswith(".part0001"):
                chunk = chunks[0]
            elif key.endswith(".part0002"):
                chunk = chunks[1]
            elif key.endswith("/kernel"):
                return HeadResult(
                    len(_KERNEL_TAR), None, "multipart-etag", STORE_MTIME, "final-version"
                )
            else:
                return None
            return HeadResult(
                len(chunk), hashlib.sha256(chunk).hexdigest(), "etag", STORE_MTIME, "chunk-version"
            )

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool, entries=[entry])
            store = _ProductionChunkStore()
            handlers = CompleteBuildHandlers(object_store_factory=lambda: store)
            first = await handlers.complete_build(pool, _ctx(), str(run_id), build_id=None)
            assert first.status == "succeeded", first
            assert first.data["build_ref"]

    asyncio.run(_run())


def test_chunked_complete_build_still_extends_the_window_before_reassembly(
    migrated_url: str,
) -> None:
    """The in-window chunked refresh survives the #1534 check: it still extends (ADR-0448 §3).

    Observed through a finalize that reaches reassembly and then fails validation, so the
    manifest is still there to read. This also pins the recorded residual — a failing retry
    leaves a freshly extended window.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(
                pool, entries=[_CHUNKED_KERNEL], ttl=timedelta(seconds=30)
            )
            async with pool.connection() as conn:
                before = await upload_manifest.get_manifest(conn, "runs", run_id)
            handlers = CompleteBuildHandlers(
                validate_complete_build=_FakeValidator(
                    CategorizedError("bad payload", category=ErrorCategory.CONFIGURATION_ERROR)
                ),
                object_store_factory=_ReassemblyStore,
            )
            resp = await handlers.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )  # a chunked store plus a failing validator: neither factory covers this pair
            async with pool.connection() as conn:
                after = await upload_manifest.get_manifest(conn, "runs", run_id)
        assert resp.status == "error"
        assert before is not None and after is not None
        assert after.deadline > before.deadline

    asyncio.run(_run())


def _assert_expiry_contract(resp: ToolResponse) -> None:
    """Pin the self-correcting expiry payload: the wall, the clock, and the re-mint (#1534)."""
    assert resp.status == "error"
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.data["reason"] == "upload_window_expired"
    assert resp.suggested_next_actions == [CREATE_RUN_UPLOAD_TOOL]
    deadline = datetime.fromisoformat(str(resp.data["manifest_deadline"]))
    server_time = datetime.fromisoformat(str(resp.data["server_time"]))
    assert deadline.tzinfo == UTC and server_time.tzinfo == UTC
    assert deadline < server_time
    assert resp.data["on_expiry"] == {
        "tool": CREATE_RUN_UPLOAD_TOOL,
        "effect": "re-mint replaces the manifest and resets the deadline",
    }


def test_complete_build_rejects_expired_window(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(
                pool, entries=[_CHUNKED_KERNEL], ttl=timedelta(seconds=-1)
            )
            store = _ReassemblyStore()
            resp = await _chunked_handlers(
                store, BuildOutput(f"local/runs/{run_id}/kernel", "", "")
            ).complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="x")
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        _assert_expiry_contract(resp)
        assert store.events == []  # no reassembly attempted
        assert run is not None and run.state is RunState.CREATED

    asyncio.run(_run())


def test_complete_build_missing_manifest_points_at_the_mint(migrated_url: str) -> None:
    """The post-reap landing carries the same recovery pointer the expiry rejection does.

    A finalize more than one reconciler sweep past the deadline sees `no_upload_manifest`, not
    `upload_window_expired`, so leaving it bare would strand the common case (ADR-0448).
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run(pool)  # never minted a window
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "no_upload_manifest"
        assert resp.suggested_next_actions == [CREATE_RUN_UPLOAD_TOOL]
        assert resp.detail is not None

    asyncio.run(_run())


def test_complete_build_replaced_window_points_at_the_mint(migrated_url: str) -> None:
    """A re-mint that lands mid-finalize is rejected through the envelope, not just the service."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool)

            def remint_then_validate(*args: Any, **kwargs: Any):
                with psycopg.connect(migrated_url, autocommit=True) as other:
                    other.execute(
                        "UPDATE upload_manifests SET deadline = now() + interval '2 hours' "
                        "WHERE owner_kind = 'runs' AND owner_id = %s",
                        (run_id,),
                    )
                return _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))(
                    *args, **kwargs
                )

            resp = await _build_handlers(remint_then_validate).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
            keys = await _artifact_keys(pool, run_id)
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "upload_window_replaced"
        assert resp.detail
        assert resp.suggested_next_actions == [CREATE_RUN_UPLOAD_TOOL]
        assert keys == set()
        assert run is not None and run.state is RunState.CREATED

    asyncio.run(_run())


def test_complete_build_rejects_expired_single_put_window(migrated_url: str) -> None:
    """The single-PUT path enforces the same deadline the chunked path does (#1534)."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool, ttl=timedelta(seconds=-1))
            validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", ""))
            resp = await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
            keys = await _artifact_keys(pool, run_id)
            kept = await _manifest_present(pool, run_id)
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        _assert_expiry_contract(resp)
        assert validator.calls == 0
        assert keys == set()
        assert kept  # left for the deadline-driven reaper
        assert run is not None and run.state is RunState.CREATED

    asyncio.run(_run())


def test_chunked_complete_build_store_factory_error_returns_envelope(migrated_url: str) -> None:
    def _failing_store() -> _ReassemblyStore:
        raise CategorizedError("store unavailable", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            handlers = CompleteBuildHandlers(
                validate_complete_build=_FakeValidator(
                    BuildOutput(f"local/runs/{run_id}/kernel", "", "")
                ),
                object_store_factory=_failing_store,
            )
            resp = await handlers.complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.INFRASTRUCTURE_FAILURE.value
        assert run is not None and run.state is RunState.CREATED

    asyncio.run(_run())


def test_chunked_finalize_deletes_chunks_and_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ReassemblyStore()
            resp = await _chunked_handlers(
                store, BuildOutput(f"local/runs/{run_id}/kernel", "", "")
            ).complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="x")
            assert resp.status == "succeeded"
            deleted = {e[1] for e in store.events if e[0] == "delete_version"}
            present = await _manifest_present(pool, run_id)
        assert deleted == {
            f"local/runs/{run_id}/kernel.part0001",
            f"local/runs/{run_id}/kernel.part0002",
        }
        assert present is False

    asyncio.run(_run())


def test_chunked_finalize_chunk_delete_failure_keeps_manifest(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_external_run_with_manifest(pool, entries=[_CHUNKED_KERNEL])
            store = _ReassemblyStore(delete_raises=".part0001")
            resp = await _chunked_handlers(
                store, BuildOutput(f"local/runs/{run_id}/kernel", "", "")
            ).complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="x")
            present = await _manifest_present(pool, run_id)
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)
        assert resp.status == "succeeded"  # finalize never fails on a cleanup error
        assert run is not None and run.state is RunState.SUCCEEDED
        assert present is True  # manifest lingers so the reaper reclaims the leftover chunk

    asyncio.run(_run())


# A kernel that mounts its root through modules only: enabled, so it parsed as satisfying the
# boot clauses before #1860, and unbootable by a direct-kernel boot with no initramfs to load
# them from. This is the exact upload the issue reports completing cleanly and then panicking.
_MODULAR_BOOT_CONFIG = b"CONFIG_EXT4_FS=m\nCONFIG_VIRTIO_BLK=m\nCONFIG_VIRTIO_PCI=m\n"


def _patched_config_load(data: bytes) -> Any:
    from unittest.mock import patch

    from kdive.kernel_config.parse import parse_kernel_config

    parsed = parse_kernel_config(data)

    async def _fake_load(conn: Any, run_id: Any, *, store_factory: Any = None) -> Any:
        return parsed

    return patch("kdive.kernel_config.gate.load_effective_config", _fake_load)


async def _complete_with_modular_boot_config(
    pool: AsyncConnectionPool, *, with_initrd: bool, times: int = 1
) -> list[ToolResponse]:
    """Finalize once (or replay) against a modular boot config, returning each envelope."""
    entries = [ManifestEntry("kernel", "c", 1)]
    if with_initrd:
        entries.append(ManifestEntry("initrd", "c", 1))
    run_id = await _seed_external_run_with_manifest(pool, entries=entries)
    validator = _FakeValidator(BuildOutput(f"local/runs/{run_id}/kernel", "", "abcd"))
    with _patched_config_load(_MODULAR_BOOT_CONFIG):
        return [
            await _build_handlers(validator).complete_build(
                pool, _ctx(), str(run_id), build_id="abcd", cmdline="x"
            )
            for _ in range(times)
        ]


def test_a_modular_boot_kernel_with_no_initrd_completes_with_the_boot_config_warning(
    migrated_url: str,
) -> None:
    # #1860 end to end. The completion still succeeds - this warns, never refuses - but the
    # envelope now carries the advisory that was silent, and `built_in_required` tells the agent
    # its config does contain these symbols, in a form nothing can load before root is mounted.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            first, replay = await _complete_with_modular_boot_config(
                pool, with_initrd=False, times=2
            )
        assert first.status == "succeeded", first
        warning = cast(dict[str, Any], first.data["missing_boot_config"])
        assert warning["missing"] == ["EXT4_FS", "VIRTIO_BLK", "XFS_FS"]
        assert warning["built_in_required"] == ["EXT4_FS", "VIRTIO_BLK"]
        assert "=y" in cast(str, warning["remediation"])
        assert first.refs["external_build_contract"] == EXTERNAL_BUILD_CONTRACT_URI
        # The idempotent replay reaches the same envelope through _existing_build_result, so the
        # advisory is recomputed off the *persisted* BuildStepResult rather than the in-memory one.
        assert replay.data["missing_boot_config"] == warning

    asyncio.run(_run())


def test_the_same_modular_kernel_is_silent_once_the_build_uploaded_an_initrd(
    migrated_url: str,
) -> None:
    # The seam supplies the carve-out from the finalized BuildStepResult it already holds, so the
    # identical config draws nothing once there is an initrd for the modules to come from. The two
    # tests differ only in the manifest entry, which is what pins the wiring rather than the flag.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            first, replay = await _complete_with_modular_boot_config(
                pool, with_initrd=True, times=2
            )
        assert first.status == "succeeded", first
        assert "missing_boot_config" not in first.data
        # non-vacuity: the initrd really reached the build result the warning reads
        assert first.refs["initrd"]
        # The replay path re-reads the build step from the row, so `initrd_ref` has to survive the
        # dump/load round trip or a second finalize would start warning where the first did not.
        assert "missing_boot_config" not in replay.data
        assert replay.refs["initrd"] == first.refs["initrd"]

    asyncio.run(_run())
