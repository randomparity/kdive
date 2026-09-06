"""Concrete remote authority-host external-boot preparation operations."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteExternalBootRecoveryRecord,
    RemoteModuleTerminalRecord,
)
from kdive.providers.remote_libvirt.external_boot_materialization import (
    ConcreteRemoteExternalBootMaterializer,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import prepare_target_definition
from kdive.providers.shared.runtime_paths import domain_name_for


class _Volume(Protocol):
    def path(self) -> str: ...


class _Pool(Protocol):
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802


class _Domain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802

    def isActive(self) -> int: ...  # noqa: N802


class RemoteExternalBootPreparationConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802

    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802


class ConcreteRemoteExternalBootOperations:
    """Materialize and prepare using only fixed provider-host dependencies."""

    def __init__(
        self,
        materializer: ConcreteRemoteExternalBootMaterializer,
        connection: Callable[[], AbstractContextManager[RemoteExternalBootPreparationConn]],
        pool_name: str,
        monotonic: Callable[[], float],
    ) -> None:
        self._materializer = materializer
        self._connection = connection
        self._pool_name = pool_name
        self._monotonic = monotonic

    def materialize(
        self,
        plan: ExternalBootPlan,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> ExternalBootMaterialization:
        return self._materializer.materialize(plan, binding, authority, deadline)

    def prepare(
        self,
        plan: ExternalBootPlan,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        modules: RemoteModuleTerminalRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> RemoteExternalBootRecoveryRecord:
        del authority
        if self._monotonic() >= deadline:
            raise TimeoutError("remote external-boot preparation deadline expired")
        modules.response.validate_terminal_for(modules.request.operation, modules.request.authority)
        if (
            str(modules.request.authority.activation_id) != binding.activation_id
            or str(modules.request.authority.system_id) != binding.system_id
            or str(modules.request.authority.run_id) != binding.run_id
            or modules.request.authority.plan_identity != plan.identity
        ):
            raise ValueError("remote module terminal record differs from activation binding")
        with self._connection() as connection:
            domain = connection.lookupByName(domain_name_for(UUID(binding.system_id)))
            source_xml = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
            prior_power = "running" if domain.isActive() else "inactive"
            pool = connection.storagePoolLookupByName(self._pool_name)

            def path(reference: OpaqueProviderRef) -> str:
                return pool.storageVolLookupByName(reference.ref).path()

            definition = prepare_target_definition(
                source_xml,
                plan=plan,
                materialization=materialization,
                binding=binding,
                pool=self._pool_name,
                overlay_path=path(modules.response.recovery.root_volume),
                kernel_path=path(materialization.artifacts.kernel),
                initrd_path=(
                    None
                    if materialization.artifacts.initrd is None
                    else path(materialization.artifacts.initrd)
                ),
            )
        result = modules.response.result
        if result.capture_absent:
            source_modules = AbsentComponentState()
        else:
            if result.capture_manifest is None:
                raise ValueError("remote module result omitted captured source manifest")
            source_modules = PresentComponentState(manifest=result.capture_manifest)
        objects = {
            materialization.artifacts.kernel,
            modules.response.recovery.source_volume,
            modules.response.recovery.scratch_volume,
        }
        if materialization.artifacts.initrd is not None:
            objects.add(materialization.artifacts.initrd)
        return RemoteExternalBootRecoveryRecord(
            binding=binding,
            plan_identity=plan.identity,
            materialization=materialization,
            definition=definition,
            module_recovery=modules.response.recovery,
            source_state=ProviderStateIdentity(
                definition=definition.source_definition, modules=source_modules
            ),
            target_state=ProviderStateIdentity(
                definition=definition.target_definition,
                modules=PresentComponentState(manifest=materialization.installed_module_tree),
            ),
            prior_power=prior_power,
            recovery_objects=tuple(sorted(objects, key=lambda value: value.to_canonical_json())),
        )
