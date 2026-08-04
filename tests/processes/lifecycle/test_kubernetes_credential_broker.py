"""Pre-start Kubernetes worker credential broker behavior."""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import socket
import ssl
import urllib.request
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import kdive.processes.lifecycle.kubernetes_credential_broker as credential_broker
from kdive.processes.lifecycle.kubernetes_credential_broker import (
    BROKER_AUDIENCE,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    BrokerRequest,
    KubernetesCredentialBroker,
    PodIdentity,
    decode_reply,
    encode_request,
    read_frame,
    serve_broker,
    tls_server_context,
    write_frame,
)
from kdive.processes.lifecycle.kubernetes_termination_witness import FINALIZER


def _pod(
    *, name: str = "kdive-worker-0", uid: str = "uid-1", phase: str = "Pending"
) -> dict[str, object]:
    return {
        "metadata": {
            "name": name,
            "uid": uid,
            "resourceVersion": "7",
            "finalizers": [FINALIZER],
        },
        "status": {"phase": phase},
    }


@dataclass
class _Store:
    envelopes: dict[tuple[str, str, str], bytes] = field(default_factory=dict)
    acknowledged: set[tuple[str, str, str]] = field(default_factory=set)
    registrations: list[tuple[PodIdentity, bytes, bytes]] = field(default_factory=list)
    pending_reads: list[PodIdentity] = field(default_factory=list)
    acknowledgments: list[PodIdentity] = field(default_factory=list)

    async def register(
        self, identity: PodIdentity, credential_hash: bytes, envelope: bytes
    ) -> bool:
        self.registrations.append((identity, credential_hash, envelope))
        self.envelopes.setdefault(_runtime_key(identity), envelope)
        return True

    async def pending_envelope(self, identity: PodIdentity) -> bytes | None:
        self.pending_reads.append(identity)
        return self.envelopes.get(_runtime_key(identity))

    async def acknowledge(self, identity: PodIdentity) -> bool:
        self.acknowledgments.append(identity)
        key = _runtime_key(identity)
        if key in self.acknowledged:
            return True
        if self.envelopes.pop(key, None) is None:
            return False
        self.acknowledged.add(key)
        return True


def _runtime_key(identity: PodIdentity) -> tuple[str, str, str]:
    return identity.namespace, identity.name, identity.uid


def _broker(store: _Store, pods: dict[str, dict[str, object]]) -> KubernetesCredentialBroker:
    return KubernetesCredentialBroker(
        namespace="kdive",
        worker_name="kdive-worker",
        ordinal_ceiling=1,
        pass_limit=1,
        read_pod=lambda namespace, name: pods.get(name),
        register=store.register,
        pending_envelope=store.pending_envelope,
        acknowledge=store.acknowledge,
        token_review=lambda token, audience: _token_identity(token, audience),
        encrypt=lambda credential: b"encrypted:" + credential.encode(),
        decrypt=lambda envelope: envelope.removeprefix(b"encrypted:").decode(),
        credential=lambda: "a" * 64,
    )


async def _token_identity(token: str, audience: str) -> PodIdentity | None:
    if token == "bound-token" and audience == BROKER_AUDIENCE:
        return PodIdentity("kdive", "kdive-worker-0", "uid-1", "")
    return None


def test_pending_finalized_pod_is_registered_before_worker_start() -> None:
    store = _Store()
    broker = _broker(store, {"kdive-worker-0": _pod()})

    assert asyncio.run(broker.pre_register_once()) == 1
    identity, credential_hash, envelope = store.registrations[0]
    assert identity == PodIdentity("kdive", "kdive-worker-0", "uid-1", "7")
    assert credential_hash == hashlib.sha256(("a" * 64).encode()).digest()
    assert envelope != ("a" * 64).encode()


