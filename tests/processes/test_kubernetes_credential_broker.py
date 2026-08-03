"""Pre-start Kubernetes worker credential broker behavior."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

import kdive.processes.kubernetes_credential_broker as credential_broker
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
