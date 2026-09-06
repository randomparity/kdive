"""Narrow provider execution seam for authority-owned Systems (ADR-0623)."""

from __future__ import annotations

from typing import Protocol

from kdive.providers.system_authority.protocol import (
    AuthoritySystemAbsenceFacts,
    AuthoritySystemCommitContextV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionSnapshot,
)


class AuthoritySystemProvider(Protocol):
    """Execute or observe the two closed activation-free System operations."""

    async def execute_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts: ...

    async def observe_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts: ...

    async def execute_preactivation_teardown(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
    ) -> AuthoritySystemAbsenceFacts: ...

    async def observe_preactivation_teardown(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
    ) -> AuthoritySystemAbsenceFacts: ...


__all__ = ["AuthoritySystemProvider"]