def test_delivery_is_idempotent_before_ack_and_refused_after_durable_ack() -> None:
    store = _Store()
    pods = {"kdive-worker-0": _pod()}
    broker = _broker(store, pods)
    assert asyncio.run(broker.pre_register_once()) == 1
    request = BrokerRequest("deliver", "bound-token", "kdive", "kdive-worker-0", "uid-1")

    first = asyncio.run(broker.handle(request))
    second = asyncio.run(broker.handle(request))
    assert first.credential == second.credential == "a" * 64

    acknowledged = asyncio.run(
        broker.handle(BrokerRequest("ack", "bound-token", "kdive", "kdive-worker-0", "uid-1"))
    )
    repeated_ack = asyncio.run(
        broker.handle(BrokerRequest("ack", "bound-token", "kdive", "kdive-worker-0", "uid-1"))
    )
    refused = asyncio.run(broker.handle(request))
    assert acknowledged.acknowledged is repeated_ack.acknowledged is True
    assert acknowledged.credential is repeated_ack.credential is refused.credential is None
    assert refused.refused is True


def test_same_uid_uses_fresh_resource_versions_for_delivery_and_acknowledgment() -> None:
    store = _Store()
    pod = _pod()
    broker = _broker(store, {"kdive-worker-0": pod})
    request = BrokerRequest("deliver", "bound-token", "kdive", "kdive-worker-0", "uid-1")

    assert asyncio.run(broker.pre_register_once()) == 1
    metadata = cast(dict[str, object], pod["metadata"])
    metadata["resourceVersion"] = "8"
    assert asyncio.run(broker.handle(request)).credential == "a" * 64

    metadata["resourceVersion"] = "9"
    acknowledged = asyncio.run(
        broker.handle(BrokerRequest("ack", "bound-token", "kdive", "kdive-worker-0", "uid-1"))
    )
    assert acknowledged.acknowledged is True
    assert store.pending_reads == [PodIdentity("kdive", "kdive-worker-0", "uid-1", "8")]
    assert store.acknowledgments == [PodIdentity("kdive", "kdive-worker-0", "uid-1", "9")]


def test_broker_requests_exact_fixed_tokenreview_audience() -> None:
    seen_audiences: list[str] = []

    async def review(token: str, audience: str) -> PodIdentity | None:
        seen_audiences.append(audience)
        return await _token_identity(token, audience)

    store = _Store()
    broker = replace(_broker(store, {"kdive-worker-0": _pod()}), token_review=review)
    assert asyncio.run(broker.pre_register_once()) == 1

    reply = asyncio.run(
        broker.handle(BrokerRequest("deliver", "bound-token", "kdive", "kdive-worker-0", "uid-1"))
    )
    assert reply.credential == "a" * 64
    assert seen_audiences == [BROKER_AUDIENCE]


