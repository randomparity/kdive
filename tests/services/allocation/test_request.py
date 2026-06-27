"""Allocation request service facade tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from kdive.domain.capacity.state import AllocationState, ResourceStatus
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation
from kdive.domain.lifecycle.shapes import ResolvedSizing
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.allocation.admission import request as request_service
from kdive.services.allocation.admission.core import AdmissionOutcome, AllocationRequest
from kdive.services.allocation.admission.placement import PlacementCandidates, PlacementRequest
from kdive.services.allocation.admission.request import (
    AdmissionRequestSpec,
    denial_details,
    request_admission,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_RESOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")
_ALLOCATION_ID = UUID("22222222-2222-2222-2222-222222222222")
_CONN = cast(AsyncConnection, object())


def _ctx() -> RequestContext:
    return RequestContext(
        principal="user-1",
        agent_session="sess-1",
        projects=("proj",),
        roles={"proj": Role.OPERATOR},
    )


def _spec(**overrides: Any) -> AdmissionRequestSpec:
    fields: dict[str, Any] = {
        "resource_id": None,
        "kind": ResourceKind.LOCAL_LIBVIRT,
        "pool": None,
        "shape": None,
        "vcpus": 2,
        "memory_gb": 4,
        "disk_gb": 20,
        "window": 3,
        "pcie_devices": (),
        "on_capacity": "deny",
    }
    fields.update(overrides)
    return AdmissionRequestSpec(**fields)


def _sizing(**overrides: Any) -> ResolvedSizing:
    fields = {"vcpus": 2, "memory_gb": 4, "disk_gb": 20, "pcie_match": None, "shape": None}
    fields.update(overrides)
    return ResolvedSizing(**fields)


def _resource(resource_id: UUID = _RESOURCE_ID) -> Resource:
    return Resource(
        id=resource_id,
        created_at=_NOW,
        updated_at=_NOW,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities={"concurrent_allocation_cap": 2},
        pool="local-libvirt",
        cost_class="local",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )


def _allocation(state: AllocationState = AllocationState.GRANTED) -> Allocation:
    return Allocation(
        id=_ALLOCATION_ID,
        created_at=_NOW,
        updated_at=_NOW,
        principal="user-1",
        agent_session="sess-1",
        project="proj",
        resource_id=_RESOURCE_ID,
        state=state,
    )


def test_request_admission_returns_sizing_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    error = CategorizedError("bad size", category=ErrorCategory.CONFIGURATION_ERROR)

    async def sizing_error(*_: object, **__: object) -> ResolvedSizing:
        raise error

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing_error)

    async def _run() -> None:
        # by-id so the selector ("id") differs from the result default ("kind"): the error
        # result must thread the real selector, not fall back to the default.
        result = await request_admission(
            _CONN, _ctx(), project="proj", spec=_spec(kind=None, resource_id=_RESOURCE_ID)
        )
        assert result.error is error
        assert result.object_id == str(_RESOURCE_ID)
        assert result.project == "proj"
        assert result.selector == "id"

    asyncio.run(_run())


def test_request_admission_rejects_malformed_pcie_before_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        raise AssertionError("malformed PCIe must reject before placement")

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)

    async def _run() -> None:
        result = await request_admission(
            _CONN,
            _ctx(),
            project="proj",
            spec=_spec(pcie_devices=("not-a-spec",)),
        )
        assert result.error is not None
        assert result.error.category is ErrorCategory.CONFIGURATION_ERROR
        assert result.error.details == {"spec": "not-a-spec"}

    asyncio.run(_run())


def test_request_admission_returns_configuration_error_when_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placement_conns: list[object] = []
    kinds_conns: list[object] = []

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(_conn: object, _request: object) -> PlacementCandidates:
        placement_conns.append(_conn)
        return PlacementCandidates(resources=[])

    async def registered_kinds(_conn: object) -> tuple[str, ...]:
        kinds_conns.append(_conn)
        return ("remote-libvirt",)

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "_registered_kinds", registered_kinds)

    async def _run() -> None:
        # A by-kind no-target denial enumerates the available kinds for the transport (#471).
        by_kind = await request_admission(_CONN, _ctx(), project="proj", spec=_spec())
        assert by_kind.resource is None
        assert by_kind.category is ErrorCategory.CONFIGURATION_ERROR
        assert by_kind.available_kinds == ("remote-libvirt",)
        assert by_kind.object_id == ResourceKind.LOCAL_LIBVIRT.value
        assert by_kind.project == "proj"
        assert by_kind.selector == "kind"
        # conn is threaded through placement and the kind enumeration, not dropped.
        assert placement_conns == [_CONN]
        assert kinds_conns == [_CONN]

    asyncio.run(_run())


def test_request_admission_by_pool_no_target_does_not_enumerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A by-pool no-target denial leaves available_kinds None and must NOT enumerate pools
    # (operator labels on affinity-scoped resources — a fleet list leaks across tenants, ADR-0186).
    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        return PlacementCandidates(resources=[])

    async def registered_kinds(*_: object, **__: object) -> tuple[str, ...]:
        raise AssertionError("a by-pool denial must not enumerate kinds or pools")

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "_registered_kinds", registered_kinds)

    async def _run() -> None:
        result = await request_admission(
            _CONN, _ctx(), project="proj", spec=_spec(kind=None, pool="big-remote")
        )
        assert result.resource is None
        assert result.category is ErrorCategory.CONFIGURATION_ERROR
        assert result.available_kinds is None
        assert result.selector == "pool"
        assert result.object_id == "big-remote"

    asyncio.run(_run())


def test_request_admission_by_pool_grant_threads_requested_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _resource()
    captured: list[AllocationRequest] = []

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        return PlacementCandidates(resources=[resource])

    async def admit(_conn: object, request: AllocationRequest) -> AdmissionOutcome:
        captured.append(request)
        return AdmissionOutcome(granted=True, allocation=_allocation())

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        await request_admission(
            _CONN, _ctx(), project="proj", spec=_spec(kind=None, pool="big-remote")
        )
        assert captured[0].requested_pool == "big-remote"
        assert captured[0].requested_kind is None
        assert captured[0].requested_resource_id is None

    asyncio.run(_run())


def test_request_admission_by_id_no_target_omits_available_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A by-id no-target denial leaves available_kinds None (the caller named a host); the kind
    # enumeration must not even be queried (#471, ADR-0132).
    placement_requests: list[PlacementRequest] = []

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(_conn: object, placement_request: object) -> PlacementCandidates:
        placement_requests.append(cast(PlacementRequest, placement_request))
        return PlacementCandidates(resources=[])

    async def registered_kinds(*_: object, **__: object) -> tuple[str, ...]:
        raise AssertionError("a by-id denial must not enumerate kinds")

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "_registered_kinds", registered_kinds)

    async def _run() -> None:
        result = await request_admission(
            _CONN, _ctx(), project="proj", spec=_spec(kind=None, resource_id=_RESOURCE_ID)
        )
        assert result.resource is None
        assert result.category is ErrorCategory.CONFIGURATION_ERROR
        assert result.available_kinds is None
        assert result.selector == "id"
        assert result.object_id == str(_RESOURCE_ID)
        assert placement_requests[0].project == "proj"
        assert placement_requests[0].resource_id == _RESOURCE_ID

    asyncio.run(_run())


def test_request_admission_uses_capacity_candidate_for_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _resource()
    placement_requests: list[PlacementRequest] = []
    admission_requests: list[AllocationRequest] = []
    denial = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason="at_capacity",
        cap=2,
        in_use=2,
        queueable=True,
    )

    sizing_conns: list[object] = []
    admit_conns: list[object] = []

    async def sizing(_conn: object, **__: object) -> ResolvedSizing:
        sizing_conns.append(_conn)
        return _sizing(pcie_match="8086:1572", shape="gpu-small")

    async def placement(_conn: AsyncConnection, placement_request: object) -> PlacementCandidates:
        placement_requests.append(cast(PlacementRequest, placement_request))
        return PlacementCandidates(resources=[], capacity_candidate=resource)

    async def admit(_conn: AsyncConnection, admission_request: object) -> AdmissionOutcome:
        admit_conns.append(_conn)
        admission_requests.append(cast(AllocationRequest, admission_request))
        return denial

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        # by-pool denial: selector threads as "pool" into the denial result, not the default.
        result = await request_admission(
            _CONN,
            _ctx(),
            project="proj",
            spec=_spec(
                kind=None,
                pool="big-remote",
                pcie_devices=("class=02",),
                on_capacity="queue",
            ),
            idempotency_key="idem-1",
        )
        assert result.resource is resource
        assert result.denial is denial
        assert result.object_id == "big-remote"
        assert result.project == "proj"
        assert result.selector == "pool"
        # conn is threaded into sizing and admit, never replaced.
        assert sizing_conns == [_CONN]
        assert admit_conns == [_CONN]
        placement_request = placement_requests[0]
        admission_request = admission_requests[0]
        assert placement_request.pcie_specs == ("class=02", "8086:1572")
        assert placement_request.kind is None
        assert placement_request.pool == "big-remote"
        assert placement_request.resource_id is None
        assert placement_request.project == "proj"
        assert admission_request.resource is resource
        assert admission_request.ctx is not None
        assert admission_request.project == "proj"
        assert admission_request.pcie_specs == ("class=02", "8086:1572")
        assert admission_request.shape == "gpu-small"
        assert admission_request.on_capacity == "queue"
        assert admission_request.idempotency_key == "idem-1"
        assert admission_request.window == 3
        assert admission_request.disk_gb == 20
        assert admission_request.selector.vcpus == 2
        assert admission_request.selector.memory_gb == 4
        assert admission_request.requested_kind is None
        assert admission_request.requested_pool == "big-remote"
        assert admission_request.requested_resource_id is None

    asyncio.run(_run())


def test_request_admission_returns_granted_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = _resource()
    allocation = _allocation()
    placement_requests: list[PlacementRequest] = []
    admission_requests: list[AllocationRequest] = []
    sizing_calls: list[dict[str, object]] = []

    async def sizing(_conn: object, **kwargs: object) -> ResolvedSizing:
        sizing_calls.append(kwargs)
        return _sizing(vcpus=6, memory_gb=12, disk_gb=80)

    async def placement(_conn: object, placement_request: object) -> PlacementCandidates:
        placement_requests.append(cast(PlacementRequest, placement_request))
        return PlacementCandidates(resources=[resource])

    async def admit(_conn: object, admission_request: object) -> AdmissionOutcome:
        admission_requests.append(cast(AllocationRequest, admission_request))
        return AdmissionOutcome(granted=True, allocation=allocation)

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        result = await request_admission(
            _CONN,
            _ctx(),
            project="proj",
            spec=_spec(resource_id=resource.id, kind=None, shape="big", disk_gb=80),
        )
        assert result.object_id == str(resource.id)
        assert result.project == "proj"
        assert result.selector == "id"
        assert result.resource is resource
        assert result.allocation is allocation

        assert sizing_calls[0] == {
            "shape": "big",
            "vcpus": 2,
            "memory_gb": 4,
            "disk_gb": 80,
        }

        placement_request = placement_requests[0]
        assert placement_request.resource_id == resource.id
        assert placement_request.kind is None
        assert placement_request.pool is None

        admission_request = admission_requests[0]
        # by-id selector: requested_kind/requested_pool stay None, resource_id threads through.
        assert admission_request.requested_kind is None
        assert admission_request.requested_pool is None
        assert admission_request.requested_resource_id == resource.id
        assert admission_request.selector.vcpus == 6
        assert admission_request.selector.memory_gb == 12
        assert admission_request.disk_gb == 80

    asyncio.run(_run())


def test_request_admission_by_kind_grant_threads_requested_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _resource()
    placement_requests: list[PlacementRequest] = []
    admission_requests: list[AllocationRequest] = []

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(_conn: object, placement_request: object) -> PlacementCandidates:
        placement_requests.append(cast(PlacementRequest, placement_request))
        return PlacementCandidates(resources=[resource])

    async def admit(_conn: object, admission_request: object) -> AdmissionOutcome:
        admission_requests.append(cast(AllocationRequest, admission_request))
        return AdmissionOutcome(granted=True, allocation=_allocation())

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        # by-kind selector: kind threads into placement AND becomes requested_kind;
        # pool/id stay None.
        await request_admission(_CONN, _ctx(), project="proj", spec=_spec())
        assert placement_requests[0].kind is ResourceKind.LOCAL_LIBVIRT
        assert placement_requests[0].pool is None
        admission_request = admission_requests[0]
        assert admission_request.requested_kind is ResourceKind.LOCAL_LIBVIRT
        assert admission_request.requested_pool is None
        assert admission_request.requested_resource_id is None

    asyncio.run(_run())


def test_request_admission_by_pool_does_not_set_requested_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # selector != "kind": requested_kind must be None even though spec.kind could be inspected.
    resource = _resource()
    admission_requests: list[AllocationRequest] = []

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        return PlacementCandidates(resources=[resource])

    async def admit(_conn: object, admission_request: object) -> AdmissionOutcome:
        admission_requests.append(cast(AllocationRequest, admission_request))
        return AdmissionOutcome(granted=True, allocation=_allocation())

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        await request_admission(
            _CONN, _ctx(), project="proj", spec=_spec(kind=None, pool="big-remote")
        )
        assert admission_requests[0].requested_kind is None

    asyncio.run(_run())


def test_request_admission_denies_when_granted_without_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # granted=True but allocation=None must fall through to the denial result (the `and` guard),
    # never returning an allocation=None "grant".
    resource = _resource()
    outcome = AdmissionOutcome(granted=True, allocation=None)

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        return PlacementCandidates(resources=[resource])

    async def admit(*_: object, **__: object) -> AdmissionOutcome:
        return outcome

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        result = await request_admission(_CONN, _ctx(), project="proj", spec=_spec())
        assert result.allocation is None
        assert result.denial is outcome
        assert result.resource is resource

    asyncio.run(_run())


def test_denial_details_copies_extra_details_and_stringifies_counts() -> None:
    outcome = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.QUOTA_EXCEEDED,
        reason="quota",
        cap=2,
        in_use=1,
        details={"kind": "local-libvirt"},
    )

    assert denial_details(outcome) == {
        "kind": "local-libvirt",
        "reason": "quota",
        "cap": "2",
        "in_use": "1",
    }
