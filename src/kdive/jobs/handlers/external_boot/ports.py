"""Injected ports the external-boot operation handlers run against (ADR-0593 decision 5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from kdive.jobs.payloads import EXTERNAL_BOOT_AUTHORITY_MARKER_KEY
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityTakeoverRequestV1,
)
from kdive.security.secrets.secret_registry import SecretRegistry

__all__ = [
    "EXTERNAL_BOOT_AUTHORITY_MARKER_KEY",
    "ExternalBootAuthorityAcknowledger",
    "ExternalBootHandlerPorts",
]


class ExternalBootAuthorityAcknowledger(Protocol):
    """The worker's one call into the provider-authority host.

    ``commit_external_boot_authority_result`` returns ``superseded`` unless an
    ``external_boot_authority_acknowledgements`` row exists for the allocated authority, and
    ``acknowledge_external_boot_authority`` is granted to ``kdive_provider_authority`` alone — a
    role no worker session holds, by ADR-0584's design. So a worker cannot acknowledge its own
    authority and must ask the host to.

    Both models are taken unchanged from
    ``kdive.providers.external_boot_authority.protocol``: ``AuthorityTakeoverRequestV1`` already
    carries every fact the handler holds after allocation, and ``AuthorityAcknowledgementV1``
    already carries exactly the three the commit needs — ``journal_sequence``, ``journal_digest``,
    and ``positive_quiescence_digest``. No new model is defined for this seam.
    """

    async def acknowledge(
        self, request: AuthorityTakeoverRequestV1
    ) -> AuthorityAcknowledgementV1: ...


@dataclass(frozen=True, slots=True)
class ExternalBootHandlerPorts:
    """Ports one ``build_operations`` call binds into all six operation handlers.

    ``acknowledger`` is absent by default and the operation then fails closed with
    ``configuration_error`` **before** the provider is touched, so an unwired deployment never
    leaves a half-applied mutation. Wiring it to the authority host's transport is
    provider-adapter work (#2199, #2200); defining the seam here is what makes an operation
    executable and testable at all.
    """

    resolver: ProviderResolver
    incarnation_credential: SecretStr
    secret_registry: SecretRegistry
    acknowledger: ExternalBootAuthorityAcknowledger | None = None