def test_authenticated_tokenreview_with_mismatched_returned_audience_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projected_token = tmp_path / "service-account-token"
    projected_token.write_text("broker-token", encoding="utf-8")
    requests: list[urllib.request.Request] = []
    response = {
        "status": {
            "authenticated": True,
            "audiences": ["other-audience"],
            "user": {
                "extra": {
                    "authentication.kubernetes.io/pod-name": ["kdive-worker-0"],
                    "authentication.kubernetes.io/pod-uid": ["uid-1"],
                }
            },
        }
    }

    def urlopen(request: urllib.request.Request, *, timeout: float, context: object) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(credential_broker, "_SERVICE_ACCOUNT_TOKEN", projected_token)
    monkeypatch.setattr(credential_broker.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(credential_broker.urllib.request, "urlopen", urlopen)

    assert credential_broker._token_review("bound-token", BROKER_AUDIENCE) is None
    assert len(requests) == 1
    request_data = requests[0].data
    assert isinstance(request_data, bytes)
    assert json.loads(request_data)["spec"]["audiences"] == [BROKER_AUDIENCE]


@pytest.mark.parametrize(
    ("token", "uid"),
    [("wrong-audience-token", "uid-1"), ("bound-token", "replacement-uid")],
)
def test_unbound_or_replaced_pod_cannot_receive_the_envelope(token: str, uid: str) -> None:
    store = _Store()
    broker = _broker(store, {"kdive-worker-0": _pod()})
    assert asyncio.run(broker.pre_register_once()) == 1

    reply = asyncio.run(
        broker.handle(BrokerRequest("deliver", token, "kdive", "kdive-worker-0", uid))
    )
    assert reply.refused is True
    assert reply.credential is None


def test_frame_bounds_reject_oversized_requests_and_responses() -> None:
    async def read_oversized() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data((MAX_REQUEST_BYTES + 1).to_bytes(4, "big"))
        with pytest.raises(ValueError, match="request frame exceeds"):
            await read_frame(reader, maximum=MAX_REQUEST_BYTES, kind="request")

    class _Writer:
        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    asyncio.run(read_oversized())
    with pytest.raises(ValueError, match="response frame exceeds"):
        asyncio.run(
            write_frame(_Writer(), b"x" * (MAX_RESPONSE_BYTES + 1), maximum=MAX_RESPONSE_BYTES)
        )


def test_broker_refuses_to_start_without_operator_supplied_tls_material(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        tls_server_context(
            certificate=str(tmp_path / "missing-cert.pem"),
            private_key=str(tmp_path / "missing-key.pem"),
            ca=str(tmp_path / "missing-ca.pem"),
        )


def _tls_contexts(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "broker.pem"
    key_path = tmp_path / "broker-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = tls_server_context(
        certificate=str(certificate_path),
        private_key=str(key_path),
        ca=str(certificate_path),
    )
    client_context = ssl.create_default_context(cafile=str(certificate_path))
    client_context.minimum_version = ssl.TLSVersion.TLSv1_3
    return server_context, client_context


def _available_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def test_tls_handshakes_are_admitted_before_the_session_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> None:
        session_limit = 2
        server_context, client_context = _tls_contexts(tmp_path)
        loop = asyncio.get_running_loop()
        connect_accepted_socket = loop.connect_accepted_socket
        active_handshakes = 0
        peak_handshakes = 0
        at_capacity = asyncio.Event()
        handshake_timeouts: list[float | None] = []
        shutdown_timeouts: list[float | None] = []

        async def track_connect_accepted_socket(*args: Any, **kwargs: Any) -> Any:
            nonlocal active_handshakes, peak_handshakes
            active_handshakes += 1
            peak_handshakes = max(peak_handshakes, active_handshakes)
            handshake_timeouts.append(kwargs.get("ssl_handshake_timeout"))
            shutdown_timeouts.append(kwargs.get("ssl_shutdown_timeout"))
            if active_handshakes == session_limit:
                at_capacity.set()
            try:
                return await connect_accepted_socket(*args, **kwargs)
            finally:
                active_handshakes -= 1

        monkeypatch.setattr(loop, "connect_accepted_socket", track_connect_accepted_socket)
        monkeypatch.setattr(
            credential_broker, "MAX_CONCURRENT_SESSIONS", session_limit, raising=False
        )
        store = _Store()
        broker = _broker(store, {"kdive-worker-0": _pod()})
        assert await broker.pre_register_once() == 1
        stop = asyncio.Event()
        port = _available_port()
        server = asyncio.create_task(
            serve_broker(
                broker,
                stop,
                host="127.0.0.1",
                port=port,
                ssl_context=server_context,
            )
        )
        await asyncio.sleep(0)
        incomplete: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        try:
            for _ in range(session_limit + 2):
                incomplete.append(await asyncio.open_connection("127.0.0.1", port))
            await asyncio.wait_for(at_capacity.wait(), timeout=1)
            assert peak_handshakes == session_limit
            assert active_handshakes <= session_limit
            assert set(handshake_timeouts) == {5}
            assert set(shutdown_timeouts) == {5}

            for _, writer in incomplete:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _, writer in incomplete))
            request = encode_request(
                BrokerRequest("deliver", "bound-token", "kdive", "kdive-worker-0", "uid-1")
            )
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    "127.0.0.1",
                    port,
                    ssl=client_context,
                    server_hostname="localhost",
                ),
                timeout=3,
            )
            try:
                await write_frame(writer, request, maximum=MAX_REQUEST_BYTES)
                reply = decode_reply(
                    await read_frame(reader, maximum=MAX_RESPONSE_BYTES, kind="response")
                )
                assert reply.credential == "a" * 64
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            for _, writer in incomplete:
                writer.close()
            stop.set()
            await server

    asyncio.run(run())


