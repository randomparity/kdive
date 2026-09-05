"""Borrow the worker assembly's credential only to encode closed requests (ADR-0606)."""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import BaseModel, SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.network_client import (
    _AuthorityNetworkTransport,
    _resolve_tls_material,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityHealthAcknowledgementV1,
    AuthorityHealthRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityTakeoverRequestV1,
)
from kdive.providers.external_boot_authority.transport import (
    MAX_ENVELOPE_BYTES,
    Operation,
    encode_request_envelope,
)
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding
from kdive.security.secrets.secrets import SecretBackend

_PEER_REASONS = frozenset(
    {
        "invalid-request",
        "unauthenticated",
        "superseded",
        "journal-conflict",
        "provider-conflict",
        "provider-not-configured",
    }
)


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
        transport_factory: Callable[[], _AuthorityNetworkTransport],
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
