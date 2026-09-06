"""Closed remote external-boot coordinator contracts (#2200)."""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import libvirt
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr, ValidationError

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptWorkerWriteContext,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCleanupEvidenceContextV1,
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryObjectBinding,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt import external_boot_materialization as materialization_module
from kdive.providers.remote_libvirt.external_boot_authority import (
    AdmittedRemoteModulePreparation,
    DurableRemoteModuleVolumePreparationHost,
    RemoteExternalBootAuthorityAdapter,
    RemoteExternalBootCoordinator,
    RemoteExternalBootOperations,
    RemoteExternalBootRecoveryRecord,
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationHost,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationResponseV1,
    RemoteModuleVolumePreparationStore,
)
from kdive.providers.remote_libvirt.external_boot_materialization import (
    ConcreteRemoteExternalBootMaterializer,
)
from kdive.providers.remote_libvirt.external_boot_operations import (
    ConcreteRemoteExternalBootOperations,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import prepare_target_definition
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    MaterializedBootArtifacts,
    artifact_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    PreparedModuleVolumes,
    PreparedVolume,
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.recovery_objects import RemoteExternalBootRecoveryObjects
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.services.remote_module_authority_preparation import (
    RemoteModulePreparationInputs,
    execute_remote_module_lifecycle_on_authority_host,
    prepare_remote_module_on_authority_host,
)
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.providers.remote_libvirt.lifecycle.external_boot_support import (
    _FakeAgentExec,
    _materialization,
    _plan,
    _replies,
    _source_xml,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    operation as module_operation,
)
from tests.support.external_boot_plan import external_boot_plan


def test_concrete_remote_materializer_binds_and_publishes_private_volume_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    binding = ExternalBootActivationBinding(
        system_id=plan.ownership.system_id,
        run_id=plan.ownership.run_id,
        activation_id=str(uuid4()),
    )
    calls: list[tuple[UUID, UUID, str]] = []

    class Connection:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            del args

    class Stager:
        def materialize_artifacts(
            self, plan: ExternalBootPlan, directory_fd: int
        ) -> tuple[dict[str, object], str]:
            Path(f"/proc/self/fd/{directory_fd}/kernel").write_bytes(b"kernel")
            return (
                {
                    "vmlinuz_sha256": plan.bundle.vmlinuz_sha256,
                    "module_source_manifest": plan.module_obligation.source_manifest,
                    "release": plan.module_obligation.release,
                    "gnu_build_id": "01020304",
                },
                "sha256:" + "1" * 64,
            )

    def upload(
        connection: object,
        pool: str,
        *,
        system_id: UUID,
        run_id: UUID,
        kernel: Path,
        initrd: Path | None,
        max_bytes: int,
    ) -> MaterializedBootArtifacts:
        del connection, kernel, initrd, max_bytes
        calls.append((system_id, run_id, pool))
        return MaterializedBootArtifacts(
            kernel=OpaqueProviderRef(ref="volumes/exact-private-kernel"),
            initrd=OpaqueProviderRef(ref="volumes/exact-private-initrd"),
        )

    monkeypatch.setattr(materialization_module, "materialize_boot_artifacts", upload)
    materializer = ConcreteRemoteExternalBootMaterializer(
        object_store=cast(Any, object()),
        connection=cast(Any, Connection),
        pool_name="boot-pool",
        capacity_bytes=64 * 1024**3,
        monotonic=lambda: 1.0,
        artifact_stager=Stager(),
    )
    result = materializer.materialize(
        plan, binding, OpaqueProviderRef(ref="authority/current"), 2.0
    )

    assert result.artifacts.kernel.ref == "volumes/exact-private-kernel"
    assert result.plan_identity == plan.identity
    assert result.ownership.system_id == binding.system_id
    assert result.ownership.run_id == binding.run_id
    assert calls == [(UUID(binding.system_id), UUID(binding.run_id), "boot-pool")]


def test_concrete_remote_materializer_rejects_capacity_and_foreign_binding() -> None:
    plan = _plan()
    materializer = ConcreteRemoteExternalBootMaterializer(
        object_store=cast(Any, object()),
        connection=cast(Any, lambda: None),
        pool_name="boot-pool",
        capacity_bytes=1,
        monotonic=lambda: 1.0,
        artifact_stager=cast(Any, object()),
    )
    binding = ExternalBootActivationBinding(
        system_id=plan.ownership.system_id,
        run_id=plan.ownership.run_id,
        activation_id=str(uuid4()),
    )
    with pytest.raises(ValueError, match="configured capacity"):
        materializer.materialize(plan, binding, OpaqueProviderRef(ref="authority/current"), 2.0)
    foreign = binding.model_copy(update={"run_id": str(uuid4())})
    with pytest.raises(ValueError, match="binding"):
        materializer.materialize(plan, foreign, OpaqueProviderRef(ref="authority/current"), 2.0)


def test_concrete_remote_prepare_captures_source_before_deriving_durable_recovery(
    tmp_path: Path,
) -> None:
    expected = _record()
    plan = _plan_for_record(expected)
    store = RemoteModuleVolumePreparationStore(tmp_path)
    _publish_terminal(store, expected)
    modules = store.reopen_terminal(expected.binding, plan.identity)
    actions: list[str] = []

    class Volume:
        def __init__(self, name: str) -> None:
            self._name = name

        def path(self) -> str:
            actions.append(f"path:{self._name}")
            if self._name == "root":
                return "/pool/overlay.qcow2"
            return f"/var/lib/libvirt/images/{self._name}"

    class Pool:
        def storageVolLookupByName(self, name: str) -> Volume:
            return Volume(name)

    class Domain:
        def XMLDesc(self, flags: int = 0) -> str:
            assert flags == libvirt.VIR_DOMAIN_XML_INACTIVE
            actions.append("source")
            return _source_xml(system_id=UUID(expected.binding.system_id), pool="modules")

        def isActive(self) -> int:
            actions.append("power")
            return 1

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def lookupByName(self, name: str) -> Domain:
            actions.append(f"domain:{name}")
            return Domain()

        def storagePoolLookupByName(self, name: str) -> Pool:
            assert name == "modules"
            actions.append("pool")
            return Pool()

    operations = ConcreteRemoteExternalBootOperations(
        cast(Any, object()), cast(Any, Connection), "modules", lambda: 1.0, cast(Any, object())
    )
    recovery = operations.prepare(
        plan,
        expected.materialization.model_copy(update={"plan_identity": plan.identity}),
        expected.binding,
        modules,
        OpaqueProviderRef(ref="authority/current"),
        2.0,
    )

    assert actions.index("source") < actions.index("pool")
    assert recovery.prior_power == "running"
    assert recovery.module_recovery == modules.response.recovery
    assert recovery.materialization.artifacts.kernel in recovery.recovery_objects
    assert recovery.module_recovery.source_volume in recovery.recovery_objects
    store.close()


def test_concrete_remote_activate_replays_target_and_observes_running_kernel() -> None:
    recovery = _record()
    kernel = OpaqueProviderRef(
        ref=artifact_volume_name(
            "kernel",
            UUID(recovery.binding.system_id),
            UUID(recovery.binding.run_id),
            recovery.materialization.extracted_vmlinuz_sha256,
        )
    )
    initrd = (
        None
        if recovery.materialization.verified_initrd_sha256 is None
        else OpaqueProviderRef(
            ref=artifact_volume_name(
                "initrd",
                UUID(recovery.binding.system_id),
                UUID(recovery.binding.run_id),
                recovery.materialization.verified_initrd_sha256,
            )
        )
    )
    recovery = recovery.model_copy(
        update={
            "materialization": recovery.materialization.model_copy(
                update={
                    "artifacts": recovery.materialization.artifacts.model_copy(
                        update={"kernel": kernel, "initrd": initrd}
                    )
                }
            )
        }
    )
    owned = set(recovery.recovery_objects)
    owned.add(recovery.materialization.artifacts.kernel)
    if recovery.materialization.artifacts.initrd is not None:
        owned.add(recovery.materialization.artifacts.initrd)
    recovery = recovery.model_copy(
        update={
            "recovery_objects": tuple(sorted(owned, key=lambda value: value.to_canonical_json()))
        }
    )

    deleted: set[str] = set()
    looked_up: list[str] = []
    modules_absent = False

    def absent() -> libvirt.libvirtError:
        error = libvirt.libvirtError("absent")
        error.err = (libvirt.VIR_ERR_NO_STORAGE_VOL, 0, "absent", 0, "", "", "", 0, 0)
        return error

    class Volume:
        def __init__(self, name: str) -> None:
            self._name = name

        def path(self) -> str:
            if self._name == recovery.materialization.artifacts.kernel.ref:
                return "/artifacts/kernel"
            if recovery.materialization.artifacts.initrd is not None and (
                self._name == recovery.materialization.artifacts.initrd.ref
            ):
                return "/artifacts/initrd"
            return "/pool/owned"

        def name(self) -> str:
            return self._name

        def delete(self, flags: int = 0) -> int:
            del flags
            deleted.add(self._name)
            return 0

    class Pool:
        def storageVolLookupByName(self, name: str) -> Volume:
            looked_up.append(name)
            assert OpaqueProviderRef(ref=name) in recovery.recovery_objects
            if name in deleted or (
                modules_absent
                and name
                in {
                    recovery.module_recovery.source_volume.ref,
                    recovery.module_recovery.scratch_volume.ref,
                }
            ):
                raise absent()
            return Volume(name)

    class Domain:
        def __init__(self) -> None:
            self.xml = recovery.definition.source_xml
            self.active = False
            self.creates = 0

        def name(self) -> str:
            return domain_name_for(UUID(recovery.binding.system_id))

        def XMLDesc(self, flags: int = 0) -> str:
            del flags
            return self.xml

        def isActive(self) -> int:
            return int(self.active)

        def create(self) -> int:
            self.active = True
            self.creates += 1
            return 0

        def destroy(self) -> int:
            self.active = False
            return 0

    domain = Domain()

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def lookupByName(self, name: str) -> Domain:
            assert name == domain.name()
            return domain

        def storagePoolLookupByName(self, name: str) -> Pool:
            assert name in {"modules", recovery.module_recovery.pool.ref}
            return Pool()

        def defineXML(self, xml: str) -> Domain:
            domain.xml = xml
            return domain

    agent = _FakeAgentExec(
        _replies(
            release=(recovery.materialization.kernel_observation.release + "\n").encode(),
            cmdline=(recovery.definition.expected_cmdline + "\n").encode(),
        )
    )
    operations = ConcreteRemoteExternalBootOperations(
        cast(Any, object()), cast(Any, Connection), "modules", lambda: 1.0, agent
    )
    authority = OpaqueProviderRef(ref="authority/current")
    operations.activate(recovery, authority, 2.0)
    operations.activate(recovery, authority, 2.0)
    observation = operations.observe(recovery, authority, 2.0)

    assert domain.creates == 1
    assert observation.identity == recovery.materialization.kernel_observation
    assert observation.cmdline == recovery.definition.expected_cmdline.encode()
    operations.recover(recovery, authority, 2.0)
    operations.recover(recovery, authority, 2.0)
    assert domain.xml == recovery.definition.source_xml
    assert not domain.active

    incomplete = recovery.model_copy(
        update={
            "recovery_objects": tuple(
                item
                for item in recovery.recovery_objects
                if item != recovery.materialization.artifacts.kernel
            )
        }
    )
    with pytest.raises(ValueError, match="omitted an owned private artifact"):
        operations.activate(incomplete, authority, 2.0)
    assert domain.creates == 1

    clock = iter((1.0, 3.0))
    expiring = ConcreteRemoteExternalBootOperations(
        cast(Any, object()), cast(Any, Connection), "modules", lambda: next(clock), agent
    )
    with pytest.raises(TimeoutError, match="activation deadline"):
        expiring.activate(recovery, authority, 2.0)
    assert domain.creates == 1

    recovery = recovery.model_copy(
        update={
            "module_recovery": recovery.module_recovery.model_copy(
                update={
                    "authority_identity": RemoteModuleRecoveryRefV2.identity_for_authority(
                        authority
                    )
                }
            )
        }
    )
    modules_absent = True
    assert recovery.materialization.artifacts.initrd is not None
    operations.cleanup(recovery, authority, 2.0)
    operations.cleanup(recovery, authority, 2.0)
    assert deleted == {
        recovery.materialization.artifacts.kernel.ref,
        recovery.materialization.artifacts.initrd.ref,
    }
    assert "kdive-sibling-volume" not in looked_up


@pytest.mark.anyio
async def test_remote_adapter_replays_materialize_and_prepare_without_repeating_mutation(
    tmp_path: Path,
) -> None:
    record = _record()
    plan = _plan_for_record(record)
    store = RemoteModuleVolumePreparationStore(tmp_path)
    _publish_terminal(store, record)
    terminal = store.reopen_terminal(record.binding, plan.identity)
    calls: list[str] = []

    class Operations:
        def materialize(
            self, plan: object, binding: object, owner: object, deadline: float
        ) -> object:
            del plan, binding, owner, deadline
            calls.append("materialize")
            return record.materialization.model_copy(
                update={"plan_identity": terminal.request.authority.plan_identity}
            )

        def prepare(self, *args: object) -> RemoteExternalBootRecoveryRecord:
            calls.append("prepare")
            return record.model_copy(
                update={
                    "plan_identity": terminal.request.authority.plan_identity,
                    "materialization": record.materialization.model_copy(
                        update={"plan_identity": terminal.request.authority.plan_identity}
                    ),
                }
            )

    class Delegate:
        async def observe(self, request: object) -> object:
            raise AssertionError(request)

        async def commit(self, request: object, context: object) -> object:
            raise AssertionError((request, context))

    coordinator = RemoteExternalBootCoordinator(cast(Any, Operations()), store, lambda: 9.0)
    executor = RemoteModulePreparationExecutor()
    adapter = RemoteExternalBootAuthorityAdapter(cast(Any, Delegate()), coordinator, executor)
    prepare_request = terminal.request.authority
    materialize_request = prepare_request.model_copy(
        update={
            "operation": AuthorityOperation.MATERIALIZE,
            "operation_identity": "materialize-path",
            "attempt_id": uuid4(),
        }
    )

    def context(request: AuthorityPreparationMutationRequestV1) -> AuthorityCommitContextV1:
        return AuthorityCommitContextV1(
            commit_point=request.operation,
            operation_identity=request.operation_identity,
            attempt_id=request.attempt_id,
            journal_sequence=1,
            journal_digest="sha256:" + "a" * 64,
        )

    first_m = await adapter.commit(materialize_request, context(materialize_request))
    assert await adapter.commit(materialize_request, context(materialize_request)) == first_m
    first_p = await adapter.commit(prepare_request, context(prepare_request))
    assert await adapter.commit(prepare_request, context(prepare_request)) == first_p
    assert calls == ["materialize", "prepare"]
    assert await adapter.preparation_receipt(prepare_request) is not None
    executor.shutdown()
    store.close()


@pytest.mark.anyio
async def test_remote_cleanup_changed_nonce_fails_before_provider_mutation() -> None:
    record = _record()
    calls: list[str] = []

    class Coordinator:
        def recovery_point(self, binding: object, plan_identity: str) -> RecoveryPoint:
            del binding, plan_identity
            calls.append("point")
            return RecoveryPoint(
                binding=record.binding,
                plan_identity=record.plan_identity,
                materialization_identity=record.materialization.identity,
                recovery_ref=OpaqueProviderRef(ref="remote/recovery"),
                source_state=record.source_state,
                target_state=record.target_state,
            )

        def recovery_record(self, point: object) -> RemoteExternalBootRecoveryRecord:
            del point
            calls.append("record")
            return record

        def recover(self, point: object, authority: object) -> None:
            del point, authority
            calls.append("recover")

        def cleanup(self, point: object, authority: object) -> None:
            del point, authority
            calls.append("cleanup")

    class Delegate:
        async def observe(self, request: object) -> object:
            raise AssertionError(request)

        async def commit(self, request: object, context: object) -> object:
            raise AssertionError((request, context))

    request = AuthorityMutationRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=UUID(record.binding.system_id),
        activation_id=UUID(record.binding.activation_id),
        run_id=UUID(record.binding.run_id),
        plan_identity=record.plan_identity,
        purpose="recover",
        operation="recover",
        provider_kind="remote-libvirt",
        authority_instance="remote-a",
        operation_identity="recover-cleanup",
        operation_digest="sha256:" + "c" * 64,
        attempt_id=uuid4(),
        expected_source_identity=record.source_state.definition,
        intended_target_identity=record.target_state.definition,
        recovery_objects=(),
    )
    context = AuthorityCommitContextV1(
        commit_point=AuthorityOperation.RECOVER,
        operation_identity=request.operation_identity,
        attempt_id=request.attempt_id,
        journal_sequence=1,
        journal_digest="sha256:" + "a" * 64,
    )
    evidence = AuthorityCleanupEvidenceContextV1(
        operation_identity=request.operation_identity,
        attempt_id=request.attempt_id,
        operation_nonce="f" * 32,
        cleanup_state="open",
        recovery_reference_json=record.module_recovery.model_dump_json(),
    )
    executor = RemoteModulePreparationExecutor()
    adapter = RemoteExternalBootAuthorityAdapter(
        cast(Any, Delegate()), cast(Any, Coordinator()), executor
    )

    with pytest.raises(ValueError, match="changed before provider deletion"):
        await adapter.commit_cleanup(request, context, evidence)
    assert calls == ["point", "record"]
    executor.shutdown()


