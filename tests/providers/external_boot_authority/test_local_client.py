"""Configured local authority-client transport contracts."""

from __future__ import annotations

import asyncio
import inspect
import json
import ssl
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

import kdive.config as config_registry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.local_client import (
    LocalAuthorityBinding,
    _AuthorityUnixTransport,
)
from kdive.providers.external_boot_authority.network_client import _resolve_tls_material
from kdive.providers.external_boot_authority.protocol import (
    AuthorityHealthAcknowledgementV1,
    AuthorityHealthRequestV1,
)
from kdive.providers.external_boot_authority.transport import (
    MAX_ENVELOPE_BYTES,
    authority_server_name,
    encode_request_envelope,
    read_frame,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from tests.providers.external_boot_authority.service_support import _mutation, _service
from tests.providers.external_boot_authority.tls_support import _tls_material

pytestmark = pytest.mark.anyio


async def test_typed_unix_mutation_replays_after_lost_terminal_response(tmp_path: Path) -> None:
    from kdive.jobs.authority_sender import AuthorityRequestSender
    from kdive.providers.external_boot_authority.service import AuthenticatedPeer
    from kdive.providers.external_boot_authority.transport import _dispatch

    service, repository, adapter, peer, takeover = _service(tmp_path)
    material = _tls_material(tmp_path, takeover.authority_instance)
    socket_path = tmp_path / "authority.sock"
    binding = replace(_binding(socket_path), authority_instance=takeover.authority_instance)
    context = _resolve_tls_material(binding, FileRefBackend(tmp_path, SecretRegistry()))
    credential = SecretStr("active-incarnation")
    sender = AuthorityRequestSender(
        lambda: _AuthorityUnixTransport(binding, context), lambda: credential
    )
    drop_mutation_response = True

    async def authenticate(value: SecretStr) -> AuthenticatedPeer:
        assert value == credential
        return peer

    async def dispatch(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal drop_mutation_response
        envelope = await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
        response = await _dispatch(envelope, authenticate, service)
        if json.loads(envelope)["operation"] == "execute-mutation" and drop_mutation_response:
            drop_mutation_response = False
            writer.transport.abort()
            return
        writer.write(len(response).to_bytes(4, "big") + response)
        await writer.drain()

    async with _server(socket_path, material, dispatch):
        acknowledgement = await sender.acknowledge_takeover(
            takeover, deadline=asyncio.get_running_loop().time() + 2
        )
        assert acknowledgement.journal_sequence == 2
        assert adapter.calls == []
        repository.current = True
        mutation = _mutation(takeover)
        with pytest.raises(
            CategorizedError, match="authority: (invalid-response|transport-failed)"
        ):
            await sender.execute_mutation(mutation, deadline=asyncio.get_running_loop().time() + 2)
        assert adapter.calls == ["commit:activate", "observe"]
        receipt = await sender.execute_mutation(
            mutation, deadline=asyncio.get_running_loop().time() + 2
        )
        assert receipt.category == "target"
        assert adapter.calls == ["commit:activate", "observe"]


_RESPONSE = json.dumps(
    {"status": "ok", "value": {"schema": "external-boot-authority-health-v1"}},
    sort_keys=True,
    separators=(",", ":"),
).encode()


def _binding(socket_path: Path) -> LocalAuthorityBinding:
    return LocalAuthorityBinding(
        authority_instance="authority-a",
        request_socket=socket_path,
        server_ca_ref="server-ca",
        client_cert_ref="client-certificate",
        client_key_ref="client-key",  # pragma: allowlist secret - fixture reference
    )


@asynccontextmanager
async def _server(
    socket_path: Path,
    material: dict[str, Path],
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncIterator[None]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(material["server_ca"]))
    context.load_cert_chain(str(material["server_certificate"]), str(material["server_key"]))
    tasks: set[asyncio.Task[None]] = set()

    async def session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await handler(reader, writer)
        finally:
            writer.close()
            with suppress(ConnectionError, ssl.SSLError):
                await writer.wait_closed()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.add(asyncio.create_task(session(reader, writer)))

    async with await asyncio.start_unix_server(connected, path=socket_path, ssl=context):
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _healthy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    request = json.loads(await read_frame(reader, maximum=MAX_ENVELOPE_BYTES))
    assert request["operation"] == "health"
    assert writer.get_extra_info("ssl_object").version() == "TLSv1.3"
    writer.write(len(_RESPONSE).to_bytes(4, "big") + _RESPONSE)
    await writer.drain()


async def test_configured_unix_binding_uses_tls_and_fixed_socket(tmp_path: Path) -> None:
    material = _tls_material(tmp_path, "authority-a")
    socket_path = tmp_path / "authority.sock"
    binding = _binding(socket_path)
    transport = _AuthorityUnixTransport(
        binding, _resolve_tls_material(binding, FileRefBackend(tmp_path, SecretRegistry()))
    )
    envelope = encode_request_envelope(
        "health",
        AuthorityHealthRequestV1().model_dump(mode="json", by_alias=True),
        "active-incarnation",
    )
    async with _server(socket_path, material, _healthy):
        response = await transport._request_frame(
            envelope, deadline=asyncio.get_running_loop().time() + 2
        )
    assert AuthorityHealthAcknowledgementV1.model_validate(json.loads(response)["value"])


@pytest.mark.parametrize("path", ["relative.sock", "\0abstract", "/" + "a" * 108])
def test_unix_binding_rejects_unconfined_or_oversize_socket(path: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        _AuthorityUnixTransport(
            _binding(Path(path)), ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert path not in str(caught.value)


async def test_wrong_authority_identity_is_redacted(tmp_path: Path) -> None:
    material = _tls_material(tmp_path, "authority-a")
    socket_path = tmp_path / "authority.sock"
    binding = LocalAuthorityBinding(
        authority_instance="authority-b",
        request_socket=socket_path,
        server_ca_ref="server-ca",
        client_cert_ref="client-certificate",
        client_key_ref="client-key",  # pragma: allowlist secret - fixture reference
    )
    transport = _AuthorityUnixTransport(
        binding, _resolve_tls_material(binding, FileRefBackend(tmp_path, SecretRegistry()))
    )
    async with _server(socket_path, material, _healthy):
        with pytest.raises(CategorizedError, match="authority: tls-rejected") as caught:
            await transport._request_frame(b"{}", deadline=asyncio.get_running_loop().time() + 2)
    assert authority_server_name(binding.authority_instance) not in str(caught.value)


def test_local_binding_is_disabled_only_when_all_worker_settings_are_absent() -> None:
    from kdive.providers.external_boot_authority.local_client import local_authority_binding

    config_registry.load({})
    assert local_authority_binding() is None
    config_registry.load({"KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a"})
    with pytest.raises(CategorizedError, match="authority: incomplete-local-binding") as caught:
        local_authority_binding()
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    ("variable", "raw"),
    [
        ("KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET", "/private/socket/" + "x" * 108),
        ("KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF", "private-ref-" + "x" * 256),
    ],
)
def test_malformed_local_binding_never_exposes_config_values(
    variable: str, raw: str, tmp_path: Path
) -> None:
    from kdive.jobs.authority_sender import local_authority_sender_factory

    config_registry.load({variable: raw})
    with pytest.raises(CategorizedError, match="authority: invalid-binding") as caught:
        local_authority_sender_factory(
            FileRefBackend(tmp_path, SecretRegistry()), lambda: SecretStr("unused")
        )

    error = caught.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert raw not in str(error)
    assert raw not in str(error.details)
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert raw not in "".join(traceback.format_exception(error))


def test_worker_validation_does_not_require_authority_host_settings() -> None:
    worker_environment = {
        "KDIVE_DATABASE_URL": "postgresql://example.invalid/kdive",
        "KDIVE_S3_ENDPOINT_URL": "http://example.invalid",
        "KDIVE_S3_BUCKET": "kdive",
    }
    config_registry.load(
        worker_environment | {"KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "host-only"}
    )
    config_registry.validate("worker")
    config_registry.load(
        worker_environment | {"KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET": "relative"}
    )
    with pytest.raises(CategorizedError) as caught:
        config_registry.validate("worker")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_worker_local_binding_matches_provisioned_authority_socket_and_refs() -> None:
    from kdive.providers.external_boot_authority.local_client import local_authority_binding
    from kdive.providers.external_boot_authority.settings import AUTHORITY_REQUEST_SOCKET

    socket_path = "/run/kdive/provider-authority/request/authority.sock"
    client_key_ref = "worker-client-key"  # pragma: allowlist secret - fixture reference
    config_registry.load(
        {
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET": socket_path,
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET": socket_path,
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF": "authority-server-ca",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF": "worker-client-cert",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF": client_key_ref,
        }
    )
    binding = local_authority_binding()
    assert binding is not None
    assert binding.request_socket == config_registry.require(AUTHORITY_REQUEST_SOCKET)
    assert (
        binding.server_ca_ref,
        binding.client_cert_ref,
        binding.client_key_ref,
    ) == ("authority-server-ca", "worker-client-cert", client_key_ref)


async def test_local_sender_factory_borrows_active_credential_at_encode(
    tmp_path: Path,
) -> None:
    from kdive.jobs.authority_sender import local_authority_sender_factory

    material = _tls_material(tmp_path, "authority-a")
    socket_path = tmp_path / "authority.sock"
    client_key_ref = "client-key"  # pragma: allowlist secret - fixture reference
    config_registry.load(
        {
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET": str(socket_path),
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF": "server-ca",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF": "client-certificate",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF": client_key_ref,
        }
    )
    credential = SecretStr("active-incarnation")
    borrowed: list[SecretStr] = []

    def borrow() -> SecretStr:
        borrowed.append(credential)
        return credential

    sender = local_authority_sender_factory(FileRefBackend(tmp_path, SecretRegistry()), borrow)
    assert sender is not None
    assert borrowed == []

    async def authenticated(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await read_frame(reader, maximum=MAX_ENVELOPE_BYTES))
        assert request["credential"] == "active-incarnation"
        writer.write(len(_RESPONSE).to_bytes(4, "big") + _RESPONSE)
        await writer.drain()

    async with _server(socket_path, material, authenticated):
        assert await sender.health(deadline=asyncio.get_running_loop().time() + 2)
    assert borrowed == [credential]


async def test_stale_credential_is_closed_and_redacted(tmp_path: Path) -> None:
    from kdive.jobs.authority_sender import local_authority_sender_factory

    material = _tls_material(tmp_path, "authority-a")
    socket_path = tmp_path / "authority.sock"
    client_key_ref = "client-key"  # pragma: allowlist secret - fixture reference
    config_registry.load(
        {
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET": str(socket_path),
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF": "server-ca",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF": "client-certificate",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF": client_key_ref,
        }
    )
    sender = local_authority_sender_factory(
        FileRefBackend(tmp_path, SecretRegistry()), lambda: SecretStr("stale-incarnation")
    )
    assert sender is not None
    response = b'{"category":"unauthenticated","status":"error"}'

    async def reject_stale(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await read_frame(reader, maximum=MAX_ENVELOPE_BYTES))
        assert request["credential"] == "stale-incarnation"
        writer.write(len(response).to_bytes(4, "big") + response)
        await writer.drain()

    async with _server(socket_path, material, reject_stale):
        with pytest.raises(CategorizedError, match="^authority: unauthenticated$") as caught:
            await sender.health(deadline=asyncio.get_running_loop().time() + 2)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "stale-incarnation" not in str(caught.value)


def test_local_sender_factory_accepts_no_caller_route() -> None:
    from kdive.jobs.authority_sender import local_authority_sender_factory

    assert tuple(inspect.signature(local_authority_sender_factory).parameters) == (
        "secret_backend",
        "borrow",
    )


async def test_local_transport_bounds_a_stalled_response(tmp_path: Path) -> None:
    material = _tls_material(tmp_path, "authority-a")
    socket_path = tmp_path / "authority.sock"
    binding = _binding(socket_path)
    transport = _AuthorityUnixTransport(
        binding, _resolve_tls_material(binding, FileRefBackend(tmp_path, SecretRegistry()))
    )

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
        await asyncio.Event().wait()

    async with _server(socket_path, material, stall):
        with pytest.raises(CategorizedError, match="authority: deadline-exceeded") as caught:
            await transport._request_frame(
                b'{"request":"bounded"}', deadline=asyncio.get_running_loop().time() + 0.05
            )
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize(
    "frame",
    [b"\0\0\0\0", (MAX_ENVELOPE_BYTES + 1).to_bytes(4, "big"), b"\0\0\0\x08x"],
)
async def test_local_transport_rejects_malformed_or_truncated_response(
    tmp_path: Path, frame: bytes
) -> None:
    material = _tls_material(tmp_path, "authority-a")
    socket_path = tmp_path / "authority.sock"
    binding = _binding(socket_path)
    transport = _AuthorityUnixTransport(
        binding, _resolve_tls_material(binding, FileRefBackend(tmp_path, SecretRegistry()))
    )

    async def malformed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
        writer.write(frame)
        await writer.drain()

    async with _server(socket_path, material, malformed):
        with pytest.raises(CategorizedError, match="authority: invalid-response") as caught:
            await transport._request_frame(
                b'{"request":"fixed"}', deadline=asyncio.get_running_loop().time() + 2
            )
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize(
    "reason",
    [
        "invalid-request",
        "unauthenticated",
        "superseded",
        "journal-conflict",
        "provider-conflict",
        "provider-not-configured",
        "provider-failure",
    ],
)
async def test_local_sender_preserves_closed_peer_rejections(tmp_path: Path, reason: str) -> None:
    from kdive.jobs.authority_sender import local_authority_sender_factory

    material = _tls_material(tmp_path, "authority-a")
    socket_path = tmp_path / "authority.sock"
    client_key_ref = "client-key"  # pragma: allowlist secret - fixture reference
    config_registry.load(
        {
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET": str(socket_path),
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF": "server-ca",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF": "client-certificate",
            "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF": client_key_ref,
        }
    )
    sender = local_authority_sender_factory(
        FileRefBackend(tmp_path, SecretRegistry()), lambda: SecretStr("active-incarnation")
    )
    assert sender is not None
    response = json.dumps(
        {"status": "error", "category": reason}, sort_keys=True, separators=(",", ":")
    ).encode()

    async def rejected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_frame(reader, maximum=MAX_ENVELOPE_BYTES)
        writer.write(len(response).to_bytes(4, "big") + response)
        await writer.drain()

    async with _server(socket_path, material, rejected):
        with pytest.raises(CategorizedError, match=f"^authority: {reason}$") as caught:
            await sender.health(deadline=asyncio.get_running_loop().time() + 2)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
