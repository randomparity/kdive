"""Closed worker-owned authority route (ADR-0606)."""

from typing import Protocol

from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityHealthAcknowledgementV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
)


class AuthorityRequestSender(Protocol):
    """One Resource's route; deadlines use the event loop's absolute monotonic clock."""

    async def health(self, *, deadline: float) -> AuthorityHealthAcknowledgementV1: ...

    async def acknowledge_takeover(
        self, request: AuthorityTakeoverRequestV1, *, deadline: float
    ) -> AuthorityAcknowledgementV1: ...

    async def execute_mutation(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityObservationV1: ...
