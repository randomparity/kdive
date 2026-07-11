"""Tests for the rotation-state sidecar (issue #892).

These tests exercise the object-store persistence seam: a real MinIO container
(testcontainers) is required because the sidecar's correctness depends on the
full put/get round-trip including metadata.  Gate on Docker exactly as the
store-layer tests do.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from kdive.artifacts.storage import ArtifactWriteRequest, FetchedArtifact, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.console_parts.rotation import RotationState
from kdive.providers.console_parts.sidecar import (
    ZERO,
    read_sidecar,
    sidecar_object_name,
    write_sidecar,
)
from kdive.store.objectstore import ObjectStore


class _ReadOnlyStore:
    def __init__(
        self,
        *,
        data: bytes | None = None,
        error: CategorizedError | None = None,
    ) -> None:
        self._data = data
        self._error = error

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        del key, etag
        if self._error is not None:
            raise self._error
        assert self._data is not None
        return FetchedArtifact(self._data, Sensitivity.REDACTED, "console")

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        del request
        raise AssertionError("read-only store cannot write")


def test_sidecar_object_name_is_stable() -> None:
    assert sidecar_object_name() == "console-rotation-state.json"


def test_zero_is_canonical_absent_state() -> None:
    assert (
        RotationState(plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None) == ZERO
    )


def test_round_trip_non_utf8_carry(minio_store: ObjectStore, key_ns: str) -> None:
    """Write then read back a state whose carry contains non-UTF-8 bytes."""
    state = RotationState(
        plaintext_offset=1024,
        carry=b"\xff\x00partial",
        next_index=3,
        boot_gen=1,
        boot_id="boot-abc",
    )
    system_id = uuid4()
    write_sidecar(minio_store, key_ns, system_id, state)
    recovered = read_sidecar(minio_store, key_ns, system_id)
    assert recovered == state


def test_absent_sidecar_returns_zero(minio_store: ObjectStore, key_ns: str) -> None:
    """A missing sidecar returns ZERO without raising."""
    system_id = uuid4()
    result = read_sidecar(minio_store, key_ns, system_id)
    assert result == ZERO


def test_stale_sidecar_handle_returns_zero() -> None:
    system_id = uuid4()
    store = _ReadOnlyStore(
        error=CategorizedError(
            "missing rotation sidecar",
            category=ErrorCategory.STALE_HANDLE,
        )
    )

    assert read_sidecar(store, "tenant", system_id) == ZERO


def test_store_failure_propagates_typed_error() -> None:
    system_id = uuid4()
    store = _ReadOnlyStore(
        error=CategorizedError(
            "object store unavailable",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    )

    with pytest.raises(CategorizedError) as excinfo:
        read_sidecar(store, "tenant", system_id)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_corrupt_body_logs_context_and_returns_zero(caplog: pytest.LogCaptureFixture) -> None:
    system_id = uuid4()
    store = _ReadOnlyStore(data=b"not-valid-json-{{{{")

    with caplog.at_level(logging.WARNING):
        result = read_sidecar(store, "tenant", system_id)

    assert result == ZERO
    assert str(system_id) in caplog.text
    assert "tenant/systems" in caplog.text


def test_corrupt_body_returns_zero(minio_store: ObjectStore, key_ns: str) -> None:
    """A sidecar with a garbage JSON body returns ZERO without raising."""
    system_id = uuid4()
    minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="systems",
            owner_id=str(system_id),
            name=sidecar_object_name(),
            data=b"not-valid-json-{{{{",
            sensitivity=Sensitivity.REDACTED,
            retention_class="console",
        )
    )
    result = read_sidecar(minio_store, key_ns, system_id)
    assert result == ZERO


def test_invalid_base64_carry_returns_zero(minio_store: ObjectStore, key_ns: str) -> None:
    """A sidecar with invalid base64 in the carry field returns ZERO without raising."""
    import json

    system_id = uuid4()
    body = json.dumps(
        {
            "plaintext_offset": 0,
            "carry": "!!!not-base64!!!",
            "next_index": 0,
            "boot_gen": 0,
            "boot_id": None,
        }
    ).encode("utf-8")
    minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="systems",
            owner_id=str(system_id),
            name=sidecar_object_name(),
            data=body,
            sensitivity=Sensitivity.REDACTED,
            retention_class="console",
        )
    )
    result = read_sidecar(minio_store, key_ns, system_id)
    assert result == ZERO


def test_round_trip_preserves_none_boot_id(minio_store: ObjectStore, key_ns: str) -> None:
    """A state with boot_id=None survives the JSON round-trip."""
    state = RotationState(plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None)
    system_id = uuid4()
    write_sidecar(minio_store, key_ns, system_id, state)
    recovered = read_sidecar(minio_store, key_ns, system_id)
    assert recovered == state


def test_write_overwrites_prior_state(minio_store: ObjectStore, key_ns: str) -> None:
    """Writing a new state overwrites the previous sidecar for the same system."""
    system_id = uuid4()
    state_a = RotationState(
        plaintext_offset=100, carry=b"\xab", next_index=1, boot_gen=0, boot_id="b1"
    )
    state_b = RotationState(
        plaintext_offset=200, carry=b"\xcd\xef", next_index=2, boot_gen=0, boot_id="b1"
    )
    write_sidecar(minio_store, key_ns, system_id, state_a)
    write_sidecar(minio_store, key_ns, system_id, state_b)
    recovered = read_sidecar(minio_store, key_ns, system_id)
    assert recovered == state_b


@pytest.mark.parametrize("bad_offset", [None, "abc"])
def test_wrong_typed_field_returns_zero(
    minio_store: ObjectStore, key_ns: str, bad_offset: object
) -> None:
    """Structurally-valid JSON with a wrong-typed field returns ZERO without raising.

    A truncated or partial write can produce well-formed JSON whose ``plaintext_offset`` is
    ``null`` (``int(None)`` raises ``TypeError``) or a non-numeric string (``int("abc")`` raises
    ``ValueError``); neither must escape ``read_sidecar``.
    """
    import json

    system_id = uuid4()
    body = json.dumps(
        {
            "plaintext_offset": bad_offset,
            "carry": "",
            "next_index": 0,
            "boot_gen": 0,
            "boot_id": None,
        }
    ).encode("utf-8")
    minio_store.put_artifact(
        ArtifactWriteRequest(
            tenant=key_ns,
            owner_kind="systems",
            owner_id=str(system_id),
            name=sidecar_object_name(),
            data=body,
            sensitivity=Sensitivity.REDACTED,
            retention_class="console",
        )
    )
    result = read_sidecar(minio_store, key_ns, system_id)
    assert result == ZERO
