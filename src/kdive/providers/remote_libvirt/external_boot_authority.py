"""Closed remote external-boot coordinator contracts (#2200)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, NoReturn, Protocol, Self, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCleanupEvidenceContextV1,
    AuthorityCommitContextV1,
    AuthorityConflictResolutionRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
    AuthorityTeardownMutationRequestV1,
    ObservationCategory,
    operation_is_permitted,
)
from kdive.providers.external_boot_authority.teardown import (
    AuthoritySystemTeardownFacts,
    AuthorityTeardownReservationV1,
    SystemTeardownSupersededError,
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
    RecoveryObjectBinding,
    RecoveryObjectObservation,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    AcknowledgedDefinition,
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

logger = logging.getLogger(__name__)

_MAX_RECORD_BYTES = 1_048_576
_MAX_PREPARATION_BYTES = 1_048_576
_OBSERVATION_NAMESPACE = UUID("9cf0fa94-f250-4e5f-a950-155e7860b692")
type PriorPower = Literal["running", "inactive"]
PREPARATION_ABANDONED = "abandoned"
"""Detail on a PREPARE failure the provider settled: the System's power is restored."""
type RemoteSystemTeardownPhase = Literal[
    "intent-recorded",
    "domain-validated",
    "domain-destroyed",
    "recovery-removed",
    "domain-undefined",
    "artifacts-removed",
    "complete",
]


def _canonical_model_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


type _AuthorityRequest = (
    AuthorityMutationRequestV1
    | AuthorityPreparationMutationRequestV1
    | AuthorityTeardownMutationRequestV1
)


def authority_reference(request: _AuthorityRequest) -> OpaqueProviderRef:
    """The opaque provider reference minted for one exact authority attempt."""
    return OpaqueProviderRef(
        ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
    )


def _activation_binding(request: _AuthorityRequest) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=str(request.system_id),
        run_id=str(request.run_id),
        activation_id=str(request.activation_id),
    )


class RemoteSystemTeardownAnchorV1(BaseModel):
    """Path-free authority request and mutation-started anchor for remote teardown."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True, strict=True)

    schema_: Literal["remote-libvirt-system-teardown-anchor-v1"] = Field(
        "remote-libvirt-system-teardown-anchor-v1", alias="schema"
    )
    authority_id: UUID
    generation: Annotated[int, Field(ge=1)]
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    provider_kind: Annotated[str, Field(min_length=1, max_length=255)]
    authority_instance: Annotated[str, Field(min_length=1, max_length=255)]
    operation_identity: Annotated[str, Field(min_length=1, max_length=255)]
    operation_digest: Digest
    attempt_id: UUID
    journal_sequence: Annotated[int, Field(ge=1)]
    journal_digest: Digest

    @property
    def identity(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical_model_bytes(self)).hexdigest()


class RemoteSystemTeardownIntentV1(RemoteSystemTeardownAnchorV1):
    """Exact request and accounting owner persisted before the first host mutation."""

    schema_: Literal["remote-libvirt-system-teardown-intent-v1"] = Field(
        "remote-libvirt-system-teardown-intent-v1", alias="schema"
    )
    reservation: AuthorityTeardownReservationV1

    def matches_anchor(self, anchor: RemoteSystemTeardownAnchorV1) -> bool:
        left = self.model_dump(exclude={"schema_", "reservation"})
        right = anchor.model_dump(exclude={"schema_"})
        return left == right

    def anchor_subject_matches(self, anchor: RemoteSystemTeardownAnchorV1) -> bool:
        """Compare the subject fields an anchor carries; it has no reservation (#2921)."""
        return (
            self.binding,
            self.plan_identity,
            self.provider_kind,
            self.authority_instance,
        ) == (anchor.binding, anchor.plan_identity, anchor.provider_kind, anchor.authority_instance)

    def same_subject(self, other: RemoteSystemTeardownIntentV1) -> bool:
        return (
            self.binding,
            self.plan_identity,
            self.provider_kind,
            self.authority_instance,
            self.reservation,
        ) == (
            other.binding,
            other.plan_identity,
            other.provider_kind,
            other.authority_instance,
            other.reservation,
        )


class RemoteSystemTeardownRecordV1(BaseModel):
    """Durable remote teardown checkpoint retained across process loss."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True, strict=True)

    schema_: Literal["remote-libvirt-system-teardown-record-v1"] = Field(
        "remote-libvirt-system-teardown-record-v1", alias="schema"
    )
    intent: RemoteSystemTeardownIntentV1
    phase: RemoteSystemTeardownPhase
    domain_validated: bool
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _completion_matches_phase(self) -> Self:
        if (self.phase == "complete") != (self.completed_at is not None):
            raise ValueError("remote System teardown completion timestamp differs from phase")
        if self.completed_at is not None and self.completed_at.utcoffset() != UTC.utcoffset(
            self.completed_at
        ):
            raise ValueError("remote System teardown completion timestamp must be UTC")
        return self

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_PREPARATION_BYTES:
            raise ValueError("remote System teardown record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote System teardown record is not canonical JSON")
        return value


@dataclass(frozen=True, slots=True)
class RemoteSystemTeardownState:
    """Exact provider-private records available for one teardown subject."""

    binding: ExternalBootActivationBinding
    plan_identity: str
    preparation: RemoteModuleVolumePreparationRequestV1 | None
    terminal: RemoteModuleTerminalRecord | None
    materialization: RemoteExternalBootMaterializationRecord | None
    recovery: RemoteExternalBootRecoveryRecord | None
    finalized: RemoteFinalizedTeardownRefsV1 | None = None


@dataclass(frozen=True, slots=True)
class RemoteExternalBootObservedState:
    """One live read of a remote activation's definition, power, and owned volumes.

    ``definition`` names which recorded side the inactive definition is: ``source``,
    ``target`` or ``other``. ``recovery_objects_absent`` is true only when every boot
    artifact and module volume the recovery record owns reads absent.
    """

    definition: str
    preserved: str
    boot: str
    active: bool
    recovery_objects_absent: bool


@dataclass(frozen=True, slots=True)
class RemoteSystemTeardownInspection:
    domain_absent: bool
    overlay_absent: bool
    baseline_absent: bool
    recovery_absent: bool


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
        exact_authority = authority_reference(authority)
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


class RemoteModuleSystemTeardownRequestV1(BaseModel):
    """Internal module reap request carrying the distinct System teardown authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)
    schema_: Literal["remote-module-system-teardown-request-v1"] = Field(
        "remote-module-system-teardown-request-v1", alias="schema"
    )
    authority: AuthorityTeardownMutationRequestV1
    budget_seconds: Annotated[int, Field(ge=1, le=300)]


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


class RemoteCleanupFinalizedError(FileNotFoundError):
    """A per-activation record is gone because its cleanup was finalized, not never written."""


class RemoteCleanupQuarantineV1(BaseModel):
    """Provider-private proof that release cleanup ran, kept until authority finalization.

    Bound to the anchored mutation-started journal record, so finalization and orphan
    disposition act only on the cleanup that record authorized.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["remote-libvirt-cleanup-quarantine-v1"] = Field(
        "remote-libvirt-cleanup-quarantine-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    recovery_ref: OpaqueProviderRef
    operation_identity: Annotated[str, Field(min_length=1, max_length=255)]
    attempt_id: UUID
    journal_sequence: Annotated[int, Field(ge=1)]
    journal_digest: Digest
    managed: bool = False

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ValueError("remote cleanup quarantine receipt exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote cleanup quarantine receipt is not canonical JSON")
        return value


class RemoteFinalizedBootArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: Annotated[str, Field(min_length=1, max_length=1024)]
    kind: Literal["kernel", "initrd"]
    sha256: Annotated[str, Field(min_length=1, max_length=128)] | None


class RemoteFinalizedTeardownRefsV1(BaseModel):
    """What System teardown must still prove absent once finalize removed the records naming it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    module_volumes: tuple[tuple[str, str], ...]
    operation_nonce: Annotated[str, Field(min_length=1, max_length=1024)] | None
    boot_artifacts: tuple[RemoteFinalizedBootArtifactV1, ...]


