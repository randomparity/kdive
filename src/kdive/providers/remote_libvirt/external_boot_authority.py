"""Closed remote external-boot coordinator contracts (#2200)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.providers.external_boot_authority.protocol import (
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
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    RemoteExternalBootDefinition,
)
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

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

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
        if (
            response.source.system_id != request.operation.system_id
            or response.source.run_id != request.operation.run_id
            or response.source.operation_nonce != request.operation.operation_nonce
            or response.source.digest != request.operation.source_manifest
        ):
            raise ValueError("provider returned volumes for a different remote module attempt")
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


class RemoteExternalBootOperations(Protocol):
    """The six closed operations available to the remote coordinator."""

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef, deadline: float
    ) -> ExternalBootMaterialization: ...

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
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
