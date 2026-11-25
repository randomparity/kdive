"""Closed remote module values exchanged across the shared authority boundary."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.domain.external_boot_values import Digest, OpaqueProviderRef
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
    AuthorityTeardownMutationRequestV1,
)
from kdive.providers.external_boot_authority.teardown import ProviderRecoveryRefusal
from kdive.providers.shared.module_volume_names import render_module_volume_name


@dataclass(frozen=True, slots=True)
class PreparedVolume:
    pool: str
    name: str
    system_id: str
    run_id: str
    operation_nonce: str
    purpose: str
    digest: str
    capacity_bytes: int


@dataclass(frozen=True, slots=True)
class PreparedModuleVolumes:
    source: PreparedVolume
    scratch: PreparedVolume


MAX_PREPARATION_BYTES = 1_048_576


PREPARATION_ABANDONED = "abandoned"
"""Detail on a PREPARE failure the provider settled: the System's power is restored."""


def canonical_model_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


type AuthorityOperationRequest = (
    AuthorityMutationRequestV1
    | AuthorityPreparationMutationRequestV1
    | AuthorityTeardownMutationRequestV1
)


def authority_reference(request: AuthorityOperationRequest) -> OpaqueProviderRef:
    """The opaque provider reference minted for one exact authority attempt."""
    return OpaqueProviderRef(
        ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
    )


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
            or operation.source_manifest != authority.plan.module_tree_manifest
        ):
            raise ValueError("remote module operation differs from authority binding")
        return self

    def to_canonical_json(self) -> bytes:
        encoded = canonical_model_bytes(self)
        if len(encoded) > MAX_PREPARATION_BYTES:
            raise ProviderRecoveryRefusal("remote module preparation request exceeds 1048576 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > MAX_PREPARATION_BYTES:
            raise ProviderRecoveryRefusal("remote module preparation request exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote module preparation request is not canonical JSON")
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


class _RemoteModuleVolumePreparationFields(BaseModel):
    """Volume geometry shared by the preparation and terminal responses."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    schema_: str = Field(alias="schema")
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
                raise ProviderRecoveryRefusal(
                    "provider returned volumes for a different remote module attempt"
                )
        if (
            self.source.name != render_module_volume_name(*expected, "source.ext4")
            or self.scratch.name != render_module_volume_name(*expected, "scratch.ext4")
            or self.source.digest != operation.source_manifest
            or self.scratch.digest != "sha256:" + "0" * 64
        ):
            raise ProviderRecoveryRefusal(
                "provider returned mismatched remote module volume identities"
            )

    def to_canonical_json(self) -> bytes:
        encoded = canonical_model_bytes(self)
        if len(encoded) > MAX_PREPARATION_BYTES:
            raise ProviderRecoveryRefusal(
                "remote module preparation response exceeds 1048576 bytes"
            )
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > MAX_PREPARATION_BYTES:
            raise ProviderRecoveryRefusal(
                "remote module preparation response exceeds 1048576 bytes"
            )
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal(
                "remote module preparation response is not canonical JSON"
            )
        return value


class RemoteModuleVolumePreparationResponseV1(_RemoteModuleVolumePreparationFields):
    """Exact bounded volume geometry returned by the authenticated provider host."""

    schema_: Literal["remote-module-volume-preparation-response-v1"] = Field(
        "remote-module-volume-preparation-response-v1", alias="schema"
    )

    @classmethod
    def from_prepared(cls, volumes: PreparedModuleVolumes) -> Self:
        return cls(
            source=RemotePreparedVolumeV1.model_validate(asdict(volumes.source)),
            scratch=RemotePreparedVolumeV1.model_validate(asdict(volumes.scratch)),
        )


class RemoteModuleTerminalPreparationResponseV1(_RemoteModuleVolumePreparationFields):
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
            raise ProviderRecoveryRefusal(
                "remote module terminal recovery differs from exact operation"
            )


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
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote module lifecycle request is not canonical JSON")
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
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote module lifecycle response is not canonical JSON")
        return value


class RemoteModuleSystemTeardownRequestV1(BaseModel):
    """Internal module reap request carrying the distinct System teardown authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)
    schema_: Literal["remote-module-system-teardown-request-v1"] = Field(
        "remote-module-system-teardown-request-v1", alias="schema"
    )
    authority: AuthorityTeardownMutationRequestV1
    budget_seconds: Annotated[int, Field(ge=1, le=300)]
