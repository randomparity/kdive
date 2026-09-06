"""Closed remote external-boot coordinator contracts (#2200)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
)
from kdive.providers.ports.external_boot import (
    Digest,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    OpaqueProviderRef,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    RemoteExternalBootDefinition,
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

_MAX_RECORD_BYTES = 1_048_576
_MAX_PREPARATION_BYTES = 1_048_576


def _canonical_model_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


class RemotePreparedVolumeV1(BaseModel):
    """Bounded wire form of one provider-host-created module volume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pool: Annotated[str, Field(min_length=1, max_length=255)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    system_id: str
    run_id: str
    operation_nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    purpose: Literal["source", "scratch"]
    digest: Digest
    capacity_bytes: Annotated[int, Field(gt=0)]

    def prepared(self) -> PreparedVolume:
        return PreparedVolume(**self.model_dump())


class RemoteModuleVolumePreparationRequestV1(BaseModel):
    """One exact remote volume creation nested under an authorized PREPARE phase."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_by_alias=True, allow_inf_nan=False
    )

    schema_: Literal["remote-module-volume-preparation-v1"] = Field(
        "remote-module-volume-preparation-v1", alias="schema"
    )
    authority: AuthorityPreparationMutationRequestV1
    operation: RemoteModuleOperationV1
    deadline: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _operation_matches_authority(self) -> Self:
        authority = self.authority
        operation = self.operation
        if authority.operation is not AuthorityOperation.PREPARE:
            raise ValueError("remote module volumes require the PREPARE authority phase")
        if (
            operation.system_id != str(authority.system_id)
            or operation.run_id != str(authority.run_id)
            or operation.plan_identity != authority.plan_identity
        ):
            raise ValueError("remote module operation differs from authority binding")
        return self

    def to_canonical_json(self) -> bytes:
        encoded = _canonical_model_bytes(self)
        if len(encoded) > _MAX_PREPARATION_BYTES:
            raise ValueError("remote module preparation request exceeds 1048576 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_PREPARATION_BYTES:
            raise ValueError("remote module preparation request exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote module preparation request is not canonical JSON")
        return value


class RemoteModuleVolumePreparationResponseV1(BaseModel):
    """Exact bounded volume geometry returned by the authenticated provider host."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    schema_: Literal["remote-module-volume-preparation-response-v1"] = Field(
        "remote-module-volume-preparation-response-v1", alias="schema"
    )
    source: RemotePreparedVolumeV1
    scratch: RemotePreparedVolumeV1

    @model_validator(mode="after")
    def _volumes_form_one_attempt(self) -> Self:
        common = (
            self.source.pool,
            self.source.system_id,
            self.source.run_id,
            self.source.operation_nonce,
        )
        if (
            common
            != (
                self.scratch.pool,
                self.scratch.system_id,
                self.scratch.run_id,
                self.scratch.operation_nonce,
            )
            or self.source.purpose != "source"
            or self.scratch.purpose != "scratch"
        ):
            raise ValueError("prepared module volumes do not form one exact attempt")
        return self

    @classmethod
    def from_prepared(cls, volumes: PreparedModuleVolumes) -> Self:
        return cls(
            source=RemotePreparedVolumeV1.model_validate(asdict(volumes.source)),
            scratch=RemotePreparedVolumeV1.model_validate(asdict(volumes.scratch)),
        )

    def prepared(self) -> PreparedModuleVolumes:
        return PreparedModuleVolumes(source=self.source.prepared(), scratch=self.scratch.prepared())

    def validate_for(self, operation: RemoteModuleOperationV1) -> None:
        expected = (
            operation.system_id,
            operation.run_id,
            operation.operation_nonce,
        )
        for volume in (self.source, self.scratch):
            if (volume.system_id, volume.run_id, volume.operation_nonce) != expected:
                raise ValueError("provider returned volumes for a different remote module attempt")
        if (
            self.source.name != render_module_volume_name(*expected, "source.ext4")
            or self.scratch.name != render_module_volume_name(*expected, "scratch.ext4")
            or self.source.digest != operation.source_manifest
            or self.scratch.digest != "sha256:" + "0" * 64
        ):
            raise ValueError("provider returned mismatched remote module volume identities")

    def to_canonical_json(self) -> bytes:
        encoded = _canonical_model_bytes(self)
        if len(encoded) > _MAX_PREPARATION_BYTES:
            raise ValueError("remote module preparation response exceeds 1048576 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_PREPARATION_BYTES:
            raise ValueError("remote module preparation response exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote module preparation response is not canonical JSON")
        return value


class RemoteModuleTerminalPreparationResponseV1(RemoteModuleVolumePreparationResponseV1):
    """Durable terminal appliance result and recovery alongside exact volume identities."""

    schema_: Literal["remote-module-terminal-preparation-response-v1"] = Field(
        "remote-module-terminal-preparation-response-v1", alias="schema"
    )
    result: RemoteModuleResultV1
    recovery: RemoteModuleRecoveryRefV2

    def validate_terminal_for(
        self,
        operation: RemoteModuleOperationV1,
        authority: AuthorityPreparationMutationRequestV1,
    ) -> None:
        super().validate_for(operation)
        self.result.validate_for(operation)
        exact_authority = OpaqueProviderRef(
            ref=f"authority/{authority.authority_id}/{authority.generation}/{authority.attempt_id}"
        )
        recovery = self.recovery
        if (
            recovery.system_id != operation.system_id
            or recovery.run_id != operation.run_id
            or recovery.plan_identity != operation.plan_identity
            or recovery.operation_nonce != operation.operation_nonce
            or recovery.pool.ref != self.source.pool
            or recovery.root_volume.ref != operation.root_volume.key
            or recovery.source_volume.ref != self.source.name
            or recovery.scratch_volume.ref != self.scratch.name
            or recovery.source_capacity_bytes != self.source.capacity_bytes
            or recovery.operation_identity != identity_for(operation)
            or recovery.result_identity != identity_for(self.result)
            or recovery.appliance_image_digest != operation.appliance_image_digest
            or recovery.authority_identity
            != RemoteModuleRecoveryRefV2.identity_for_authority(exact_authority)
        ):
            raise ValueError("remote module terminal recovery differs from exact operation")


type RemoteModulePreparationResponse = (
    RemoteModuleVolumePreparationResponseV1 | RemoteModuleTerminalPreparationResponseV1
)


class RemoteModuleTerminalRecord(BaseModel):
    """Exact authenticated request and terminal provider-host response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: RemoteModuleVolumePreparationRequestV1
    response: RemoteModuleTerminalPreparationResponseV1

    @model_validator(mode="after")
    def _terminal_matches_request(self) -> Self:
        self.response.validate_terminal_for(self.request.operation, self.request.authority)
        return self

    def to_canonical_json(self) -> bytes:
        encoded = _canonical_model_bytes(self)
        if len(encoded) > _MAX_PREPARATION_BYTES:
            raise ValueError("remote module terminal record exceeds 1048576 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_PREPARATION_BYTES:
            raise ValueError("remote module terminal record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote module terminal record is not canonical JSON")
        return value


class RemoteModuleVolumePreparationHost:
    """Completion-own one fixed provider-host volume preparation operation."""

    def __init__(
        self,
        prepare: Callable[[RemoteModuleOperationV1], PreparedModuleVolumes],
        executor: RemoteModulePreparationExecutor,
    ) -> None:
        self._prepare = prepare
        self._executor = executor

    async def execute(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModuleVolumePreparationResponseV1:
        volumes = await self._executor.run(lambda: self._prepare(request.operation))
        response = RemoteModuleVolumePreparationResponseV1.from_prepared(volumes)
        response.validate_for(request.operation)
        return response


class RemoteModuleVolumePreparationStore:
    """Descriptor-confined durable request/result evidence for provider-host restart."""

    def __init__(self, root: Path) -> None:
        self._root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        status = os.fstat(self._root_fd)
        if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o022:
            os.close(self._root_fd)
            raise PermissionError("remote preparation store must not be group/world writable")
        self._owner_uid = status.st_uid

    def close(self) -> None:
        descriptor, self._root_fd = self._root_fd, -1
        if descriptor >= 0:
            os.close(descriptor)

    @staticmethod
    def _key(request: RemoteModuleVolumePreparationRequestV1) -> str:
        authority = request.authority
        bound = "\0".join(
            (
                str(authority.system_id),
                str(authority.activation_id),
                str(authority.run_id),
                str(authority.generation),
                authority.operation_identity,
                str(authority.attempt_id),
            )
        ).encode()
        return hashlib.sha256(b"kdive-remote-preparation-v1\0" + bound).hexdigest()

    def _read(self, name: str) -> bytes | None:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._root_fd)
        except FileNotFoundError:
            return None
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or stat.S_IMODE(status.st_mode) != 0o600
                or status.st_uid != self._owner_uid
            ):
                raise PermissionError("remote preparation evidence mode is unsafe")
            chunks: list[bytes] = []
            remaining = _MAX_PREPARATION_BYTES + 1
            while remaining and (chunk := os.read(descriptor, min(65_536, remaining))):
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > _MAX_PREPARATION_BYTES:
                raise ValueError("remote preparation evidence exceeds 1048576 bytes")
            return data
        finally:
            os.close(descriptor)

    def _publish(self, name: str, data: bytes) -> None:
        existing = self._read(name)
        if existing is not None:
            if existing != data:
                raise ValueError("remote preparation evidence conflicts with durable bytes")
            return
        temporary = f".{name}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._root_fd,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=self._root_fd,
                    dst_dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if self._read(name) != data:
                    raise ValueError(
                        "remote preparation evidence conflicts with durable bytes"
                    ) from None
            os.unlink(temporary, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._root_fd)
            raise

    def stage(self, request: RemoteModuleVolumePreparationRequestV1) -> None:
        self._publish(f"{self._key(request)}.request", request.to_canonical_json())

    def reopen_request(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModuleVolumePreparationRequestV1:
        data = self._read(f"{self._key(request)}.request")
        if data is None:
            raise FileNotFoundError("remote preparation request is absent")
        reopened = RemoteModuleVolumePreparationRequestV1.from_canonical_json(data)
        if reopened != request:
            raise ValueError("remote preparation request differs from durable authority evidence")
        return reopened

    def publish_result(
        self,
        request: RemoteModuleVolumePreparationRequestV1,
        response: RemoteModulePreparationResponse,
    ) -> None:
        self.reopen_request(request)
        self._publish(f"{self._key(request)}.result", response.to_canonical_json())
        if isinstance(response, RemoteModuleTerminalPreparationResponseV1):
            terminal = RemoteModuleTerminalRecord(request=request, response=response)
            authority = request.authority
            key = self._terminal_key(
                authority.system_id,
                authority.activation_id,
                authority.run_id,
                authority.plan_identity,
            )
            self._publish(
                f"{key}.terminal",
                terminal.to_canonical_json(),
            )

    def reopen_result(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModulePreparationResponse | None:
        self.reopen_request(request)
        data = self._read(f"{self._key(request)}.result")
        if data is None:
            return None
        raw = json.loads(data)
        model = (
            RemoteModuleTerminalPreparationResponseV1
            if isinstance(raw, dict)
            and raw.get("schema") == "remote-module-terminal-preparation-response-v1"
            else RemoteModuleVolumePreparationResponseV1
        )
        return model.from_canonical_json(data)

    @staticmethod
    def _terminal_key(
        system_id: object, activation_id: object, run_id: object, plan_identity: str
    ) -> str:
        bound = "\0".join(
            (
                str(system_id),
                str(activation_id),
                str(run_id),
                plan_identity,
            )
        ).encode()
        return hashlib.sha256(b"kdive-remote-terminal-index-v1\0" + bound).hexdigest()

    def reopen_terminal(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteModuleTerminalRecord:
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        data = self._read(f"{key}.terminal")
        if data is None:
            raise FileNotFoundError("remote module terminal preparation is absent")
        record = RemoteModuleTerminalRecord.from_canonical_json(data)
        if (
            str(record.request.authority.system_id) != binding.system_id
            or str(record.request.authority.activation_id) != binding.activation_id
            or str(record.request.authority.run_id) != binding.run_id
            or record.request.authority.plan_identity != plan_identity
        ):
            raise ValueError("remote module terminal preparation binding differs")
        return record

    def publish_materialization(
        self, plan: ExternalBootPlan, materialization: ExternalBootMaterialization
    ) -> None:
        record = RemoteExternalBootMaterializationRecord(plan=plan, materialization=materialization)
        identity = materialization.identity.removeprefix("sha256:")
        self._publish(f"{identity}.materialization", record.to_canonical_json())

    def reopen_materialization(
        self, materialization: ExternalBootMaterialization
    ) -> RemoteExternalBootMaterializationRecord:
        identity = materialization.identity.removeprefix("sha256:")
        data = self._read(f"{identity}.materialization")
        if data is None:
            raise FileNotFoundError("remote materialization record is absent")
        record = RemoteExternalBootMaterializationRecord.from_canonical_json(data)
        if record.materialization != materialization:
            raise ValueError("remote materialization differs from durable record")
        return record

    def publish_recovery(self, recovery: RemoteExternalBootRecoveryRecord) -> OpaqueProviderRef:
        encoded = recovery.to_canonical_json()
        identity = hashlib.sha256(b"kdive-remote-recovery-v1\0" + encoded).hexdigest()
        self._publish(f"{identity}.recovery", encoded)
        self._publish(
            f"{self._recovery_key(recovery.binding, recovery.plan_identity)}.recovery-index",
            identity.encode("ascii"),
        )
        return OpaqueProviderRef(ref=f"remote-external-boot/{identity}")

    @staticmethod
    def _recovery_key(binding: ExternalBootActivationBinding, plan_identity: str) -> str:
        bound = "\0".join(
            (binding.system_id, binding.activation_id, binding.run_id, plan_identity)
        ).encode()
        return hashlib.sha256(b"kdive-remote-recovery-index-v1\0" + bound).hexdigest()

    def recovery_point(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RecoveryPoint:
        index = self._read(f"{self._recovery_key(binding, plan_identity)}.recovery-index")
        if index is None:
            raise FileNotFoundError("remote recovery index is absent")
        try:
            identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("remote recovery index is malformed") from None
        data = self._read(f"{identity}.recovery")
        if data is None:
            raise FileNotFoundError("remote recovery record is absent")
        if hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity:
            raise ValueError("remote recovery record identity mismatched")
        recovery = RemoteExternalBootRecoveryRecord.from_canonical_json(data)
        if recovery.binding != binding or recovery.plan_identity != plan_identity:
            raise ValueError("remote recovery index differs from durable record")
        return RecoveryPoint(
            binding=binding,
            plan_identity=plan_identity,
            materialization_identity=recovery.materialization.identity,
            recovery_ref=OpaqueProviderRef(ref=f"remote-external-boot/{identity}"),
            source_state=recovery.source_state,
            target_state=recovery.target_state,
        )

    def reopen_recovery(self, point: RecoveryPoint) -> RemoteExternalBootRecoveryRecord:
        prefix = "remote-external-boot/"
        if not point.recovery_ref.ref.startswith(prefix):
            raise ValueError("remote recovery reference has the wrong namespace")
        identity = point.recovery_ref.ref.removeprefix(prefix)
        if len(identity) != 64 or any(value not in "0123456789abcdef" for value in identity):
            raise ValueError("remote recovery reference is malformed")
        data = self._read(f"{identity}.recovery")
        if data is None:
            raise FileNotFoundError("remote recovery record is absent")
        if hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity:
            raise ValueError("remote recovery record identity mismatched")
        recovery = RemoteExternalBootRecoveryRecord.from_canonical_json(data)
        if (
            recovery.binding != point.binding
            or recovery.plan_identity != point.plan_identity
            or recovery.materialization.identity != point.materialization_identity
            or recovery.source_state != point.source_state
            or recovery.target_state != point.target_state
        ):
            raise ValueError("remote recovery point differs from durable record")
        return recovery


class RemoteModulePreparationOperation(Protocol):
    async def execute(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModulePreparationResponse: ...


class RemoteAuthorityMutationDelegate(Protocol):
    """Remote mutation implementation decorated with durable running reads."""

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1: ...

    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1: ...


class DurableRemoteModuleVolumePreparationHost:
    """Resume idempotent preparation from exact provider-private durable evidence."""

    def __init__(
        self,
        store: RemoteModuleVolumePreparationStore,
        host: RemoteModulePreparationOperation,
    ) -> None:
        self._store = store
        self._host = host

    async def execute(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModulePreparationResponse:
        self._store.stage(request)
        if result := self._store.reopen_result(request):
            result.validate_for(request.operation)
            return result
        result = await self._host.execute(self._store.reopen_request(request))
        self._store.publish_result(request, result)
        return result


class RemoteExternalBootRecoveryRecord(BaseModel):
    """Durable facts sufficient to reconstruct one remote activation after process loss."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    schema_: Literal["remote-libvirt-external-boot-recovery-v1"] = Field(
        "remote-libvirt-external-boot-recovery-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization: ExternalBootMaterialization
    definition: RemoteExternalBootDefinition
    module_recovery: RemoteModuleRecoveryRefV2
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity
    prior_power: Literal["running", "inactive"]
    recovery_objects: Annotated[tuple[OpaqueProviderRef, ...], Field(min_length=1, max_length=32)]

    @field_validator("recovery_objects")
    @classmethod
    def _objects_are_canonical(
        cls, values: tuple[OpaqueProviderRef, ...]
    ) -> tuple[OpaqueProviderRef, ...]:
        encoded = [value.to_canonical_json() for value in values]
        if len(encoded) != len(set(encoded)) or encoded != sorted(encoded):
            raise ValueError("recovery objects must be unique and canonically sorted")
        return values

    @model_validator(mode="after")
    def _identities_are_exact(self) -> Self:
        owner = self.binding
        materialization = self.materialization
        module = self.module_recovery
        definition = self.definition
        if (
            materialization.ownership.system_id != owner.system_id
            or materialization.ownership.run_id != owner.run_id
            or definition.binding != owner
            or module.system_id != owner.system_id
            or module.run_id != owner.run_id
        ):
            raise ValueError("remote recovery ownership differs from activation binding")
        if (
            materialization.plan_identity != self.plan_identity
            or definition.plan_identity != self.plan_identity
            or module.plan_identity != self.plan_identity
            or definition.materialization_identity != materialization.identity
        ):
            raise ValueError("remote recovery plan or materialization identity differs")
        if (
            self.source_state.definition != definition.source_definition
            or self.target_state.definition != definition.target_definition
        ):
            raise ValueError("remote recovery definition state differs")
        source_modules = self.source_state.modules
        target_modules = self.target_state.modules
        if (
            source_modules.state == "present"
            and source_modules.manifest != materialization.source_module_manifest
        ) or (
            target_modules.state != "present"
            or target_modules.manifest != materialization.installed_module_tree
        ):
            raise ValueError("remote recovery module state differs")
        encoded = self.to_canonical_json()
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ValueError("remote recovery record exceeds 1048576 bytes")
        return self

    def to_canonical_json(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ValueError("remote recovery record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote recovery record is not canonical JSON")
        return value


class RemoteExternalBootMaterializationRecord(BaseModel):
    """Exact plan and materialization retained for provider-host PREPARE restart."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    schema_: Literal["remote-libvirt-external-boot-materialization-v1"] = Field(
        "remote-libvirt-external-boot-materialization-v1", alias="schema"
    )
    plan: ExternalBootPlan
    materialization: ExternalBootMaterialization

    @model_validator(mode="after")
    def _materialization_matches_plan(self) -> Self:
        materialization = self.materialization
        if (
            materialization.plan_identity != self.plan.identity
            or materialization.ownership.system_id != self.plan.ownership.system_id
            or materialization.ownership.run_id != self.plan.ownership.run_id
        ):
            raise ValueError("remote materialization differs from its durable plan")
        return self

    def to_canonical_json(self) -> bytes:
        encoded = _canonical_model_bytes(self)
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ValueError("remote materialization record exceeds 1048576 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ValueError("remote materialization record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote materialization record is not canonical JSON")
        return value


class RemoteExternalBootOperations(Protocol):
    """The six closed operations available to the remote coordinator."""

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef, deadline: float
    ) -> ExternalBootMaterialization: ...

    def prepare(
        self,
        plan: ExternalBootPlan,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        modules: RemoteModuleTerminalRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> RemoteExternalBootRecoveryRecord: ...

    def activate(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None: ...

    def observe(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> RunningKernelObservation: ...

    def recover(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None: ...

    def cleanup(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None: ...


class RemoteExternalBootCoordinator:
    """Six-operation public adapter over exact durable provider-host recovery."""

    def __init__(
        self,
        operations: RemoteExternalBootOperations,
        store: RemoteModuleVolumePreparationStore,
        deadline: Callable[[], float],
    ) -> None:
        self._operations = operations
        self._store = store
        self._deadline = deadline

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        materialization = self._operations.materialize(plan, authority, self._deadline())
        if (
            materialization.plan_identity != plan.identity
            or materialization.ownership.system_id != plan.ownership.system_id
            or materialization.ownership.run_id != plan.ownership.run_id
        ):
            raise ValueError("remote materialization differs from the requested plan")
        self._store.publish_materialization(plan, materialization)
        return materialization

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryPoint:
        durable = self._store.reopen_materialization(materialization)
        modules = self._store.reopen_terminal(binding, materialization.plan_identity)
        recovery = self._operations.prepare(
            durable.plan, materialization, binding, modules, authority, self._deadline()
        )
        if recovery.binding != binding or recovery.materialization != materialization:
            raise ValueError("remote preparation returned a different activation")
        reference = self._store.publish_recovery(recovery)
        return RecoveryPoint(
            binding=recovery.binding,
            plan_identity=recovery.plan_identity,
            materialization_identity=recovery.materialization.identity,
            recovery_ref=reference,
            source_state=recovery.source_state,
            target_state=recovery.target_state,
        )

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        self._operations.activate(
            self._store.reopen_recovery(recovery), authority, self._deadline()
        )

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        return self._operations.observe(
            self._store.reopen_recovery(recovery), authority, self._deadline()
        )

    def recovery_point(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RecoveryPoint:
        """Reopen the exact provider-private recovery point after process restart."""
        return self._store.recovery_point(binding, plan_identity)

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        self._operations.recover(self._store.reopen_recovery(recovery), authority, self._deadline())

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        self._operations.cleanup(self._store.reopen_recovery(recovery), authority, self._deadline())


class RemoteExternalBootAuthorityAdapter:
    """Decorate remote mutations with completion-owned durable kernel observation."""

    def __init__(
        self,
        delegate: RemoteAuthorityMutationDelegate,
        coordinator: RemoteExternalBootCoordinator,
        executor: RemoteModulePreparationExecutor,
    ) -> None:
        self._delegate = delegate
        self._coordinator = coordinator
        self._executor = executor

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        return await self._delegate.observe(request)

    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1:
        return await self._delegate.commit(request, context)

    async def observe_running(
        self, request: AuthorityMutationRequestV1
    ) -> RunningKernelObservation:
        binding = ExternalBootActivationBinding(
            system_id=str(request.system_id),
            run_id=str(request.run_id),
            activation_id=str(request.activation_id),
        )
        authority = OpaqueProviderRef(
            ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
        )

        def read() -> RunningKernelObservation:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            if (
                point.source_state.definition != request.expected_source_identity
                or point.target_state.definition != request.intended_target_identity
            ):
                raise ValueError("remote running observation identities differ from request")
            return self._coordinator.observe(point, authority)

        return await self._executor.run(read)
