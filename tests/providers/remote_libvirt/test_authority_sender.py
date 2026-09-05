"""Worker authority borrowing and Resource routing contracts (ADR-0606)."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority import protocol, transport
from kdive.providers.external_boot_authority.network_client import _AuthorityNetworkTransport
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.providers.remote_libvirt import composition
from kdive.providers.remote_libvirt.config import (
    RemoteAuthorityBinding,
    RemoteLibvirtConfig,
    TlsCertRefs,
)
from kdive.security.secrets.secret_registry import SecretRegistry

pytestmark = pytest.mark.anyio


def _sender(backend: object, borrow: object):
    from kdive.jobs.authority_sender import AuthorityRequestSender

    return AuthorityRequestSender(
        cast(_AuthorityNetworkTransport, backend), cast(Callable[[], SecretStr], borrow)
    )


async def test_sender_borrows_only_while_encoding_and_authenticates_active_incarnation() -> None:
    owner = SimpleNamespace(incarnation_credential=SecretStr("active-test-incarnation"))
    borrowed: list[SecretStr] = []

    def borrow() -> SecretStr:
        borrowed.append(owner.incarnation_credential)
        return owner.incarnation_credential

    async def authenticate(value: SecretStr) -> AuthenticatedPeer:
        if value.get_secret_value() != "active-test-incarnation":
            raise ValueError("private authentication detail")
        return AuthenticatedPeer("worker-a")

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            assert deadline == 123.0
            return await transport._dispatch(envelope, authenticate, None)

    sender = _sender(Backend(), borrow)
    assert borrowed == []
    assert await sender.health(deadline=123.0) == protocol.AuthorityHealthAcknowledgementV1()
    assert borrowed == [owner.incarnation_credential]
    owner.incarnation_credential = SecretStr("inactive-test-incarnation")
    with pytest.raises(CategorizedError, match="authority: unauthenticated") as caught:
        await sender.health(deadline=123.0)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert borrowed[-1] is owner.incarnation_credential
    assert {name for name in dir(sender) if not name.startswith("_")} == {
        "health",
        "acknowledge_takeover",
        "execute_mutation",
    }
    assert all(not isinstance(getattr(sender, slot), SecretStr) for slot in sender.__slots__)


async def test_cancellation_retains_no_method_local_secret_or_copied_credential() -> None:
    started = asyncio.Event()
    owner = SimpleNamespace(incarnation_credential=SecretStr("cancellation-test-incarnation"))

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            started.set()
            await asyncio.Event().wait()
            return b""

    sender = _sender(Backend(), lambda: owner.incarnation_credential)
    task = asyncio.create_task(sender.health(deadline=123.0))
    await started.wait()
    coroutine = task.get_coro()
    while coroutine is not None:
        frame = getattr(coroutine, "cr_frame", None)
        if frame is not None:
            assert all(not isinstance(value, SecretStr) for value in frame.f_locals.values())
            assert owner.incarnation_credential.get_secret_value() not in [
                value for value in frame.f_locals.values() if isinstance(value, str)
            ]
        coroutine = getattr(coroutine, "cr_await", None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not isinstance(getattr(sender, slot), SecretStr) for slot in sender.__slots__)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"status":"ok","value":{},"address":"private"}',
        b'{"status":"error","category":"private-peer-detail"}',
        b'{"status":"ok","value":{"schema":"wrong"}}',
    ],
)
async def test_sender_rejects_malformed_response_without_peer_text(payload: bytes) -> None:
    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            return payload

    sender = _sender(Backend(), lambda: SecretStr("response-test-incarnation"))
    with pytest.raises(CategorizedError, match="^authority: invalid-response$") as caught:
        await sender.health(deadline=123.0)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {}


def test_resource_rebinding_selects_its_own_closed_route(monkeypatch: pytest.MonkeyPatch) -> None:
    configs = {
        name: RemoteLibvirtConfig(
            uri="qemu+tls://example.invalid/system",
            cert_refs=TlsCertRefs("cert", "key", "ca"),
            concurrent_allocation_cap=1,
            authority=RemoteAuthorityBinding(name, address, 9443, "ca", "cert", "key"),
        )
        for name, address in (("resource-a", "192.0.2.1"), ("resource-b", "192.0.2.2"))
    }
    selected = []

    def factory(binding):
        selected.append(binding)
        return SimpleNamespace(health=None)

    monkeypatch.setattr(composition, "remote_config_for_resource", configs.__getitem__)
    runtime = composition.build_runtime(
        secret_registry=SecretRegistry(), authority_sender_factory=factory
    )
    assert runtime.authority is None
    first = runtime.for_resource("resource-a")
    second = runtime.for_resource("resource-b")
    assert first.authority is not second.authority
    assert selected == [configs["resource-a"].authority, configs["resource-b"].authority]
    configs["resource-a"] = replace(configs["resource-a"], authority=None)
    assert runtime.for_resource("resource-a").authority is None
    assert (
        composition.build_runtime(secret_registry=SecretRegistry())
        .for_resource("resource-b")
        .authority
        is None
    )


async def test_missing_credential_fails_before_transport() -> None:
    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            raise AssertionError("missing credential reached transport")

    sender = _sender(Backend(), lambda: None)
    with pytest.raises(CategorizedError, match="^authority: credential-unavailable$"):
        await sender.health(deadline=123.0)


@pytest.mark.parametrize("mutation", [False, True])
async def test_existing_operations_preserve_envelopes_and_typed_responses(mutation: bool) -> None:
    binding = {
        "authority_id": uuid4(),
        "generation": 1,
        "system_id": uuid4(),
        "activation_id": uuid4(),
        "run_id": uuid4(),
        "plan_identity": "sha256:" + "a" * 64,
        "purpose": "recover",
        "operation": "recover",
        "provider_kind": "remote-libvirt",
        "authority_instance": "authority-a",
        "operation_identity": "operation-a",
        "operation_digest": "sha256:" + "b" * 64,
    }
    takeover = protocol.AuthorityTakeoverRequestV1.model_validate(binding)
    request = (
        protocol.AuthorityMutationRequestV1.model_validate(
            {
                **binding,
                "attempt_id": uuid4(),
                "expected_source_identity": "source",
                "intended_target_identity": "target",
                "recovery_objects": [],
            }
        )
        if mutation
        else takeover
    )
    expected = (
        protocol.AuthorityObservationV1(
            observation_id=uuid4(),
            category="target",
            composite_state="sha256:" + "c" * 64,
        )
        if mutation
        else protocol.AuthorityAcknowledgementV1(
            authority_id=takeover.authority_id,
            generation=1,
            system_id=takeover.system_id,
            journal_sequence=1,
            journal_digest="sha256:" + "d" * 64,
            positive_quiescence_digest="sha256:" + "e" * 64,
        )
    )
    operation = "execute-mutation" if mutation else "acknowledge-takeover"

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            assert envelope == transport.encode_request_envelope(
                operation,
                request.model_dump(mode="json", by_alias=True),
                "operation-test-incarnation",
            )
            assert deadline == 321.0
            return json.dumps(
                {"status": "ok", "value": expected.model_dump(mode="json", by_alias=True)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()

    sender = _sender(Backend(), lambda: SecretStr("operation-test-incarnation"))
    method = sender.execute_mutation if mutation else sender.acknowledge_takeover
    assert set(inspect.signature(method).parameters) == {"request", "deadline"}
    assert await method(request, deadline=321.0) == expected


@pytest.mark.parametrize("credential", ["", "a" * 4097])
async def test_invalid_credential_is_redacted_before_io(credential: str) -> None:
    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            raise AssertionError("invalid credential reached transport")

    sender = _sender(Backend(), lambda: SecretStr(credential))
    with pytest.raises(CategorizedError, match="^authority: invalid-request$"):
        await sender.health(deadline=123.0)
