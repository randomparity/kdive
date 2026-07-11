"""Rotation-state sidecar: object-store persistence for one System's RotationState.

The ``console_rotate`` worker job is stateless across invocations; this module
persists a System's :class:`~kdive.providers.console_parts.rotation.RotationState`
as a small JSON object in the object store so each job invocation can resume where
the prior one left off.

The sidecar lives at the System's owner-prefixed key
``{tenant}/systems/{system_id}/console-rotation-state.json`` and carries
``Sensitivity.REDACTED`` metadata so callers cannot mistake it for a public object.
It is NOT registered as an ``artifacts`` row and is therefore never exposed by
``artifacts.get``/``artifacts.list`` (those tools only serve rows, not raw objects).

The ``carry`` field is base64-encoded in JSON because it is raw bytes that may
contain a partial unredacted secret (the held-back overlap awaiting the next redaction
pass).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Protocol
from uuid import UUID

from kdive.artifacts.storage import (
    ArtifactWriteRequest,
    FetchedArtifact,
    StoredArtifact,
    artifact_key,
)
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.console_parts.rotation import RotationState

_log = logging.getLogger(__name__)

_OWNER_KIND = "systems"
_RETENTION_CLASS = "console"

#: The absent/zero state returned when no sidecar has been written yet.
ZERO: RotationState = RotationState(
    plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None
)


def sidecar_object_name() -> str:
    """Return the stable object name component for the rotation-state sidecar.

    Returns:
        ``"console-rotation-state.json"`` — a single path component (no ``/``),
        safe to pass as the ``name`` argument to :func:`~kdive.artifacts.storage.artifact_key`.
    """
    return "console-rotation-state.json"


class _StorePort(Protocol):
    """Minimal object-store port required by the sidecar read/write seam."""

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


def _sidecar_key(tenant: str, system_id: UUID) -> str:
    return artifact_key(tenant, _OWNER_KIND, str(system_id), sidecar_object_name())


def _serialize(state: RotationState) -> bytes:
    doc = {
        "plaintext_offset": state.plaintext_offset,
        "carry": base64.b64encode(state.carry).decode("ascii"),
        "next_index": state.next_index,
        "boot_gen": state.boot_gen,
        "boot_id": state.boot_id,
    }
    return json.dumps(doc).encode("utf-8")


def _deserialize(data: bytes) -> RotationState:
    doc = json.loads(data)
    carry = base64.b64decode(doc["carry"], validate=True)
    return RotationState(
        plaintext_offset=int(doc["plaintext_offset"]),
        carry=carry,
        next_index=int(doc["next_index"]),
        boot_gen=int(doc["boot_gen"]),
        boot_id=doc["boot_id"],
    )


def read_sidecar(store: _StorePort, tenant: str, system_id: UUID) -> RotationState:
    """Read the System's rotation-state sidecar.

    A missing/stale sidecar returns :data:`ZERO` so the first rotation job starts fresh.
    A corrupt sidecar logs system/key context and also returns :data:`ZERO`; object-store
    infrastructure failures propagate as typed :class:`CategorizedError` so the worker records
    the store failure instead of silently rewinding rotation state.

    Args:
        store: Object store port exposing ``put_artifact`` and ``get_artifact``.
        tenant: The tenant key component (e.g. ``"remote-libvirt"``).
        system_id: The System whose sidecar to read.

    Returns:
        The persisted :class:`~kdive.providers.console_parts.rotation.RotationState`,
        or :data:`ZERO` if the sidecar is absent or corrupt.

    Raises:
        CategorizedError: The object-store read fails for any reason other than an absent
            or stale sidecar handle.
    """
    key = _sidecar_key(tenant, system_id)
    try:
        fetched = store.get_artifact(key, None)
        return _deserialize(fetched.data)
    except CategorizedError as exc:
        if exc.category is ErrorCategory.STALE_HANDLE:
            return ZERO
        raise
    except binascii.Error, KeyError, ValueError, TypeError:
        # ValueError covers json.JSONDecodeError; TypeError covers a wrong-typed field
        # (e.g. a null where an int is expected) from a truncated write.
        _log.warning(
            "console rotation sidecar %s for system %s is corrupt; starting from zero",
            key,
            system_id,
            exc_info=True,
        )
        return ZERO


def write_sidecar(store: _StorePort, tenant: str, system_id: UUID, state: RotationState) -> None:
    """Persist the rotation state for ``system_id``.

    Stores the sidecar at the System's owner-prefixed key with ``Sensitivity.REDACTED``
    metadata.  Overwrites any prior sidecar for the same System.

    Args:
        store: Object store port exposing ``put_artifact`` and ``get_artifact``.
        tenant: The tenant key component (e.g. ``"remote-libvirt"``).
        system_id: The System whose sidecar to write.
        state: The :class:`~kdive.providers.console_parts.rotation.RotationState` to persist.

    Raises:
        CategorizedError: The object-store write fails
            (:attr:`~kdive.domain.errors.ErrorCategory.INFRASTRUCTURE_FAILURE`).
    """
    store.put_artifact(
        ArtifactWriteRequest(
            tenant=tenant,
            owner_kind=_OWNER_KIND,
            owner_id=str(system_id),
            name=sidecar_object_name(),
            data=_serialize(state),
            sensitivity=Sensitivity.REDACTED,
            retention_class=_RETENTION_CLASS,
        )
    )
