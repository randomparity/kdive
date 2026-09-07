"""Authenticated transport for authority-owned System operations."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.providers.external_boot_authority import transport
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.providers.system_authority.protocol import (
    AuthoritySystemAcknowledgementV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemResponseV1,
    AuthoritySystemRetainedQuarantineV1,
    AuthoritySystemTakeoverRequestV1,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _takeover() -> AuthoritySystemTakeoverRequestV1:
    return AuthoritySystemTakeoverRequestV1(
        system_id=uuid4(),
        allocation_id=uuid4(),
        resource_id=uuid4(),
        provider_kind="local-libvirt",
        resource_name="host-a",
        authority_instance="authority-a",
        profile_identity=_DIGEST_A,
        root_identity=_DIGEST_B,
        bootstrap_identity=_DIGEST_A,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-a",
        authority_id=uuid4(),
        generation=1,
        attempt_id=uuid4(),
        operation_digest=_DIGEST_B,
    )


def _mutation(request: AuthoritySystemTakeoverRequestV1) -> AuthoritySystemMutationRequestV1:
    return AuthoritySystemMutationRequestV1.model_validate(
        request.model_dump(mode="python", by_alias=True)
    )


def _acknowledgement(request: AuthoritySystemTakeoverRequestV1) -> AuthoritySystemAcknowledgementV1:
    return AuthoritySystemAcknowledgementV1(
        authority_id=request.authority_id,
        generation=request.generation,
        attempt_id=request.attempt_id,
        journal_sequence=3,
        journal_digest=_DIGEST_A,
        quiescence_digest=_DIGEST_B,
    )


def _response(request: AuthoritySystemMutationRequestV1) -> AuthoritySystemResponseV1:
    return AuthoritySystemResponseV1(
        proof=AuthoritySystemRetainedQuarantineV1(
            **request.model_dump(mode="python", by_alias=True, exclude={"schema_"}),
            disposition="retained-quarantine",
            observation_digest=_DIGEST_A,
        ),
        journal_sequence=7,
        journal_digest=_DIGEST_B,
    )


@pytest.mark.anyio
async def test_sender_dispatches_typed_takeover_and_execution_after_authentication() -> None:
    request = _takeover()
    mutation = _mutation(request)
    expected_acknowledgement = _acknowledgement(request)
    expected = _response(mutation)
    calls: list[tuple[str, UUID]] = []
    deadlines: list[float] = []

    async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
        assert credential.get_secret_value() == "test-credential"
        return AuthenticatedPeer("worker-a")

    class Service:
        async def begin(
            self, peer_incarnation: str, request: AuthoritySystemTakeoverRequestV1
        ) -> AuthoritySystemAcknowledgementV1:
            assert peer_incarnation == "worker-a"
            calls.append(("begin", request.attempt_id))
            return expected_acknowledgement

        async def execute(
            self,
            peer_incarnation: str,
            request: AuthoritySystemMutationRequestV1,
            acknowledgement: AuthoritySystemAcknowledgementV1,
        ) -> AuthoritySystemResponseV1:
            assert peer_incarnation == "worker-a"
            assert request == mutation
            assert acknowledgement == expected_acknowledgement
            calls.append(("execute", request.attempt_id))
            return expected

    service = Service()

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            deadlines.append(deadline)
            return await transport._dispatch(
                envelope,
                authenticate,
                None,
                system_service=service,
            )

    sender = AuthorityRequestSender(Backend, lambda: SecretStr("test-credential"))
    assert (
        await sender.acknowledge_system_takeover(request, deadline=11.0) == expected_acknowledgement
    )
    assert (
        await sender.execute_system_operation(mutation, expected_acknowledgement, deadline=12.0)
        == expected
    )
    assert calls == [("begin", request.attempt_id), ("execute", request.attempt_id)]
    assert deadlines == [11.0, 12.0]


@pytest.mark.anyio
async def test_system_dispatch_authenticates_before_service_and_requires_configuration() -> None:
    request = _takeover()
    called = False

    async def reject(_credential: SecretStr) -> AuthenticatedPeer:
        raise ValueError

    class Service:
        async def begin(self, *_args: object) -> AuthoritySystemAcknowledgementV1:
            nonlocal called
            called = True
            return _acknowledgement(request)

    envelope = transport.encode_request_envelope(
        "acknowledge-system-takeover",
        request.model_dump(mode="json", by_alias=True),
        "credential",
    )
    denied = await transport._dispatch(
        envelope,
        reject,
        None,
        system_service=cast(Any, Service()),
    )
    assert json.loads(denied) == {"category": "unauthenticated", "status": "error"}
    assert called is False

    async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
        return AuthenticatedPeer("worker-a")

    unconfigured = await transport._dispatch(envelope, authenticate, None)
    assert json.loads(unconfigured) == {
        "category": "provider-not-configured",
        "status": "error",
    }


@pytest.mark.anyio
async def test_sender_rejects_acknowledgement_for_another_attempt_before_transport() -> None:
    request = _takeover()

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            del envelope
            del deadline
            pytest.fail("mismatched acknowledgement must not reach the transport")

    sender = AuthorityRequestSender(Backend, lambda: SecretStr("test-credential"))
    wrong = _acknowledgement(request).model_copy(update={"attempt_id": uuid4()})
    with pytest.raises(CategorizedError, match="authority: invalid-request"):
        await sender.execute_system_operation(_mutation(request), wrong, deadline=12.0)
