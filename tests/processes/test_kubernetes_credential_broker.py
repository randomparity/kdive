"""Pre-start Kubernetes worker credential broker behavior."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kdive.processes.kubernetes_credential_broker import (
    BROKER_AUDIENCE,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    BrokerRequest,
    KubernetesCredentialBroker,
    PodIdentity,
    read_frame,
    tls_server_context,
    write_frame,
)
from kdive.processes.kubernetes_termination_witness import FINALIZER


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
    envelopes: dict[PodIdentity, bytes] = field(default_factory=dict)
    acknowledged: set[PodIdentity] = field(default_factory=set)
    registrations: list[tuple[PodIdentity, bytes, bytes]] = field(default_factory=list)

    async def register(
        self, identity: PodIdentity, credential_hash: bytes, envelope: bytes
    ) -> bool:
        self.registrations.append((identity, credential_hash, envelope))
        self.envelopes.setdefault(identity, envelope)
        return True

    async def pending_envelope(self, identity: PodIdentity) -> bytes | None:
        return self.envelopes.get(identity)

    async def acknowledge(self, identity: PodIdentity) -> bool:
        if identity in self.acknowledged:
            return True
        if self.envelopes.pop(identity, None) is None:
            return False
        self.acknowledged.add(identity)
        return True


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