def _remote_preparation_request() -> RemoteModuleVolumePreparationRequestV1:
    operation = module_operation()
    system_id = UUID(operation.system_id)
    run_id = UUID(operation.run_id)
    plan = external_boot_plan(system_id, run_id)
    operation = operation.model_copy(
        update={
            "plan_identity": plan.identity,
            "release": plan.module_obligation.release,
            "source_manifest": plan.module_obligation.source_manifest,
        }
    )
    authority = AuthorityPreparationMutationRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=system_id,
        activation_id=uuid4(),
        run_id=run_id,
        plan_identity=plan.identity,
        purpose="activate",
        operation="prepare",
        provider_kind="remote-libvirt",
        authority_instance="remote-a",
        operation_identity="prepare-op",
        operation_digest="sha256:" + "c" * 64,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )
    return RemoteModuleVolumePreparationRequestV1(authority=authority, operation=operation)


def _prepared_volumes(request: RemoteModuleVolumePreparationRequestV1) -> PreparedModuleVolumes:
    common = {
        "pool": "modules",
        "system_id": request.operation.system_id,
        "run_id": request.operation.run_id,
        "operation_nonce": request.operation.operation_nonce,
    }
    return PreparedModuleVolumes(
        source=PreparedVolume(
            **common,
            name=render_module_volume_name(
                request.operation.system_id,
                request.operation.run_id,
                request.operation.operation_nonce,
                "source.ext4",
            ),
            purpose="source",
            digest=request.operation.source_manifest,
            capacity_bytes=4096,
        ),
        scratch=PreparedVolume(
            **common,
            name=render_module_volume_name(
                request.operation.system_id,
                request.operation.run_id,
                request.operation.operation_nonce,
                "scratch.ext4",
            ),
            purpose="scratch",
            digest="sha256:" + "0" * 64,
            capacity_bytes=8192,
        ),
    )


