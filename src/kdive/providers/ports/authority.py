"""Closed worker-owned authority route (ADR-0606)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

from kdive.providers.external_boot_authority.device_identity import (
    DeviceIdentityRequestV1,
    DeviceIdentityResponseV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityConflictResolutionRequestV1,
    AuthorityHealthAcknowledgementV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
    AuthorityRecoveryOrphanDispositionRequestV1,
    AuthorityRecoveryOrphanDispositionResponseV1,
    AuthorityRunningObservationV1,
    AuthorityTakeoverRequestV1,
    AuthorityTeardownMutationRequestV1,
    AuthorityTeardownResponseV1,
)
from kdive.providers.ports.module_operation import ModuleAuthority
from kdive.providers.system_authority.protocol import (
    AuthoritySystemAcknowledgementV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemResponseV1,
    AuthoritySystemTakeoverRequestV1,
)

if TYPE_CHECKING:
    from kdive.providers.external_boot_authority.transport import Operation


@dataclass(frozen=True, slots=True)
class AuthorityReservationGeometry:
    store_identity: str
    reserve_bytes: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class AuthorityCapability:
    """Fixed provider route metadata, with a sender only in worker composition."""

    authority_instance: str | None
    geometry: AuthorityReservationGeometry | None = None
    sender: AuthorityRequestSender | None = None
    modules: ModuleAuthority | None = None
    # Preserve each provider's existing admission refusal when its worker route is absent.
    missing_route_reason: Literal["authority_route_missing", "authority_route_mismatch"] = (
        "authority_route_missing"
    )


class AuthorityRequestSender(Protocol):
    """One Resource's route; deadlines use the event loop's absolute monotonic clock."""

    async def _request[Value: BaseModel](
        self, operation: Operation, request: BaseModel, model: type[Value], *, deadline: float
    ) -> Value:
        """Composition-only exchange for provider codecs; callers use typed operations."""
        ...

    async def health(self, *, deadline: float) -> AuthorityHealthAcknowledgementV1: ...

    async def resolve_device_identity(
        self, request: DeviceIdentityRequestV1, *, deadline: float
    ) -> DeviceIdentityResponseV1: ...

    async def acknowledge_takeover(
        self, request: AuthorityTakeoverRequestV1, *, deadline: float
    ) -> AuthorityAcknowledgementV1: ...

    async def execute_mutation(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityObservationV1: ...

    async def execute_conflict_resolution(
        self, request: AuthorityConflictResolutionRequestV1, *, deadline: float
    ) -> AuthorityObservationV1: ...

    async def resolve_recovery_orphan(
        self, request: AuthorityRecoveryOrphanDispositionRequestV1, *, deadline: float
    ) -> AuthorityRecoveryOrphanDispositionResponseV1: ...

    async def execute_preparation(
        self, request: AuthorityPreparationMutationRequestV1, *, deadline: float
    ) -> AuthorityPreparationResponseV1: ...

    async def execute_teardown(
        self, request: AuthorityTeardownMutationRequestV1, *, deadline: float
    ) -> AuthorityTeardownResponseV1: ...

    async def observe_authority(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityObservationV1: ...

    async def observe_running(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityRunningObservationV1: ...

    async def acknowledge_system_takeover(
        self, request: AuthoritySystemTakeoverRequestV1, *, deadline: float
    ) -> AuthoritySystemAcknowledgementV1: ...

    async def execute_system_operation(
        self,
        request: AuthoritySystemMutationRequestV1,
        acknowledgement: AuthoritySystemAcknowledgementV1,
        *,
        deadline: float,
    ) -> AuthoritySystemResponseV1: ...
