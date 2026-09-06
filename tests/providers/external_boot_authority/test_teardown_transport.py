"""Closed teardown proof transport retains the worker binding and deadline."""

from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import ExternalBootAuthorityClient
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.providers.external_boot_authority import protocol, transport
from kdive.providers.external_boot_authority.service import AuthenticatedPeer


def _request() -> protocol.AuthorityTeardownMutationRequestV1:
    return protocol.AuthorityTeardownMutationRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=uuid4(),
        activation_id=uuid4(),
        run_id=uuid4(),
        plan_identity="sha256:" + "1" * 64,
        purpose="teardown",
        operation="teardown",
        provider_kind="local-libvirt",
        authority_instance="authority-a",
        operation_identity="teardown-a",
        operation_digest="sha256:" + "2" * 64,
        attempt_id=uuid4(),
    )


@pytest.mark.anyio
async def test_typed_teardown_keeps_binding_deadline_and_proof() -> None:
    request = _request()
    proof = protocol.AuthorityTeardownRetainedQuarantineV1(disposition="retained_quarantine")
    expected = protocol.AuthorityTeardownResponseV1(
        observation=protocol.AuthorityObservationV1(
            observation_id=uuid4(),
            category="conflict",
            composite_state=protocol.teardown_proof_digest(proof),
        ),
        proof=proof,
        journal_sequence=5,
        journal_digest="sha256:" + "3" * 64,
    )
    deadlines: list[float] = []
    calls: list[protocol.AuthorityTeardownMutationRequestV1] = []

    async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
        assert credential.get_secret_value() == "test-credential"
        return AuthenticatedPeer("worker")

    class Service:
        async def execute_teardown(self, peer, value):
            assert peer.incarnation_id == "worker"
            calls.append(value)
            return expected

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            deadlines.append(deadline)
            return await transport._dispatch(envelope, authenticate, cast(Any, Service()))

    sender = AuthorityRequestSender(Backend, lambda: SecretStr("test-credential"))
    marker = ExternalBootAuthorityMarkerV1.model_validate(
        {
            name: getattr(request, name)
            for name in ExternalBootAuthorityMarkerV1.model_fields
            if hasattr(request, name)
        }
    )
    client = ExternalBootAuthorityClient(sender, marker, 123.0)
    assert await client.execute_teardown(request) == expected
    with pytest.raises(CategorizedError, match="binding-mismatch"):
        await client.execute_teardown(request.model_copy(update={"system_id": uuid4()}))
    assert calls == [request]
    assert deadlines == [123.0]


def test_teardown_envelope_rejects_non_teardown_mutation() -> None:
    request = protocol.AuthorityHealthRequestV1()
    with pytest.raises(ValueError, match="invalid-request"):
        transport.encode_request_envelope(
            "execute-teardown", request.model_dump(mode="json", by_alias=True), "test"
        )