def _terminal_response(
    request: RemoteModuleVolumePreparationRequestV1,
) -> RemoteModuleTerminalPreparationResponseV1:
    operation = request.operation
    volumes = _prepared_volumes(request)
    result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=operation.system_id,
        run_id=operation.run_id,
        plan_identity=operation.plan_identity,
        operation_nonce=operation.operation_nonce,
        appliance_image_digest=operation.appliance_image_digest,
        release=operation.release,
        root_volume_key=operation.root_volume.key,
        root_volume_identity=operation.root_volume.identity,
        source_manifest=operation.source_manifest,
        installed_manifest=operation.source_manifest,
        capture_absent=True,
        entry_count=1,
        content_bytes=3,
    )
    authority = request.authority
    authority_ref = OpaqueProviderRef(
        ref=f"authority/{authority.authority_id}/{authority.generation}/{authority.attempt_id}"
    )
    base = RemoteModuleVolumePreparationResponseV1.from_prepared(volumes)
    return RemoteModuleTerminalPreparationResponseV1(
        source=base.source,
        scratch=base.scratch,
        result=result,
        recovery=RemoteModuleRecoveryRefV2(
            system_id=operation.system_id,
            run_id=operation.run_id,
            plan_identity=operation.plan_identity,
            operation_nonce=operation.operation_nonce,
            pool=OpaqueProviderRef(ref=volumes.source.pool),
            root_volume=OpaqueProviderRef(ref=operation.root_volume.key),
            source_volume=OpaqueProviderRef(ref=volumes.source.name),
            scratch_volume=OpaqueProviderRef(ref=volumes.scratch.name),
            source_capacity_bytes=volumes.source.capacity_bytes,
            operation_identity=identity_for(operation),
            result_identity=identity_for(result),
            installed_entry_count=1,
            installed_content_bytes=3,
            appliance_image_digest=operation.appliance_image_digest,
            authority_identity=RemoteModuleRecoveryRefV2.identity_for_authority(authority_ref),
        ),
    )


def _module_lifecycle_request(
    request: RemoteModuleVolumePreparationRequestV1,
    *,
    purpose: str = "recover",
    operation: str = "recover",
    action: Literal["restore", "reap"] = "restore",
) -> RemoteModuleLifecycleRequestV1:
    authority = request.authority
    mutation = AuthorityMutationRequestV1.model_validate(
        {
            **authority.model_dump(
                mode="python",
                by_alias=True,
                exclude={"plan", "purpose", "operation", "operation_identity"},
            ),
            "purpose": purpose,
            "operation": operation,
            "operation_identity": f"{operation}-module-op",
        }
    )
    return RemoteModuleLifecycleRequestV1(
        authority=mutation,
        action=action,
        budget_seconds=30,
    )


def _restored_response(
    request: RemoteModuleVolumePreparationRequestV1,
) -> RemoteModuleLifecycleResponseV1:
    terminal = _terminal_response(request)
    capture = request.operation
    operation = RemoteModuleOperationV1(
        operation="restore",
        system_id=capture.system_id,
        run_id=capture.run_id,
        plan_identity=capture.plan_identity,
        operation_nonce=capture.operation_nonce,
        release=capture.release,
        root_volume=capture.root_volume,
        source_manifest=capture.source_manifest,
        capture_absent=True,
        installed_manifest=capture.source_manifest,
        appliance_image_digest=capture.appliance_image_digest,
    )
    result = terminal.result.model_copy(
        update={"phase": "restored", "entry_count": None, "content_bytes": None}
    )
    return RemoteModuleLifecycleResponseV1(
        action="restore",
        recovery=terminal.recovery,
        operation=operation,
        result=result,
        volumes_absent=False,
    )


async def _seed_remote_attempt(
    conn: psycopg.AsyncConnection, request: RemoteModuleVolumePreparationRequestV1
) -> ModuleAttempt:
    resource_id, allocation_id, investigation_id = uuid4(), uuid4(), uuid4()
    authority = request.authority
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'remote-libvirt', 'default', 'standard', 'available', "
        "'qemu+tls://example.invalid/system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj')",
        (authority.system_id, allocation_id),
    )
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES "
        "(%s, %s, %s, 'remote-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (authority.run_id, investigation_id, authority.system_id),
    )
    return ModuleAttempt(authority.system_id, authority.run_id, request.operation.operation_nonce)


def test_lost_prep_reply_keeps_real_verifier_lock_until_matching_completion(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        request = _remote_preparation_request()
        response = _terminal_response(request)
        backing = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed_remote_attempt(admin, request)
            await backing.open_mutation_obligation(admin, attempt)

        class Repository:
            async def attempt_is_preparable(self, conn: object, candidate: ModuleAttempt) -> bool:
                return await backing.attempt_is_preparable(cast(Any, conn), candidate)

            async def mutation_obligation_is_open(
                self, conn: object, candidate: ModuleAttempt
            ) -> bool:
                return await backing.mutation_obligation_is_open(cast(Any, conn), candidate)

            async def read_terminal_evidence(
                self, _conn: object, _candidate: ModuleAttempt
            ) -> None:
                return None

            async def worker_record_terminal_evidence(self, *_args: object) -> bool:
                return True

        preparation = ModuleAttemptPreparationRequestV1(
            module_attempt_obligation=ModuleAttemptObligationReceiptV1(
                system_id=attempt.system_id,
                run_id=attempt.run_id,
                operation_nonce=attempt.operation_nonce,
            )
        )
        host_release = asyncio.Event()
        retry_refused = asyncio.Event()
        observer_task: asyncio.Task[object] | None = None
        calls = 0

        class Sender:
            async def open_remote_module_attempt(
                self, _begin: object, *, deadline: float
            ) -> RemoteModulePreparationBeginResponseV1:
                assert deadline > asyncio.get_running_loop().time()
                return RemoteModulePreparationBeginResponseV1(
                    preparation=preparation, operation=request.operation
                )

            async def execute_remote_module_preparation(
                self, candidate: RemoteModuleVolumePreparationRequestV1, *, deadline: float
            ) -> RemoteModuleTerminalPreparationResponseV1:
                nonlocal calls, observer_task
                assert candidate == request
                assert deadline > asyncio.get_running_loop().time()
                observer_task = cast(asyncio.Task[object], asyncio.current_task())
                calls += 1
                if calls == 1:
                    raise TimeoutError
                if not host_release.is_set():
                    retry_refused.set()
                    raise CategorizedError(
                        "authority: provider-conflict", category=ErrorCategory.CONFLICT
                    )
                return response

        executor = RemoteModulePreparationExecutor()
        worker = AsyncConnectionPool(
            authority_role_dsns("kdive_worker"), min_size=1, max_size=1, open=False
        )
        await worker.open()
        try:
            task = asyncio.create_task(
                prepare_remote_module_on_authority_host(
                    pool=worker,
                    repository=cast(Any, Repository()),
                    sender=cast(Any, Sender()),
                    inputs=RemoteModulePreparationInputs(authority=request.authority),
                    executor=executor,
                    job_id=uuid4(),
                    job_attempt=1,
                    incarnation_credential=SecretStr("worker-credential"),
                    deadline=asyncio.get_running_loop().time() + 10,
                )
            )
            await asyncio.wait_for(retry_refused.wait(), 1)
            assert not task.done()
            assert observer_task is not None
            task.cancel("worker shutdown")
            await asyncio.sleep(0)
            task.cancel("later cancellation")
            observer_task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            async with await psycopg.AsyncConnection.connect(migrated_url) as contender:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    async with contender.transaction():
                        await contender.execute("SET LOCAL lock_timeout = '100ms'")
                        await backing.discharge_mutation_obligation(
                            contender, attempt, reason="restored"
                        )
            host_release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 2)
            assert caught.value.args == ("worker shutdown",)
            assert task.cancelling() == 2
            async with await psycopg.AsyncConnection.connect(migrated_url) as contender:
                assert await backing.discharge_mutation_obligation(
                    contender, attempt, reason="restored"
                )
        finally:
            host_release.set()
            await worker.close()
            executor.shutdown()

    asyncio.run(run())


def test_asyncio_run_shutdown_waits_for_late_prep_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _remote_preparation_request()
    response = _terminal_response(request)
    attempt = ModuleAttempt(
        request.authority.system_id,
        request.authority.run_id,
        request.operation.operation_nonce,
    )
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=attempt.system_id,
            run_id=attempt.run_id,
            operation_nonce=attempt.operation_nonce,
        )
    )
    second_call_started = threading.Event()
    post_shutdown_call_started = threading.Event()
    host_release = threading.Event()
    host_completed = threading.Event()
    runner_returned = threading.Event()
    release_failures: list[str] = []
    calls = 0

    async def verify(
        _pool: object,
        _repository: object,
        _request: object,
        candidate: ModuleAttempt,
        consumer: Any,
        **_kwargs: object,
    ) -> object:
        assert candidate == attempt
        return await consumer(candidate)

    monkeypatch.setattr(
        "kdive.services.remote_module_volume_preparation.run_verified_module_attempt_preparation",
        verify,
    )

    class Sender:
        async def open_remote_module_attempt(
            self, _begin: object, *, deadline: float
        ) -> RemoteModulePreparationBeginResponseV1:
            assert deadline > asyncio.get_running_loop().time()
            return RemoteModulePreparationBeginResponseV1(
                preparation=preparation,
                operation=request.operation,
            )

        async def execute_remote_module_preparation(
            self, candidate: RemoteModuleVolumePreparationRequestV1, *, deadline: float
        ) -> RemoteModuleTerminalPreparationResponseV1:
            nonlocal calls
            assert candidate == request
            assert deadline > asyncio.get_running_loop().time()
            calls += 1
            if calls == 1:
                raise TimeoutError
            if calls == 2:
                second_call_started.set()
                await asyncio.Event().wait()
                raise AssertionError("cancelled transport call unexpectedly resumed")
            post_shutdown_call_started.set()
            while not host_release.is_set():
                await asyncio.sleep(0.01)
            host_completed.set()
            return response

    def release_host_after_shutdown() -> None:
        if not post_shutdown_call_started.wait(2):
            release_failures.append("authority completion was not observed after shutdown")
        elif runner_returned.is_set():
            release_failures.append("asyncio.run returned before authority completion")
        host_release.set()

    executor = RemoteModulePreparationExecutor()

    async def scenario() -> None:
        asyncio.create_task(
            prepare_remote_module_on_authority_host(
                pool=cast(Any, object()),
                repository=cast(Any, object()),
                sender=cast(Any, Sender()),
                inputs=RemoteModulePreparationInputs(authority=request.authority),
                executor=executor,
                job_id=uuid4(),
                job_attempt=1,
                incarnation_credential=SecretStr("worker-credential"),
                deadline=asyncio.get_running_loop().time() + 10,
            )
        )
        assert await asyncio.to_thread(second_call_started.wait, 1)

    releaser = threading.Thread(target=release_host_after_shutdown)
    releaser.start()
    try:
        asyncio.run(scenario())
        runner_returned.set()
    finally:
        host_release.set()
        releaser.join()
        executor.shutdown()

    assert release_failures == []
    assert host_completed.is_set()
    assert calls == 3


