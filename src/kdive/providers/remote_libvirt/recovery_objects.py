"""Exact remote-libvirt quarantine observation and disposition."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Protocol

import libvirt

from kdive.providers.ports.external_boot import (
    OpaqueProviderRef,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleVolumePreparationStore,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_name import (
    parse_boot_artifact_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    parse_module_volume_name,
)


class _Volume(Protocol):
    def name(self) -> str: ...
    def info(self) -> list[int]: ...
    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802


class RecoveryObjectConnection(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802


class RemoteExternalBootRecoveryObjects:
    """Use durable recovery geometry and exact configured pools for orphan disposition."""

    def __init__(
        self,
        *,
        store: RemoteModuleVolumePreparationStore,
        connection: RecoveryObjectConnection,
        boot_artifact_pool: str,
        inspect_attachments: Callable[[], AttachmentInspection],
    ) -> None:
        self._store = store
        self._connection = connection
        self._boot_artifact_pool = boot_artifact_pool
        self._inspect_attachments = inspect_attachments

    @staticmethod
    def _observation(
        binding: RecoveryObjectBinding, *, present: bool, managed: bool
    ) -> RecoveryObjectObservation:
        digest = (
            "sha256:"
            + hashlib.sha256(
                b"kdive-remote-recovery-object-observation-v1\0"
                + binding.to_canonical_json()
                + f"\0{present!s}\0{managed!s}".encode()
            ).hexdigest()
        )
        return RecoveryObjectObservation(
            binding=binding, present=present, managed=managed, observed_digest=digest
        )

    def _volume(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> _Volume | None:
        recovery = self._store.recovery_for_object(binding.reference)
        if recovery.binding != binding.binding:
            raise ValueError("remote recovery-object activation differs from durable record")
        recovery.module_recovery.validate_authority(authority)
        name = binding.reference.ref
        if binding.kind in {"kernel", "initrd"}:
            parsed = parse_boot_artifact_name(name)
            if (
                parsed is None
                or parsed.partial
                or parsed.kind != binding.kind
                or str(parsed.system_id) != binding.binding.system_id
                or str(parsed.run_id) != binding.binding.run_id
            ):
                raise ValueError("remote boot-artifact ownership differs")
            pool_name = self._boot_artifact_pool
        elif binding.kind == "modules":
            parsed_module = parse_module_volume_name(name)
            module = recovery.module_recovery
            capacities = {
                module.source_volume.ref: module.source_capacity_bytes,
                module.scratch_volume.ref: 10 * 1024**3,
            }
            if (
                parsed_module is None
                or parsed_module.system_id != binding.binding.system_id
                or parsed_module.run_id != binding.binding.run_id
                or name not in capacities
            ):
                raise ValueError("remote module-volume ownership differs")
            pool_name = module.pool.ref
        else:
            raise ValueError("remote recovery record is not a libvirt volume")
        try:
            pool = self._connection.storagePoolLookupByName(pool_name)
            volume = pool.storageVolLookupByName(name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return None
            raise
        if volume.name() != name:
            raise ValueError("remote recovery-object readback name differs")
        if binding.kind == "modules":
            if volume.info()[1] != capacities[name]:
                raise ValueError("remote module-volume geometry differs")
            if not self._inspect_attachments().proves_detached(pool_name, name):
                raise ValueError("remote module volume remains attached")
        return volume

    def observe_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation:
        return self._observation(
            binding, present=self._volume(binding, authority) is not None, managed=False
        )

    def delete_recovery_object(
        self,
        binding: RecoveryObjectBinding,
        authority: OpaqueProviderRef,
        expected_observed_digest: str,
    ) -> RecoveryObjectObservation:
        observed = self.observe_object(binding, authority)
        if observed.observed_digest != expected_observed_digest:
            raise ValueError("remote recovery-object observation changed before delete")
        volume = self._volume(binding, authority)
        if volume is not None:
            volume.delete(0)
        result = self.observe_object(binding, authority)
        if result.present:
            raise ValueError("remote recovery object remained after deletion")
        return result

    def adopt_object(
        self,
        binding: RecoveryObjectBinding,
        authority: OpaqueProviderRef,
        expected_observed_digest: str,
    ) -> RecoveryObjectObservation:
        observed = self.observe_object(binding, authority)
        if observed.observed_digest != expected_observed_digest or not observed.present:
            raise ValueError("remote recovery-object observation changed before adoption")
        return self._observation(binding, present=True, managed=True)