class _TestWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = asyncio.Event()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed.set()

    async def wait_closed(self) -> None:
        return None


def _session_handler(
    monkeypatch: pytest.MonkeyPatch,
    broker: KubernetesCredentialBroker,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, None]]:
    monkeypatch.setattr(credential_broker, "CONNECTION_TIMEOUT_SECONDS", 0.02, raising=False)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await credential_broker._handle_session(broker, reader, writer)

    return handle


def _valid_reader() -> asyncio.StreamReader:
    request = encode_request(
        BrokerRequest("deliver", "bound-token", "kdive", "kdive-worker-0", "uid-1")
    )
    reader = asyncio.StreamReader()
    reader.feed_data(len(request).to_bytes(4, "big") + request)
    return reader


def _invalid_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data((1).to_bytes(4, "big") + b"{")
    return reader


@pytest.mark.parametrize(
    "partial",
    [b"", b"\x00\x00", b"\x00\x00\x00\x08{"],
    ids=["no-prefix", "partial-prefix", "partial-payload"],
)
def test_partial_frames_are_closed_by_the_full_exchange_timeout(
    monkeypatch: pytest.MonkeyPatch, partial: bytes
) -> None:
    async def run() -> None:
        handler = _session_handler(monkeypatch, _broker(_Store(), {}))
        reader = asyncio.StreamReader()
        reader.feed_data(partial)
        writer = _TestWriter()
        task = asyncio.create_task(handler(reader, cast(asyncio.StreamWriter, writer)))
        await asyncio.sleep(0.05)
        try:
            assert task.done()
            assert writer.closed.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_timed_out_application_operation_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BlockingBroker:
        calls = 0

        async def handle(self, request: BrokerRequest) -> credential_broker.BrokerReply:
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            return credential_broker.BrokerReply(refused=True)

    async def run() -> None:
        broker = cast(KubernetesCredentialBroker, _BlockingBroker())
        handler = _session_handler(monkeypatch, broker)
        timed_out_writer = _TestWriter()
        timed_out = asyncio.create_task(
            handler(_valid_reader(), cast(asyncio.StreamWriter, timed_out_writer))
        )
        await asyncio.sleep(0.05)
        assert timed_out.done()
        assert timed_out_writer.closed.is_set()
        valid_writer = _TestWriter()
        await handler(_valid_reader(), cast(asyncio.StreamWriter, valid_writer))
        assert valid_writer.data

    asyncio.run(run())


@pytest.mark.parametrize("first_reader", [_valid_reader, _invalid_reader], ids=["success", "error"])
def test_success_and_error_release_capacity_for_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
    first_reader: Callable[[], asyncio.StreamReader],
) -> None:
    async def run() -> None:
        store = _Store()
        broker = _broker(store, {"kdive-worker-0": _pod()})
        assert await broker.pre_register_once() == 1
        handler = _session_handler(monkeypatch, broker)

        first_writer = _TestWriter()
        await handler(first_reader(), cast(asyncio.StreamWriter, first_writer))
        assert first_writer.closed.is_set()

        valid_writer = _TestWriter()
        await handler(_valid_reader(), cast(asyncio.StreamWriter, valid_writer))
        assert valid_writer.data

    asyncio.run(run())
