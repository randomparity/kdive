"""Provider operation ports over shared external-boot domain values (ADR-0583)."""

from __future__ import annotations

from typing import Protocol

from kdive.domain.external_boot_values import (
    Digest,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
    RecoveryPoint,
    RunningKernelObservation,
)


class ExternalBootArtifactStager(Protocol):
    """Write and independently validate exact boot artifacts into an owned directory."""

    def materialize_artifacts(
        self, plan: ExternalBootPlan, directory_fd: int
    ) -> tuple[dict[str, object], str]: ...


class ExternalBootPreparationPorts(Protocol):
    """Provider-owned durable receipt seam for server preparation."""

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation: ...

    def execute_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation: ...

    def adopt_preparation(
        self,
        request: ExternalBootPreparationRequest,
        predecessor: ExternalBootPreparationRequest,
        predecessor_receipt_identity: str,
    ) -> ExternalBootPreparationObservation: ...


class ExternalBootRecoveryObjectPorts(Protocol):
    """Bounded per-object quarantine observation and disposition."""

    def quarantined_objects(
        self,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> tuple[RecoveryObjectObservation, ...]: ...

    def observe_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation: ...

    def delete_recovery_object(
        self,
        binding: RecoveryObjectBinding,
        authority: OpaqueProviderRef,
        expected_observed_digest: Digest,
    ) -> RecoveryObjectObservation: ...

    def adopt_object(
        self,
        binding: RecoveryObjectBinding,
        authority: OpaqueProviderRef,
        expected_observed_digest: Digest,
    ) -> RecoveryObjectObservation: ...


class ExternalBootPorts(Protocol):
    """Six narrow operations shared by external-boot providers."""

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization: ...

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryPoint: ...

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation: ...

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...