class RemoteCleanupFinalizedMarkerV1(BaseModel):
    """The ``cleanup-finalized`` marker: the cleaned receipt plus the refs teardown still needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["remote-libvirt-cleanup-finalized-v1"] = Field(
        "remote-libvirt-cleanup-finalized-v1", alias="schema"
    )
    receipt: RemoteCleanupQuarantineV1
    teardown: RemoteFinalizedTeardownRefsV1

    def to_canonical_json(self) -> bytes:
        return _canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ValueError("remote cleanup finalized marker exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("remote cleanup finalized marker is not canonical JSON")
        return value


def _parse_finalized_marker(
    data: bytes,
) -> tuple[RemoteCleanupQuarantineV1, RemoteFinalizedTeardownRefsV1 | None]:
    """Receipt and teardown refs; a marker from before the refs existed has none."""
    try:
        marker = RemoteCleanupFinalizedMarkerV1.from_canonical_json(data)
    except ValueError:
        try:
            return RemoteCleanupQuarantineV1.from_canonical_json(data), None
        except ValueError:
            raise ValueError("remote cleanup finalized marker is malformed") from None
    return marker.receipt, marker.teardown


def _quarantine_observation(receipt: RemoteCleanupQuarantineV1) -> RecoveryObjectObservation:
    reference = receipt.recovery_ref
    binding = RecoveryObjectBinding(
        record_id=str(
            uuid5(
                NAMESPACE_URL,
                f"kdive/remote-recovery/{reference.ref}/{receipt.journal_digest}",
            )
        ),
        binding=receipt.binding,
        kind="recovery-record",
        reference=reference,
        ownership_digest="sha256:" + _recovery_identity(reference),
        operation_identity=receipt.operation_identity,
        attempt_id=str(receipt.attempt_id),
        mutation_journal_sequence=receipt.journal_sequence,
        mutation_journal_digest=receipt.journal_digest,
        reserved_bytes=0,
    )
    digest = (
        "sha256:"
        + hashlib.sha256(
            b"kdive-remote-cleanup-quarantine-observation-v1\0" + receipt.to_canonical_json()
        ).hexdigest()
    )
    return RecoveryObjectObservation(
        binding=binding, present=True, managed=receipt.managed, observed_digest=digest
    )


def _recovery_identity(reference: OpaqueProviderRef) -> str:
    prefix = "remote-external-boot/"
    if not reference.ref.startswith(prefix):
        raise ValueError("remote recovery reference has the wrong namespace")
    identity = reference.ref.removeprefix(prefix)
    if len(identity) != 64 or any(value not in "0123456789abcdef" for value in identity):
        raise ValueError("remote recovery reference is malformed")
    return identity


def _authority_binding(
    authority: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=str(authority.system_id),
        run_id=str(authority.run_id),
        activation_id=str(authority.activation_id),
    )


def _authority_owned(
    authority: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
    binding: ExternalBootActivationBinding,
    plan_identity: str,
) -> bool:
    return (
        str(authority.system_id),
        str(authority.activation_id),
        str(authority.run_id),
        authority.plan_identity,
    ) == (binding.system_id, binding.activation_id, binding.run_id, plan_identity)


def _object_index_name(reference: OpaqueProviderRef) -> str:
    key = hashlib.sha256(b"kdive-remote-recovery-object-v1\0" + reference.to_canonical_json())
    return f"{key.hexdigest()}.object-index"


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
        return RemoteModuleVolumePreparationStore._authority_key(request.authority)

    @staticmethod
    def _authority_key(authority: AuthorityPreparationMutationRequestV1) -> str:
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

    def _replace(self, name: str, expected: bytes, data: bytes) -> None:
        if self._read(name) != expected:
            raise ValueError("remote teardown evidence changed before checkpoint")
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
                        raise OSError("remote teardown evidence write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._root_fd)

    @staticmethod
    def _system_teardown_key(binding: ExternalBootActivationBinding) -> str:
        bound = binding.system_id.encode()
        return hashlib.sha256(b"kdive-remote-system-teardown-v1\0" + bound).hexdigest()

    def begin_system_teardown(
        self, intent: RemoteSystemTeardownIntentV1
    ) -> RemoteSystemTeardownRecordV1:
        name = f"{self._system_teardown_key(intent.binding)}.teardown"
        data = self._read(name)
        if data is None:
            record = RemoteSystemTeardownRecordV1(
                intent=intent, phase="intent-recorded", domain_validated=False
            )
            self._publish(name, record.to_canonical_json())
            return record
        record = RemoteSystemTeardownRecordV1.from_canonical_json(data)
        if record.intent == intent:
            return record
        if not record.intent.same_subject(intent) or intent.generation <= record.intent.generation:
            raise ValueError("remote System teardown conflicts with retained intent")
        adopted = record.model_copy(update={"intent": intent})
        self._replace(name, data, adopted.to_canonical_json())
        return adopted

    def reopen_system_teardown(
        self, anchor: RemoteSystemTeardownAnchorV1
    ) -> RemoteSystemTeardownRecordV1 | None:
        """Reopen the record this anchor owns; ``None`` when it never reached ``begin``.

        ADR-0620 (#2921): an earlier generation's record of the same subject is not this
        anchor's, so it reopens as absent; a later generation's means this one is superseded.
        """
        name = f"{self._system_teardown_key(anchor.binding)}.teardown"
        data = self._read(name)
        if data is None:
            return None
        record = RemoteSystemTeardownRecordV1.from_canonical_json(data)
        if record.intent.matches_anchor(anchor):
            return record
        if (
            not record.intent.anchor_subject_matches(anchor)
            or record.intent.generation == anchor.generation
        ):
            raise ValueError("remote System teardown observation conflicts with retained intent")
        if record.intent.generation > anchor.generation:
            raise SystemTeardownSupersededError
        return None

    def checkpoint_system_teardown(
        self,
        record: RemoteSystemTeardownRecordV1,
        phase: RemoteSystemTeardownPhase,
        *,
        completed_at: datetime | None = None,
    ) -> RemoteSystemTeardownRecordV1:
        phases: tuple[RemoteSystemTeardownPhase, ...] = (
            "intent-recorded",
            "domain-validated",
            "domain-destroyed",
            "recovery-removed",
            "domain-undefined",
            "artifacts-removed",
            "complete",
        )
        current = phases.index(record.phase)
        requested = phases.index(phase)
        if requested < current:
            return record
        if requested > current + 1:
            raise ValueError("remote System teardown phase skipped a checkpoint")
        if requested == current:
            return record
        updated = record.model_copy(
            update={
                "phase": phase,
                "domain_validated": record.domain_validated or phase == "domain-validated",
                "completed_at": completed_at,
            }
        )
        updated = RemoteSystemTeardownRecordV1.from_canonical_json(updated.to_canonical_json())
        key = self._system_teardown_key(record.intent.binding)
        name = f"{key}.teardown"
        self._replace(name, record.to_canonical_json(), updated.to_canonical_json())
        return updated

    def stage(self, request: RemoteModuleVolumePreparationRequestV1, local_deadline: float) -> None:
        self.refuse_finalized(
            _authority_binding(request.authority), request.authority.plan_identity
        )
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
        self.refuse_finalized(
            _authority_binding(request.authority), request.authority.plan_identity
        )
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
        # The restored record goes first: a crash before the completion leaves evidence a restore
        # retry publishes the completion from, never a completion that hides absent evidence.
        if response is not None and response.action == "restore":
            authority = request.authority
            restored_key = self._terminal_key(
                authority.system_id,
                authority.activation_id,
                authority.run_id,
                authority.plan_identity,
            )
            self._publish(f"{restored_key}.restored", response.to_canonical_json())
        completion = RemoteModuleLifecycleCompletionV1(
            state="terminal" if response is not None else "failed-after-mutation",
            response=response,
        )
        self._publish(
            f"{self._lifecycle_key(request)}.lifecycle-completion",
            completion.to_canonical_json(),
        )

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
            self.refuse_finalized(binding, plan_identity)
            return None
        response = RemoteModuleLifecycleResponseV1.from_canonical_json(data)
        if response.action != "restore":
            raise ValueError("remote module restored evidence has the wrong action")
        return response

    def record_prior_power(
        self, authority: AuthorityPreparationMutationRequestV1, observed: PriorPower
    ) -> PriorPower:
        """Durably fix the System's power from before the first stop; the first record wins.

        A retry after the stop landed observes ``inactive``, so it must read this record
        rather than the domain. The key is the activation, so a later activation records anew.
        """
        key = self._terminal_key(
            authority.system_id, authority.activation_id, authority.run_id, authority.plan_identity
        )
        name = f"{key}.prior-power"
        if (existing := self._read(name)) is None:
            # An owner with no power file is the residue of a crash or a failed clear; it must
            # not make a later request's differing owner bytes a conflict.
            with suppress(FileNotFoundError):
                os.unlink(f"{key}.prior-power-owner", dir_fd=self._root_fd)
            self._publish(
                f"{key}.prior-power-owner", self._authority_key(authority).encode("ascii")
            )
            self._publish(name, observed.encode("ascii"))
            existing = self._read(name)
        return self._decode_prior_power(existing)

    def prior_power_started_by(self, authority: AuthorityPreparationMutationRequestV1) -> bool:
        """Whether this exact request wrote the activation's prior-power record.

        The record is per activation and outlives a successful PREPARE, so only the request
        that wrote it may treat it as proof that its own stop was reached.
        """
        key = self._terminal_key(
            authority.system_id, authority.activation_id, authority.run_id, authority.plan_identity
        )
        owner = self._read(f"{key}.prior-power-owner")
        return owner is not None and owner == self._authority_key(authority).encode("ascii")

    def clear_prior_power(self, binding: ExternalBootActivationBinding, plan_identity: str) -> None:
        """Forget the record once the System is back in that power, so the next stop re-observes."""
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        # The owner goes first: a failure between the two leaves a power file, never an orphan.
        for suffix in ("prior-power-owner", "prior-power"):
            with suppress(FileNotFoundError):
                os.unlink(f"{key}.{suffix}", dir_fd=self._root_fd)
        os.fsync(self._root_fd)

    def prior_power(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> PriorPower | None:
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        data = self._read(f"{key}.prior-power")
        return None if data is None else self._decode_prior_power(data)

    @staticmethod
    def _decode_prior_power(data: bytes | None) -> PriorPower:
        if data == b"running":
            return "running"
        if data == b"inactive":
            return "inactive"
        raise ValueError("remote prior-power evidence is malformed")

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
            self.refuse_finalized(binding, plan_identity)
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

    def preparation_authority(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> OpaqueProviderRef:
        """The exact PREPARE authority a module recovery reference was minted under.

        ``RemoteModuleRecoveryRefV2.validate_authority`` binds the reference to that one
        attempt. A later release or teardown attempt carries its own authority, which the
        service journal and cleanup-evidence gate fence; checking the reference against this
        value instead proves the durable record was not re-presented under another
        preparation.
        """
        return authority_reference(self.reopen_terminal(binding, plan_identity).request.authority)

    def teardown_preparation(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteModuleVolumePreparationRequestV1 | None:
        """Find one exact durable PREPARE request without trusting a caller-selected path."""
        names = [
            name
            for name in os.listdir(self._root_fd)
            if len(name) == 64 + len(".request") and name.endswith(".request")
        ]
        # Only .request names count: finalized markers stay for the life of the store.
        if len(names) > 4096:
            raise ValueError("remote preparation store exceeds teardown scan bound")
        matches: list[RemoteModuleVolumePreparationRequestV1] = []
        for name in names:
            digest = name.removesuffix(".request")
            if any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("remote preparation request name is malformed")
            data = self._read(name)
            if data is None:
                continue
            persisted = _PersistedRemoteModulePreparationV1.from_canonical_json(data)
            request = persisted.request
            authority = request.authority
            if (
                str(authority.system_id) == binding.system_id
                and str(authority.activation_id) == binding.activation_id
                and str(authority.run_id) == binding.run_id
                and authority.plan_identity == plan_identity
            ):
                matches.append(request)
        if not matches:
            return None
        first = matches[0]
        if any(candidate.operation != first.operation for candidate in matches[1:]):
            raise ValueError("remote teardown found conflicting preparation attempts")
        return first

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

    def teardown_materialization(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteExternalBootMaterializationRecord | None:
        identity = plan_identity.removeprefix("sha256:")
        if len(identity) != 64 or any(
            character not in "0123456789abcdef" for character in identity
        ):
            raise ValueError("remote teardown plan identity is malformed")
        index = self._read(f"{identity}.materialization-index")
        if index is None:
            return None
        try:
            materialization_identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("remote materialization plan index is malformed") from None
        if len(materialization_identity) != 64 or any(
            character not in "0123456789abcdef" for character in materialization_identity
        ):
            raise ValueError("remote materialization plan index is malformed")
        data = self._read(f"{materialization_identity}.materialization")
        if data is None:
            raise ValueError("remote materialization plan index is incomplete")
        record = RemoteExternalBootMaterializationRecord.from_canonical_json(data)
        materialization = record.materialization
        if (
            record.plan.identity != plan_identity
            or materialization.identity.removeprefix("sha256:") != materialization_identity
            or materialization.ownership.system_id != binding.system_id
            or materialization.ownership.run_id != binding.run_id
        ):
            raise ValueError("remote teardown materialization differs from activation")
        return record

    def teardown_recovery(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteExternalBootRecoveryRecord | None:
        try:
            point = self.recovery_point(binding, plan_identity)
        except FileNotFoundError:
            return None
        return self.reopen_recovery(point)

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
            self._publish(_object_index_name(reference), identity.encode("ascii"))
        return OpaqueProviderRef(ref=f"remote-external-boot/{identity}")

    def recovery_for_object(self, reference: OpaqueProviderRef) -> RemoteExternalBootRecoveryRecord:
        index = self._read(_object_index_name(reference))
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
        identity = _recovery_identity(point.recovery_ref)
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

    @staticmethod
    def _quarantine_name(reference: OpaqueProviderRef) -> str:
        return f"{_recovery_identity(reference)}.cleanup-quarantine"

    def _finalized_name(self, binding: ExternalBootActivationBinding, plan_identity: str) -> str:
        return f"{self._recovery_key(binding, plan_identity)}.cleanup-finalized"

    def record_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        """Record the receipt; another operation's standing receipt is a conflict."""
        name = self._quarantine_name(receipt.recovery_ref)
        existing = self.read_cleanup_quarantine(receipt.binding, receipt.plan_identity)
        if existing is None:
            self._publish(name, receipt.to_canonical_json())
        elif existing.operation_identity != receipt.operation_identity:
            raise ValueError("remote cleanup quarantine belongs to another operation")
        elif existing != receipt:
            self._replace(name, existing.to_canonical_json(), receipt.to_canonical_json())

    def _receipt(self, reference: OpaqueProviderRef) -> RemoteCleanupQuarantineV1 | None:
        data = self._read(self._quarantine_name(reference))
        if data is None:
            return None
        receipt = RemoteCleanupQuarantineV1.from_canonical_json(data)
        if receipt.recovery_ref != reference:
            raise ValueError("remote cleanup quarantine receipt names another recovery record")
        return receipt

    def read_cleanup_quarantine(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteCleanupQuarantineV1 | None:
        """The receipt on disk, found by the recovery index or, once that is gone, the marker."""
        index = self._read(f"{self._recovery_key(binding, plan_identity)}.recovery-index")
        marker = self._read(self._finalized_name(binding, plan_identity))
        if index is not None:
            reference = OpaqueProviderRef(ref=f"remote-external-boot/{index.decode('ascii')}")
        elif marker is not None:
            reference = _parse_finalized_marker(marker)[0].recovery_ref
        else:
            return None
        receipt = self._receipt(reference)
        if receipt is not None and (
            receipt.binding != binding or receipt.plan_identity != plan_identity
        ):
            raise ValueError("remote cleanup quarantine receipt differs from its owner")
        return receipt

    def cleanup_finalized(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
        """Whether finalization began removing this activation's recovery record."""
        return self._read(self._finalized_name(binding, plan_identity)) is not None

    def refuse_finalized(self, binding: ExternalBootActivationBinding, plan_identity: str) -> None:
        """Refuse a finalized activation; the caller's later write is not atomic with this check.

        Nothing locks the store between the check and the write, and the module executor runs
        up to four calls at once. The ordering rests on the authority service running one
        mutation at a time per System lane (ADR-0584), so a finalize never overlaps a stage or
        publish for the same activation. A second authority instance writing this store
        directory would break that assumption.
        """
        if self.cleanup_finalized(binding, plan_identity):
            raise RemoteCleanupFinalizedError(
                "remote activation records were removed when its cleanup was finalized"
            )

    def _scan_owned(
        self,
        suffix: str,
        owns: Callable[[bytes], bool],
        *,
        strict: bool = False,
    ) -> list[str]:
        """Names of this suffix whose bytes the activation owns, in no particular order.

        A file that does not parse cannot be proven foreign. The deleting scan leaves it in
        place; a ``strict`` scan, which captures what teardown must still prove, refuses it.
        The scan is unbounded (ADR-0787, #3419): store files accumulate until something
        collects a torn-down activation's files.
        """
        return [
            name
            for name in os.listdir(self._root_fd)
            if len(name) == 64 + len(suffix)
            and name.endswith(suffix)
            and all(character in "0123456789abcdef" for character in name.removesuffix(suffix))
            and (data := self._read(name)) is not None
            and (self._strictly_owns(name, owns, data) if strict else self._owns(name, owns, data))
        ]

    @staticmethod
    def _strictly_owns(name: str, owns: Callable[[bytes], bool], data: bytes) -> bool:
        try:
            return owns(data)
        except ValueError as error:
            raise ValueError(
                f"remote finalize cannot parse {name}, so it cannot prove which activation owns "
                "it, and it may be any activation's on this host; keep a copy of the file, find "
                "the System and run it names, or every System on this host if the copy names "
                "none, confirm that no module volume of those Systems remains unaccounted for in "
                "the storage pool, then remove it and retry finalize"
            ) from error

    @staticmethod
    def _owns(name: str, owns: Callable[[bytes], bool], data: bytes) -> bool:
        try:
            return owns(data)
        except ValueError:
            logger.warning("remote store file %s is unreadable and left in place", name)
            return False

    def _activation_files(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> list[str]:
        """Every per-activation file but the recovery record, each anchor after what it names.

        A file another file's name is derived from is listed after it, so a retry that finds
        the anchor still finds everything it anchors.
        """

        def owned(
            authority: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
        ) -> bool:
            return _authority_owned(authority, binding, plan_identity)

        names: list[str] = []
        for suffix, dependents, parse in (
            (
                ".lifecycle-request",
                (".lifecycle-completion",),
                lambda data: owned(
                    _PersistedRemoteModuleLifecycleV1.from_canonical_json(data).request.authority
                ),
            ),
            (
                ".request",
                (".result", ".completion"),
                lambda data: owned(
                    _PersistedRemoteModulePreparationV1.from_canonical_json(data).request.authority
                ),
            ),
        ):
            for name in self._scan_owned(suffix, parse):
                stem = name.removesuffix(suffix)
                names += [*(f"{stem}{dependent}" for dependent in dependents), name]
        names += self._scan_owned(
            ".preparation",
            lambda data: (
                (receipt := ExternalBootPreparationObservation.from_canonical_json(data)).binding
                == binding
                and receipt.plan_identity == plan_identity
            ),
        )
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        names += [
            f"{key}.{suffix}"
            for suffix in ("terminal", "restored", "prior-power-owner", "prior-power")
        ]
        return names + self._materialization_files(binding, plan_identity)

    def _materialization_files(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> list[str]:
        index_name = f"{plan_identity.removeprefix('sha256:')}.materialization-index"
        index = self._read(index_name)
        if index is None:
            return []
        identity = index.decode("ascii", errors="replace")
        if len(identity) != 64 or any(value not in "0123456789abcdef" for value in identity):
            raise ValueError("remote materialization plan index is malformed")
        data = self._read(f"{identity}.materialization")
        if data is not None:
            ownership = RemoteExternalBootMaterializationRecord.from_canonical_json(
                data
            ).materialization.ownership
            if ownership.system_id != binding.system_id or ownership.run_id != binding.run_id:
                raise ValueError("remote materialization differs from the finalized activation")
        return [f"{identity}.materialization", index_name]

    def finalized_teardown_refs(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteFinalizedTeardownRefsV1 | None:
        """The refs teardown proves absent for a finalized activation; ``None`` if not finalized.

        A marker written before it carried refs is only usable while the activation's terminal
        record still stands, because that earlier finalize removed nothing else. Without it
        nothing says what to prove absent, so teardown is refused instead of passing vacuously.
        """
        data = self._read(self._finalized_name(binding, plan_identity))
        if data is None:
            return None
        refs = _parse_finalized_marker(data)[1]
        if refs is not None:
            return refs
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        if self._read(f"{key}.terminal") is not None:
            return None
        raise ValueError(
            "remote cleanup finalized marker has no teardown refs and the activation's terminal "
            "record is gone, so teardown is refused; confirm none of this activation's module "
            f"volumes remains in the storage pool, then remove "
            f"{self._finalized_name(binding, plan_identity)} and retry teardown"
        )

    def _teardown_refs(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteFinalizedTeardownRefsV1:
        try:
            module = self.reopen_terminal(binding, plan_identity).response.recovery
            volumes = (
                (module.pool.ref, module.source_volume.ref),
                (module.pool.ref, module.scratch_volume.ref),
            )
        except FileNotFoundError:
            volumes = ()
        operations: list[RemoteModuleOperationV1] = []

        def collect(data: bytes) -> bool:
            request = _PersistedRemoteModulePreparationV1.from_canonical_json(data).request
            if owned := _authority_owned(request.authority, binding, plan_identity):
                operations.append(request.operation)
            return owned

        self._scan_owned(".request", collect, strict=True)
        # The same comparison teardown_preparation applies, so finalize captures no nonce that
        # teardown would have refused.
        if any(operation != operations[0] for operation in operations[1:]):
            raise ValueError(
                "remote finalize found conflicting preparation requests for one activation; "
                "resolve the duplicate .request records before finalizing"
            )
        boot: list[RemoteFinalizedBootArtifactV1] = []
        if (record := self.teardown_materialization(binding, plan_identity)) is not None:
            materialization = record.materialization
            artifacts = materialization.artifacts
            boot.append(
                RemoteFinalizedBootArtifactV1(
                    ref=artifacts.kernel.ref,
                    kind="kernel",
                    sha256=materialization.extracted_vmlinuz_sha256,
                )
            )
            if artifacts.initrd is not None:
                boot.append(
                    RemoteFinalizedBootArtifactV1(
                        ref=artifacts.initrd.ref,
                        kind="initrd",
                        sha256=materialization.verified_initrd_sha256,
                    )
                )
        return RemoteFinalizedTeardownRefsV1(
            module_volumes=volumes,
            operation_nonce=operations[0].operation_nonce if operations else None,
            boot_artifacts=tuple(boot),
        )

    def _write_finalized_marker(self, receipt: RemoteCleanupQuarantineV1) -> None:
        """Publish the marker once; a retry keeps the refs captured before anything was removed."""
        name = self._finalized_name(receipt.binding, receipt.plan_identity)
        existing = self._read(name)
        cleaned = receipt.model_copy(update={"managed": False})
        if existing is not None:
            held, refs = _parse_finalized_marker(existing)
            if held != cleaned:
                raise ValueError("remote cleanup finalized marker conflicts with durable bytes")
            if refs is not None:
                return
        marker = RemoteCleanupFinalizedMarkerV1(
            receipt=cleaned, teardown=self._teardown_refs(receipt.binding, receipt.plan_identity)
        )
        if existing is None:
            self._publish(name, marker.to_canonical_json())
        else:
            self._replace(name, existing, marker.to_canonical_json())

    def _recovery_record(self, identity: str) -> RemoteExternalBootRecoveryRecord | None:
        data = self._read(f"{identity}.recovery")
        if data is None:
            return None
        if hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity:
            raise ValueError("remote recovery record identity mismatched")
        return RemoteExternalBootRecoveryRecord.from_canonical_json(data)

    def cleanup_quarantine_for_object(
        self, reference: OpaqueProviderRef
    ) -> RemoteCleanupQuarantineV1 | None:
        """The receipt actually on disk, whatever the finalization marker says."""
        return self._receipt(reference)

    def adopt_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        self._replace(
            self._quarantine_name(receipt.recovery_ref),
            receipt.to_canonical_json(),
            receipt.model_copy(update={"managed": True}).to_canonical_json(),
        )

    def finalize_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        """Remove the activation's records and indexes, then the receipt that proves the cleanup.

        A finalized marker is written first and the receipt goes last. The activation's other
        files go before the recovery record, whose identity and plan name them. The receipt is
        found by its record's identity, or through the marker once the index is gone, so an
        interrupted finalization is repeatable until nothing but the marker remains. The marker
        keeps a finalized cleanup distinguishable from a record that never existed. An index is
        removed only while it names this record, whether or not the record is still present.
        The ordering against a concurrent stage rests on the System lane (``refuse_finalized``).
        """
        identity = _recovery_identity(receipt.recovery_ref)
        record = self._recovery_record(identity)
        if record is not None and (
            record.binding != receipt.binding or record.plan_identity != receipt.plan_identity
        ):
            raise ValueError("remote recovery record differs from the quarantine receipt")
        self._write_finalized_marker(receipt)
        names = self._activation_files(receipt.binding, receipt.plan_identity)
        owned = identity.encode("ascii")
        if record is not None:
            names += [
                name
                for reference in record.recovery_objects
                if self._read(name := _object_index_name(reference)) == owned
            ]
        index = f"{self._recovery_key(receipt.binding, receipt.plan_identity)}.recovery-index"
        if self._read(index) == owned:
            names.append(index)
        names += [f"{identity}.recovery", self._quarantine_name(receipt.recovery_ref)]
        for name in names:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)


class RemoteModulePreparationOperation(Protocol):
    async def derive_operation(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
    ) -> RemoteModuleOperationV1: ...

    async def observe_system_power(
        self, admitted: AdmittedRemoteModulePreparation
    ) -> PriorPower: ...

    async def stop_system(self, admitted: AdmittedRemoteModulePreparation) -> None: ...

    async def restore_system_power(
        self, admitted: AdmittedRemoteModulePreparation, prior: PriorPower
    ) -> None: ...

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

    async def execute_system_teardown(
        self,
        request: RemoteModuleSystemTeardownRequestV1,
        terminal: RemoteModuleTerminalRecord,
        restored: RemoteModuleLifecycleResponseV1 | None,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1: ...


async def _wait_for_owned_child(
    task: asyncio.Task[object],
) -> tuple[asyncio.CancelledError | None, int]:
    """Await one completion-owned child while recording repeated caller cancellation."""
    caller = asyncio.current_task()
    assert caller is not None
    cancelled: asyncio.CancelledError | None = None
    consumed = 0
    completed = asyncio.Event()
    task.add_done_callback(lambda _task: completed.set())
    while not completed.is_set():
        try:
            await completed.wait()
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
            caller.uncancel()
            consumed += 1
    if cancelled is None and caller.cancelling() != 0:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            cancelled = error
            caller.uncancel()
            consumed += 1
    return cancelled, consumed


def _restore_cancellation(
    caller: asyncio.Task[object], cancelled: asyncio.CancelledError, consumed: int
) -> None:
    message = cancelled.args[0] if cancelled.args else None
    for _ in range(consumed):
        caller.cancel(message)


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

    def execution_started(self, request: RemoteModuleVolumePreparationRequestV1) -> bool:
        """Whether an earlier execute already reached the provider for this request.

        A prior power this request recorded counts: it is written ahead of the stop, and a
        successful abandon clears it, so a crash after the stop leaves a System that must not be
        refused. A record another request wrote does not count, or a later PREPARE would skip the
        held-System recheck. A replay of a refused operation whose holders have since cleared is
        answered ``provider_conflict``, which the host never runs and ADR-0714 counts as a repeat.
        """
        self._store.reopen_request(request)
        return (
            self._store.reopen_completion(request) is not None
            or self._store.reopen_result(request) is not None
            or self._store.prior_power_started_by(request.authority)
        )

    async def _quiesce_system(self, admitted: AdmittedRemoteModulePreparation) -> None:
        """Record the System's power, then stop it, before any other provider mutation.

        ADR-0585 starts the appliance only against a shut-off System. The record is written
        ahead of the stop so a retry that finds the domain already stopped keeps the power the
        System had before this activation. A refusal before the destroy has mutated nothing.
        """
        observed = await self._host.observe_system_power(admitted)
        self._store.record_prior_power(admitted.request.authority, observed)
        await self._host.stop_system(admitted)

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
        try:
            await self._quiesce_system(admitted)
        except Exception as failure:
            await self._raise_abandoned(admitted, failure)
        task = asyncio.create_task(self._host.execute(admitted))
        cancelled, consumed = await _wait_for_owned_child(task)
        if cancelled is not None:
            if task.cancelled() or task.exception() is not None:
                self._store.publish_failed_completion(request)
            else:
                result = task.result()
                self._store.publish_result(request, result)
            caller = asyncio.current_task()
            assert caller is not None
            _restore_cancellation(caller, cancelled, consumed)
            raise cancelled from None
        try:
            result = task.result()
        except Exception as failure:
            await self._raise_abandoned(admitted, failure)
        except BaseException:
            self._store.publish_failed_completion(request)
            raise
        self._store.publish_result(request, result)
        return result

    async def _raise_abandoned(
        self, admitted: AdmittedRemoteModulePreparation, failure: Exception
    ) -> NoReturn:
        try:
            settled = await self._abandon(admitted, failure)
        except BaseException:
            self._store.publish_failed_completion(admitted.request)
            raise
        if settled is failure:
            raise failure
        raise settled from failure

    async def _abandon(
        self, admitted: AdmittedRemoteModulePreparation, failure: Exception
    ) -> Exception:
        """Put the System back in its recorded power state, then report the failure as abandoned.

        A restored System leaves no completion behind, so the failure does not latch: the
        authority settles its journal and the retry derives a fresh attempt. When the restore
        fails the System's power is unknown, so the latch is kept and the original failure stands.
        """
        request = admitted.request
        authority = request.authority
        binding = ExternalBootActivationBinding(
            system_id=str(authority.system_id),
            run_id=str(authority.run_id),
            activation_id=str(authority.activation_id),
        )
        try:
            prior = self._store.prior_power(binding, authority.plan_identity)
            if prior is not None:
                await self._host.restore_system_power(admitted, prior)
        except Exception as restore_failure:  # noqa: BLE001 -- any failure keeps the latch
            logger.error(
                "remote module preparation abandon could not restore System power: %s: %s",
                type(restore_failure).__qualname__,
                restore_failure if isinstance(restore_failure, CategorizedError) else "",
            )
            self._store.publish_failed_completion(request)
            return failure
        if prior is not None:
            try:
                self._store.clear_prior_power(binding, authority.plan_identity)
            except OSError as clear_failure:
                logger.error(
                    "remote module preparation abandon could not clear the prior-power record: %s",
                    type(clear_failure).__qualname__,
                )
        details = dict(failure.details) if isinstance(failure, CategorizedError) else {}
        # Nothing was stopped when no power was recorded, so a failure before that mutated nothing.
        details.setdefault(
            "completion",
            "failed-after-mutation" if prior is not None else "refused-before-mutation",
        )
        details[PREPARATION_ABANDONED] = True
        return CategorizedError(
            str(failure) or "remote module preparation failed",
            category=failure.category
            if isinstance(failure, CategorizedError)
            else ErrorCategory.CONFLICT,
            details=details,
            terminal=isinstance(failure, CategorizedError) and failure.terminal,
        )

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
        cancelled, consumed = await _wait_for_owned_child(task)
        if cancelled is not None:
            self._store.publish_lifecycle_completion(
                request, None if task.cancelled() or task.exception() is not None else task.result()
            )
            caller = asyncio.current_task()
            assert caller is not None
            _restore_cancellation(caller, cancelled, consumed)
            raise cancelled from None
        try:
            response = task.result()
        except BaseException:
            self._store.publish_lifecycle_completion(request, None)
            raise
        self._store.publish_lifecycle_completion(request, response)
        return response

    async def execute_system_teardown(
        self, request: RemoteModuleSystemTeardownRequestV1
    ) -> RemoteModuleLifecycleResponseV1:
        binding = ExternalBootActivationBinding(
            system_id=str(request.authority.system_id),
            run_id=str(request.authority.run_id),
            activation_id=str(request.authority.activation_id),
        )
        terminal = self._store.reopen_terminal(binding, request.authority.plan_identity)
        # A restore that completed before a failed release leaves the restored result in the
        # scratch volume, so the reap is checked against it rather than the installed result.
        restored = self._store.reopen_restored(binding, request.authority.plan_identity)
        task = asyncio.create_task(
            self._host.execute_system_teardown(
                request, terminal, restored, self._monotonic() + request.budget_seconds
            )
        )
        cancelled, consumed = await _wait_for_owned_child(task)
        if cancelled is not None:
            caller = asyncio.current_task()
            assert caller is not None
            _restore_cancellation(caller, cancelled, consumed)
            raise cancelled from None
        return task.result()


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
    prior_power: PriorPower
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


class RemoteSystemTeardownOperations(Protocol):
    """Authority-owned System destruction outside the six external-boot operations."""

    def inspect_system_teardown(
        self, state: RemoteSystemTeardownState, *, domain_validated: bool
    ) -> RemoteSystemTeardownInspection: ...

    def validate_system_teardown(self, state: RemoteSystemTeardownState) -> None: ...

    def destroy_system_domain(self, state: RemoteSystemTeardownState) -> None: ...

    def undefine_system_domain(self, state: RemoteSystemTeardownState) -> None: ...

    def remove_system_artifacts(self, state: RemoteSystemTeardownState) -> None: ...

    def unfinished_preparation_absent(self, state: RemoteSystemTeardownState) -> bool: ...


class RemoteExternalBootConflictOperations(Protocol):
    """Source restoration from an acknowledged third definition, outside the six operations."""

    def recover_acknowledged(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        acknowledged: AcknowledgedDefinition,
        deadline: float,
    ) -> None: ...


class RemoteExternalBootObservationOperations(Protocol):
    """Read-only categorized observation outside the six external-boot operations."""

    def observe_state(
        self, recovery: RemoteExternalBootRecoveryRecord, deadline: float
    ) -> RemoteExternalBootObservedState: ...


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
        self._store.refuse_finalized(request.binding, request.plan.identity)
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
        # PREPARE stopped the System before the appliance ran, so the live read above is
        # always "inactive"; the record written ahead of that stop is the power to restore.
        if (
            recorded := self._store.prior_power(binding, materialization.plan_identity)
        ) is not None:
            recovery = recovery.model_copy(update={"prior_power": recorded})
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

    def record_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        self._store.record_cleanup_quarantine(receipt)

    def cleanup_quarantine(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: str,
    ) -> RemoteCleanupQuarantineV1 | None:
        return self._store.read_cleanup_quarantine(binding, plan_identity)

    def cleanup_finalized(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
        return self._store.cleanup_finalized(binding, plan_identity)

    def cleanup_quarantine_for_object(
        self, reference: OpaqueProviderRef
    ) -> RemoteCleanupQuarantineV1 | None:
        return self._store.cleanup_quarantine_for_object(reference)

    def adopt_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        self._store.adopt_cleanup_quarantine(receipt)

    def finalize_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        self._store.finalize_cleanup_quarantine(receipt)

    def recover(
        self,
        recovery: RecoveryPoint,
        authority: OpaqueProviderRef,
        acknowledged: AcknowledgedDefinition | None = None,
    ) -> None:
        record = self._store.reopen_recovery(recovery)
        if acknowledged is None:
            self._operations.recover(record, authority, self._deadline())
            return
        cast(RemoteExternalBootConflictOperations, self._operations).recover_acknowledged(
            record, authority, acknowledged, self._deadline()
        )

    def cleanup(self, recovery: RecoveryPoint) -> None:
        """Delete owned boot artifacts, fencing the record by its own PREPARE authority.

        The fence is a consistency check between two provider-private records; request
        authority is enforced by the service journal and cleanup-evidence gate.
        """
        record = self._store.reopen_recovery(recovery)
        authority = self._store.preparation_authority(recovery.binding, recovery.plan_identity)
        self._operations.cleanup(record, authority, self._deadline())

    def observe_state(self, recovery: RecoveryPoint) -> RemoteExternalBootObservedState:
        operations = cast(RemoteExternalBootObservationOperations, self._operations)
        return operations.observe_state(self._store.reopen_recovery(recovery), self._deadline())

    def modules_restored(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
        """Whether durable restore evidence exists for this activation's module tree."""
        return self._store.reopen_restored(binding, plan_identity) is not None

    def system_teardown_state(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteSystemTeardownState:
        finalized = self._store.finalized_teardown_refs(binding, plan_identity)
        try:
            terminal = self._store.reopen_terminal(binding, plan_identity)
        except FileNotFoundError:
            terminal = None
        # A finalized activation's marker names what was removed or is mid-removal, so the
        # records a partial removal may have left half-deleted are not read.
        live = finalized is None
        return RemoteSystemTeardownState(
            binding=binding,
            plan_identity=plan_identity,
            preparation=self._store.teardown_preparation(binding, plan_identity) if live else None,
            terminal=terminal,
            materialization=(
                self._store.teardown_materialization(binding, plan_identity) if live else None
            ),
            recovery=self._store.teardown_recovery(binding, plan_identity),
            finalized=finalized,
        )

    def begin_system_teardown(
        self, intent: RemoteSystemTeardownIntentV1
    ) -> RemoteSystemTeardownRecordV1:
        return self._store.begin_system_teardown(intent)

    def reopen_system_teardown(
        self, anchor: RemoteSystemTeardownAnchorV1
    ) -> RemoteSystemTeardownRecordV1 | None:
        return self._store.reopen_system_teardown(anchor)

    def checkpoint_system_teardown(
        self,
        record: RemoteSystemTeardownRecordV1,
        phase: RemoteSystemTeardownPhase,
        *,
        completed_at: datetime | None = None,
    ) -> RemoteSystemTeardownRecordV1:
        return self._store.checkpoint_system_teardown(record, phase, completed_at=completed_at)

    def inspect_system_teardown(
        self, state: RemoteSystemTeardownState, *, domain_validated: bool
    ) -> RemoteSystemTeardownInspection:
        operations = cast(RemoteSystemTeardownOperations, self._operations)
        return operations.inspect_system_teardown(state, domain_validated=domain_validated)

    def validate_system_teardown(self, state: RemoteSystemTeardownState) -> None:
        cast(RemoteSystemTeardownOperations, self._operations).validate_system_teardown(state)

    def destroy_system_domain(self, state: RemoteSystemTeardownState) -> None:
        cast(RemoteSystemTeardownOperations, self._operations).destroy_system_domain(state)

    def undefine_system_domain(self, state: RemoteSystemTeardownState) -> None:
        cast(RemoteSystemTeardownOperations, self._operations).undefine_system_domain(state)

    def remove_system_artifacts(self, state: RemoteSystemTeardownState) -> None:
        cast(RemoteSystemTeardownOperations, self._operations).remove_system_artifacts(state)

    def unfinished_preparation_absent(self, state: RemoteSystemTeardownState) -> bool:
        return cast(RemoteSystemTeardownOperations, self._operations).unfinished_preparation_absent(
            state
        )


class RemoteExternalBootAuthorityAdapter:
    """``AuthorityMutationAdapter`` over the remote coordinator and provider-private store.

    Every commit and observation reaches provider state only through the remote coordinator,
    which reopens the exact durable recovery record for the request's activation binding and
    plan. Release cleanup reaches deletion only through ``commit_cleanup``, behind the
    service's cleanup-evidence gate.

    Categorized observation reads the definition half live: which recorded side the inactive
    definition is, power, and whether every owned boot artifact and module volume is absent.
    The module half is durable evidence, never a live appliance read: the remote module tree
    is installed into the shared System overlay at PREPARE (ADR-0585), so the installed tree
    is present under both recorded definitions until restore evidence exists. Hence:

    - every owned object absent on the source definition is ``absent``;
    - the source definition is ``source``, with or without restore evidence, which makes the
      prepared, not-yet-activated state ``source`` rather than ``mixed``;
    - the target definition is ``target`` before restore evidence and ``mixed`` after it;
    - any other definition is ``conflict``, and an unreadable record or read is
      ``unreadable``.
    """

    def __init__(
        self,
        coordinator: RemoteExternalBootCoordinator,
        executor: RemoteModulePreparationExecutor,
        remote_module_host: DurableRemoteModuleVolumePreparationHost | None = None,
        close: Callable[[], None] | None = None,
        teardown_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._coordinator = coordinator
        self._executor = executor
        self._remote_module_host = remote_module_host
        self._close = close
        self._teardown_clock = teardown_clock

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
            binding=_activation_binding(request),
            authority=authority_reference(request),
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
        return await self._executor.run(lambda: self._observe(request))

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
        operation = self._require_commit_point(request, context)
        if operation is AuthorityOperation.RESOLVE_CONFLICT:
            return await self._resolve_conflict(request)
        mutation: Callable[[RecoveryPoint, OpaqueProviderRef], None] | None
        if operation is AuthorityOperation.ACTIVATE:
            mutation = self._coordinator.activate
        elif operation is AuthorityOperation.RECOVER and request.purpose in {"release", "recover"}:
            mutation = self._coordinator.recover
        elif operation in _BOOKKEEPING_COMMITS:
            mutation = None
        else:
            # Deleting commit points reach the provider only through commit_cleanup, behind the
            # service's cleanup-evidence gate.
            raise ValueError(f"remote external-boot commit point {operation.value} is not admitted")
        authority = authority_reference(request)

        def apply() -> None:
            point = self._matched_point(request)
            if mutation is not None:
                mutation(point, authority)

        await self._executor.run(apply)
        return await self.observe(request)

    async def _resolve_conflict(
        self, request: AuthorityMutationRequestV1
    ) -> AuthorityObservationV1:
        """Recover from the acknowledged identity and leave the recovery objects for the release.

        The module restore already ran on the worker, as for RECOVER; nothing is deleted here, so
        the System observes `source` and its release CLEANUP reaps and deletes later (#3414).
        """
        point = await self._executor.run(lambda: self._matched_point(request))
        acknowledged = await self._acknowledged_definition(request, point)
        authority = authority_reference(request)
        await self._executor.run(lambda: self._coordinator.recover(point, authority, acknowledged))
        return await self.observe(request)

    @staticmethod
    def _require_commit_point(
        request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityOperation:
        operation = context.commit_point
        if (
            not operation_is_permitted(request.purpose, operation)
            or operation is not request.operation
            or context.operation_identity != request.operation_identity
            or context.attempt_id != request.attempt_id
        ):
            raise ValueError("remote external-boot commit context differs from request")
        return operation

    def _matched_point(self, request: AuthorityMutationRequestV1) -> RecoveryPoint:
        """Reopen this activation's exact recovery point and prove the request names it.

        Deletion must name the recovery object it destroys; no commit may name another.
        """
        point = self._coordinator.recovery_point(
            _activation_binding(request), request.plan_identity
        )
        if (
            point.source_state.definition != request.expected_source_identity
            or point.target_state.definition != request.intended_target_identity
        ):
            raise ValueError("remote external-boot identities differ from request")
        named = request.recovery_objects
        if any(
            item.reference != point.recovery_ref.ref
            or str(item.system_id) != point.binding.system_id
            or str(item.activation_id) != point.binding.activation_id
            for item in named
        ) or (request.operation is AuthorityOperation.CLEANUP and not named):
            raise ValueError("remote external-boot recovery objects are not this activation's")
        return point

    def _finalized_cleanup(self, request: AuthorityMutationRequestV1) -> bool:
        """Whether this CLEANUP's record is gone because finalization removed it."""
        if request.operation is not AuthorityOperation.CLEANUP:
            return False
        try:
            return self._coordinator.cleanup_finalized(
                _activation_binding(request), request.plan_identity
            )
        except Exception:  # noqa: BLE001 - an unreadable marker stays the unreadable class
            logger.exception("remote cleanup finalization marker is unreadable")
            return False

    def _observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        binding = _activation_binding(request)
        state: RemoteExternalBootObservedState | None = None
        restored: bool | None = None
        category: ObservationCategory = "unreadable"
        try:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            restored = self._coordinator.modules_restored(binding, request.plan_identity)
            state = self._coordinator.observe_state(point)
        except FileNotFoundError:
            if self._finalized_cleanup(request):
                category = "absent"
            else:
                logger.exception("remote external-boot provider state is unreadable")
        except Exception:  # noqa: BLE001 - a failed read is the unreadable classification
            logger.exception("remote external-boot provider state is unreadable")
        else:
            category = (
                "conflict"
                if point.source_state.definition != request.expected_source_identity
                or point.target_state.definition != request.intended_target_identity
                else _categorize(state, restored=restored)
            )
        composite = _composite_state(request, state)
        return AuthorityObservationV1(
            observation_id=uuid5(
                _OBSERVATION_NAMESPACE,
                "\0".join(
                    (
                        str(request.attempt_id),
                        str(request.generation),
                        request.operation.value,
                        category,
                        composite,
                    )
                ),
            ),
            category=category,
            composite_state=composite,
        )

    @staticmethod
    def _teardown_anchor(
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
    ) -> RemoteSystemTeardownAnchorV1:
        if (
            request.operation is not AuthorityOperation.TEARDOWN
            or request.purpose != "teardown"
            or context.commit_point is not AuthorityOperation.TEARDOWN
            or context.operation_identity != request.operation_identity
            or context.attempt_id != request.attempt_id
        ):
            raise ValueError("remote System teardown context differs from request")
        return RemoteSystemTeardownAnchorV1(
            authority_id=request.authority_id,
            generation=request.generation,
            binding=ExternalBootActivationBinding(
                system_id=str(request.system_id),
                run_id=str(request.run_id),
                activation_id=str(request.activation_id),
            ),
            plan_identity=request.plan_identity,
            provider_kind=request.provider_kind,
            authority_instance=request.authority_instance,
            operation_identity=request.operation_identity,
            operation_digest=request.operation_digest,
            attempt_id=request.attempt_id,
            journal_sequence=context.journal_sequence,
            journal_digest=context.journal_digest,
        )

    @staticmethod
    def _teardown_facts(
        owner: RemoteSystemTeardownAnchorV1 | RemoteSystemTeardownIntentV1,
        record: RemoteSystemTeardownRecordV1 | None,
        inspection: RemoteSystemTeardownInspection,
    ) -> AuthoritySystemTeardownFacts:
        reservation = record.intent.reservation if record is not None else None
        completed_at = record.completed_at if record is not None else None
        complete = (
            inspection.domain_absent
            and inspection.overlay_absent
            and inspection.baseline_absent
            and inspection.recovery_absent
            and completed_at is not None
            and reservation is not None
        )
        return AuthoritySystemTeardownFacts(
            intent_identity=owner.identity,
            domain_absent=inspection.domain_absent,
            overlay_absent=inspection.overlay_absent,
            baseline_absent=inspection.baseline_absent,
            recovery_absent=inspection.recovery_absent,
            quarantine_retained=not complete,
            completed_at=completed_at,
            reservation=reservation,
        )

    async def execute_system_teardown(
        self,
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
        reservation: AuthorityTeardownReservationV1,
    ) -> AuthoritySystemTeardownFacts:
        """Persist intent, then resume exact remote domain and storage destruction."""
        anchor = self._teardown_anchor(request, context)
        retained = AuthorityTeardownReservationV1.model_validate(
            reservation.model_dump(mode="json")
        )
        intent = RemoteSystemTeardownIntentV1.model_validate(
            anchor.model_dump(exclude={"schema_"}) | {"reservation": retained}
        )
        record = await self._executor.run(lambda: self._coordinator.begin_system_teardown(intent))
        state = await self._executor.run(
            lambda: self._coordinator.system_teardown_state(intent.binding, intent.plan_identity)
        )

        async def phase(
            expected: RemoteSystemTeardownPhase,
            achieved: RemoteSystemTeardownPhase,
            action: Callable[[], None],
        ) -> None:
            nonlocal record
            if record.phase != expected:
                return

            def complete_phase() -> RemoteSystemTeardownRecordV1:
                action()
                return self._coordinator.checkpoint_system_teardown(record, achieved)

            record = await self._executor.run(complete_phase)

        await phase(
            "intent-recorded",
            "domain-validated",
            lambda: self._coordinator.validate_system_teardown(state),
        )
        await phase(
            "domain-validated",
            "domain-destroyed",
            lambda: self._coordinator.destroy_system_domain(state),
        )
        if record.phase == "domain-destroyed":
            if state.terminal is not None:
                host = self._remote_module_host
                if host is None:
                    raise ValueError("remote module teardown host is unavailable")
                await host.execute_system_teardown(
                    RemoteModuleSystemTeardownRequestV1(authority=request, budget_seconds=300)
                )
            elif state.preparation is not None or state.finalized is not None:
                absent = await self._executor.run(
                    lambda: self._coordinator.unfinished_preparation_absent(state)
                )
                if not absent:
                    inspection = await self._executor.run(
                        lambda: self._coordinator.inspect_system_teardown(
                            state, domain_validated=record.domain_validated
                        )
                    )
                    return self._teardown_facts(record.intent, record, inspection)
            record = await self._executor.run(
                lambda: self._coordinator.checkpoint_system_teardown(record, "recovery-removed")
            )
        await phase(
            "recovery-removed",
            "domain-undefined",
            lambda: self._coordinator.undefine_system_domain(state),
        )
        await phase(
            "domain-undefined",
            "artifacts-removed",
            lambda: self._coordinator.remove_system_artifacts(state),
        )
        inspection = await self._executor.run(
            lambda: self._coordinator.inspect_system_teardown(
                state, domain_validated=record.domain_validated
            )
        )
        if (
            record.phase == "artifacts-removed"
            and inspection.domain_absent
            and inspection.overlay_absent
            and inspection.baseline_absent
            and inspection.recovery_absent
        ):
            record = await self._executor.run(
                lambda: self._coordinator.checkpoint_system_teardown(
                    record, "complete", completed_at=self._teardown_clock()
                )
            )
        return self._teardown_facts(record.intent, record, inspection)

    async def observe_system_teardown(
        self,
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
    ) -> AuthoritySystemTeardownFacts:
        """Observe retained remote teardown facts without resuming any mutation."""
        anchor = self._teardown_anchor(request, context)

        def observe() -> AuthoritySystemTeardownFacts:
            record = self._coordinator.reopen_system_teardown(anchor)
            state = self._coordinator.system_teardown_state(anchor.binding, anchor.plan_identity)
            inspection = self._coordinator.inspect_system_teardown(
                state, domain_validated=record.domain_validated if record is not None else False
            )
            return self._teardown_facts(
                record.intent if record is not None else anchor, record, inspection
            )

        return await self._executor.run(observe)

    async def preparation_receipt(
        self, request: AuthorityPreparationMutationRequestV1
    ) -> ExternalBootPreparationObservation:
        return await self._executor.run(
            lambda: self._coordinator.observe_preparation(self._preparation_request(request))
        )

    async def adopt_preparation(
        self,
        request: AuthorityPreparationMutationRequestV1,
        predecessor: AuthorityPreparationMutationRequestV1,
        predecessor_receipt_identity: str,
        context: AuthorityCommitContextV1,
    ) -> AuthorityObservationV1:
        """Adopt an earlier generation's identical receipt instead of repeating the mutation."""
        self._require_commit_point(cast(AuthorityMutationRequestV1, request), context)
        receipt = await self._executor.run(
            lambda: self._coordinator.adopt_preparation(
                self._preparation_request(request),
                self._preparation_request(predecessor),
                predecessor_receipt_identity,
            )
        )
        return self._preparation_observation(receipt)

    async def cleanup_subject(self, request: AuthorityMutationRequestV1) -> str:
        binding = _activation_binding(request)

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

        def reopen() -> tuple[RecoveryPoint, RemoteExternalBootRecoveryRecord]:
            point = self._matched_point(request)
            return point, self._coordinator.recovery_record(point)

        point, record = await self._executor.run(reopen)
        reference = RemoteModuleRecoveryRefV2.model_validate_json(evidence.recovery_reference_json)
        if (
            reference != record.module_recovery
            or evidence.operation_nonce != reference.operation_nonce
        ):
            raise ValueError("remote cleanup evidence changed before provider deletion")
        authority = authority_reference(request)
        existing = await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine(point.binding, point.plan_identity)
        )
        if existing is not None and existing.operation_identity != context.operation_identity:
            raise ValueError("remote cleanup quarantine belongs to another operation")
        acknowledged = await self._acknowledged_definition(request, point)
        await self._executor.run(lambda: self._coordinator.recover(point, authority, acknowledged))
        await self._executor.run(lambda: self._coordinator.cleanup(point))
        if request.operation is AuthorityOperation.CLEANUP:
            receipt = RemoteCleanupQuarantineV1(
                binding=point.binding,
                plan_identity=point.plan_identity,
                recovery_ref=point.recovery_ref,
                operation_identity=context.operation_identity,
                attempt_id=context.attempt_id,
                journal_sequence=context.journal_sequence,
                journal_digest=context.journal_digest,
            )
            await self._executor.run(lambda: self._coordinator.record_cleanup_quarantine(receipt))
        return await self.observe(request)

    async def cleanup_quarantine_inventory(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> tuple[RecoveryObjectObservation, ...]:
        """Reopen only the exact standing private cleanup receipt.

        The receipt is compared with the proof ``context``, not the request: a retried release
        that adopted an earlier generation's cleanup still names that cleanup (ADR-0787, #3434).
        """
        if request.operation is not AuthorityOperation.CLEANUP:
            return ()
        binding = _activation_binding(request)
        receipt = await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine(binding, request.plan_identity)
        )
        if receipt is None or receipt.managed:
            return ()
        if (
            receipt.operation_identity != context.operation_identity
            or receipt.attempt_id != context.attempt_id
        ):
            # Deferred: the service module imports this one.
            from kdive.providers.external_boot_authority.service import AuthorityServiceError

            raise AuthorityServiceError("provider_conflict")
        return (_quarantine_observation(receipt),)

    async def finalize(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> None:
        """Remove the cleaned recovery record once the authority anchored its terminal receipt."""
        if request.operation is not AuthorityOperation.CLEANUP:
            return
        binding = _activation_binding(request)
        receipt = await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine(binding, request.plan_identity)
        )
        if receipt is None or receipt.managed:
            # An adopted receipt is the operator's; finalize leaves it in place (ADR-0787).
            return
        if (
            receipt.operation_identity != context.operation_identity
            or receipt.attempt_id != context.attempt_id
            or receipt.journal_sequence != context.journal_sequence
            or receipt.journal_digest != context.journal_digest
        ):
            raise ValueError("remote cleanup proof differs from the quarantine receipt")
        await self._executor.run(lambda: self._coordinator.finalize_cleanup_quarantine(receipt))

    async def _quarantine_receipt(
        self, binding: RecoveryObjectBinding
    ) -> RemoteCleanupQuarantineV1 | None:
        if binding.kind != "recovery-record":
            raise ValueError("remote recovery-object port serves only the recovery record")
        return await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine_for_object(binding.reference)
        )

    async def observe_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation:
        receipt = await self._quarantine_receipt(binding)
        if receipt is None:
            return RecoveryObjectObservation(
                binding=binding,
                present=False,
                managed=False,
                observed_digest=binding.mutation_journal_digest,
            )
        observation = _quarantine_observation(receipt)
        if observation.binding != binding:
            raise ValueError("remote cleanup quarantine ownership conflicts with request")
        return observation

    async def delete_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation:
        observed = await self.observe_recovery_object(binding, authority)
        if observed.observed_digest != digest or observed.managed:
            raise ValueError("remote cleanup quarantine observation changed before delete")
        receipt = await self._quarantine_receipt(binding)
        if receipt is not None:
            if _quarantine_observation(receipt).observed_digest != digest or receipt.managed:
                raise ValueError("remote cleanup quarantine observation changed before delete")
            await self._executor.run(lambda: self._coordinator.finalize_cleanup_quarantine(receipt))
        return await self.observe_recovery_object(binding, authority)

    async def adopt_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation:
        observed = await self.observe_recovery_object(binding, authority)
        receipt = await self._quarantine_receipt(binding)
        if (
            observed.observed_digest != digest
            or receipt is None
            or _quarantine_observation(receipt).observed_digest != digest
        ):
            raise ValueError("remote cleanup quarantine observation changed before adopt")
        await self._executor.run(lambda: self._coordinator.adopt_cleanup_quarantine(receipt))
        return await self.observe_recovery_object(binding, authority)

    async def _acknowledged_definition(
        self, request: AuthorityMutationRequestV1, point: RecoveryPoint
    ) -> AcknowledgedDefinition | None:
        """Return the third definition a conflict resolution acknowledged, if that is the state.

        The digest proves a fresh read is the acknowledged observation; the read's own digest
        pair is then the expectation the provider compares again at the write.
        """
        if not isinstance(request, AuthorityConflictResolutionRequestV1):
            return None
        state = await self._executor.run(lambda: self._coordinator.observe_state(point))
        if _composite_state(request, state) != request.expected_observed_composite:
            raise ValueError("remote conflict resolution observation changed before restore")
        return (state.preserved, state.boot) if state.definition == "other" else None

    async def observe_running(
        self, request: AuthorityMutationRequestV1
    ) -> RunningKernelObservation:
        binding = _activation_binding(request)
        authority = authority_reference(request)

        def read() -> RunningKernelObservation:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            if (
                point.source_state.definition != request.expected_source_identity
                or point.target_state.definition != request.intended_target_identity
            ):
                raise ValueError("remote running observation identities differ from request")
            return self._coordinator.observe(point, authority)

        return await self._executor.run(read)


# Commit points whose provider effect is exactly one observation (ADR-0584).
_BOOKKEEPING_COMMITS = frozenset(
    {AuthorityOperation.DEADLINE, AuthorityOperation.FAIL, AuthorityOperation.RELEASE}
)


def _categorize(state: RemoteExternalBootObservedState, *, restored: bool) -> ObservationCategory:
    """Apply the adapter's documented module-half rule to one live read."""
    if state.definition == "source":
        return "absent" if state.recovery_objects_absent else "source"
    if state.definition == "target" and not state.recovery_objects_absent:
        return "mixed" if restored else "target"
    return "conflict"


def _composite_state(
    request: AuthorityMutationRequestV1,
    state: RemoteExternalBootObservedState | None,
) -> str:
    """Digest the observed state together with the activation it was observed on.

    A proof-of-observation token, as for local: it names the System, activation, Run and plan
    so two identically provisioned Systems never mint interchangeable tokens, and it carries
    only closed identifiers and digests, never provider output. The module restore evidence is
    deliberately not in it: conflict resolution writes that evidence before the authority
    rechecks the acknowledged composite, so including it would make every resolve stale.
    """
    payload = json.dumps(
        {
            "system_id": str(request.system_id),
            "activation_id": str(request.activation_id),
            "run_id": str(request.run_id),
            "plan_identity": request.plan_identity,
            "observed": None if state is None else asdict(state),
            "expected_source_identity": request.expected_source_identity,
            "intended_target_identity": request.intended_target_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return (
        "sha256:"
        + hashlib.sha256(b"kdive-remote-external-boot-composite-v1\0" + payload).hexdigest()
    )