def _record() -> RemoteExternalBootRecoveryRecord:
    system_id = "00000000-0000-4000-8000-000000000001"
    run_id = "00000000-0000-4000-8000-000000000002"
    base_plan = _plan()
    plan = base_plan.model_copy(
        update={
            "ownership": base_plan.ownership.model_copy(
                update={"system_id": system_id, "run_id": run_id}
            )
        }
    )
    materialization = _materialization(plan=plan)
    materialization = materialization.model_copy(
        update={
            "ownership": materialization.ownership.model_copy(
                update={"system_id": system_id, "run_id": run_id}
            )
        }
    )
    binding = ExternalBootActivationBinding(
        system_id=plan.ownership.system_id,
        run_id=plan.ownership.run_id,
        activation_id="00000000-0000-4000-8000-000000000003",
    )
    definition = prepare_target_definition(
        _source_xml(system_id=UUID(system_id)),
        plan=plan,
        materialization=materialization,
        binding=binding,
        pool="kdive",
        overlay_path="/pool/overlay.qcow2",
        kernel_path="/artifacts/kernel",
        initrd_path="/artifacts/initrd",
    )
    digest = "sha256:" + "a" * 64
    module = RemoteModuleRecoveryRefV2.model_validate(
        {
            "system_id": binding.system_id,
            "run_id": binding.run_id,
            "plan_identity": plan.identity,
            "operation_nonce": "b" * 32,
            "pool": {"ref": "pool/modules"},
            "root_volume": {"ref": "volumes/root"},
            "source_volume": {"ref": "volumes/source"},
            "scratch_volume": {"ref": "volumes/scratch"},
            "operation_identity": digest,
            "result_identity": digest,
            "installed_entry_count": 1,
            "installed_content_bytes": 3,
            "appliance_image_digest": digest,
            "authority_identity": digest,
            "source_capacity_bytes": 4096,
        }
    )
    return RemoteExternalBootRecoveryRecord(
        binding=binding,
        plan_identity=plan.identity,
        materialization=materialization,
        definition=definition,
        module_recovery=module,
        source_state=ProviderStateIdentity(
            definition=definition.source_definition, modules=AbsentComponentState()
        ),
        target_state=ProviderStateIdentity(
            definition=definition.target_definition,
            modules=PresentComponentState(manifest=materialization.installed_module_tree),
        ),
        prior_power="inactive",
        recovery_objects=tuple(
            sorted(
                (
                    OpaqueProviderRef(ref="volumes/source"),
                    OpaqueProviderRef(ref="volumes/scratch"),
                ),
                key=lambda value: value.to_canonical_json(),
            )
        ),
    )


def _plan_for_record(record: RemoteExternalBootRecoveryRecord) -> ExternalBootPlan:
    base = _plan()
    return base.model_copy(
        update={
            "ownership": base.ownership.model_copy(
                update={
                    "system_id": record.binding.system_id,
                    "run_id": record.binding.run_id,
                }
            )
        }
    )


def _publish_terminal(
    store: RemoteModuleVolumePreparationStore, record: RemoteExternalBootRecoveryRecord
) -> None:
    plan = _plan_for_record(record)
    base = _remote_preparation_request()
    authority = base.authority.model_copy(
        update={
            "system_id": UUID(record.binding.system_id),
            "activation_id": UUID(record.binding.activation_id),
            "run_id": UUID(record.binding.run_id),
            "plan_identity": plan.identity,
            "plan": plan,
        }
    )
    operation = base.operation.model_copy(
        update={
            "system_id": record.binding.system_id,
            "run_id": record.binding.run_id,
            "plan_identity": plan.identity,
            "release": plan.module_obligation.release,
            "source_manifest": plan.module_obligation.source_manifest,
        }
    )
    request = RemoteModuleVolumePreparationRequestV1(authority=authority, operation=operation)
    store.stage(request, 999.0)
    store.publish_result(request, _terminal_response(request))


def _preparation_request(
    record: RemoteExternalBootRecoveryRecord,
    phase: Literal["materialize", "prepare"],
    *,
    authority: str = "authority/remote-a",
    operation_identity: str | None = None,
) -> ExternalBootPreparationRequest:
    return ExternalBootPreparationRequest(
        phase=phase,
        plan=_plan_for_record(record),
        binding=record.binding,
        authority=OpaqueProviderRef(ref=authority),
        operation_identity=operation_identity or f"{phase}-operation",
    )


def test_recovery_record_round_trips_only_canonical_closed_bytes() -> None:
    record = _record()
    encoded = record.to_canonical_json()

    assert RemoteExternalBootRecoveryRecord.from_canonical_json(encoded) == record
    with pytest.raises(ValueError, match="not canonical"):
        RemoteExternalBootRecoveryRecord.from_canonical_json(b" " + encoded)
    with pytest.raises(ValidationError, match="Extra inputs"):
        RemoteExternalBootRecoveryRecord.model_validate(
            {**record.model_dump(mode="json", by_alias=True), "destination": "host"}
        )


@pytest.mark.parametrize("mismatch", ["system", "plan", "definition", "modules"], ids=str)
def test_recovery_record_rejects_cross_owned_or_mismatched_facts(mismatch: str) -> None:
    record = _record()
    changes: dict[str, object]
    if mismatch == "system":
        changes = {
            "module_recovery": record.module_recovery.model_copy(
                update={"system_id": "00000000-0000-4000-8000-000000000099"}
            )
        }
    elif mismatch == "plan":
        changes = {"plan_identity": "sha256:" + "f" * 64}
    elif mismatch == "definition":
        changes = {
            "target_state": record.target_state.model_copy(
                update={"definition": "sha256:" + "f" * 64}
            )
        }
    else:
        changes = {
            "target_state": record.target_state.model_copy(
                update={"modules": PresentComponentState(manifest="sha256:" + "f" * 64)}
            )
        }
    with pytest.raises(ValidationError, match="remote recovery"):
        RemoteExternalBootRecoveryRecord.model_validate(
            {**record.model_dump(mode="json", by_alias=True), **changes}
        )


def test_recovery_objects_are_sorted_unique_and_bounded() -> None:
    record = _record()
    for objects in (
        tuple(reversed(record.recovery_objects)),
        (record.recovery_objects[0], record.recovery_objects[0]),
    ):
        with pytest.raises(ValidationError, match="unique and canonically sorted"):
            RemoteExternalBootRecoveryRecord.model_validate(
                {**record.model_dump(mode="json", by_alias=True), "recovery_objects": objects}
            )


def test_operations_protocol_has_exact_six_deadline_bearing_methods() -> None:
    methods = {
        name: value
        for name, value in vars(RemoteExternalBootOperations).items()
        if callable(value) and not name.startswith("_")
    }
    assert set(methods) == {"materialize", "prepare", "activate", "observe", "recover", "cleanup"}
    for method in methods.values():
        parameters = inspect.signature(method).parameters
        assert list(parameters)[-1] == "deadline"
        assert parameters["deadline"].annotation in {"float", float}


def test_remote_volume_request_binds_exact_prepare_phase_and_operation() -> None:
    request = _remote_preparation_request()

    assert request.operation.system_id == str(request.authority.system_id)
    with pytest.raises(ValidationError, match="PREPARE"):
        RemoteModuleVolumePreparationRequestV1(
            authority=AuthorityPreparationMutationRequestV1.model_validate(
                {
                    **request.authority.model_dump(mode="python", by_alias=True),
                    "operation": "materialize",
                }
            ),
            operation=request.operation,
        )
    with pytest.raises(ValidationError, match="differs from authority"):
        RemoteModuleVolumePreparationRequestV1(
            authority=request.authority,
            operation=request.operation.model_copy(
                update={"run_id": "00000000-0000-4000-8000-000000000099"}
            ),
        )


def test_remote_volume_response_round_trips_exact_attempt_geometry() -> None:
    request = _remote_preparation_request()
    volumes = _prepared_volumes(request)

    response = RemoteModuleVolumePreparationResponseV1.from_prepared(volumes)

    assert response.prepared() == volumes
    with pytest.raises(ValidationError, match="one exact attempt"):
        RemoteModuleVolumePreparationResponseV1.model_validate(
            {
                **response.model_dump(mode="python", by_alias=True),
                "scratch": response.scratch.model_copy(update={"operation_nonce": "2" * 32}),
            }
        )


@pytest.mark.anyio
async def test_remote_volume_host_retains_completion_through_cancellation() -> None:
    request = _remote_preparation_request()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    executor = RemoteModulePreparationExecutor()

    def prepare(operation: RemoteModuleOperationV1) -> PreparedModuleVolumes:
        assert operation == request.operation
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        common = {
            "pool": "modules",
            "system_id": request.operation.system_id,
            "run_id": request.operation.run_id,
            "operation_nonce": request.operation.operation_nonce,
        }
        return PreparedModuleVolumes(
            source=PreparedVolume(
                **common,
                name=render_module_volume_name(
                    request.operation.system_id,
                    request.operation.run_id,
                    request.operation.operation_nonce,
                    "source.ext4",
                ),
                purpose="source",
                digest=request.operation.source_manifest,
                capacity_bytes=4096,
            ),
            scratch=PreparedVolume(
                **common,
                name=render_module_volume_name(
                    request.operation.system_id,
                    request.operation.run_id,
                    request.operation.operation_nonce,
                    "scratch.ext4",
                ),
                purpose="scratch",
                digest="sha256:" + "0" * 64,
                capacity_bytes=8192,
            ),
        )

    host = RemoteModuleVolumePreparationHost(prepare, executor)
    task = asyncio.create_task(host.execute(request))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    executor.shutdown()


