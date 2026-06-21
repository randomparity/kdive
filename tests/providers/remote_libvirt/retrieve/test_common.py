"""Remote-libvirt retrieve shared primitive tests."""

from __future__ import annotations

from uuid import UUID

import libvirt
import pytest

from kdive.artifacts.storage import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    HeadResult,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.retrieve import common
from kdive.security.secrets.secret_registry import SecretRegistry


class _LookupFails:
    def lookupByName(self, name: str) -> common.Domain:  # noqa: N802 - libvirt binding name
        raise libvirt.libvirtError(f"missing {name}")

    def close(self) -> None:
        return None


class _Store:
    def __init__(self) -> None:
        self.requests: list[ArtifactWriteRequest] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.requests.append(request)
        return StoredArtifact(request.key(), "etag", request.sensitivity, request.retention_class)

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        raise AssertionError("persist_redacted must not presign")

    def head(self, key: str) -> HeadResult | None:
        raise AssertionError("persist_redacted must not head")

    def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact:
        raise AssertionError("persist_redacted must not stream")


class _LookupSucceeds:
    def __init__(self) -> None:
        self.looked_up: list[str] = []

    def lookupByName(self, name: str) -> common.Domain:  # noqa: N802 - libvirt binding name
        self.looked_up.append(name)
        return _NamedDomain(name)

    def close(self) -> None:
        return None


class _NamedDomain:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


def test_lookup_returns_domain_for_requested_name() -> None:
    conn = _LookupSucceeds()

    domain = common.lookup(conn, "kdive-domain")

    assert domain.name() == "kdive-domain"
    assert conn.looked_up == ["kdive-domain"]


def test_lookup_maps_libvirt_error_to_infrastructure_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        common.lookup(_LookupFails(), "kdive-domain")

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details == {"domain": "kdive-domain"}
    assert str(exc.value) == "remote domain lookup failed for capture"


def test_readiness_failure_includes_system_context() -> None:
    system_id = UUID("00000000-0000-0000-0000-000000000123")

    err = common.readiness_failure(system_id, "agent did not expose vmcore")

    assert err.category is ErrorCategory.READINESS_FAILURE
    assert err.details == {"system_id": str(system_id)}
    assert str(err) == "agent did not expose vmcore"


def test_persist_redacted_masks_dmesg_before_storing() -> None:
    system_id = UUID("00000000-0000-0000-0000-000000000124")
    registry = SecretRegistry()
    registry.register("SECRET", scope=None)
    store = _Store()

    stored = common.persist_redacted(
        lambda: store,
        registry,
        system_id,
        CaptureMethod.KDUMP,
        b"panic SECRET\n",
    )

    assert stored.sensitivity is Sensitivity.REDACTED
    assert store.requests[0].owner_kind == common.OWNER_KIND
    assert store.requests[0].owner_id == str(system_id)
    assert store.requests[0].name == "vmcore-kdump-redacted"
    assert store.requests[0].retention_class == common.RETENTION
    assert store.requests[0].data == b"panic [REDACTED]\n"


def test_persist_redacted_replaces_invalid_utf8_bytes() -> None:
    system_id = UUID("00000000-0000-0000-0000-000000000125")
    store = _Store()

    common.persist_redacted(
        lambda: store,
        SecretRegistry(),
        system_id,
        CaptureMethod.KDUMP,
        b"panic \xff\xfe end\n",
    )

    assert store.requests[0].data == "panic �� end\n".encode()
