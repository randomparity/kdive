"""End-to-end reachability of the rootfs-upload lane (define -> upload -> provision, #111).

DB/tool-lane reachability under a fake provider: it proves the upload-kind profile flows
through systems.define, artifacts.create_system_upload, systems.provision_defined, and the provision
handler's _commit_uploaded_rootfs. It does NOT boot — staging the object to the libvirt
disk is the install/boot spec's concern (ADR-0048 §7).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row

from kdive.artifacts.storage import ArtifactStreamRequest, ArtifactWriteRequest
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.jobs.handlers import systems as systems_handlers
from kdive.mcp.tools.catalog.artifacts.uploads import create_system_upload
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    RootfsUploadContext,
    upload_rootfs_path,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch import fetch_uploaded_rootfs
from kdive.store.objectstore import ObjectStore, artifact_key
from tests.mcp.systems_support import (
    SYSTEM_PROVISION_HANDLERS as _SYSTEM_PROVISION_HANDLERS,
)
from tests.mcp.systems_support import (
    FakeProvisioning as _FakeProvisioning,
)
from tests.mcp.systems_support import (
    ctx as _ctx,
)
from tests.mcp.systems_support import (
    define_system as _define,
)
from tests.mcp.systems_support import (
    enqueue_provision as _enqueue_provision,
)
from tests.mcp.systems_support import (
    granted_allocation as _granted_allocation,
)
from tests.mcp.systems_support import (
    pool as _pool,
)
from tests.mcp.systems_support import (
    provider_resolver as _provider_resolver,
)
from tests.mcp.systems_support import (
    upload_profile as _upload_profile,
)


def test_define_upload_provision_reaches_ready_with_committed_rootfs(
    migrated_url: str, minio_store: ObjectStore
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)

            # 1. define -> DEFINED, allocation granted->active
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id

            # 2. create_system_upload opens the window (persists the manifest, mints a PUT)
            uploads = await create_system_upload(
                pool,
                _ctx(),
                system_id=sys_id,
                artifacts=[{"name": "rootfs", "sha256": "sha256:x", "size_bytes": 18}],
                resolver=_provider_resolver(),
                store=minio_store,
            )
            upload_items = uploads.items
            assert upload_items[0].status == "upload_ready"
            assert upload_items[0].suggested_next_actions == ["systems.provision_defined"]

            # 3. the agent PUTs the qcow2 (staged directly into the store for the test)
            minio_store.put_artifact(
                ArtifactWriteRequest(
                    tenant="local",
                    owner_kind="systems",
                    owner_id=sys_id,
                    name="rootfs",
                    data=b"rootfs-image-bytes",
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class="rootfs",
                )
            )

            # 4. provision_defined admits the DEFINED System by System id
            resp = await _SYSTEM_PROVISION_HANDLERS.provision_defined_system(
                pool, _ctx(), system_id=sys_id
            )
            assert resp.status == "queued"

            # 5. the provision handler drives provisioning -> ready and commits the rootfs
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(
                    conn,
                    job,
                    resolver=_provider_resolver(provisioner=_FakeProvisioning()),
                    artifact_store=minio_store,
                )

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT object_key, owner_kind, sensitivity FROM artifacts WHERE owner_id = %s",
                    (sys_id,),
                )
                art_rows = await cur.fetchall()
        assert sys_row is not None and sys_row["state"] == "ready"
        assert len(art_rows) == 1
        assert art_rows[0]["object_key"] == artifact_key("local", "systems", sys_id, "rootfs")
        assert art_rows[0]["owner_kind"] == "systems"
        assert art_rows[0]["sensitivity"] == "sensitive"

    asyncio.run(_run())


def test_uploaded_rootfs_staged_from_real_store(minio_store: ObjectStore, tmp_path: Path) -> None:
    """The staging step ADR-0048 §7 deferred, proved against a real object store (ADR-0434).

    Closes the "does NOT boot / staging deferred" gap the reachability test above disclaims: a
    checksum-carrying object (as production presigned PUTs write) is HEADed for its stored
    ``checksum_sha256``, downloaded, verified, and staged to the ``rootfs-uploads`` path — the
    real MinIO round-trip the unit tests fake. The provider wiring is unit-covered in
    ``test_provisioning.py::test_provision_upload_rootfs_stages_via_injected_fetch``.
    """
    system_id = uuid4()
    # The staged base must pass the ADR-0438 qcow2 magic check, so it starts with the qcow2 magic.
    data = b"QFI\xfb" + b"real-minio-uploaded-rootfs-bytes\n" * 512
    spool = tmp_path / "source.qcow2"
    spool.write_bytes(data)
    sha256_b64 = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
    minio_store.put_stream(
        ArtifactStreamRequest(
            tenant="local",
            owner_kind="systems",
            owner_id=str(system_id),
            name="rootfs",
            path=spool,
            sha256_b64=sha256_b64,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="rootfs",
        )
    )
    key = artifact_key("local", "systems", str(system_id), "rootfs")
    head = minio_store.head(key)
    assert head is not None and head.checksum_sha256 == sha256_b64, (
        "a presigned/streamed PUT must leave a stored checksum for the fetch to verify against"
    )

    staging = tmp_path / "uploads"
    upload = RootfsUploadContext("local", system_id, staging)
    staged = fetch_uploaded_rootfs(minio_store, upload)

    assert staged == upload_rootfs_path("local", system_id, upload_dir=staging)
    assert staged.read_bytes() == data
    # Idempotent reuse: a present verified file is returned without re-downloading.
    assert fetch_uploaded_rootfs(minio_store, upload) == staged
