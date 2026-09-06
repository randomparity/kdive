"""Borrow the worker assembly's credential only to encode closed requests (ADR-0606)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel, SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.device_identity import (
    DeviceIdentityRequestV1,
    DeviceIdentityResponseV1,
    decode_device_identity_response,
)
from kdive.providers.external_boot_authority.local_client import (
    _AuthorityUnixTransport,
    local_authority_binding,
)
from kdive.providers.external_boot_authority.network_client import (
    _AuthorityNetworkTransport,
    _resolve_tls_material,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityConflictResolutionRequestV1,
    AuthorityHealthAcknowledgementV1,
    AuthorityHealthRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
    AuthorityRunningObservationV1,
    AuthorityTakeoverRequestV1,
)
from kdive.providers.external_boot_authority.transport import (
    MAX_ENVELOPE_BYTES,
    Operation,
    encode_request_envelope,
)
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.security.secrets.secrets import SecretBackend

_PEER_REASONS = frozenset(
    {
        "invalid-request",
        "unauthenticated",
        "superseded",
        "journal-conflict",
        "provider-conflict",
        "provider-not-configured",
        "provider-failure",
    }
)


class _AuthorityTransport(Protocol):
    def _request_frame(self, envelope: bytes, *, deadline: float) -> Awaitable[bytes]: ...


def _failure(reason: str) -> CategorizedError:
    return CategorizedError(f"authority: {reason}", category=ErrorCategory.INFRASTRUCTURE_FAILURE)


def _decode_response[Value: BaseModel](payload: bytes, model: type[Value]) -> Value:
    try:
        if not payload or len(payload) > MAX_ENVELOPE_BYTES:
            raise ValueError
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError
        if (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            != payload
        ):
            raise ValueError
        if value.get("status") == "ok" and set(value) == {"status", "value"}:
            return model.model_validate(value["value"])
        if (
            value.get("status") == "error"
            and set(value) == {"status", "category"}
            and isinstance(value["category"], str)
            and value["category"] in _PEER_REASONS
        ):
            raise _failure(value["category"])
    except ValueError, TypeError, RecursionError:
        pass
    raise _failure("invalid-response") from None


class AuthorityRequestSender:
    """Reference-only route; TLS and transport exist only during typed authority calls."""

    __slots__ = ("_transport_factory", "_borrow")

    def __init__(
        self,
        transport_factory: Callable[[], _AuthorityTransport],
        borrow: Callable[[], SecretStr],
    ) -> None:
        self._transport_factory = transport_factory
        self._borrow = borrow

    def _encode(self, operation: Operation, request: BaseModel) -> bytes:
        credential = self._borrow()
        if not isinstance(credential, SecretStr):
            raise _failure("credential-unavailable")
        try:
            return encode_request_envelope(
                operation,
                request.model_dump(mode="json", by_alias=True),
                credential.get_secret_value(),
            )
        except ValueError, TypeError:
            raise _failure("invalid-request") from None

    async def health(self, *, deadline: float) -> AuthorityHealthAcknowledgementV1:
        response = await self._transport_factory()._request_frame(
            self._encode("health", AuthorityHealthRequestV1()), deadline=deadline
        )
        return _decode_response(response, AuthorityHealthAcknowledgementV1)

    async def resolve_device_identity(
        self, request: DeviceIdentityRequestV1, *, deadline: float
    ) -> DeviceIdentityResponseV1:
        response = await self._transport_factory()._request_frame(
            self._encode("resolve-device-identity", request), deadline=deadline
        )
        try:
            value = json.loads(response)
            if (
                not isinstance(value, dict)
                or json.dumps(
                    value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
                != response
            ):
                raise ValueError
            if value.get("status") == "ok" and set(value) == {"status", "value"}:
                encoded = json.dumps(
                    value["value"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
                return decode_device_identity_response(encoded)
            if (
                value.get("status") == "error"
                and set(value) == {"status", "category"}
                and value.get("category") in _PEER_REASONS
            ):
                raise _failure(str(value["category"]))
        except CategorizedError:
            raise
        except ValueError, TypeError, RecursionError:
            pass
        raise CategorizedError(
            "remote device identity is invalid", category=ErrorCategory.CONFLICT
        ) from None

    async def acknowledge_takeover(
        self, request: AuthorityTakeoverRequestV1, *, deadline: float
    ) -> AuthorityAcknowledgementV1:
        response = await self._transport_factory()._request_frame(
            self._encode("acknowledge-takeover", request), deadline=deadline
        )
        return _decode_response(response, AuthorityAcknowledgementV1)

    async def execute_mutation(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityObservationV1:
        response = await self._transport_factory()._request_frame(
            self._encode("execute-mutation", request), deadline=deadline
        )
        return _decode_response(response, AuthorityObservationV1)

    async def execute_preparation(
        self, request: AuthorityPreparationMutationRequestV1, *, deadline: float
    ) -> AuthorityPreparationResponseV1:
        response = await self._transport_factory()._request_frame(
            self._encode("execute-preparation", request), deadline=deadline
        )
        return _decode_response(response, AuthorityPreparationResponseV1)

    async def execute_remote_module_preparation(
        self, request: RemoteModuleVolumePreparationRequestV1, *, deadline: float
    ) -> RemoteModuleTerminalPreparationResponseV1:
        """Run one closed remote-module preparation on the Resource-bound authority host."""
        response = await self._transport_factory()._request_frame(
            self._encode("execute-remote-module-preparation", request), deadline=deadline
        )
        return _decode_response(response, RemoteModuleTerminalPreparationResponseV1)

    async def open_remote_module_attempt(
        self, request: AuthorityPreparationMutationRequestV1, *, deadline: float
    ) -> ModuleAttemptPreparationRequestV1:
        """Anchor one PREPARE phase and return its authority-opened module-attempt receipt."""
        response = await self._transport_factory()._request_frame(
            self._encode("begin-remote-module-preparation", request), deadline=deadline
        )
        return _decode_response(response, ModuleAttemptPreparationRequestV1)

    async def observe_authority(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityObservationV1:
        """Read the current bound provider state without admitting a mutation."""
        response = await self._transport_factory()._request_frame(
            self._encode("observe-authority", request), deadline=deadline
        )
        return _decode_response(response, AuthorityObservationV1)

    async def observe_running(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityRunningObservationV1:
        response = await self._transport_factory()._request_frame(
            self._encode("observe-running", request), deadline=deadline
        )
        return _decode_response(response, AuthorityRunningObservationV1)

    async def execute_conflict_resolution(
        self, request: AuthorityConflictResolutionRequestV1, *, deadline: float
    ) -> AuthorityObservationV1:
        response = await self._transport_factory()._request_frame(
            self._encode("execute-conflict-resolution", request), deadline=deadline
        )
        return _decode_response(response, AuthorityObservationV1)


def authority_sender_factory(
    secret_backend: SecretBackend, borrow: Callable[[], SecretStr]
) -> Callable[[RemoteAuthorityBinding], AuthorityRequestSender]:
    """Capture the selected binding and existing owners; resolve no authority material."""

    def build(binding: RemoteAuthorityBinding) -> AuthorityRequestSender:
        return AuthorityRequestSender(
            lambda: _AuthorityNetworkTransport(
                binding, _resolve_tls_material(binding, secret_backend)
            ),
            borrow,
        )

    return build


def local_authority_sender_factory(
    secret_backend: SecretBackend, borrow: Callable[[], SecretStr]
) -> AuthorityRequestSender | None:
    """Build the configured worker-local sender without accepting a caller route."""
    binding = local_authority_binding()
    if binding is None:
        return None
    return AuthorityRequestSender(
        lambda: _AuthorityUnixTransport(binding, _resolve_tls_material(binding, secret_backend)),
        borrow,
    )