@pytest.mark.anyio
async def test_durable_remote_preparation_reopens_before_and_after_mutation(tmp_path: Path) -> None:
    request = _remote_preparation_request()
    calls = 0

    class Host:
        async def execute(
            self, admitted: AdmittedRemoteModulePreparation
        ) -> RemoteModuleTerminalPreparationResponseV1:
            nonlocal calls
            assert admitted.request == request
            assert admitted.local_deadline == 999.0
            calls += 1
            return _terminal_response(request)

    first = RemoteModuleVolumePreparationStore(tmp_path)
    first.stage(request, 999.0)
    first.close()

    second = RemoteModuleVolumePreparationStore(tmp_path)
    durable = DurableRemoteModuleVolumePreparationHost(second, cast(Any, Host()))
    expected = await durable.execute(request)
    second.close()

    third = RemoteModuleVolumePreparationStore(tmp_path)
    replay = DurableRemoteModuleVolumePreparationHost(third, cast(Any, Host()))
    assert await replay.execute(request) == expected
    assert calls == 1
    third.close()


@pytest.mark.anyio
async def test_durable_remote_lifecycle_waits_through_lost_reply_and_reopens_terminal(
    tmp_path: Path,
) -> None:
    preparation = _remote_preparation_request()
    lifecycle = _module_lifecycle_request(preparation)
    expected = _restored_response(preparation)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Host:
        async def execute_lifecycle(
            self, request: object, terminal: object, restored: object, deadline: float
        ) -> RemoteModuleLifecycleResponseV1:
            del terminal
            nonlocal calls
            assert request == lifecycle
            assert restored is None
            assert deadline == 30.0
            calls += 1
            started.set()
            await release.wait()
            return expected

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(preparation, 999.0)
    store.publish_result(preparation, _terminal_response(preparation))
    durable = DurableRemoteModuleVolumePreparationHost(
        store, cast(Any, Host()), monotonic=lambda: 0.0
    )
    task = asyncio.create_task(durable.execute_lifecycle(lifecycle))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()

    restarted = RemoteModuleVolumePreparationStore(tmp_path)
    assert (
        await DurableRemoteModuleVolumePreparationHost(
            restarted, cast(Any, Host()), monotonic=lambda: 100.0
        ).execute_lifecycle(lifecycle)
        == expected
    )
    assert calls == 1
    retry_with_new_budget = lifecycle.model_copy(update={"budget_seconds": 300})
    assert (
        await DurableRemoteModuleVolumePreparationHost(
            restarted, cast(Any, Host()), monotonic=lambda: 100.0
        ).execute_lifecycle(retry_with_new_budget)
        == expected
    )
    assert restarted.reopen_lifecycle(retry_with_new_budget).local_deadline == 30.0
    assert calls == 1
    successor = RemoteModuleLifecycleRequestV1(
        authority=lifecycle.authority.model_copy(
            update={
                "authority_id": uuid4(),
                "generation": 2,
                "operation_identity": "successor-recover-op",
            }
        ),
        action="restore",
        budget_seconds=30,
    )
    assert (
        await DurableRemoteModuleVolumePreparationHost(
            restarted, cast(Any, Host()), monotonic=lambda: 100.0
        ).execute_lifecycle(successor)
        == expected
    )
    assert calls == 1
    restarted.close()


@pytest.mark.anyio
async def test_remote_reap_requires_restored_cleanup_or_installed_teardown(
    tmp_path: Path,
) -> None:
    preparation = _remote_preparation_request()
    cleanup = _module_lifecycle_request(
        preparation, purpose="release", operation="cleanup", action="reap"
    )
    with pytest.raises(ValidationError, match="action differs"):
        _module_lifecycle_request(
            preparation,
            purpose="release",
            operation="release",
            action="reap",
        )
    terminal = _terminal_response(preparation)
    expected = RemoteModuleLifecycleResponseV1(
        action="reap",
        recovery=terminal.recovery,
        operation=preparation.operation,
        result=terminal.result,
        volumes_absent=True,
    )
    calls = 0

    class Host:
        async def execute_lifecycle(
            self,
            request: RemoteModuleLifecycleRequestV1,
            terminal: object,
            prior_restore: RemoteModuleLifecycleResponseV1 | None,
            deadline: float,
        ) -> RemoteModuleLifecycleResponseV1:
            del terminal
            nonlocal calls
            assert request.authority.operation is AuthorityOperation.TEARDOWN
            assert prior_restore is None
            assert deadline > 0
            calls += 1
            return expected

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(preparation, 999.0)
    store.publish_result(preparation, _terminal_response(preparation))
    durable = DurableRemoteModuleVolumePreparationHost(store, cast(Any, Host()))
    with pytest.raises(CategorizedError, match="requires restored evidence"):
        await durable.execute_lifecycle(cleanup)
    assert calls == 0
    wrong_binding = RemoteModuleLifecycleRequestV1(
        authority=cleanup.authority.model_copy(update={"activation_id": uuid4()}),
        action="reap",
        budget_seconds=30,
    )
    with pytest.raises(FileNotFoundError, match="terminal preparation"):
        await durable.execute_lifecycle(wrong_binding)
    assert calls == 0
    teardown = _module_lifecycle_request(
        preparation, purpose="teardown", operation="teardown", action="reap"
    )
    assert await durable.execute_lifecycle(teardown) == expected
    assert calls == 1
    store.close()


@pytest.mark.anyio
async def test_worker_lifecycle_stops_on_authenticated_predispatch_refusal() -> None:
    preparation_request = _remote_preparation_request()
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=preparation_request.authority.system_id,
            run_id=preparation_request.authority.run_id,
            operation_nonce=preparation_request.operation.operation_nonce,
        )
    )
    calls = 0

    class Sender:
        async def execute_remote_module_lifecycle(self, *_args: object, **_kwargs: object) -> None:
            nonlocal calls
            calls += 1
            raise CategorizedError(
                "authority: remote-module-refused",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )

    with pytest.raises(CategorizedError, match="remote-module-refused"):
        await asyncio.wait_for(
            execute_remote_module_lifecycle_on_authority_host(
                connection=cast(Any, object()),
                repository=cast(Any, object()),
                sender=cast(Any, Sender()),
                authority=_module_lifecycle_request(preparation_request).authority,
                preparation=preparation,
                worker_context=ModuleAttemptWorkerWriteContext(
                    job_id=uuid4(),
                    job_attempt=1,
                    incarnation_credential=SecretStr("worker-credential"),
                    preparation=preparation,
                ),
                action="restore",
                deadline=asyncio.get_running_loop().time() + 10,
            ),
            timeout=1,
        )
    assert calls == 1


@pytest.mark.anyio
async def test_worker_reap_requires_retained_terminal_evidence_before_dispatch() -> None:
    preparation_request = _remote_preparation_request()
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=preparation_request.authority.system_id,
            run_id=preparation_request.authority.run_id,
            operation_nonce=preparation_request.operation.operation_nonce,
        )
    )

    class Repository:
        async def reap_obligation_is_open(self, connection: object, attempt: ModuleAttempt) -> bool:
            assert connection is worker_connection
            assert attempt.operation_nonce == preparation_request.operation.operation_nonce
            return False

    class Sender:
        async def execute_remote_module_lifecycle(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("reap reached authority without retained evidence")

    worker_connection = object()
    with pytest.raises(CategorizedError, match="reap obligation is not retained"):
        await execute_remote_module_lifecycle_on_authority_host(
            connection=cast(Any, worker_connection),
            repository=cast(Any, Repository()),
            sender=cast(Any, Sender()),
            authority=_module_lifecycle_request(
                preparation_request,
                purpose="teardown",
                operation="teardown",
                action="reap",
            ).authority,
            preparation=preparation,
            worker_context=ModuleAttemptWorkerWriteContext(
                job_id=uuid4(),
                job_attempt=1,
                incarnation_credential=SecretStr("worker-credential"),
                preparation=preparation,
            ),
            action="reap",
            deadline=asyncio.get_running_loop().time() + 10,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "later_category", [ErrorCategory.CONFIGURATION_ERROR, ErrorCategory.STALE_HANDLE]
)
async def test_worker_lifecycle_keeps_later_denial_indeterminate_until_matching_completion(
    later_category: ErrorCategory,
) -> None:
    preparation_request = _remote_preparation_request()
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=preparation_request.authority.system_id,
            run_id=preparation_request.authority.run_id,
            operation_nonce=preparation_request.operation.operation_nonce,
        )
    )
    response = _restored_response(preparation_request)
    later_denial = asyncio.Event()
    host_release = asyncio.Event()
    calls = 0

    class Repository:
        async def read_restored_evidence(self, *_args: object) -> None:
            return None

        async def worker_record_restored_evidence(self, *_args: object) -> bool:
            return True

    class Sender:
        async def execute_remote_module_lifecycle(
            self, *_args: object, **_kwargs: object
        ) -> RemoteModuleLifecycleResponseV1:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError
            if not host_release.is_set():
                later_denial.set()
                raise CategorizedError("authority: later-denial", category=later_category)
            return response

    task = asyncio.create_task(
        execute_remote_module_lifecycle_on_authority_host(
            connection=cast(Any, object()),
            repository=cast(Any, Repository()),
            sender=cast(Any, Sender()),
            authority=_module_lifecycle_request(preparation_request).authority,
            preparation=preparation,
            worker_context=ModuleAttemptWorkerWriteContext(
                job_id=uuid4(),
                job_attempt=1,
                incarnation_credential=SecretStr("worker-credential"),
                preparation=preparation,
            ),
            action="restore",
            deadline=asyncio.get_running_loop().time() + 10,
        )
    )
    await asyncio.wait_for(later_denial.wait(), 1)
    assert not task.done()
    host_release.set()
    assert await asyncio.wait_for(task, 1) == response
    assert calls >= 3


