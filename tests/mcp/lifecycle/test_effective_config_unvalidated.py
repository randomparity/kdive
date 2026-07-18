"""`effective_config` is accepted as an upload and never rejected — but warns (ADR-0330).

Spec 1 removed all kernel-config *validation*: a config that provably lacks the boot-required
``rootfs_mount`` symbols must still upload and drive the Run to ``succeeded`` (kdive stores the
artifact verbatim and rejects nothing). ADR-0330 adds one non-blocking signal on top: the success
envelope now carries a ``data.missing_boot_config`` advisory naming the missing symbols, so a
kernel that cannot mount its root filesystem is no longer *silently* completed.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from typing import Any, cast
from unittest.mock import patch

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult, PresignedUpload, PresignPutRequest
from kdive.db.repositories import RUNS
from kdive.domain.capacity.state import RunState
from kdive.kernel_config.gate import MISSING_BOOT_CONFIG_REASON
from kdive.kernel_config.parse import KernelConfig, parse_kernel_config
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.feature_requirements import (
    FEATURE_CONFIG_REQUIREMENTS_TOOL,
)
from kdive.mcp.tools.catalog.artifacts.uploads import (
    ArtifactDeclaration,
)
from kdive.mcp.tools.catalog.artifacts.uploads import (
    create_run_upload as _create_run_upload,
)
from kdive.mcp.tools.lifecycle.runs.complete_build import CompleteBuildHandlers
from tests.mcp.complete_build_support import ctx as _ctx
from tests.mcp.complete_build_support import pool as _pool
from tests.mcp.complete_build_support import seed_run as _seed_run
from tests.mcp.systems_support import provider_resolver

# A real, non-degenerate config that still lacks the ext4 direct-kernel boot set: it enables an
# unrelated symbol but neither EXT4_FS nor VIRTIO_BLK, so the advisory fires (fail-open only
# suppresses on an absent/unreadable/degenerate config, not on this one).
_BAD_CONFIG = b"CONFIG_XFS_FS=y\n# CONFIG_EXT4_FS is not set\n# CONFIG_VIRTIO_BLK is not set\n"


def _patched_load(config: KernelConfig | None) -> Any:
    async def _fake_load(
        conn: Any, run_id: Any, *, store_factory: Any = None
    ) -> KernelConfig | None:
        return config

    return patch("kdive.kernel_config.gate.load_effective_config", _fake_load)


def _combined_kernel_tar() -> bytes:
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


class _UploadStore:
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        return PresignedUpload(
            url=f"https://store/{request.key}",
            required_headers={"x-amz-checksum-sha256": request.sha256},
        )


class _ValidationStore:
    def __init__(self, blobs: dict[str, bytes], heads: dict[str, HeadResult]) -> None:
        self._blobs = blobs
        self._heads = heads

    def head(self, key: str) -> HeadResult | None:
        return self._heads.get(key)

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        return self._blobs[key][start : start + length]

    def delete(self, key: str) -> None:
        raise AssertionError("single-PUT path must not delete")

    def create_multipart_upload(
        self, key: str, *, sensitivity: object, retention_class: str
    ) -> str:
        raise AssertionError("single-PUT path must not reassemble")

    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str:
        raise AssertionError("single-PUT path must not reassemble")

    def complete_multipart_upload(self, key: str, upload_id: str, parts: object) -> str:
        raise AssertionError("single-PUT path must not reassemble")

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        raise AssertionError("single-PUT path must not reassemble")


async def _create_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    artifacts: list[ArtifactDeclaration],
    store: Any,
) -> ToolResponse:
    return await _create_run_upload(
        pool, ctx, run_id=run_id, artifacts=artifacts, resolver=provider_resolver(), store=store
    )


async def _artifact_keys(pool: AsyncConnectionPool, run_id: Any) -> set[str]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT object_key FROM artifacts WHERE owner_kind='runs' AND owner_id=%s",
            (run_id,),
        )
        return {row["object_key"] for row in await cur.fetchall()}


# A build_profile persisted under the OLD (pre-Spec-1) schema: a server-lane document with keys
# the flat BuildProfile no longer declares. The re-read sites must NOT re-parse it (extra="forbid"
# would reject the legacy keys and brick an in-flight CREATED run across a deploy).
_LEGACY_PROFILE = {
    "schema_version": 1,
    "source": "server",
    "kernel_source_ref": "warm-tree",
    "config": None,
    "profile_requirements": None,
    "patch_ref": None,
    "build_host": None,
}


def test_legacy_build_profile_uploads_and_completes(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _LEGACY_PROFILE)
            # _run_accepts_upload must not reject on the legacy profile shape.
            responses = await _create_upload(
                pool,
                _ctx(),
                run_id=str(run_id),
                artifacts=[{"name": "kernel", "sha256": "ck", "size_bytes": len(_KERNEL_TAR)}],
                store=_UploadStore(),
            )
            assert {response.status for response in responses.items} == {"upload_ready"}

            kernel_key = f"local/runs/{run_id}/kernel"
            store = _ValidationStore(
                {kernel_key: _KERNEL_TAR},
                {kernel_key: HeadResult(len(_KERNEL_TAR), "ck", "e-k")},
            )
            resp = await CompleteBuildHandlers(object_store_factory=lambda: store).complete_build(
                pool, _ctx(), str(run_id), build_id=None, cmdline="x"
            )
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)

        assert resp.status == "succeeded", resp
        assert run is not None and run.state is RunState.SUCCEEDED

    asyncio.run(_run())


def test_bad_effective_config_uploads_completes_and_warns(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, {"schema_version": 1})
            responses = await _create_upload(
                pool,
                _ctx(),
                run_id=str(run_id),
                artifacts=[
                    {"name": "kernel", "sha256": "ck", "size_bytes": len(_KERNEL_TAR)},
                    {"name": "effective_config", "sha256": "cc", "size_bytes": len(_BAD_CONFIG)},
                ],
                store=_UploadStore(),
            )
            assert {response.status for response in responses.items} == {"upload_ready"}

            kernel_key = f"local/runs/{run_id}/kernel"
            config_key = f"local/runs/{run_id}/effective_config"
            store = _ValidationStore(
                {kernel_key: _KERNEL_TAR, config_key: _BAD_CONFIG},
                {
                    kernel_key: HeadResult(len(_KERNEL_TAR), "ck", "e-k"),
                    config_key: HeadResult(len(_BAD_CONFIG), "cc", "e-c"),
                },
            )
            with _patched_load(parse_kernel_config(_BAD_CONFIG)):
                resp = await CompleteBuildHandlers(
                    object_store_factory=lambda: store
                ).complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="x")
            keys = await _artifact_keys(pool, run_id)
            async with pool.connection() as conn:
                run = await RUNS.get(conn, run_id)

        # The upload is never rejected: the Run still reaches SUCCEEDED and the config is stored.
        assert resp.status == "succeeded", resp
        assert keys == {kernel_key, config_key}
        assert run is not None and run.state is RunState.SUCCEEDED
        # ...but the success envelope now carries the non-blocking boot-config advisory.
        warning = cast("dict[str, Any]", resp.data["missing_boot_config"])
        assert warning["reason"] == MISSING_BOOT_CONFIG_REASON
        assert warning["missing"] == ["EXT4_FS", "VIRTIO_BLK"]
        assert resp.suggested_next_actions[0] == FEATURE_CONFIG_REQUIREMENTS_TOOL

    asyncio.run(_run())


def test_supported_effective_config_completes_without_warning(migrated_url: str) -> None:
    good = b"CONFIG_EXT4_FS=y\nCONFIG_VIRTIO_BLK=y\n"

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, {"schema_version": 1})
            responses = await _create_upload(
                pool,
                _ctx(),
                run_id=str(run_id),
                artifacts=[
                    {"name": "kernel", "sha256": "ck", "size_bytes": len(_KERNEL_TAR)},
                    {"name": "effective_config", "sha256": "cc", "size_bytes": len(good)},
                ],
                store=_UploadStore(),
            )
            assert {response.status for response in responses.items} == {"upload_ready"}

            kernel_key = f"local/runs/{run_id}/kernel"
            config_key = f"local/runs/{run_id}/effective_config"
            store = _ValidationStore(
                {kernel_key: _KERNEL_TAR, config_key: good},
                {
                    kernel_key: HeadResult(len(_KERNEL_TAR), "ck", "e-k"),
                    config_key: HeadResult(len(good), "cc", "e-c"),
                },
            )
            with _patched_load(parse_kernel_config(good)):
                resp = await CompleteBuildHandlers(
                    object_store_factory=lambda: store
                ).complete_build(pool, _ctx(), str(run_id), build_id=None, cmdline="x")

        assert resp.status == "succeeded", resp
        assert "missing_boot_config" not in resp.data
        assert resp.suggested_next_actions == ["runs.get"]

    asyncio.run(_run())
