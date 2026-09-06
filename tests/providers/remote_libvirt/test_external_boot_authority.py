"""Closed remote external-boot coordinator contracts (#2200)."""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kdive.providers.external_boot_authority.protocol import AuthorityPreparationMutationRequestV1
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    DurableRemoteModuleVolumePreparationHost,
    RemoteExternalBootCoordinator,
    RemoteExternalBootOperations,
    RemoteExternalBootRecoveryRecord,
    RemoteModuleVolumePreparationHost,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationResponseV1,
    RemoteModuleVolumePreparationStore,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import prepare_target_definition
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    PreparedModuleVolumes,
    PreparedVolume,
    render_module_volume_name,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    operation as module_operation,
)
from tests.providers.remote_libvirt.lifecycle.test_external_boot import (
    _materialization,
    _plan,
    _source_xml,
)
from tests.support.external_boot_plan import external_boot_plan


def _remote_preparation_request() -> RemoteModuleVolumePreparationRequestV1:
    operation = module_operation()
    system_id = UUID(operation.system_id)
    run_id = UUID(operation.run_id)
    plan = external_boot_plan(system_id, run_id)
    operation = operation.model_copy(update={"plan_identity": plan.identity})
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


def _plan_for_record(record: RemoteExternalBootRecoveryRecord):
    plan = _plan()
    return plan.model_copy(
        update={
            "ownership": plan.ownership.model_copy(
                update={
                    "system_id": record.binding.system_id,
                    "run_id": record.binding.run_id,
                }
            )
        }
    )


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

    def prepare(_operation: RemoteModuleOperationV1) -> PreparedModuleVolumes:
        nonlocal calls
        calls += 1
        return _prepared_volumes(request)

    first = RemoteModuleVolumePreparationStore(tmp_path)
    first.stage(request)
    first.close()

    executor = RemoteModulePreparationExecutor()
    second = RemoteModuleVolumePreparationStore(tmp_path)
    durable = DurableRemoteModuleVolumePreparationHost(
        second, RemoteModuleVolumePreparationHost(prepare, executor)
    )
    expected = await durable.execute(request)
    second.close()

    third = RemoteModuleVolumePreparationStore(tmp_path)
    replay = DurableRemoteModuleVolumePreparationHost(
        third, RemoteModuleVolumePreparationHost(prepare, executor)
    )
    assert await replay.execute(request) == expected
    assert calls == 1
    third.close()
    executor.shutdown()


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

    executor = RemoteModulePreparationExecutor()
    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request)
    await RemoteModuleVolumePreparationHost(prepare, executor).execute(request)
    store.close()

    restarted = RemoteModuleVolumePreparationStore(tmp_path)
    durable = DurableRemoteModuleVolumePreparationHost(
        restarted, RemoteModuleVolumePreparationHost(prepare, executor)
    )
    assert (await durable.execute(request)).prepared() == _prepared_volumes(request)
    assert calls == 2
    restarted.close()
    executor.shutdown()


def test_durable_remote_preparation_rejects_same_authority_with_changed_operation(
    tmp_path: Path,
) -> None:
    request = _remote_preparation_request()
    store = RemoteModuleVolumePreparationStore(tmp_path)
    store.stage(request)
    changed = request.model_copy(
        update={
            "operation": request.operation.model_copy(
                update={"source_manifest": "sha256:" + "f" * 64}
            )
        }
    )

    with pytest.raises(ValueError, match="conflicts with durable bytes"):
        store.stage(changed)
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
        store.stage(_remote_preparation_request())
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
        store.stage(_remote_preparation_request())
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
        def materialize(self, plan: object, owner: object, deadline: float) -> object:
            assert plan == _plan_for_record(record)
            assert owner == OpaqueProviderRef(ref="authority/remote-a")
            assert deadline in {123.0, 456.0}
            calls.append("materialize")
            return record.materialization

        def prepare(
            self, materialization: object, binding: object, owner: object, deadline: float
        ) -> RemoteExternalBootRecoveryRecord:
            assert materialization == record.materialization
            assert binding == record.binding
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
        def materialize(self, plan: object, owner: object, deadline: float) -> object:
            assert owner == authority
            calls.append(("materialize", deadline))
            return record.materialization

        def prepare(
            self, materialization: object, binding: object, owner: object, deadline: float
        ) -> RemoteExternalBootRecoveryRecord:
            assert materialization == record.materialization
            assert binding == record.binding
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
    base_plan = _plan()
    plan = base_plan.model_copy(
        update={
            "ownership": base_plan.ownership.model_copy(
                update={
                    "system_id": record.binding.system_id,
                    "run_id": record.binding.run_id,
                }
            )
        }
    )
    assert coordinator.materialize(plan, record.binding, authority) == record.materialization
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