@pytest.mark.anyio
@pytest.mark.parametrize("cancelled", [False, True], ids=["active", "cancelled"])
async def test_worker_lifecycle_accepts_only_authenticated_terminal_failure_after_lost_reply(
    cancelled: bool,
) -> None:
    preparation_request = _remote_preparation_request()
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=preparation_request.authority.system_id,
            run_id=preparation_request.authority.run_id,
            operation_nonce=preparation_request.operation.operation_nonce,
        )
    )
    retrying = asyncio.Event()
    terminal_release = asyncio.Event()
    observer_task: asyncio.Task[object] | None = None
    calls = 0

    class Sender:
        async def execute_remote_module_lifecycle(self, *_args: object, **_kwargs: object) -> None:
            nonlocal calls, observer_task
            observer_task = cast(asyncio.Task[object], asyncio.current_task())
            calls += 1
            if calls == 1:
                raise TimeoutError
            if cancelled and calls == 2:
                retrying.set()
                raise CategorizedError(
                    "authority: superseded", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                )
            if cancelled:
                await terminal_release.wait()
            raise CategorizedError(
                "authority: remote-module-failed",
                category=ErrorCategory.CONFLICT,
                details={"completion": "failed-after-mutation"},
            )

    operation = execute_remote_module_lifecycle_on_authority_host(
        connection=cast(Any, object()),
        repository=cast(Any, object()),
        sender=cast(Any, Sender()),
        authority=_module_lifecycle_request(preparation_request).authority,
        preparation=preparation,
        worker_context=ModuleAttemptWorkerWriteContext(
            job_id=uuid4(),
            job_attempt=1,
            incarnation_credential=SecretStr("worker-credential"),
            preparation=preparation,
        ),
        action="restore",
        deadline=asyncio.get_running_loop().time() + 10,
    )
    if not cancelled:
        with pytest.raises(CategorizedError, match="remote-module-failed"):
            await asyncio.wait_for(operation, timeout=1)
        assert calls == 2
        return

    task = asyncio.create_task(operation)
    await asyncio.wait_for(retrying.wait(), 1)
    assert task.cancel("worker shutdown")
    await asyncio.sleep(0)
    assert task.cancel("later cancellation")
    await asyncio.sleep(0)
    assert not task.done()
    assert observer_task is not None
    observer_task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    terminal_release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, 1)
    assert caught.value.args == ("worker shutdown",)
    assert task.cancelling() == 2
    assert calls == 3


@pytest.mark.anyio
async def test_worker_lifecycle_keeps_ambiguous_stale_reply_indeterminate() -> None:
    preparation_request = _remote_preparation_request()
    preparation = ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=preparation_request.authority.system_id,
            run_id=preparation_request.authority.run_id,
            operation_nonce=preparation_request.operation.operation_nonce,
        )
    )
    response = _restored_response(preparation_request)
    second = asyncio.Event()
    release = asyncio.Event()
    observer_task: asyncio.Task[object] | None = None
    calls = 0

    class Repository:
        async def read_restored_evidence(self, *_args: object) -> None:
            return None

        async def worker_record_restored_evidence(self, *_args: object) -> bool:
            return True

    class Sender:
        async def execute_remote_module_lifecycle(
            self, *_args: object, **_kwargs: object
        ) -> RemoteModuleLifecycleResponseV1:
            nonlocal calls, observer_task
            observer_task = cast(asyncio.Task[object], asyncio.current_task())
            calls += 1
            if calls == 1:
                raise TimeoutError
            if release.is_set():
                return response
            second.set()
            raise CategorizedError(
                "authority: superseded", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )

    task = asyncio.create_task(
        execute_remote_module_lifecycle_on_authority_host(
            connection=cast(Any, object()),
            repository=cast(Any, Repository()),
            sender=cast(Any, Sender()),
            authority=_module_lifecycle_request(preparation_request).authority,
            preparation=preparation,
            worker_context=ModuleAttemptWorkerWriteContext(
                job_id=uuid4(),
                job_attempt=1,
                incarnation_credential=SecretStr("worker-credential"),
                preparation=preparation,
            ),
            action="restore",
            deadline=asyncio.get_running_loop().time() + 10,
        )
    )
    await asyncio.wait_for(second.wait(), 1)
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    assert observer_task is not None
    observer_task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert calls >= 3


def test_durable_remote_preparation_preserves_first_authority_clock_deadline(
    tmp_path: Path,
) -> None:
    request = _remote_preparation_request()
    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request, 999.0)
    store.stage(request, 1029.0)
    admitted = store.reopen_request(request)
    assert admitted.request == request
    assert admitted.local_deadline == 999.0
    changed = request.model_copy(
        update={"operation": request.operation.model_copy(update={"operation_nonce": "f" * 32})}
    )
    with pytest.raises(ValueError, match="durable"):
        store.stage(changed, 999.0)
    store.close()


@pytest.mark.anyio
async def test_durable_remote_preparation_failure_is_terminal_for_recovery(tmp_path: Path) -> None:
    request = _remote_preparation_request()

    class FailingHost:
        async def execute(self, request: object) -> object:
            del request
            raise RuntimeError("provider returned after mutation")

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request, 999.0)
    durable = DurableRemoteModuleVolumePreparationHost(store, cast(Any, FailingHost()))
    with pytest.raises(RuntimeError, match="after mutation"):
        await durable.execute(request)
    store.close()
    restarted = RemoteModuleVolumePreparationStore(tmp_path)
    with pytest.raises(CategorizedError, match="requires recovery"):
        await DurableRemoteModuleVolumePreparationHost(restarted, cast(Any, FailingHost())).execute(
            request
        )
    restarted.close()


@pytest.mark.anyio
async def test_durable_remote_preparation_cancellation_waits_and_records_terminal(
    tmp_path: Path,
) -> None:
    request = _remote_preparation_request()
    started = asyncio.Event()
    release = asyncio.Event()
    expected = _terminal_response(request)

    class BlockingHost:
        async def execute(self, request: object) -> object:
            del request
            started.set()
            await release.wait()
            return expected

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request, 999.0)
    task = asyncio.create_task(
        DurableRemoteModuleVolumePreparationHost(store, cast(Any, BlockingHost())).execute(request)
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()

    restarted = RemoteModuleVolumePreparationStore(tmp_path)
    assert (
        await DurableRemoteModuleVolumePreparationHost(
            restarted, cast(Any, BlockingHost())
        ).execute(request)
        == expected
    )
    restarted.close()


@pytest.mark.anyio
async def test_durable_remote_preparation_cancel_persists_exceptional_completion(
    tmp_path: Path,
) -> None:
    request = _remote_preparation_request()
    started = asyncio.Event()
    release = asyncio.Event()

    class FailingHost:
        async def execute(self, request: object) -> object:
            del request
            started.set()
            await release.wait()
            raise RuntimeError("provider failed after cancellation")

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request, 999.0)
    task = asyncio.create_task(
        DurableRemoteModuleVolumePreparationHost(store, cast(Any, FailingHost())).execute(request)
    )
    await started.wait()
    task.cancel("authority shutdown")
    await asyncio.sleep(0)
    task.cancel("later cancellation")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("authority shutdown",)
    assert task.cancelling() == 2
    completion = store.reopen_completion(request)
    assert completion is not None
    assert completion.state == "failed-after-mutation"
    assert completion.response is None
    store.close()


@pytest.mark.anyio
async def test_durable_remote_lifecycle_cancel_persists_exceptional_completion(
    tmp_path: Path,
) -> None:
    preparation = _remote_preparation_request()
    lifecycle = _module_lifecycle_request(preparation)
    started = asyncio.Event()
    release = asyncio.Event()

    class FailingHost:
        async def execute_lifecycle(self, *_args: object) -> object:
            started.set()
            await release.wait()
            raise RuntimeError("provider failed after cancellation")

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(preparation, 999.0)
    store.publish_result(preparation, _terminal_response(preparation))
    durable = DurableRemoteModuleVolumePreparationHost(
        store, cast(Any, FailingHost()), monotonic=lambda: 0.0
    )
    task = asyncio.create_task(durable.execute_lifecycle(lifecycle))
    await started.wait()
    task.cancel("authority shutdown")
    await asyncio.sleep(0)
    task.cancel("later cancellation")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("authority shutdown",)
    assert task.cancelling() == 2
    completion = store.reopen_lifecycle_completion(lifecycle)
    assert completion is not None
    assert completion.state == "failed-after-mutation"
    assert completion.response is None
    store.close()


@pytest.mark.anyio
async def test_durable_remote_preparation_retries_unrecorded_provider_return(
    tmp_path: Path,
) -> None:
    request = _remote_preparation_request()
    calls = 0

    def prepare(_operation: RemoteModuleOperationV1) -> PreparedModuleVolumes:
        nonlocal calls
        calls += 1
        return _prepared_volumes(request)

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request, 999.0)
    executor = RemoteModulePreparationExecutor()
    await RemoteModuleVolumePreparationHost(prepare, executor).execute(request)
    executor.shutdown()
    store.close()

    class Host:
        async def execute(
            self, admitted: AdmittedRemoteModulePreparation
        ) -> RemoteModuleTerminalPreparationResponseV1:
            nonlocal calls
            assert admitted.request == request
            calls += 1
            return _terminal_response(request)

    restarted = RemoteModuleVolumePreparationStore(tmp_path)
    durable = DurableRemoteModuleVolumePreparationHost(restarted, cast(Any, Host()))
    assert (await durable.execute(request)).prepared() == _prepared_volumes(request)
    assert calls == 2
    restarted.close()


