"""TLS-only credential delivery for pre-registered Kubernetes worker Pods."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from kdive.processes.kubernetes_termination_witness import FINALIZER

BROKER_AUDIENCE = "kdive-worker-credential-broker"
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 4 * 1024
MAX_PASS_COUNT = 1_000
_log = logging.getLogger(__name__)
_SERVICE_ACCOUNT_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_SERVICE_ACCOUNT_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

type ReadPod = Callable[[str, str], Mapping[str, Any] | None]
type Register = Callable[[PodIdentity, bytes, bytes], Awaitable[bool]]
type PendingEnvelope = Callable[[PodIdentity], Awaitable[bytes | None]]
type Acknowledge = Callable[[PodIdentity], Awaitable[bool]]
type TokenReview = Callable[[str, str], Awaitable[PodIdentity | None]]
type Encrypt = Callable[[str], bytes]
type Decrypt = Callable[[bytes], str]
type Credential = Callable[[], str]


class _FrameWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PodIdentity:
    """One exact Kubernetes Pod incarnation, with a live resource version."""

    namespace: str
    name: str
    uid: str
    resource_version: str

    def same_runtime(self, other: PodIdentity) -> bool:
        """Compare the immutable Pod identity while permitting a fresh resource version."""
        return (self.namespace, self.name, self.uid) == (other.namespace, other.name, other.uid)


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    """A bounded init-to-broker request authenticated by a projected Pod token."""

    operation: str
    token: str
    namespace: str
    name: str
    uid: str

    @property
    def identity(self) -> PodIdentity:
        """Return the request's immutable runtime identity before its live read."""
        return PodIdentity(self.namespace, self.name, self.uid, "")


@dataclass(frozen=True, slots=True)
class BrokerReply:
    """A response that contains a credential only for a pending delivery."""

    credential: str | None = None
    acknowledged: bool = False
    refused: bool = False


def _pod_identity(pod: Mapping[str, Any], namespace: str, expected_name: str) -> PodIdentity | None:
    metadata = pod.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    name = metadata.get("name")
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    if not all(isinstance(value, str) and value for value in (name, uid, resource_version)):
        return None
    if name != expected_name:
        return None
    return PodIdentity(namespace, name, uid, resource_version)


def _pending_claim(
    pod: Mapping[str, Any], namespace: str, expected_name: str
) -> PodIdentity | None:
    identity = _pod_identity(pod, namespace, expected_name)
    metadata = pod.get("metadata")
    status = pod.get("status")
    if (
        identity is None
        or not isinstance(metadata, Mapping)
        or not isinstance(status, Mapping)
        or status.get("phase") != "Pending"
        or metadata.get("deletionTimestamp") is not None
    ):
        return None
    finalizers = metadata.get("finalizers")
    if not isinstance(finalizers, list) or FINALIZER not in finalizers:
        return None
    return identity


@dataclass(frozen=True, slots=True)
class KubernetesCredentialBroker:
    """Pre-register and deliver one encrypted credential for each exact live Pod."""

    namespace: str
    worker_name: str
    ordinal_ceiling: int
    pass_limit: int
    read_pod: ReadPod
    register: Register
    pending_envelope: PendingEnvelope
    acknowledge: Acknowledge
    token_review: TokenReview
    encrypt: Encrypt
    decrypt: Decrypt
    credential: Credential = lambda: secrets.token_hex(32)

    async def pre_register_once(self) -> int:
        """Register finalized Pending Pods before their worker container can run."""
        completed = 0
        limit = min(self.ordinal_ceiling, self.pass_limit, MAX_PASS_COUNT)
        for ordinal in range(max(limit, 0)):
            name = f"{self.worker_name}-{ordinal}"
            pod = await asyncio.to_thread(self.read_pod, self.namespace, name)
            if pod is None or (identity := _pending_claim(pod, self.namespace, name)) is None:
                continue
            credential = self.credential()
            if len(credential) != 64:
                raise RuntimeError("worker incarnation credential must be a 256-bit hex value")
            envelope = self.encrypt(credential)
            if not envelope:
                raise RuntimeError("worker credential envelope must not be empty")
            if await self.register(identity, sha256(credential.encode()).digest(), envelope):
                completed += 1
        return completed

    async def handle(self, request: BrokerRequest) -> BrokerReply:
        """Authenticate a delivery or acknowledgment against the exact current Pod."""
        identity = await self._authenticate(request)
        if identity is None:
            return BrokerReply(refused=True)
        if request.operation == "deliver":
            envelope = await self.pending_envelope(identity)
            if envelope is None:
                return BrokerReply(refused=True)
            return BrokerReply(credential=self.decrypt(envelope))
        if request.operation == "ack":
            return BrokerReply(acknowledged=await self.acknowledge(identity))
        return BrokerReply(refused=True)

    async def _authenticate(self, request: BrokerRequest) -> PodIdentity | None:
        reviewed = await self.token_review(request.token, BROKER_AUDIENCE)
        if reviewed is None or (reviewed.name, reviewed.uid) != (request.name, request.uid):
            return None
        pod = await asyncio.to_thread(self.read_pod, request.namespace, request.name)
        if pod is None:
            return None
        live = _pod_identity(pod, request.namespace, request.name)
        if live is None or not live.same_runtime(request.identity):
            return None
        return live


async def read_frame(reader: asyncio.StreamReader, *, maximum: int, kind: str) -> bytes:
    """Read exactly one bounded length-prefixed JSON frame."""
    size = int.from_bytes(await reader.readexactly(4), "big")
    if size > maximum:
        raise ValueError(f"{kind} frame exceeds {maximum} bytes")
    return await reader.readexactly(size)


