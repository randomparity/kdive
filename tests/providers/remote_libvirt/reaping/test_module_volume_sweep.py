"""The authority sweeps its own pool, keeping retained and attached volumes (ADR-0588)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.module_volume_sweep import (
    ModuleVolumeKeyV1,
    ModuleVolumeKind,
    ModuleVolumeSweepRequestV1,
    ModuleVolumeSweepResponseV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentity,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.reaping.module_volume_sweep import ModuleVolumeSweepService

POOL = "modules"
SYSTEM = "00000000-0000-4000-8000-000000000001"
RUN = "00000000-0000-4000-8000-000000000002"


def _name(nonce: str, kind: str = "source.ext4") -> str:
    return render_module_volume_name(SYSTEM, RUN, nonce, kind)


class _Volume:
    def __init__(self, name: str, path: str, deleted: list[str]) -> None:
        self._name, self._path, self._deleted = name, path, deleted

    def name(self) -> str:
        return self._name

    def path(self) -> str:
        return self._path

    def delete(self, flags: int = 0) -> int:
        self._deleted.append(self._name)
        return 0


class _Pool:
    def __init__(self, volumes: list[_Volume], *, fail: bool = False) -> None:
        self._volumes, self._fail = volumes, fail

    def refresh(self, flags: int = 0) -> int:
        return 0

    def listAllVolumes(self, flags: int = 0) -> list[_Volume]:  # noqa: N802 - libvirt naming
        if self._fail:
            raise libvirt.libvirtError("pool is gone")
        return self._volumes


class _Domain:
    def __init__(self, source: str) -> None:
        self._xml = (
            f"<domain><devices><disk type='file' device='disk'><source file='{source}'/></disk>"
            "</devices></domain>"
        )

    def isActive(self) -> int:  # noqa: N802 - libvirt naming
        return 1

    def isPersistent(self) -> int:  # noqa: N802 - libvirt naming
        return 0

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt naming
        return self._xml


class _Conn:
    def __init__(self, pool: _Pool, domains: list[_Domain] | None = None) -> None:
        self._pool, self._domains = pool, domains or []

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802 - libvirt naming
        assert name == POOL
        return self._pool

    def listAllDomains(self, flags: int = 0) -> list[_Domain]:  # noqa: N802 - libvirt naming
        return self._domains


class _Identity:
    """Each path is its own distinct file, so only an exact path match is a reference."""

    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        return RemoteDeviceIdentity("inode", 1, abs(hash(path)) & 0xFFFF_FFFF)


def _service(conn: _Conn) -> ModuleVolumeSweepService:
    return ModuleVolumeSweepService(
        cast(Any, conn), POOL, RemoteModulePreparationExecutor(), _Identity()
    )


def _key(nonce: str, kind: ModuleVolumeKind = "source.ext4") -> ModuleVolumeKeyV1:
    return ModuleVolumeKeyV1(
        system_id=UUID(SYSTEM), run_id=UUID(RUN), operation_nonce=nonce, kind=kind
    )


def _sweep(service: ModuleVolumeSweepService, *retained: ModuleVolumeKeyV1) -> Any:
    return asyncio.run(service.sweep(ModuleVolumeSweepRequestV1(retained=tuple(retained))))


def test_unretained_owned_volumes_are_deleted_and_counted() -> None:
    deleted: list[str] = []
    volumes = [
        _Volume(_name("1" * 32), "/pool/a", deleted),
        _Volume(_name("1" * 32, "reaped.journal"), "/pool/b", deleted),
    ]

    response = _sweep(_service(_Conn(_Pool(volumes))))

    assert response == ModuleVolumeSweepResponseV1(removed=2)
    assert sorted(deleted) == sorted(volume.name() for volume in volumes)


def test_a_retained_attempt_is_kept() -> None:
    deleted: list[str] = []
    kept, orphan = "1" * 32, "2" * 32
    volumes = [
        _Volume(_name(kept), "/pool/kept", deleted),
        _Volume(_name(orphan), "/pool/orphan", deleted),
    ]

    response = _sweep(_service(_Conn(_Pool(volumes))), _key(kept))

    assert response.removed == 1
    assert deleted == [_name(orphan)]


def test_a_volume_that_a_domain_still_references_is_kept() -> None:
    deleted: list[str] = []
    volumes = [_Volume(_name("1" * 32), "/pool/attached", deleted)]

    response = _sweep(_service(_Conn(_Pool(volumes), [_Domain("/pool/attached")])))

    assert response.removed == 0
    assert deleted == []


def test_volumes_that_are_not_module_volumes_are_never_deleted() -> None:
    deleted: list[str] = []
    volumes = [
        _Volume("kdive-someone-elses-overlay.qcow2", "/pool/overlay", deleted),
        _Volume("q3183-base.qcow2", "/pool/base", deleted),
    ]

    response = _sweep(_service(_Conn(_Pool(volumes))))

    assert response.removed == 0
    assert deleted == []


def test_an_unreadable_pool_fails_instead_of_reporting_nothing_removed() -> None:
    with pytest.raises(CategorizedError) as failed:
        _sweep(_service(_Conn(_Pool([], fail=True))))

    assert failed.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
