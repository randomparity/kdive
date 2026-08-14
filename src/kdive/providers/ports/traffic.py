"""Traffic-capture provider ports (ADR-0385/0432/0558).

``TrafficCapturer`` is the low-level, DB-free provider primitive for a running guest. Supervised
captures reconstruct a provider-specific ``TrafficCaptureExecutor`` after child release; that
synchronous executor owns the bounded poll, detach, fetch, private spool write, and reclaim. A
separate ``TrafficCaptureQuiescence`` opens a fresh connection after exact process absence and
returns evidence only after an ordered QOM absence query. Local and remote share these value and
protocol models, not transport or probe implementations.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_CONFIGURATION_BYTES = 16_384


def _canonical_bytes(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", exclude_none=True)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _configuration_object(data: bytes) -> dict[str, object]:
    if len(data) > _MAX_CONFIGURATION_BYTES:
        raise ValueError("capture provider configuration exceeds 16384 bytes")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("capture provider configuration is malformed") from error
    if not isinstance(value, dict):
        raise ValueError("capture provider configuration must be an object")
    return value


class LocalCaptureConfiguration(BaseModel):
    """Replayable local-libvirt child configuration, bound to one Resource."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_kind: Literal["local-libvirt"] = "local-libvirt"
    resource_id: UUID
    uri: Annotated[str, Field(min_length=1, max_length=255)]

    @field_validator("uri")
    @classmethod
    def _local_uri(cls, uri: str) -> str:
        if uri not in {"qemu:///system", "qemu:///session"}:
            raise ValueError("local libvirt URI must be qemu:///system or qemu:///session")
        return uri

    def to_canonical_json(self) -> bytes:
        """Serialize the bounded post-release configuration."""
        return _canonical_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse a strict local configuration object."""
        _configuration_object(data)
        return cls.model_validate_json(data)


class RemoteCaptureConfiguration(BaseModel):
    """Exact Resource-bound remote TLS references consumed only after child release."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_kind: Literal["remote-libvirt"] = "remote-libvirt"
    resource_id: UUID
    uri: Annotated[str, Field(min_length=1, max_length=2048)]
    client_cert_ref: Annotated[str, Field(min_length=1, max_length=1024)]
    client_key_ref: Annotated[str, Field(min_length=1, max_length=1024)]
    ca_cert_ref: Annotated[str, Field(min_length=1, max_length=1024)]
    secrets_root: Annotated[str, Field(min_length=1, max_length=1024)]
    storage_pool: Annotated[str, Field(min_length=1, max_length=255)]

    def to_canonical_json(self) -> bytes:
        """Serialize references and topology without resolving credential bytes."""
        return _canonical_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse a strict remote configuration object."""
        _configuration_object(data)
        return cls.model_validate_json(data)


class QuiescenceEvidence(BaseModel):
    """Bounded positive evidence from a provider-specific ordered absence probe."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider_kind: Literal["local-libvirt", "remote-libvirt"]
    resource_id: UUID
    domain_name: Annotated[str, Field(min_length=1, max_length=255)]
    qom_id: Annotated[str, Field(min_length=1, max_length=255)]
    result: Literal["absent"]
    ordering: Literal["fresh-qmp-connection"]

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-ready evidence persisted by the operation authority."""
        return self.model_dump(mode="json")

    def to_canonical_json(self) -> bytes:
        """Serialize evidence deterministically for its database size bound."""
        return _canonical_bytes(self)


@dataclass(frozen=True, slots=True)
class CaptureExecutionResult:
    """Metadata for the private ``capture.pcap`` written by a provider child executor."""

    size_bytes: int
    truncated: bool


class CaptureExecutionRequest(Protocol):
    """Request fields a synchronous provider executor consumes."""

    @property
    def provider_kind(self) -> Literal["local-libvirt", "remote-libvirt"]: ...

    @property
    def job_id(self) -> UUID: ...

    @property
    def resource_id(self) -> UUID: ...

    @property
    def system_id(self) -> UUID: ...

    @property
    def domain_name(self) -> str: ...

    @property
    def snaplen(self) -> int: ...

    @property
    def max_bytes(self) -> int: ...

    @property
    def max_polls(self) -> int: ...


class TrafficCaptureExecutor(Protocol):
    """Execute one bounded provider phase synchronously inside the gated child."""

    def execute(
        self, request: CaptureExecutionRequest, result_dir: Path
    ) -> CaptureExecutionResult: ...


class TrafficCaptureQuiescence(Protocol):
    """Prove an exact capture object absent after crossing a fresh transport barrier."""

    def prove_absent(
        self, resource_id: UUID, domain_name: str, qom_id: str
    ) -> QuiescenceEvidence: ...


@dataclass(frozen=True, slots=True)
class TrafficCaptureOperationPorts:
    """Provider-neutral factories used by worker supervision and startup recovery."""

    configuration: Callable[[UUID], bytes]
    quiescence: Callable[[bytes], TrafficCaptureQuiescence]


class TrafficCapturer(Protocol):
    """Attach/detach a host-side packet-capture sink and own the pcap file lifecycle.

    Which netdev is captured is a provider-internal detail (local-libvirt's SSH-forward netdev;
    remote-libvirt's runtime-discovered data-plane netdev), so it is not a port parameter. The
    ``dest_path`` returned by :meth:`prepare` is an opaque provider token — a worker path for
    local-libvirt, a remote storage-pool path for remote-libvirt — that the handler threads through
    :meth:`attach`/:meth:`detach`/:meth:`captured_size`/:meth:`fetch`/:meth:`reclaim` without
    interpreting it.
    """

    @property
    def write_remediation(self) -> str:
        """Operator guidance when a short/absent pcap means the hypervisor could not write it.

        The handler attaches this to the ``pcap_not_written`` configuration error; each provider
        returns the remedy for *its* write path (local: the qemu:///system pcap dir; remote: the
        storage pool).
        """
        ...

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        """Prepare the capture destination and return the opaque ``dest_path`` token.

        Local-libvirt prepares the per-System pcap directory (QEMU-writable, SELinux-labelled) and
        returns the worker path. Remote-libvirt pre-deletes this job's own stale pcap volume (so an
        at-least-once retry starts clean, without disturbing a concurrent capture on the same
        System) and returns the remote storage-pool path. Called once before :meth:`attach`.
        """
        ...

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        """Start capturing into libpcap file ``dest_path`` (``snaplen`` bytes/pkt).

        Idempotent: any pre-existing sink under ``qom_id`` is removed first, tolerating not-found
        (the first-ever capture has no stale sink). Raises ``CategorizedError`` with a
        ``CONTROL_FAILURE`` category on any monitor failure other than not-found.
        """
        ...

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        """Remove the capture sink ``qom_id`` (tolerating not-found)."""
        ...

    def captured_size(self, dest_path: str) -> int:
        """Current byte size of the growing pcap (0 if not yet written), for the poll loop."""
        ...

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        """Read the captured pcap back to worker memory; an absent capture is empty ``bytes``.

        Bounded by ``max_bytes`` (a mid-stream overrun on the remote download raises). Raises
        ``CategorizedError`` on a genuine read fault (e.g. the ADR-0223 root-readback wall).
        """
        ...

    def reclaim(self, dest_path: str) -> None:
        """Delete the host-side pcap; best-effort, never masks the handler's real result."""
        ...
