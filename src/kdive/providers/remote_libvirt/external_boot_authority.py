"""Closed remote external-boot coordinator contracts (#2200)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCleanupEvidenceContextV1,
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
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
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
_OBSERVATION_NAMESPACE = UUID("9cf0fa94-f250-4e5f-a950-155e7860b692")


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
            or operation.release != authority.plan.module_obligation.release
            or operation.source_manifest != authority.plan.module_obligation.source_manifest
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


class RemoteModulePreparationBeginRequestV1(BaseModel):
    """PREPARE authority plus a duration interpreted only on the authority clock."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True, strict=True)

    schema_: Literal["remote-module-preparation-begin-v1"] = Field(
        "remote-module-preparation-begin-v1", alias="schema"
    )
    authority: AuthorityPreparationMutationRequestV1
    budget_seconds: Annotated[int, Field(ge=1, le=900)]

    @model_validator(mode="after")
    def _prepare_only(self) -> Self:
        if self.authority.operation is not AuthorityOperation.PREPARE:
            raise ValueError("remote module begin requires the PREPARE authority phase")
        return self


class RemoteModulePreparationBeginResponseV1(BaseModel):
    """Authority-opened obligation and its authority-derived operation descriptor."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    schema_: Literal["remote-module-preparation-begin-response-v1"] = Field(
        "remote-module-preparation-begin-response-v1", alias="schema"
    )
    preparation: ModuleAttemptPreparationRequestV1
    operation: RemoteModuleOperationV1

    @model_validator(mode="after")
    def _receipt_matches_operation(self) -> Self:
        receipt = self.preparation.module_attempt_obligation
        if (
            self.operation.system_id != str(receipt.system_id)
            or self.operation.run_id != str(receipt.run_id)
            or self.operation.operation_nonce != receipt.operation_nonce
        ):
            raise ValueError("remote module operation differs from its obligation receipt")
        return self


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


class RemoteModulePreparationCompletionV1(BaseModel):
    """Durable completion published only after the provider call has stopped."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_: Literal["remote-module-preparation-completion-v1"] = Field(
        "remote-module-preparation-completion-v1", alias="schema"
    )
    state: Literal["terminal", "failed-after-mutation"]
    response: RemoteModuleTerminalPreparationResponseV1 | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.response is not None) != (self.state == "terminal"):
            raise ValueError("remote preparation completion state differs from response")
        return self

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote preparation completion is not canonical JSON")
        return value


class RemoteModuleLifecycleRequestV1(BaseModel):
    """One lifecycle action bound to the current authority, with no provider selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)
    schema_: Literal["remote-module-lifecycle-request-v1"] = Field(
        "remote-module-lifecycle-request-v1", alias="schema"
    )
    authority: AuthorityMutationRequestV1
    action: Literal["restore", "reap"]
    budget_seconds: Annotated[int, Field(ge=1, le=300)]

    @model_validator(mode="after")
    def _action_matches_authority(self) -> Self:
        allowed = (
            {AuthorityOperation.RECOVER, AuthorityOperation.RESOLVE_CONFLICT}
            if self.action == "restore"
            else {
                AuthorityOperation.CLEANUP,
                AuthorityOperation.TEARDOWN,
            }
        )
        if self.authority.operation not in allowed:
            raise ValueError("remote module lifecycle action differs from authority operation")
        return self

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote module lifecycle request is not canonical JSON")
        return value


class RemoteModuleLifecycleResponseV1(BaseModel):
    """Authenticated provider completion for restore or exact-volume absence."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)
    schema_: Literal["remote-module-lifecycle-response-v1"] = Field(
        "remote-module-lifecycle-response-v1", alias="schema"
    )
    action: Literal["restore", "reap"]
    recovery: RemoteModuleRecoveryRefV2
    operation: RemoteModuleOperationV1
    result: RemoteModuleResultV1
    volumes_absent: bool

    @model_validator(mode="after")
    def _terminal_shape(self) -> Self:
        self.result.validate_for(self.operation)
        terminal_phase = (
            self.operation.operation == "restore" and self.result.phase == "restored"
        ) or (
            self.action == "reap"
            and self.operation.operation == "capture_install"
            and self.result.phase == "installed"
        )
        if (
            not terminal_phase
            or self.result.status != "success"
            or self.volumes_absent != (self.action == "reap")
            or self.operation.system_id != self.recovery.system_id
            or self.operation.run_id != self.recovery.run_id
            or self.operation.plan_identity != self.recovery.plan_identity
            or self.operation.operation_nonce != self.recovery.operation_nonce
        ):
            raise ValueError("remote module lifecycle response is not terminal and bound")
        return self

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote module lifecycle response is not canonical JSON")
        return value