async def write_frame(writer: _FrameWriter, payload: bytes, *, maximum: int) -> None:
    """Write exactly one bounded length-prefixed JSON frame."""
    if len(payload) > maximum:
        raise ValueError(f"response frame exceeds {maximum} bytes")
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


def tls_server_context(*, certificate: str, private_key: str, ca: str) -> ssl.SSLContext:
    """Build the broker's private TLS listener context without logging secret paths or values."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_verify_locations(cafile=ca)
    context.load_cert_chain(certificate, private_key)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    return context


def envelope_codec(key_path: Path) -> tuple[Encrypt, Decrypt]:
    """Load the reconciler-only Fernet key for encrypted transient envelopes."""
    from cryptography.fernet import Fernet

    cipher = Fernet(key_path.read_bytes().strip())
    return (
        lambda credential: cipher.encrypt(credential.encode()),
        lambda envelope: cipher.decrypt(envelope).decode(),
    )


async def token_review(token: str, audience: str) -> PodIdentity | None:
    """Verify a projected Pod token using the fixed broker audience."""
    return await asyncio.to_thread(_token_review, token, audience)


def _token_review(token: str, audience: str) -> PodIdentity | None:
    request = urllib.request.Request(
        "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews",
        data=json.dumps(
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": token, "audiences": [audience]},
            },
            separators=(",", ":"),
        ).encode(),
        headers={
            "Authorization": f"Bearer {_SERVICE_ACCOUNT_TOKEN.read_text(encoding='utf-8').strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=3, context=ssl.create_default_context(cafile=str(_SERVICE_ACCOUNT_CA))
        ) as response:
            response_value = json.load(response)
    except urllib.error.HTTPError:
        return None
    return _token_review_identity(response_value, audience)


def _token_review_identity(value: object, expected_audience: str) -> PodIdentity | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if not isinstance(status, Mapping) or status.get("authenticated") is not True:
        return None
    audiences = status.get("audiences")
    if audiences != [expected_audience]:
        return None
    user = status.get("user")
    if not isinstance(user, Mapping):
        return None
    extra = user.get("extra")
    if not isinstance(extra, Mapping):
        return None
    name = _single_extra(extra, "authentication.kubernetes.io/pod-name")
    uid = _single_extra(extra, "authentication.kubernetes.io/pod-uid")
    if name is None or uid is None:
        return None
    return PodIdentity("", name, uid, "")


def _single_extra(extra: Mapping[Any, object], key: str) -> str | None:
    values = extra.get(key)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0] or None


def decode_request(payload: bytes) -> BrokerRequest:
    """Decode one bounded JSON request without accepting additional fields."""
    value = json.loads(payload)
    if not isinstance(value, dict) or set(value) != {
        "operation",
        "token",
        "namespace",
        "name",
        "uid",
    }:
        raise ValueError("credential broker request shape is invalid")
    if not all(isinstance(value[key], str) for key in value):
        raise ValueError("credential broker request values must be strings")
    return BrokerRequest(**value)


def encode_request(request: BrokerRequest) -> bytes:
    """Encode one strict bounded client request."""
    return json.dumps(
        {
            "operation": request.operation,
            "token": request.token,
            "namespace": request.namespace,
            "name": request.name,
            "uid": request.uid,
        },
        separators=(",", ":"),
    ).encode()


def encode_reply(reply: BrokerReply) -> bytes:
    """Encode the small response without recording its potential secret value."""
    return json.dumps(
        {
            "credential": reply.credential,
            "acknowledged": reply.acknowledged,
            "refused": reply.refused,
        },
        separators=(",", ":"),
    ).encode()


def decode_reply(payload: bytes) -> BrokerReply:
    """Decode a strict bounded broker reply."""
    value = json.loads(payload)
    if (
        not isinstance(value, dict)
        or set(value) != {"credential", "acknowledged", "refused"}
        or value["credential"] is not None
        and not isinstance(value["credential"], str)
        or not isinstance(value["acknowledged"], bool)
        or not isinstance(value["refused"], bool)
    ):
        raise ValueError("credential broker response shape is invalid")
    return BrokerReply(**value)


async def serve_broker(
    broker: KubernetesCredentialBroker,
    stop: asyncio.Event,
    *,
    host: str,
    port: int,
    ssl_context: ssl.SSLContext,
) -> None:
    """Serve only bounded internal TLS credential requests until reconciler shutdown."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = decode_request(
                await read_frame(reader, maximum=MAX_REQUEST_BYTES, kind="request")
            )
            reply = await broker.handle(request)
            await write_frame(writer, encode_reply(reply), maximum=MAX_RESPONSE_BYTES)
        except Exception:  # noqa: BLE001 -- malformed/internal peers receive no details
            _log.warning("worker credential broker rejected a request")
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port, ssl=ssl_context)
    try:
        await stop.wait()
    finally:
        server.close()
        await server.wait_closed()


async def run_pre_registration(
    broker: KubernetesCredentialBroker, stop: asyncio.Event, *, interval: float = 1
) -> None:
    """Keep retrying bounded Pending-Pod registration until the reconciler stops."""
    while not stop.is_set():
        try:
            await broker.pre_register_once()
        except Exception:  # noqa: BLE001 -- Pending Pod must remain gated until the next pass
            _log.warning("Kubernetes worker credential pre-registration failed", exc_info=True)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
