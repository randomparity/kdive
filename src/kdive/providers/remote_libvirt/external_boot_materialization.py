"""Concrete remote external-boot artifact materialization (ADR-0608)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from uuid import UUID

from kdive.providers.ports.external_boot import (
    ActivationOwnership,
    ExternalBootActivationBinding,
    ExternalBootArtifactStager,
    ExternalBootMaterialization,
    ExternalBootPlan,
    KernelIdentity,
    MaterializedArtifacts,
    OpaqueProviderRef,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    BootArtifactVolumeConn,
    materialize_boot_artifacts,
)
from kdive.providers.shared.external_boot_bounds import (
    MAX_MODULE_ARCHIVE_BYTES,
    source_byte_limit,
)
from kdive.store.objectstore import ObjectStore

_TEMPORARY_METADATA_BYTES = 2 * 1_048_576


class ConcreteRemoteExternalBootMaterializer:
    """Validate exact object versions and publish deterministic remote volume names."""

    def __init__(
        self,
        *,
        object_store: ObjectStore,
        connection: Callable[[], AbstractContextManager[BootArtifactVolumeConn]],
        pool_name: str,
        capacity_bytes: int,
        monotonic: Callable[[], float],
        artifact_stager: ExternalBootArtifactStager,
    ) -> None:
        del object_store
        self._connection = connection
        self._pool_name = pool_name
        self._capacity_bytes = capacity_bytes
        self._monotonic = monotonic
        self._artifact_stager = artifact_stager

    def materialize(
        self,
        plan: ExternalBootPlan,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> ExternalBootMaterialization:
        """Materialize one authenticated activation without worker database access."""
        del authority  # authentication is consumed by the enclosing authority service lane
        if (binding.system_id, binding.run_id) != (
            plan.ownership.system_id,
            plan.ownership.run_id,
        ):
            raise ValueError("external-boot binding does not match plan ownership")
        initrd_bytes = 0 if plan.initrd is None else plan.initrd.size_bytes
        reservation = (
            plan.bundle.decoded_kernel_size_bytes
            + initrd_bytes
            + plan.module_obligation.uncompressed_bytes
            + plan.module_obligation.member_count * 1024
            + MAX_MODULE_ARCHIVE_BYTES * 2
            + source_byte_limit(plan.bundle)
            + _TEMPORARY_METADATA_BYTES
        )
        if reservation > self._capacity_bytes:
            raise ValueError("remote external-boot materialization exceeds configured capacity")
        self._require_deadline(deadline)
        with tempfile.TemporaryDirectory(prefix="kdive-remote-boot-") as temporary:
            directory = Path(temporary)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                evidence, installed_manifest = self._artifact_stager.materialize_artifacts(
                    plan, descriptor
                )
                kernel = directory / "kernel"
                initrd = None if plan.initrd is None else directory / "initrd"
                self._require_deadline(deadline)
                with self._connection() as connection:
                    artifacts = materialize_boot_artifacts(
                        connection,
                        self._pool_name,
                        system_id=UUID(binding.system_id),
                        run_id=UUID(binding.run_id),
                        kernel=kernel,
                        initrd=initrd,
                        max_bytes=plan.bundle.vmlinuz_size_bytes + initrd_bytes,
                    )
            finally:
                os.close(descriptor)
        return ExternalBootMaterialization(
            architecture=plan.architecture,
            provider_kind="remote-libvirt",
            ownership=ActivationOwnership(system_id=binding.system_id, run_id=binding.run_id),
            plan_identity=plan.identity,
            extracted_vmlinuz_sha256=str(evidence["vmlinuz_sha256"]),
            source_module_manifest=str(evidence["module_source_manifest"]),
            installed_module_tree=installed_manifest,
            verified_bundle_sha256=plan.bundle.sha256,
            verified_initrd_sha256=None if plan.initrd is None else plan.initrd.sha256,
            kernel_observation=KernelIdentity(
                architecture=plan.architecture,
                release=str(evidence["release"]),
                gnu_build_id=str(evidence["gnu_build_id"]),
            ),
            artifacts=MaterializedArtifacts(
                kernel=artifacts.kernel,
                modules=OpaqueProviderRef(
                    ref=f"bundle/{plan.bundle.sha256.removeprefix('sha256:')}"
                ),
                initrd=artifacts.initrd,
            ),
        )

    def _require_deadline(self, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise TimeoutError("remote external-boot materialization deadline expired")