class _PersistedRemoteModuleLifecycleV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    request: RemoteModuleLifecycleRequestV1
    local_deadline: Annotated[float, Field(gt=0)]

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("persisted remote module lifecycle request is not canonical JSON")
        return value


class RemoteModuleLifecycleCompletionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["terminal", "failed-after-mutation"]
    response: RemoteModuleLifecycleResponseV1 | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.response is not None) != (self.state == "terminal"):
            raise ValueError("remote module lifecycle completion differs from response")
        return self

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote module lifecycle completion is not canonical JSON")
        return value


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


class _PersistedRemoteModulePreparationV1(BaseModel):
    """Authority-private request and one non-renewable local monotonic deadline."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    request: RemoteModuleVolumePreparationRequestV1
    local_deadline: Annotated[float, Field(gt=0)]

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("persisted remote module preparation is not canonical JSON")
        return value


@dataclass(frozen=True, slots=True)
class AdmittedRemoteModulePreparation:
    """Process-local view of the persisted request and authority-clock deadline."""

    request: RemoteModuleVolumePreparationRequestV1
    local_deadline: float


@dataclass(frozen=True, slots=True)
class AdmittedRemoteModuleLifecycle:
    request: RemoteModuleLifecycleRequestV1
    local_deadline: float


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
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=self._root_fd,
            )
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
        try:
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
                    if written <= 0:
                        raise OSError("remote preparation evidence write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
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
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._root_fd)

    def stage(self, request: RemoteModuleVolumePreparationRequestV1, local_deadline: float) -> None:
        name = f"{self._key(request)}.request"
        candidate = _PersistedRemoteModulePreparationV1(
            request=request, local_deadline=local_deadline
        )
        if data := self._read(name):
            persisted = _PersistedRemoteModulePreparationV1.from_canonical_json(data)
            if persisted.request != request:
                raise ValueError("remote preparation evidence conflicts with durable bytes")
            return
        self._publish(name, candidate.to_canonical_json())

    def reopen_request(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> AdmittedRemoteModulePreparation:
        data = self._read(f"{self._key(request)}.request")
        if data is None:
            raise FileNotFoundError("remote preparation request is absent")
        reopened = _PersistedRemoteModulePreparationV1.from_canonical_json(data)
        if reopened.request != request:
            raise ValueError("remote preparation evidence conflicts with durable bytes")
        return AdmittedRemoteModulePreparation(reopened.request, reopened.local_deadline)

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
            self._publish(
                f"{self._key(request)}.completion",
                RemoteModulePreparationCompletionV1(
                    state="terminal", response=response
                ).to_canonical_json(),
            )

    def publish_failed_completion(self, request: RemoteModuleVolumePreparationRequestV1) -> None:
        self.reopen_request(request)
        self._publish(
            f"{self._key(request)}.completion",
            RemoteModulePreparationCompletionV1(state="failed-after-mutation").to_canonical_json(),
        )

    def reopen_completion(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModulePreparationCompletionV1 | None:
        self.reopen_request(request)
        data = self._read(f"{self._key(request)}.completion")
        return (
            None if data is None else RemoteModulePreparationCompletionV1.from_canonical_json(data)
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
    def _lifecycle_key(request: RemoteModuleLifecycleRequestV1) -> str:
        identity = json.dumps(
            request.model_dump(
                mode="json",
                by_alias=True,
                exclude={"budget_seconds"},
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(b"kdive-remote-module-lifecycle-v1\0" + identity).hexdigest()

    @staticmethod
    def _same_lifecycle(
        left: RemoteModuleLifecycleRequestV1,
        right: RemoteModuleLifecycleRequestV1,
    ) -> bool:
        return left.authority == right.authority and left.action == right.action

    def stage_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1, local_deadline: float
    ) -> None:
        name = f"{self._lifecycle_key(request)}.lifecycle-request"
        candidate = _PersistedRemoteModuleLifecycleV1(
            request=request, local_deadline=local_deadline
        )
        if data := self._read(name):
            persisted = _PersistedRemoteModuleLifecycleV1.from_canonical_json(data)
            if not self._same_lifecycle(persisted.request, request):
                raise ValueError("remote lifecycle evidence conflicts with durable bytes")
            return
        self._publish(name, candidate.to_canonical_json())

    def reopen_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> AdmittedRemoteModuleLifecycle:
        data = self._read(f"{self._lifecycle_key(request)}.lifecycle-request")
        if data is None:
            raise FileNotFoundError("remote module lifecycle request is absent")
        persisted = _PersistedRemoteModuleLifecycleV1.from_canonical_json(data)
        if not self._same_lifecycle(persisted.request, request):
            raise ValueError("remote lifecycle evidence conflicts with durable bytes")
        return AdmittedRemoteModuleLifecycle(persisted.request, persisted.local_deadline)

    def publish_lifecycle_completion(
        self,
        request: RemoteModuleLifecycleRequestV1,
        response: RemoteModuleLifecycleResponseV1 | None,
    ) -> None:
        self.reopen_lifecycle(request)
        completion = RemoteModuleLifecycleCompletionV1(
            state="terminal" if response is not None else "failed-after-mutation",
            response=response,
        )
        self._publish(
            f"{self._lifecycle_key(request)}.lifecycle-completion",
            completion.to_canonical_json(),
        )
        if response is not None and response.action == "restore":
            authority = request.authority
            restored_key = self._terminal_key(
                authority.system_id,
                authority.activation_id,
                authority.run_id,
                authority.plan_identity,
            )
            self._publish(f"{restored_key}.restored", response.to_canonical_json())

    def reopen_lifecycle_completion(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> RemoteModuleLifecycleCompletionV1 | None:
        self.reopen_lifecycle(request)
        data = self._read(f"{self._lifecycle_key(request)}.lifecycle-completion")
        return None if data is None else RemoteModuleLifecycleCompletionV1.from_canonical_json(data)

    def reopen_restored(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteModuleLifecycleResponseV1 | None:
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        data = self._read(f"{key}.restored")
        if data is None:
            return None
        response = RemoteModuleLifecycleResponseV1.from_canonical_json(data)
        if response.action != "restore":
            raise ValueError("remote module restored evidence has the wrong action")
        return response

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

    @staticmethod
    def _preparation_key(request: ExternalBootPreparationRequest) -> str:
        return hashlib.sha256(
            b"kdive-remote-external-boot-preparation-v1\0" + request.to_canonical_json()
        ).hexdigest()

    @staticmethod
    def _validate_preparation(
        request: ExternalBootPreparationRequest,
        receipt: ExternalBootPreparationObservation,
    ) -> None:
        ExternalBootPreparationObservation.model_validate(
            receipt.model_dump(mode="python", by_alias=True)
        )
        expected_state = "materialized" if request.phase == "materialize" else "prepared"
        if (
            receipt.state == "absent"
            or receipt.state != expected_state
            or receipt.binding != request.binding
            or receipt.plan_identity != request.plan.identity
            or receipt.authority != request.authority
            or receipt.operation_identity != request.operation_identity
        ):
            detail = (
                "phase and state differ"
                if receipt.state != expected_state
                else "receipt differs from request"
            )
            raise ValueError(f"remote preparation {detail}")

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        data = self._read(f"{self._preparation_key(request)}.preparation")
        if data is not None:
            receipt = ExternalBootPreparationObservation.from_canonical_json(data)
            self._validate_preparation(request, receipt)
            return receipt
        return ExternalBootPreparationObservation(
            state="absent",
            binding=request.binding,
            plan_identity=request.plan.identity,
            authority=request.authority,
            operation_identity=request.operation_identity,
        )

    def publish_preparation(
        self, request: ExternalBootPreparationRequest, receipt: ExternalBootPreparationObservation
    ) -> ExternalBootPreparationObservation:
        self._validate_preparation(request, receipt)
        existing = self.observe_preparation(request)
        if existing.state != "absent":
            if existing != receipt:
                raise ValueError("remote preparation receipt conflicts with durable bytes")
            return existing
        self._publish(f"{self._preparation_key(request)}.preparation", receipt.to_canonical_json())
        reopened = self.observe_preparation(request)
        if reopened != receipt:
            raise ValueError("published remote preparation receipt failed exact reopen")
        return reopened

    def publish_materialization(
        self, plan: ExternalBootPlan, materialization: ExternalBootMaterialization
    ) -> None:
        record = RemoteExternalBootMaterializationRecord(plan=plan, materialization=materialization)
        identity = materialization.identity.removeprefix("sha256:")
        self._publish(f"{identity}.materialization", record.to_canonical_json())
        self._publish(
            f"{plan.identity.removeprefix('sha256:')}.materialization-index",
            identity.encode("ascii"),
        )

    def reopen_materialization_for_plan(
        self, plan: ExternalBootPlan
    ) -> RemoteExternalBootMaterializationRecord:
        index = self._read(f"{plan.identity.removeprefix('sha256:')}.materialization-index")
        if index is None:
            raise FileNotFoundError("remote materialization plan index is absent")
        try:
            identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("remote materialization plan index is malformed") from None
        if len(identity) != 64 or any(value not in "0123456789abcdef" for value in identity):
            raise ValueError("remote materialization plan index is malformed")
        data = self._read(f"{identity}.materialization")
        if data is None:
            raise ValueError("remote materialization plan index is incomplete")
        record = RemoteExternalBootMaterializationRecord.from_canonical_json(data)
        if (
            record.plan != plan
            or record.materialization.identity.removeprefix("sha256:") != identity
        ):
            raise ValueError("remote materialization plan index differs")
        return record

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
        for reference in recovery.recovery_objects:
            object_key = hashlib.sha256(
                b"kdive-remote-recovery-object-v1\0" + reference.to_canonical_json()
            ).hexdigest()
            self._publish(f"{object_key}.object-index", identity.encode("ascii"))
        return OpaqueProviderRef(ref=f"remote-external-boot/{identity}")

    def recovery_for_object(self, reference: OpaqueProviderRef) -> RemoteExternalBootRecoveryRecord:
        object_key = hashlib.sha256(
            b"kdive-remote-recovery-object-v1\0" + reference.to_canonical_json()
        ).hexdigest()
        index = self._read(f"{object_key}.object-index")
        if index is None:
            raise FileNotFoundError("remote recovery object index is absent")
        try:
            identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("remote recovery object index is malformed") from None
        data = self._read(f"{identity}.recovery")
        if (
            data is None
            or hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity
        ):
            raise ValueError("remote recovery object index is unauthenticated")
        recovery = RemoteExternalBootRecoveryRecord.from_canonical_json(data)
        if reference not in recovery.recovery_objects:
            raise ValueError("remote recovery object differs from durable record")
        return recovery

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
    async def derive_operation(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
    ) -> RemoteModuleOperationV1: ...

    async def execute(
        self, admitted: AdmittedRemoteModulePreparation
    ) -> RemoteModuleTerminalPreparationResponseV1: ...

    async def execute_lifecycle(
        self,
        request: RemoteModuleLifecycleRequestV1,
        terminal: RemoteModuleTerminalRecord,
        restored: RemoteModuleLifecycleResponseV1 | None,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1: ...


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
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._host = host
        self._monotonic = monotonic

    async def begin(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
        budget_seconds: int,
    ) -> RemoteModulePreparationBeginResponseV1:
        """Derive and persist one descriptor before returning it to the worker."""
        operation = await self._host.derive_operation(authority, preparation)
        request = RemoteModuleVolumePreparationRequestV1(authority=authority, operation=operation)
        self._store.stage(request, self._monotonic() + budget_seconds)
        admitted = self._store.reopen_request(request)
        return RemoteModulePreparationBeginResponseV1(
            preparation=preparation, operation=admitted.request.operation
        )

    async def execute(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModuleTerminalPreparationResponseV1:
        admitted = self._store.reopen_request(request)
        completion = self._store.reopen_completion(request)
        if completion is not None:
            if completion.response is not None:
                completion.response.validate_for(request.operation)
                return completion.response
            raise CategorizedError(
                "remote module preparation requires recovery",
                category=ErrorCategory.CONFLICT,
                details={"completion": completion.state},
            )
        if result := self._store.reopen_result(request):
            result.validate_for(request.operation)
        task = asyncio.create_task(self._host.execute(admitted))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            if task.cancelled() or task.exception() is not None:
                self._store.publish_failed_completion(request)
            else:
                result = task.result()
                self._store.publish_result(request, result)
            raise cancelled from None
        except BaseException:
            self._store.publish_failed_completion(request)
            raise
        self._store.publish_result(request, result)
        return result

    async def observe_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> RemoteModuleLifecycleResponseV1 | None:
        try:
            completion = self._store.reopen_lifecycle_completion(request)
        except FileNotFoundError:
            return None
        if completion is None:
            return None
        if completion.response is None:
            raise CategorizedError(
                "remote module lifecycle requires recovery",
                category=ErrorCategory.CONFLICT,
                details={"completion": completion.state},
            )
        return completion.response

    async def execute_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> RemoteModuleLifecycleResponseV1:
        self._store.stage_lifecycle(request, self._monotonic() + request.budget_seconds)
        admitted = self._store.reopen_lifecycle(request)
        request = admitted.request
        if completion := await self.observe_lifecycle(request):
            return completion
        binding = ExternalBootActivationBinding(
            system_id=str(request.authority.system_id),
            run_id=str(request.authority.run_id),
            activation_id=str(request.authority.activation_id),
        )
        terminal = self._store.reopen_terminal(binding, request.authority.plan_identity)
        restored = self._store.reopen_restored(binding, request.authority.plan_identity)
        if request.action == "restore" and restored is not None:
            self._store.publish_lifecycle_completion(request, restored)
            return restored
        if (
            request.action == "reap"
            and restored is None
            and request.authority.operation is not AuthorityOperation.TEARDOWN
        ):
            raise CategorizedError(
                "ordinary remote module cleanup requires restored evidence",
                category=ErrorCategory.CONFLICT,
                details={"completion": "refused-before-mutation"},
            )
        task = asyncio.create_task(
            self._host.execute_lifecycle(request, terminal, restored, admitted.local_deadline)
        )
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            self._store.publish_lifecycle_completion(
                request, None if task.cancelled() or task.exception() is not None else task.result()
            )
            raise cancelled from None
        except BaseException:
            self._store.publish_lifecycle_completion(request, None)
            raise
        self._store.publish_lifecycle_completion(request, response)
        return response


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
        self,
        plan: ExternalBootPlan,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
        deadline: float,
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
        self,
        plan: ExternalBootPlan,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> ExternalBootMaterialization:
        if binding.system_id != plan.ownership.system_id or binding.run_id != plan.ownership.run_id:
            raise ValueError("remote materialization binding differs from the requested plan")
        materialization = self._operations.materialize(plan, binding, authority, self._deadline())
        if (
            materialization.plan_identity != plan.identity
            or materialization.ownership.system_id != plan.ownership.system_id
            or materialization.ownership.run_id != plan.ownership.run_id
        ):
            raise ValueError("remote materialization differs from the requested plan")
        self._store.publish_materialization(plan, materialization)
        return materialization

    def execute_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        observed = self._store.observe_preparation(request)
        if observed.state != "absent":
            return observed
        if request.phase == "materialize":
            materialization = self.materialize(request.plan, request.binding, request.authority)
            receipt = observed.model_copy(
                update={"state": "materialized", "materialization": materialization}
            )
        else:
            materialization = self._store.reopen_materialization_for_plan(
                request.plan
            ).materialization
            recovery = self.prepare(materialization, request.binding, request.authority)
            receipt = ExternalBootPreparationObservation(
                state="prepared",
                binding=request.binding,
                plan_identity=request.plan.identity,
                authority=request.authority,
                operation_identity=request.operation_identity,
                materialization=materialization,
                recovery_point=recovery,
            )
        return self._store.publish_preparation(request, receipt)

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        return self._store.observe_preparation(request)

    def adopt_preparation(
        self,
        request: ExternalBootPreparationRequest,
        predecessor: ExternalBootPreparationRequest,
        predecessor_receipt_identity: str,
    ) -> ExternalBootPreparationObservation:
        receipt = self._store.observe_preparation(predecessor)
        if (
            receipt.state == "absent"
            or receipt.identity != predecessor_receipt_identity
            or request.phase != predecessor.phase
            or request.binding != predecessor.binding
            or request.plan.identity != predecessor.plan.identity
        ):
            raise ValueError("preparation predecessor cannot be adopted")
        adopted = receipt.model_copy(
            update={
                "authority": request.authority,
                "operation_identity": request.operation_identity,
            }
        )
        return self._store.publish_preparation(request, adopted)

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

    def recovery_record(self, recovery: RecoveryPoint) -> RemoteExternalBootRecoveryRecord:
        return self._store.reopen_recovery(recovery)

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
        close: Callable[[], None] | None = None,
    ) -> None:
        self._delegate = delegate
        self._coordinator = coordinator
        self._executor = executor
        self._close = close

    def close(self) -> None:
        self._executor.shutdown()
        if self._close is not None:
            self._close()

    @staticmethod
    def _preparation_request(
        request: AuthorityPreparationMutationRequestV1,
    ) -> ExternalBootPreparationRequest:
        if request.operation not in {AuthorityOperation.MATERIALIZE, AuthorityOperation.PREPARE}:
            raise ValueError("remote preparation request has a non-preparation operation")
        phase: Literal["materialize", "prepare"] = (
            "materialize" if request.operation is AuthorityOperation.MATERIALIZE else "prepare"
        )
        return ExternalBootPreparationRequest(
            phase=phase,
            plan=request.plan,
            binding=ExternalBootActivationBinding(
                system_id=str(request.system_id),
                run_id=str(request.run_id),
                activation_id=str(request.activation_id),
            ),
            authority=OpaqueProviderRef(
                ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
            ),
            operation_identity=request.operation_identity,
        )

    @staticmethod
    def _preparation_observation(
        receipt: ExternalBootPreparationObservation,
    ) -> AuthorityObservationV1:
        return AuthorityObservationV1(
            observation_id=uuid5(_OBSERVATION_NAMESPACE, receipt.identity),
            category="target",
            composite_state=receipt.identity,
        )

    async def observe(
        self, request: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1
    ) -> AuthorityObservationV1:
        if isinstance(request, AuthorityPreparationMutationRequestV1):
            receipt = await self._executor.run(
                lambda: self._coordinator.observe_preparation(self._preparation_request(request))
            )
            return self._preparation_observation(receipt)
        return await self._delegate.observe(request)

    async def commit(
        self,
        request: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
        context: AuthorityCommitContextV1,
    ) -> AuthorityObservationV1:
        if isinstance(request, AuthorityPreparationMutationRequestV1):
            if (
                context.operation_identity != request.operation_identity
                or context.attempt_id != request.attempt_id
            ):
                raise ValueError("preparation commit context differs from request")
            receipt = await self._executor.run(
                lambda: self._coordinator.execute_preparation(self._preparation_request(request))
            )
            return self._preparation_observation(receipt)
        return await self._delegate.commit(request, context)

    async def preparation_receipt(
        self, request: AuthorityPreparationMutationRequestV1
    ) -> ExternalBootPreparationObservation:
        return await self._executor.run(
            lambda: self._coordinator.observe_preparation(self._preparation_request(request))
        )

    async def cleanup_subject(self, request: AuthorityMutationRequestV1) -> str:
        binding = ExternalBootActivationBinding(
            system_id=str(request.system_id),
            run_id=str(request.run_id),
            activation_id=str(request.activation_id),
        )

        def reopen() -> RemoteExternalBootRecoveryRecord:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            return self._coordinator.recovery_record(point)

        record = await self._executor.run(reopen)
        return record.module_recovery.operation_nonce

    async def commit_cleanup(
        self,
        request: AuthorityMutationRequestV1,
        context: AuthorityCommitContextV1,
        evidence: AuthorityCleanupEvidenceContextV1,
    ) -> AuthorityObservationV1:
        if (
            evidence.operation_identity != request.operation_identity
            or evidence.attempt_id != request.attempt_id
            or context.operation_identity != request.operation_identity
            or context.attempt_id != request.attempt_id
        ):
            raise ValueError("remote cleanup evidence differs from exact request")
        binding = ExternalBootActivationBinding(
            system_id=str(request.system_id),
            run_id=str(request.run_id),
            activation_id=str(request.activation_id),
        )

        def reopen() -> tuple[RecoveryPoint, RemoteExternalBootRecoveryRecord]:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            return point, self._coordinator.recovery_record(point)

        point, record = await self._executor.run(reopen)
        reference = RemoteModuleRecoveryRefV2.model_validate_json(evidence.recovery_reference_json)
        if (
            reference != record.module_recovery
            or evidence.operation_nonce != reference.operation_nonce
        ):
            raise ValueError("remote cleanup evidence changed before provider deletion")
        authority = OpaqueProviderRef(
            ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
        )
        await self._executor.run(lambda: self._coordinator.recover(point, authority))
        await self._executor.run(lambda: self._coordinator.cleanup(point, authority))
        return await self.observe(request)

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
