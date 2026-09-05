"""Native metadata-free remote boot-artifact inventory and reap proof (ADR-0599)."""

from __future__ import annotations

import contextlib
import sys
from typing import Protocol, cast
from uuid import uuid4

import libvirt
import pytest

from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_name import (
    parse_boot_artifact_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    BootArtifactVolumeConn,
    materialize_boot_artifacts,
)
from kdive.providers.remote_libvirt.reaping.boot_artifacts import (
    BootArtifactReaperConn,
    list_owned_boot_artifacts,
    reap_orphaned_boot_artifacts,
)
from tests.live_vm import require_live_vm_remote


class _Volume(Protocol):
    def delete(self, flags: int = 0) -> object: ...


class _Pool(Protocol):
    def listAllVolumes(self, flags: int = 0) -> list[_Volume]: ...  # noqa: N802
    def isActive(self) -> int: ...  # noqa: N802
    def destroy(self) -> object: ...
    def delete(self, flags: int = 0) -> object: ...
    def undefine(self) -> object: ...


class _Conn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802


def _cleanup_pool(conn: _Conn, pool_name: str) -> None:
    failures: list[str] = []
    try:
        pool = conn.storagePoolLookupByName(pool_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
            return
        raise AssertionError("remote artifact proof cleanup could not find its pool") from None
    for volume in pool.listAllVolumes(0):
        try:
            volume.delete(0)
        except libvirt.libvirtError:
            failures.append("volume delete")
    if pool.isActive():
        try:
            pool.destroy()
        except libvirt.libvirtError:
            failures.append("pool destroy")
    for name, action in (("pool delete", pool.delete), ("pool undefine", pool.undefine)):
        try:
            action()
        except libvirt.libvirtError:
            failures.append(name)
    if failures:
        raise AssertionError("remote artifact proof cleanup failed: " + ", ".join(failures))


@pytest.mark.live_vm
@pytest.mark.live_vm_remote
def test_remote_boot_artifacts_reap_after_metadata_free_readback() -> None:
    contract = require_live_vm_remote()
    system_id, run_id = uuid4(), uuid4()
    pool_name = f"kdive-boot-proof-{run_id.hex}"
    pool_path = f"/var/lib/libvirt/images/{pool_name}"
    conn = libvirt.open(contract.libvirt_uri)
    try:
        pool = conn.storagePoolDefineXML(
            f"<pool type='dir'><name>{pool_name}</name>"
            f"<target><path>{pool_path}</path></target></pool>"
        )
        pool.build(0)
        pool.create(0)
        materialize_boot_artifacts(
            cast("BootArtifactVolumeConn", conn),
            pool_name,
            system_id=system_id,
            run_id=run_id,
            kernel=b"native-kernel-proof",
            initrd=b"native-initrd-proof",
        )

        conn.close()
        conn = libvirt.open(contract.libvirt_uri)
        pool = conn.storagePoolLookupByName(pool_name)
        volumes = pool.listAllVolumes(0)
        readback = [volume.XMLDesc(0) for volume in volumes]
        assert len(readback) == 2
        assert all("metadata" not in document for document in readback)
        parsed = [parse_boot_artifact_name(volume.name()) for volume in volumes]
        assert all(identity is not None for identity in parsed)
        owned = list_owned_boot_artifacts(cast("BootArtifactReaperConn", conn), pool_name)
        assert {artifact.kind for artifact in owned} == {"kernel", "initrd"}
        assert (
            reap_orphaned_boot_artifacts(
                cast("BootArtifactReaperConn", conn), pool_name, live_owners=()
            )
            == 2
        )
        pool.refresh(0)
        assert pool.listAllVolumes(0) == []
    finally:
        primary = sys.exception()
        try:
            _cleanup_pool(cast("_Conn", conn), pool_name)
        except AssertionError as cleanup_error:
            if isinstance(primary, Exception):
                raise ExceptionGroup(
                    "remote artifact proof and cleanup both failed", [primary, cleanup_error]
                ) from None
            raise
        with contextlib.suppress(libvirt.libvirtError):
            conn.close()
