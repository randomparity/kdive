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
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteExternalBootRecoveryRecord,
    RemoteModuleTerminalRecord,
    RemoteSystemTeardownInspection,
    RemoteSystemTeardownState,
)
from kdive.providers.remote_libvirt.external_boot_materialization import (
    ConcreteRemoteExternalBootMaterializer,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    RemoteExternalBootRecovery,
    _AgentRunner,
    activate_definition,
    boot_projection_identity,
    observe_guest_identity,
    parse_projected_domain_xml,
    prepare_target_definition,
    preserved_definition_identity,
    recover_disk_grub_baseline,
    require_disk_grub_source,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_name import (
    parse_boot_artifact_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    HostStatDeviceIdentity,
    RemoteDeviceIdentityPort,
    prove_no_foreign_storage_references,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    overlay_volume_name,
    supplied_base_volume_name,
)
from kdive.providers.shared.runtime_paths import domain_name_for


class _Volume(Protocol):
    def path(self) -> str: ...

    def name(self) -> str: ...

    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802


class _Domain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802

    def isActive(self) -> int: ...  # noqa: N802

    def name(self) -> str: ...

    def create(self) -> int: ...

    def destroy(self) -> int: ...

    def undefineFlags(self, flags: int = 0) -> int: ...  # noqa: N802

    def isPersistent(self) -> int: ...  # noqa: N802


class RemoteExternalBootPreparationConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802

    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802

    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802

    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...  # noqa: N802


class ConcreteRemoteExternalBootOperations:
    """Materialize and prepare using only fixed provider-host dependencies."""

    def __init__(
        self,
        materializer: ConcreteRemoteExternalBootMaterializer,
        connection: Callable[[], AbstractContextManager[RemoteExternalBootPreparationConn]],
        pool_name: str,
        monotonic: Callable[[], float],
        agent_exec: _AgentRunner,
        identity_port: RemoteDeviceIdentityPort | None = None,
    ) -> None:
        self._materializer = materializer
        self._connection = connection
        self._pool_name = pool_name
        self._monotonic = monotonic
        self._agent_exec = agent_exec
        self._identity_port = identity_port or HostStatDeviceIdentity()

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

    def _validate_owned_artifacts(
        self,
        connection: RemoteExternalBootPreparationConn,
        recovery: RemoteExternalBootRecoveryRecord,
    ) -> None:
        pool = connection.storagePoolLookupByName(self._pool_name)
        expected = {
            recovery.materialization.artifacts.kernel,
            recovery.module_recovery.source_volume,
            recovery.module_recovery.scratch_volume,
        }
        if recovery.materialization.artifacts.initrd is not None:
            expected.add(recovery.materialization.artifacts.initrd)
        if not expected.issubset(recovery.recovery_objects):
            raise ValueError("remote recovery omitted an owned private artifact")
        boot_references = [(recovery.materialization.artifacts.kernel, "kernel")]
        if recovery.materialization.artifacts.initrd is not None:
            boot_references.append((recovery.materialization.artifacts.initrd, "initrd"))
        for reference, kind in boot_references:
            parsed = parse_boot_artifact_name(reference.ref)
            digest = (
                recovery.materialization.extracted_vmlinuz_sha256
                if kind == "kernel"
                else recovery.materialization.verified_initrd_sha256
            )
            if (
                parsed is None
                or parsed.partial
                or parsed.kind != kind
                or str(parsed.system_id) != recovery.binding.system_id
                or str(parsed.run_id) != recovery.binding.run_id
                or parsed.digest != digest
            ):
                raise ValueError("remote recovery boot artifact identity differs")
        paths = {
            reference: pool.storageVolLookupByName(reference.ref).path() for reference in expected
        }
        for path in paths.values():
            if not path.startswith("/"):
                raise ValueError("remote recovery artifact path is not absolute")
        os_element = parse_projected_domain_xml(recovery.definition.target_xml).find("os")
        if os_element is None:
            raise ValueError("remote target definition omitted operating-system paths")
        recorded_kernel = os_element.findtext("kernel")
        recorded_initrd = os_element.findtext("initrd")
        if (
            recorded_kernel != paths[recovery.materialization.artifacts.kernel]
            or (recovery.materialization.artifacts.initrd is None and recorded_initrd is not None)
            or (
                recovery.materialization.artifacts.initrd is not None
                and recorded_initrd != paths[recovery.materialization.artifacts.initrd]
            )
        ):
            raise ValueError("remote recovery artifact paths differ from target definition")

    def activate(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None:
        del authority
        if self._monotonic() >= deadline:
            raise TimeoutError("remote external-boot activation deadline expired")
        with self._connection() as connection:
            self._validate_owned_artifacts(connection, recovery)
            self._require_deadline(deadline, "activation")
            activate_definition(
                connection,
                recovery.definition,
                lambda: self._require_deadline(deadline, "activation"),
            )

    def observe(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> RunningKernelObservation:
        del authority
        if self._monotonic() >= deadline:
            raise TimeoutError("remote external-boot observation deadline expired")
        with self._connection() as connection:
            self._validate_owned_artifacts(connection, recovery)
            domain = connection.lookupByName(domain_name_for(UUID(recovery.binding.system_id)))
            return observe_guest_identity(self._agent_exec, domain, recovery.definition)

    def recover(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None:
        del authority
        if self._monotonic() >= deadline:
            raise TimeoutError("remote external-boot recovery deadline expired")
        with self._connection() as connection:
            recover_disk_grub_baseline(
                connection,
                RemoteExternalBootRecovery(
                    definition=recovery.definition, prior_power=recovery.prior_power
                ),
            )

    @staticmethod
    def _lookup_optional(pool: _Pool, name: str) -> _Volume | None:
        try:
            return pool.storageVolLookupByName(name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return None
            raise

    def cleanup(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None:
        recovery.module_recovery.validate_authority(authority)
        self._require_deadline(deadline, "cleanup")
        with self._connection() as connection:
            boot_pool = connection.storagePoolLookupByName(self._pool_name)
            module_pool = connection.storagePoolLookupByName(recovery.module_recovery.pool.ref)
            for reference in (
                recovery.module_recovery.source_volume,
                recovery.module_recovery.scratch_volume,
            ):
                if self._lookup_optional(module_pool, reference.ref) is not None:
                    raise ValueError("remote module volume remains before authorized cleanup")
            boot = [recovery.materialization.artifacts.kernel]
            if recovery.materialization.artifacts.initrd is not None:
                boot.append(recovery.materialization.artifacts.initrd)
            for index, reference in enumerate(boot):
                kind = "kernel" if index == 0 else "initrd"
                parsed = parse_boot_artifact_name(reference.ref)
                digest = (
                    recovery.materialization.extracted_vmlinuz_sha256
                    if kind == "kernel"
                    else recovery.materialization.verified_initrd_sha256
                )
                if (
                    parsed is None
                    or parsed.partial
                    or parsed.kind != kind
                    or str(parsed.system_id) != recovery.binding.system_id
                    or str(parsed.run_id) != recovery.binding.run_id
                    or parsed.digest != digest
                ):
                    raise ValueError("remote cleanup boot artifact identity differs")
                self._require_deadline(deadline, "cleanup")
                volume = self._lookup_optional(boot_pool, reference.ref)
                if volume is None:
                    continue
                if volume.name() != reference.ref:
                    raise ValueError("remote cleanup volume name differs")
                volume.delete(0)
                if self._lookup_optional(boot_pool, reference.ref) is not None:
                    raise ValueError("remote cleanup volume remained after deletion")

    @staticmethod
    def _lookup_domain_optional(
        connection: RemoteExternalBootPreparationConn, system_id: str
    ) -> _Domain | None:
        try:
            return connection.lookupByName(domain_name_for(UUID(system_id)))
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise

    def _protected_teardown_volumes(
        self, state: RemoteSystemTeardownState
    ) -> frozenset[tuple[str, str]]:
        protected = {
            (self._pool_name, overlay_volume_name(state.binding.system_id)),
            (self._pool_name, supplied_base_volume_name(state.binding.system_id)),
        }
        materialization = state.materialization
        if materialization is not None:
            artifacts = materialization.materialization.artifacts
            protected.add((self._pool_name, artifacts.kernel.ref))
            if artifacts.initrd is not None:
                protected.add((self._pool_name, artifacts.initrd.ref))
        terminal = state.terminal
        if terminal is not None:
            module = terminal.response.recovery
            protected.update(
                {
                    (module.pool.ref, module.source_volume.ref),
                    (module.pool.ref, module.scratch_volume.ref),
                }
            )
        elif state.preparation is not None:
            operation = state.preparation.operation
            protected.update(
                {
                    (
                        self._pool_name,
                        render_module_volume_name(
                            operation.system_id,
                            operation.run_id,
                            operation.operation_nonce,
                            purpose,
                        ),
                    )
                    for purpose in ("source.ext4", "scratch.ext4")
                }
            )
        return frozenset(protected)

    def _teardown_boot_volumes(
        self, state: RemoteSystemTeardownState
    ) -> frozenset[tuple[str, str]]:
        materialization_record = state.materialization
        if materialization_record is None:
            return frozenset()
        materialization = materialization_record.materialization
        candidates = [(materialization.artifacts.kernel, "kernel")]
        if materialization.artifacts.initrd is not None:
            candidates.append((materialization.artifacts.initrd, "initrd"))
        boot: set[tuple[str, str]] = set()
        for reference, kind in candidates:
            parsed = parse_boot_artifact_name(reference.ref)
            digest = (
                materialization.extracted_vmlinuz_sha256
                if kind == "kernel"
                else materialization.verified_initrd_sha256
            )
            if (
                parsed is None
                or parsed.partial
                or parsed.kind != kind
                or str(parsed.system_id) != state.binding.system_id
                or str(parsed.run_id) != state.binding.run_id
                or parsed.digest != digest
            ):
                raise ValueError("remote teardown boot artifact identity differs")
            boot.add((self._pool_name, reference.ref))
        return frozenset(boot)

    def _validate_teardown_domain(
        self,
        connection: RemoteExternalBootPreparationConn,
        domain: _Domain,
        state: RemoteSystemTeardownState,
    ) -> None:
        documents = [domain.XMLDesc(0), domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)]
        recovery = state.recovery
        pool = connection.storagePoolLookupByName(self._pool_name)
        overlay = pool.storageVolLookupByName(overlay_volume_name(state.binding.system_id)).path()
        if recovery is None:
            for document in documents:
                require_disk_grub_source(
                    document,
                    system_id=UUID(state.binding.system_id),
                    pool=self._pool_name,
                    overlay_path=overlay,
                )
            return
        allowed = {
            (recovery.definition.source_definition, recovery.definition.source_boot),
            (recovery.definition.target_definition, recovery.definition.target_boot),
        }
        for document in documents:
            observed = (
                preserved_definition_identity(document),
                boot_projection_identity(document),
            )
            if observed not in allowed:
                raise ValueError("remote teardown domain identity differs from durable recovery")

    def _prove_teardown_graph(
        self, connection: RemoteExternalBootPreparationConn, state: RemoteSystemTeardownState
    ) -> None:
        prove_no_foreign_storage_references(
            connection,
            self._identity_port,
            state.binding.system_id,
            self._protected_teardown_volumes(state),
        )

    def validate_system_teardown(self, state: RemoteSystemTeardownState) -> None:
        with self._connection() as connection:
            domain = self._lookup_domain_optional(connection, state.binding.system_id)
            if domain is None:
                raise ValueError("remote teardown cannot validate an absent System domain")
            self._validate_teardown_domain(connection, domain, state)
            self._prove_teardown_graph(connection, state)

    def destroy_system_domain(self, state: RemoteSystemTeardownState) -> None:
        with self._connection() as connection:
            domain = self._lookup_domain_optional(connection, state.binding.system_id)
            if domain is None:
                return
            self._validate_teardown_domain(connection, domain, state)
            self._prove_teardown_graph(connection, state)
            if domain.isActive():
                domain.destroy()
            if domain.isActive():
                raise RuntimeError("remote System domain remained active after destroy")

    def undefine_system_domain(self, state: RemoteSystemTeardownState) -> None:
        with self._connection() as connection:
            domain = self._lookup_domain_optional(connection, state.binding.system_id)
            if domain is None:
                return
            if domain.isActive():
                raise ValueError("remote System domain became active before undefine")
            self._validate_teardown_domain(connection, domain, state)
            self._prove_teardown_graph(connection, state)
            try:
                domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                    raise
            if self._lookup_domain_optional(connection, state.binding.system_id) is not None:
                raise RuntimeError("remote System domain remained after undefine")

    def _teardown_volume_absent(
        self, connection: RemoteExternalBootPreparationConn, pool_name: str, name: str
    ) -> bool:
        pool = connection.storagePoolLookupByName(pool_name)
        return self._lookup_optional(pool, name) is None

    def inspect_system_teardown(
        self, state: RemoteSystemTeardownState, *, domain_validated: bool
    ) -> RemoteSystemTeardownInspection:
        with self._connection() as connection:
            domain = self._lookup_domain_optional(connection, state.binding.system_id)
            if domain is not None:
                self._validate_teardown_domain(connection, domain, state)
            elif not domain_validated:
                return RemoteSystemTeardownInspection(False, False, False, False)
            protected = self._protected_teardown_volumes(state)
            overlay = (self._pool_name, overlay_volume_name(state.binding.system_id))
            baseline = (self._pool_name, supplied_base_volume_name(state.binding.system_id))
            recovery = protected - {overlay, baseline}
            return RemoteSystemTeardownInspection(
                domain_absent=domain is None,
                overlay_absent=self._teardown_volume_absent(connection, *overlay),
                baseline_absent=self._teardown_volume_absent(connection, *baseline),
                recovery_absent=all(
                    self._teardown_volume_absent(connection, *volume) for volume in recovery
                ),
            )

    def unfinished_preparation_absent(self, state: RemoteSystemTeardownState) -> bool:
        preparation = state.preparation
        if preparation is None:
            return True
        operation = preparation.operation
        names = (
            render_module_volume_name(
                operation.system_id,
                operation.run_id,
                operation.operation_nonce,
                "source.ext4",
            ),
            render_module_volume_name(
                operation.system_id,
                operation.run_id,
                operation.operation_nonce,
                "scratch.ext4",
            ),
        )
        with self._connection() as connection:
            self._prove_teardown_graph(connection, state)
            return all(
                self._teardown_volume_absent(connection, self._pool_name, name) for name in names
            )

    def remove_system_artifacts(self, state: RemoteSystemTeardownState) -> None:
        with self._connection() as connection:
            if self._lookup_domain_optional(connection, state.binding.system_id) is not None:
                raise ValueError("remote System domain remains before storage deletion")
            self._prove_teardown_graph(connection, state)
            protected = self._protected_teardown_volumes(state)
            overlay = (self._pool_name, overlay_volume_name(state.binding.system_id))
            baseline = (self._pool_name, supplied_base_volume_name(state.binding.system_id))
            recovery = protected - {overlay, baseline}
            if any(not self._teardown_volume_absent(connection, *volume) for volume in recovery):
                allowed_boot = self._teardown_boot_volumes(state)
                remaining_non_boot = recovery - allowed_boot
                if any(
                    not self._teardown_volume_absent(connection, *volume)
                    for volume in remaining_non_boot
                ):
                    raise ValueError(
                        "remote module recovery remains before System storage deletion"
                    )
                for pool_name, name in sorted(allowed_boot):
                    pool = connection.storagePoolLookupByName(pool_name)
                    volume = self._lookup_optional(pool, name)
                    if volume is not None:
                        if volume.name() != name:
                            raise ValueError("remote boot artifact name differs before deletion")
                        volume.delete(0)
            for pool_name, name in (overlay, baseline):
                pool = connection.storagePoolLookupByName(pool_name)
                volume = self._lookup_optional(pool, name)
                if volume is not None:
                    if volume.name() != name:
                        raise ValueError("remote System volume name differs before deletion")
                    volume.delete(0)
                if self._lookup_optional(pool, name) is not None:
                    raise RuntimeError("remote System volume remained after deletion")
            if any(not self._teardown_volume_absent(connection, *volume) for volume in protected):
                raise RuntimeError("remote System storage remained after deletion")

    def _require_deadline(self, deadline: float, operation: str) -> None:
        if self._monotonic() >= deadline:
            raise TimeoutError(f"remote external-boot {operation} deadline expired")