def test_durable_remote_preparation_rejects_same_authority_with_changed_operation(
    tmp_path: Path,
) -> None:
    request = _remote_preparation_request()
    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request, 999.0)
    changed = request.model_copy(
        update={
            "operation": request.operation.model_copy(
                update={"appliance_image_digest": "sha256:" + "f" * 64}
            )
        }
    )

    with pytest.raises(ValueError, match="durable"):
        store.stage(changed, 999.0)
    store.close()


@pytest.mark.parametrize("failure", ["write", "fsync", "link"])
def test_preparation_store_removes_its_exact_temporary_after_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = RemoteModuleVolumePreparationStore(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError(f"controlled {failure} failure")

    monkeypatch.setattr(
        f"kdive.providers.remote_libvirt.external_boot_authority.os.{failure}", fail
    )
    with pytest.raises(OSError, match=f"controlled {failure} failure"):
        store.stage(_remote_preparation_request(), 999.0)
    store.close()

    assert list(tmp_path.iterdir()) == []


def test_preparation_store_rejects_zero_progress_write_without_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RemoteModuleVolumePreparationStore(tmp_path)
    calls = 0

    def zero_once(_descriptor: int, _data: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0
        raise AssertionError("write retried after reporting zero progress")

    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.external_boot_authority.os.write", zero_once
    )
    with pytest.raises(OSError, match="made no progress"):
        store.stage(_remote_preparation_request(), 999.0)
    store.close()

    assert calls == 1
    assert list(tmp_path.iterdir()) == []


def test_preparation_store_refuses_fifo_without_blocking_before_metadata_check(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "foreign-fifo"
    os.mkfifo(fifo, 0o600)
    repository = Path(__file__).resolve().parents[3]
    program = """
from pathlib import Path
import sys
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleVolumePreparationStore,
)

store = RemoteModuleVolumePreparationStore(Path(sys.argv[1]))
try:
    store._read('foreign-fifo')
except PermissionError:
    pass
else:
    raise SystemExit('FIFO was accepted as evidence')
finally:
    store.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        cwd=repository,
        timeout=2,
    )

    assert result.stdout == ""


@pytest.mark.parametrize("phase", ["materialize", "prepare"])
def test_preparation_receipt_survives_core_commit_loss_without_repeating_provider(
    tmp_path: Path, phase: Literal["materialize", "prepare"]
) -> None:
    record = _record()
    calls: list[str] = []

    class Operations:
        def materialize(
            self, plan: object, binding: object, owner: object, deadline: float
        ) -> object:
            assert plan == _plan_for_record(record)
            assert binding == record.binding
            assert owner == OpaqueProviderRef(ref="authority/remote-a")
            assert deadline in {123.0, 456.0}
            calls.append("materialize")
            return record.materialization

        def prepare(
            self,
            plan: object,
            materialization: object,
            binding: object,
            modules: object,
            owner: object,
            deadline: float,
        ) -> RemoteExternalBootRecoveryRecord:
            assert plan == _plan_for_record(record)
            assert materialization == record.materialization
            assert binding == record.binding
            assert modules is not None
            assert owner == OpaqueProviderRef(ref="authority/remote-a")
            assert deadline in {123.0, 456.0}
            calls.append("prepare")
            return record

    store = RemoteModuleVolumePreparationStore(tmp_path)
    coordinator = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()), store, lambda: 123.0
    )
    if phase == "prepare":
        coordinator.execute_preparation(_preparation_request(record, "materialize"))
        _publish_terminal(store, record)
        calls.clear()
    request = _preparation_request(record, phase)

    expected = coordinator.execute_preparation(request)
    store.close()  # Simulate loss before core commits the returned receipt.

    reopened_store = RemoteModuleVolumePreparationStore(tmp_path)
    reopened = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()), reopened_store, lambda: 456.0
    )
    assert reopened.observe_preparation(request) == expected
    assert reopened.execute_preparation(request) == expected
    assert calls == [phase]
    reopened_store.close()


def test_materialization_requires_explicit_matching_activation_binding(tmp_path: Path) -> None:
    record = _record()

    class Operations:
        def materialize(self, *args: object) -> object:
            raise AssertionError("provider reached without an activation binding")

    store = RemoteModuleVolumePreparationStore(tmp_path)
    coordinator = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()), store, lambda: 1.0
    )
    assert list(inspect.signature(RemoteExternalBootCoordinator.materialize).parameters) == [
        "self",
        "plan",
        "binding",
        "authority",
    ]
    changed_binding = record.binding.model_copy(
        update={"system_id": "00000000-0000-4000-8000-000000000099"}
    )
    with pytest.raises(ValueError, match="binding differs"):
        coordinator.materialize(
            _plan_for_record(record),
            changed_binding,
            OpaqueProviderRef(ref="authority/remote-a"),
        )
    store.close()


def test_preparation_store_refuses_mismatched_or_conflicting_publication(tmp_path: Path) -> None:
    record = _record()
    request = _preparation_request(record, "materialize")
    store = RemoteModuleVolumePreparationStore(tmp_path)
    mismatched = ExternalBootPreparationObservation(
        state="materialized",
        binding=request.binding,
        plan_identity=request.plan.identity,
        authority=OpaqueProviderRef(ref="authority/attacker-selected"),
        operation_identity=request.operation_identity,
        materialization=record.materialization,
    )
    with pytest.raises(ValueError, match="differs from request"):
        store.publish_preparation(request, mismatched)
    assert store.observe_preparation(request).state == "absent"

    receipt = mismatched.model_copy(update={"authority": request.authority})
    assert store.publish_preparation(request, receipt) == receipt
    changed = receipt.model_copy(
        update={
            "materialization": record.materialization.model_copy(
                update={"verified_bundle_sha256": "sha256:" + "f" * 64}
            )
        }
    )
    with pytest.raises(ValueError, match="conflicts with durable bytes"):
        store.publish_preparation(request, changed)
    store.close()


def test_preparation_store_rejects_wrong_phase_and_invalid_closed_shape_before_write(
    tmp_path: Path,
) -> None:
    record = _record()
    request = _preparation_request(record, "materialize")
    point = RecoveryPoint(
        binding=record.binding,
        plan_identity=record.plan_identity,
        materialization_identity=record.materialization.identity,
        recovery_ref=OpaqueProviderRef(ref="remote/recovery"),
        source_state=record.source_state,
        target_state=record.target_state,
    )
    wrong_phase = ExternalBootPreparationObservation(
        state="prepared",
        binding=request.binding,
        plan_identity=request.plan.identity,
        authority=request.authority,
        operation_identity=request.operation_identity,
        materialization=record.materialization,
        recovery_point=point,
    )
    store = RemoteModuleVolumePreparationStore(tmp_path)
    with pytest.raises(ValueError, match="phase and state differ"):
        store.publish_preparation(request, wrong_phase)

    malformed = wrong_phase.model_copy(update={"recovery_point": None})
    with pytest.raises(ValueError, match="invalid values"):
        store.publish_preparation(request, malformed)
    assert not list(tmp_path.glob("*.preparation"))
    store.close()


def test_materialization_index_rejects_path_before_descriptor_relative_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan_for_record(_record())
    store = RemoteModuleVolumePreparationStore(tmp_path)
    name = f"{plan.identity.removeprefix('sha256:')}.materialization-index"
    store._publish(name, b"../outside")
    real_open = os.open
    escaped: list[str] = []

    def guarded_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if dir_fd is not None and "/" in path:
            escaped.append(path)
            raise AssertionError("corrupted index escaped the preparation root")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.external_boot_authority.os.open", guarded_open
    )
    with pytest.raises(ValueError, match="index is malformed"):
        store.reopen_materialization_for_plan(plan)
    assert escaped == []
    store.close()


def test_preparation_takeover_adopts_only_authenticated_same_phase_receipt(
    tmp_path: Path,
) -> None:
    record = _record()
    calls: list[str] = []

    class Operations:
        def materialize(self, *args: object) -> object:
            calls.append("materialize")
            return record.materialization

    store = RemoteModuleVolumePreparationStore(tmp_path)
    coordinator = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()), store, lambda: 1.0
    )
    predecessor = _preparation_request(record, "materialize")
    receipt = coordinator.execute_preparation(predecessor)
    takeover = _preparation_request(
        record,
        "materialize",
        authority="authority/takeover",
        operation_identity="materialize-takeover",
    )

    adopted = coordinator.adopt_preparation(takeover, predecessor, receipt.identity)
    assert adopted.authority == takeover.authority
    assert adopted.operation_identity == takeover.operation_identity
    assert adopted.materialization == receipt.materialization
    assert coordinator.observe_preparation(takeover) == adopted
    assert coordinator.observe_preparation(predecessor) == receipt
    assert calls == ["materialize"]

    with pytest.raises(ValueError, match="predecessor cannot be adopted"):
        coordinator.adopt_preparation(takeover, predecessor, "sha256:" + "0" * 64)
    wrong_phase = takeover.model_copy(
        update={"phase": "prepare", "operation_identity": "prepare-takeover"}
    )
    with pytest.raises(ValueError, match="predecessor cannot be adopted"):
        coordinator.adopt_preparation(wrong_phase, predecessor, receipt.identity)
    store.close()


def test_six_operation_coordinator_reopens_exact_recovery_after_restart(tmp_path: Path) -> None:
    record = _record()
    authority = OpaqueProviderRef(ref="authority/remote-a")
    calls: list[tuple[str, float]] = []

    class Operations:
        def materialize(
            self, plan: object, binding: object, owner: object, deadline: float
        ) -> object:
            assert owner == authority
            assert binding == record.binding
            assert plan == expected_plan
            calls.append(("materialize", deadline))
            return record.materialization

        def prepare(
            self,
            plan: object,
            materialization: object,
            binding: object,
            modules: object,
            owner: object,
            deadline: float,
        ) -> RemoteExternalBootRecoveryRecord:
            assert plan == expected_plan
            assert materialization == record.materialization
            assert binding == record.binding
            assert modules is not None
            assert owner == authority
            calls.append(("prepare", deadline))
            return record

        def activate(self, recovery: object, owner: object, deadline: float) -> None:
            assert recovery == record and owner == authority
            calls.append(("activate", deadline))

        def observe(
            self, recovery: object, owner: object, deadline: float
        ) -> RunningKernelObservation:
            assert recovery == record and owner == authority
            calls.append(("observe", deadline))
            return RunningKernelObservation(
                identity=record.materialization.kernel_observation,
                cmdline=b"root=/dev/vda",
                expected_cmdline=b"root=/dev/vda",
            )

        def recover(self, recovery: object, owner: object, deadline: float) -> None:
            assert recovery == record and owner == authority
            calls.append(("recover", deadline))

        def cleanup(self, recovery: object, owner: object, deadline: float) -> None:
            assert recovery == record and owner == authority
            calls.append(("cleanup", deadline))

    store = RemoteModuleVolumePreparationStore(tmp_path)
    coordinator = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()), store, lambda: 123.0
    )
    expected_plan = _plan_for_record(record)
    assert (
        coordinator.materialize(expected_plan, record.binding, authority) == record.materialization
    )
    _publish_terminal(store, record)
    point = coordinator.prepare(record.materialization, record.binding, authority)
    store.close()

    restarted_store = RemoteModuleVolumePreparationStore(tmp_path)
    restarted = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()),
        restarted_store,
        lambda: 456.0,
    )
    restarted.activate(point, authority)
    assert restarted.observe(point, authority).identity == record.materialization.kernel_observation
    restarted.recover(point, authority)
    restarted.cleanup(point, authority)
    assert calls == [
        ("materialize", 123.0),
        ("prepare", 123.0),
        ("activate", 456.0),
        ("observe", 456.0),
        ("recover", 456.0),
        ("cleanup", 456.0),
    ]
    restarted_store.close()


def test_coordinator_rejects_changed_recovery_before_provider_contact(tmp_path: Path) -> None:
    record = _record()

    class Operations:
        def prepare(self, *args: object) -> RemoteExternalBootRecoveryRecord:
            return record

        def activate(self, *args: object) -> None:
            raise AssertionError("provider touched through activate")

    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.publish_materialization(_plan_for_record(record), record.materialization)
    _publish_terminal(store, record)
    coordinator = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()), store, lambda: 1.0
    )
    point = coordinator.prepare(
        record.materialization, record.binding, OpaqueProviderRef(ref="authority/remote-a")
    )
    changed = RecoveryPoint.model_validate(
        {
            **point.model_dump(mode="python", by_alias=True),
            "plan_identity": "sha256:" + "f" * 64,
        }
    )
    with pytest.raises(ValueError, match="differs from durable record"):
        coordinator.activate(changed, OpaqueProviderRef(ref="authority/remote-a"))
    store.close()


def test_running_observation_reopens_exact_recovery_after_restart(tmp_path: Path) -> None:
    record = _record()
    authority_id = uuid4()
    attempt_id = uuid4()
    request = AuthorityMutationRequestV1(
        authority_id=authority_id,
        generation=4,
        system_id=UUID(record.binding.system_id),
        activation_id=UUID(record.binding.activation_id),
        run_id=UUID(record.binding.run_id),
        plan_identity=record.plan_identity,
        purpose="activate",
        operation=AuthorityOperation.ACTIVATE,
        provider_kind="remote-libvirt",
        authority_instance="remote-a",
        operation_identity="observe-running",
        operation_digest="sha256:" + "d" * 64,
        attempt_id=attempt_id,
        expected_source_identity=record.source_state.definition,
        intended_target_identity=record.target_state.definition,
        recovery_objects=(),
    )
    first = RemoteModuleVolumePreparationStore(tmp_path)
    first.publish_recovery(record)
    first.close()
    observed = RunningKernelObservation(
        identity=record.materialization.kernel_observation,
        cmdline=b"root=/dev/vda",
        expected_cmdline=b"root=/dev/vda",
    )
    calls: list[tuple[object, object]] = []

    class Operations:
        def observe(self, recovery: object, authority: object, deadline: float) -> object:
            calls.append((recovery, authority))
            return observed

    class Delegate:
        async def observe(self, request: object) -> AuthorityObservationV1:
            raise AssertionError("ordinary observation used")

        async def commit(self, request: object, context: object) -> AuthorityObservationV1:
            raise AssertionError("mutation used")

    restarted = RemoteModuleVolumePreparationStore(tmp_path)
    coordinator = RemoteExternalBootCoordinator(
        cast(RemoteExternalBootOperations, Operations()), restarted, lambda: 123.0
    )
    executor = RemoteModulePreparationExecutor()
    adapter = RemoteExternalBootAuthorityAdapter(Delegate(), coordinator, executor)  # type: ignore[arg-type]
    assert asyncio.run(adapter.observe_running(request)) == observed
    assert calls == [
        (
            record,
            OpaqueProviderRef(ref=f"authority/{authority_id}/4/{attempt_id}"),
        )
    ]
    executor.shutdown()
    restarted.close()


def test_running_observation_rejects_changed_request_before_provider(tmp_path: Path) -> None:
    record = _record()
    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.publish_recovery(record)

    class Operations:
        def observe(self, *args: object) -> RunningKernelObservation:
            raise AssertionError("provider touched")

    class Delegate:
        async def observe(self, request: object) -> AuthorityObservationV1:
            raise AssertionError

        async def commit(self, request: object, context: object) -> AuthorityObservationV1:
            raise AssertionError

    request = AuthorityMutationRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=UUID(record.binding.system_id),
        activation_id=UUID(record.binding.activation_id),
        run_id=UUID(record.binding.run_id),
        plan_identity=record.plan_identity,
        purpose="activate",
        operation=AuthorityOperation.ACTIVATE,
        provider_kind="remote-libvirt",
        authority_instance="remote-a",
        operation_identity="observe-running",
        operation_digest="sha256:" + "d" * 64,
        attempt_id=uuid4(),
        expected_source_identity="foreign-source",
        intended_target_identity=record.target_state.definition,
        recovery_objects=(),
    )
    executor = RemoteModulePreparationExecutor()
    adapter = RemoteExternalBootAuthorityAdapter(
        Delegate(),
        RemoteExternalBootCoordinator(
            cast(RemoteExternalBootOperations, Operations()), store, lambda: 1.0
        ),
        executor,
    )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identities differ"):
        asyncio.run(adapter.observe_running(request))
    executor.shutdown()
    store.close()


def test_remote_recovery_object_reopens_geometry_and_deletes_exact_volume(tmp_path: Path) -> None:
    authority = OpaqueProviderRef(ref="authority/remote-a")
    record = _record()
    source_name = render_module_volume_name(
        record.binding.system_id,
        record.binding.run_id,
        record.module_recovery.operation_nonce,
        "source.ext4",
    )
    scratch_name = render_module_volume_name(
        record.binding.system_id,
        record.binding.run_id,
        record.module_recovery.operation_nonce,
        "scratch.ext4",
    )
    module = record.module_recovery.model_copy(
        update={
            "authority_identity": RemoteModuleRecoveryRefV2.identity_for_authority(authority),
            "source_volume": OpaqueProviderRef(ref=source_name),
            "scratch_volume": OpaqueProviderRef(ref=scratch_name),
        }
    )
    record = record.model_copy(
        update={
            "module_recovery": module,
            "recovery_objects": tuple(
                sorted(
                    (module.source_volume, module.scratch_volume),
                    key=lambda value: value.to_canonical_json(),
                )
            ),
        }
    )
    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.publish_recovery(record)

    class Volume:
        def name(self) -> str:
            return module.source_volume.ref

        def info(self) -> list[int]:
            return [0, module.source_capacity_bytes]

        def delete(self, flags: int = 0) -> int:
            del flags
            volumes.clear()
            return 0

    volumes: dict[str, Volume] = {}
    volumes[module.source_volume.ref] = Volume()

    class Pool:
        def storageVolLookupByName(self, name: str) -> Volume:  # noqa: N802
            if name in volumes:
                return volumes[name]
            error = libvirt.libvirtError("absent")
            error.err = (libvirt.VIR_ERR_NO_STORAGE_VOL,) + (0,) * 8
            raise error

    class Connection:
        def storagePoolLookupByName(self, name: str) -> Pool:  # noqa: N802
            assert name == module.pool.ref
            return Pool()

    port = RemoteExternalBootRecoveryObjects(
        store=store,
        connection=Connection(),
        boot_artifact_pool="boot",
        inspect_attachments=lambda: AttachmentInspection(
            system_shut_off=True,
            exclusive=True,
            appliance_present=False,
            detached_volumes=frozenset({(module.pool.ref, module.source_volume.ref)}),
        ),
    )
    binding = RecoveryObjectBinding(
        record_id=str(uuid4()),
        binding=record.binding,
        kind="modules",
        reference=module.source_volume,
        ownership_digest="sha256:" + "e" * 64,
        operation_identity="cleanup-a",
        attempt_id=str(uuid4()),
        mutation_journal_sequence=7,
        mutation_journal_digest="sha256:" + "f" * 64,
        reserved_bytes=4096,
    )
    observed = port.observe_object(binding, authority)
    assert observed.present and not observed.managed
    deleted = port.delete_recovery_object(binding, authority, observed.observed_digest)
    assert not deleted.present
    store.close()


def test_remote_worker_inputs_exclude_provider_owned_volume_and_appliance_identities() -> None:
    request = _remote_preparation_request()
    inputs = RemoteModulePreparationInputs(authority=request.authority)

    assert set(RemoteModulePreparationInputs.__dataclass_fields__) == {"authority"}
    assert inputs.authority == request.authority
