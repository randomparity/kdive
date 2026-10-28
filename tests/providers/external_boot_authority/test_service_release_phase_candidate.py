"""Derived release phases are admitted against the root release candidate (#3390)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityOperation,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
)
from tests.providers.external_boot_authority.service_support import (
    _DIGEST_A,
    _Adapter,
    _binding,
    _mutation,
    _Repository,
    _takeover,
)

_DERIVED = [
    (AuthorityOperation.RECOVER, "restore"),
    (AuthorityOperation.CLEANUP, "reap"),
]


def _derived(
    root: Any, operation: AuthorityOperation, **update: object
) -> AuthorityMutationRequestV1:
    return _mutation(root).model_copy(
        update={
            "operation": operation,
            "operation_identity": f"release-phase-{operation.value}",
            "operation_digest": _DIGEST_A,
            **update,
        }
    )


class _Host:
    def __init__(self) -> None:
        self.executed: list[RemoteModuleLifecycleRequestV1] = []
        self.response = cast(RemoteModuleLifecycleResponseV1, object())

    async def observe_lifecycle(self, _request: object) -> None:
        return None

    async def execute_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> RemoteModuleLifecycleResponseV1:
        self.executed.append(request)
        return self.response


async def _acknowledged_release(
    tmp_path: Path, host: _Host | None = None
) -> tuple[ExternalBootAuthorityService, AuthenticatedPeer, Any, _Adapter]:
    peer = AuthenticatedPeer(uuid4())
    root = _takeover().model_copy(
        update={
            "provider_kind": "remote-libvirt",
            "purpose": "release",
            "operation": AuthorityOperation.RELEASE,
        }
    )
    repository = _Repository(peer, root)
    adapter = _Adapter()
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
        remote_module_host=cast(Any, host),
    )
    await service.acknowledge_takeover(peer, root)
    repository.current = True
    return service, peer, root, adapter


@pytest.mark.anyio
@pytest.mark.parametrize(("operation", "action"), _DERIVED)
async def test_remote_lifecycle_admits_a_derived_release_phase(
    tmp_path: Path, operation: AuthorityOperation, action: Literal["restore", "reap"]
) -> None:
    host = _Host()
    service, peer, root, _adapter = await _acknowledged_release(tmp_path, host)
    remote = RemoteModuleLifecycleRequestV1(
        authority=_derived(root, operation), action=action, budget_seconds=30
    )
    assert await service.execute_remote_module_lifecycle(peer, remote) is host.response
    assert host.executed == [remote]


@pytest.mark.anyio
@pytest.mark.parametrize(("operation", "_action"), _DERIVED)
async def test_authenticated_read_admits_a_derived_release_phase(
    tmp_path: Path, operation: AuthorityOperation, _action: str
) -> None:
    service, peer, root, adapter = await _acknowledged_release(tmp_path)
    adapter.observe_category = "source"
    observation = await service.observe_authority(peer, _derived(root, operation))
    assert observation.category == "source"
    assert "observe" in adapter.calls


@pytest.mark.anyio
async def test_remote_lifecycle_rejects_a_derived_phase_of_another_system(
    tmp_path: Path,
) -> None:
    host = _Host()
    service, peer, root, _adapter = await _acknowledged_release(tmp_path, host)
    foreign = _derived(root, AuthorityOperation.RECOVER, system_id=uuid4())
    remote = RemoteModuleLifecycleRequestV1(authority=foreign, action="restore", budget_seconds=30)
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.execute_remote_module_lifecycle(peer, remote)
    assert host.executed == []


@pytest.mark.parametrize("field", ["authority_id", "generation", "system_id", "run_id"])
def test_candidate_predicate_refuses_a_derived_phase_of_another_root(field: str) -> None:
    peer = AuthenticatedPeer(uuid4())
    root = _takeover().model_copy(
        update={"purpose": "release", "operation": AuthorityOperation.RELEASE}
    )
    candidate = _binding(peer, root, "current")
    other: dict[str, object] = {
        "authority_id": uuid4(),
        "generation": root.generation + 1,
        "system_id": uuid4(),
        "run_id": uuid4(),
    }
    derived = _derived(root, AuthorityOperation.RECOVER, **{field: other[field]})
    assert not ExternalBootAuthorityService._candidate_matches(candidate, derived)


def test_candidate_predicate_admits_a_derived_phase_only_under_a_release_root() -> None:
    peer = AuthenticatedPeer(uuid4())
    root = _takeover().model_copy(
        update={"purpose": "release", "operation": AuthorityOperation.RELEASE}
    )
    derived = _derived(root, AuthorityOperation.RECOVER)
    candidate = _binding(peer, root, "current")
    assert ExternalBootAuthorityService._candidate_matches(candidate, derived)
    not_a_root = replace(candidate, operation=AuthorityOperation.RECOVER)
    assert not ExternalBootAuthorityService._candidate_matches(not_a_root, derived)


def test_candidate_predicate_keeps_exact_match_for_non_release_operations() -> None:
    peer = AuthenticatedPeer(uuid4())
    root = _takeover()
    request = _mutation(root)
    assert ExternalBootAuthorityService._candidate_matches(_binding(peer, root, "current"), request)
    shifted = request.model_copy(update={"operation_identity": "other"})
    assert not ExternalBootAuthorityService._candidate_matches(
        _binding(peer, root, "current"), shifted
    )
