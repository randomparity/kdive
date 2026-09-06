"""Typed database-repository consumption of authority-owned teardown snapshots."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from kdive.domain.external_boot_activation import ExternalBootReleaseEvidenceV1
from kdive.providers.external_boot_authority.protocol import AuthorityTeardownMutationRequestV1
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.providers.ports.external_boot import OpaqueProviderRef

pytestmark = pytest.mark.anyio

_AUTHORITY_ID = UUID("00000000-0000-0000-0000-000000000001")
_SYSTEM_ID = UUID("00000000-0000-0000-0000-000000000002")
_ACTIVATION_ID = UUID("00000000-0000-0000-0000-000000000003")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000004")
_ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000005")
_PEER = AuthenticatedPeer("worker-current")
_ACK_DIGEST = "sha256:" + "a" * 64


def _request() -> AuthorityTeardownMutationRequestV1:
    return AuthorityTeardownMutationRequestV1(
        authority_id=_AUTHORITY_ID,
        generation=7,
        system_id=_SYSTEM_ID,
        activation_id=_ACTIVATION_ID,
        run_id=_RUN_ID,
        plan_identity="sha256:" + "b" * 64,
        purpose="teardown",
        operation="teardown",
        provider_kind="local-libvirt",
        authority_instance="authority-a",
        operation_identity="teardown-a",
        operation_digest="sha256:" + "c" * 64,
        attempt_id=_ATTEMPT_ID,
    )


def _release_evidence() -> ExternalBootReleaseEvidenceV1:
    return ExternalBootReleaseEvidenceV1(
        activation_id=_ACTIVATION_ID,
        system_id=_SYSTEM_ID,
        store_identity=OpaqueProviderRef(ref="stores/private"),
        owner_key=OpaqueProviderRef(ref="owners/private"),
        reserved_bytes=4096,
        objects=(),
        verified_at=datetime(2026, 9, 6, tzinfo=UTC),
    )


def _row(disposition: str) -> dict[str, object]:
    request = _request()
    release = _release_evidence() if disposition == "released" else None
    return {
        "peer_incarnation_id": str(_PEER.incarnation_id),
        "authority_id": request.authority_id,
        "generation": request.generation,
        "system_id": request.system_id,
        "activation_id": request.activation_id,
        "run_id": request.run_id,
        "plan_identity": request.plan_identity,
        "purpose": request.purpose,
        "operation": request.operation.value,
        "provider_kind": request.provider_kind,
        "authority_instance": request.authority_instance,
        "operation_identity": request.operation_identity,
        "operation_digest": request.operation_digest,
        "state": "current",
        "reservation_disposition": disposition,
        "store_identity": "stores/private",
        "owner_key": "owners/private",
        "reserved_bytes": 4096,
        "release_identity": release.identity if release is not None else None,
        "release_evidence": (
            release.model_dump(mode="json", by_alias=True) if release is not None else None
        ),
    }


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self._connection.call = (query, parameters)

    async def fetchone(self) -> dict[str, object] | None:
        return self._connection.row


class _Connection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.call: tuple[str, tuple[object, ...]] | None = None

    def cursor(self, **_kwargs: object) -> _Cursor:
        return _Cursor(self)

    @asynccontextmanager
    async def transaction(self):
        yield


def _repository(row: dict[str, object] | None) -> tuple[DatabaseAuthorityRepository, _Connection]:
    connection = _Connection(row)

    @asynccontextmanager
    async def connections():
        yield cast(AsyncConnection, connection)

    return DatabaseAuthorityRepository(connections), connection


@pytest.mark.parametrize("disposition", ["pending", "ready", "released"])
async def test_resolve_current_teardown_returns_exact_typed_snapshot(disposition: str) -> None:
    repository, connection = _repository(_row(disposition))
    request = _request()

    snapshot = await repository.resolve_current_teardown(
        _PEER, request, acknowledgement_sequence=11, acknowledgement_digest=_ACK_DIGEST
    )

    assert snapshot is not None
    assert snapshot.binding.authority_id == request.authority_id
    assert snapshot.reservation_disposition == disposition
    assert snapshot.store_identity == OpaqueProviderRef(ref="stores/private")
    assert snapshot.owner_key == OpaqueProviderRef(ref="owners/private")
    assert snapshot.reserved_bytes == 4096
    assert (snapshot.release_evidence is not None) == (disposition == "released")
    assert connection.call is not None
    assert "resolve_current_external_boot_teardown_authority" in connection.call[0]
    assert connection.call[1] == (
        str(_PEER.incarnation_id),
        request.authority_id,
        request.generation,
        11,
        _ACK_DIGEST,
    )


async def test_resolve_current_teardown_preserves_sql_fence_denial() -> None:
    repository, _connection = _repository(None)

    assert (
        await repository.resolve_current_teardown(
            _PEER, _request(), acknowledgement_sequence=12, acknowledgement_digest=_ACK_DIGEST
        )
        is None
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"purpose": "release"}, "binding"),
        ({"reservation_disposition": "unknown"}, "disposition"),
        ({"reserved_bytes": 0}, "positive"),
        ({"release_identity": "sha256:" + "d" * 64}, "release fields"),
    ],
)
async def test_resolve_current_teardown_rejects_malformed_snapshot(
    change: dict[str, object], message: str
) -> None:
    repository, _connection = _repository(_row("ready") | change)

    with pytest.raises(ValueError, match=message):
        await repository.resolve_current_teardown(
            _PEER, _request(), acknowledgement_sequence=11, acknowledgement_digest=_ACK_DIGEST
        )


async def test_resolve_current_teardown_rejects_release_evidence_ownership_mismatch() -> None:
    row = _row("released")
    release = cast(dict[str, Any], row["release_evidence"])
    release["owner_key"] = {"ref": "owners/other"}
    row["release_identity"] = ExternalBootReleaseEvidenceV1.model_validate(release).identity
    repository, _connection = _repository(row)

    with pytest.raises(ValueError, match="ownership"):
        await repository.resolve_current_teardown(
            _PEER, _request(), acknowledgement_sequence=11, acknowledgement_digest=_ACK_DIGEST
        )
