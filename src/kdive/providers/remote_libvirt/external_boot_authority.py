"""Bounded remote-libvirt authority mutation adapter (ADR-0584)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    ObservationCategory,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.shared.runtime_paths import domain_name_for

SUPPORTED_COMMIT_POINTS = frozenset({"activate", "recover", "cleanup"})


class _Domain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def reset(self, flags: int) -> int: ...


class _Connection(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802


type ConnectionFactory = Callable[[RemoteLibvirtConfig], AbstractContextManager[_Connection]]


class RemoteLibvirtAuthorityMutationAdapter:
    """Observe and mutate one System through one resource-bound remote config."""

    def __init__(self, config: RemoteLibvirtConfig, *, connection: ConnectionFactory) -> None:
        self._config = config
        self._connection = connection

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        try:
            identity = self._identity(request)
        except Exception:
            return self._observation(request, "unreadable", "unreadable")
        if identity == request.expected_source_identity:
            category = "source"
        elif identity == request.intended_target_identity:
            category = "target"
        elif identity.startswith("mixed:"):
            category = "mixed"
        else:
            category = "conflict"
        return self._observation(request, category, identity)

    async def commit(
        self, request: AuthorityMutationRequestV1, commit_point: str
    ) -> AuthorityObservationV1:
        if commit_point not in SUPPORTED_COMMIT_POINTS:
            raise ValueError("unsupported external-boot commit point")
        try:
            with self._connection(self._config) as connection:
                domain = connection.lookupByName(domain_name_for(request.system_id))
                if commit_point == "activate":
                    domain.create()
                elif commit_point == "recover":
                    domain.reset(0)
                else:
                    domain.destroy()
        except Exception:
            raise CategorizedError(
                "remote-libvirt authority mutation failed",
                category=ErrorCategory.CONTROL_FAILURE,
            ) from None
        return await self.observe(request)

    def _identity(self, request: AuthorityMutationRequestV1) -> str:
        with self._connection(self._config) as connection:
            xml = connection.lookupByName(domain_name_for(request.system_id)).XMLDesc(0)
        if xml.startswith("kdive-authority-state:"):
            return xml.removeprefix("kdive-authority-state:")
        if xml.startswith("kdive-authority-mixed:"):
            return "mixed:" + hashlib.sha256(xml.encode()).hexdigest()
        return "sha256:" + hashlib.sha256(xml.encode()).hexdigest()

    @staticmethod
    def _observation(
        request: AuthorityMutationRequestV1, category: ObservationCategory, identity: str
    ) -> AuthorityObservationV1:
        digest = "sha256:" + hashlib.sha256(identity.encode()).hexdigest()
        return AuthorityObservationV1(
            observation_id=uuid5(NAMESPACE_URL, f"{request.system_id}:{category}:{digest}"),
            category=category,
            composite_state=digest,
        )


__all__ = ["RemoteLibvirtAuthorityMutationAdapter", "SUPPORTED_COMMIT_POINTS"]
