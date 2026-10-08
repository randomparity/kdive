"""Availability carrier contracts across its HTTP and read-only SQL boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

import pytest

from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import AllocationRequestPayload
from tests.integration import test_catalog_tool_cells_live as carrier
from tests.integration.live_stack.scenario import ScenarioStop
from tests.integration.live_stack.tool_cells import Grants


class Boundary:
    def __init__(self) -> None:
        self.quota: dict[str, Any] = {
            "project": "funded",
            "max_concurrent_allocations": 4,
            "max_concurrent_systems": 7,
            "max_pending_allocations": 0,
        }
        self.original = self.quota.copy()
        self.allocations: list[dict[str, Any]] = []
        self.resources = [
            {
                "id": "host",
                "kind": "local-libvirt",
                "status": "available",
                "cordoned": False,
                "owner_project": None,
                "affinity_allowlist": [],
                "capabilities": {
                    "concurrent_allocation_cap": 1,
                    "vcpus": 8,
                    "memory_mb": 4096,
                    "disk_gb": 10,
                },
            }
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_release: str | None = None
        self.lost_response = False
        self.corrupt_queue = False
        self.fail_restore = False
        self.fail_held = False
        self.fail_setup = False
        self.late_response = False
        self.fail_owned = False
        self.subject = ""

    def token(self, grants: Grants) -> str:
        self.subject = grants.subject
        return grants.subject

    async def rows(
        self, db_url: str, query: str, params: tuple[object, ...] = ()
    ) -> list[dict[str, Any]]:
        if "FROM resources" in query:
            return deepcopy(self.resources)
        if "FROM system_shapes" in query:
            return []
        if "FROM quotas" in query:
            return [self.quota.copy()]
        assert "FROM allocations" in query, query
        if "GROUP BY resource_id" in query:
            n = sum(a["state"] == "granted" for a in self.allocations)
            return [{"resource_id": "host", "n": n}] if n else []
        queued = [a for a in self.allocations if a["state"] == "requested"]
        if "GROUP BY requested_kind" in query:
            return [
                {"requested_kind": kind, "n": sum(a["requested_kind"] == kind for a in queued)}
                for kind in {a["requested_kind"] for a in queued}
            ]
        if "count(*)" in query:
            return [{"n": len(queued)}]
        if "principal" in query:
            if self.fail_owned:
                raise RuntimeError("ownership query failed")
            return deepcopy([a for a in self.allocations if a["principal"] == params[1]])
        if "WHERE id" in query:
            return deepcopy([a for a in self.allocations if a["id"] == params[0]])
        return deepcopy([a for a in self.allocations if a["state"] in cast(list[str], params[0])])

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse:
        data = dict(args)
        self.calls.append((tool, data))
        if tool == "accounting.set_quota":
            if self.fail_setup and data["max_pending_allocations"] == 2:
                raise RuntimeError("quota setup failed")
            if self.fail_restore and data["max_pending_allocations"] == 0:
                raise RuntimeError("quota restore failed")
            self.quota = data.copy()
            return ToolResponse.success("quota", "ok")
        if tool == "allocations.request":
            AllocationRequestPayload.model_validate(
                {k: v for k, v in data.items() if k != "project"}
            )
            selector = data.get("resource", {"mode": "kind", "kind": "local-libvirt"})
            assert isinstance(selector, dict)
            selector = cast(dict[str, object], selector)
            queued = data.get("on_capacity") == "queue"
            aid = str(len(self.allocations) + 1)
            row = {
                "id": aid,
                "resource_id": None if queued else "host",
                "project": "funded",
                "principal": token,
                "state": "requested" if queued else "granted",
                "requested_kind": "local-libvirt" if selector["mode"] == "kind" else None,
                "requested_resource_id": "host" if selector["mode"] == "id" else None,
            }
            if queued and self.late_response:
                raise TimeoutError("request may commit later")
            self.allocations.append(row)
            if queued and self.lost_response:
                raise TimeoutError("request completion unknown")
            return ToolResponse.success(aid, row["state"])
        if tool == "allocations.release":
            aid = str(data["allocation_id"])
            if aid == self.fail_release:
                raise RuntimeError("withdrawal failed")
            row = next(a for a in self.allocations if a["id"] == aid)
            if row["state"] == "granted":
                assert not any(a["state"] == "requested" for a in self.allocations), (
                    "early blocker release"
                )
            row["state"] = "released"
            return ToolResponse.success(aid, "released")
        assert tool == "resources.availability"
        occupied = sum(a["state"] == "granted" for a in self.allocations)
        queued = [a for a in self.allocations if a["state"] == "requested"]
        if self.fail_held and occupied:
            raise RuntimeError("held observation failed")
        queue = {
            "total": len(queued),
            "by_kind": {"local-libvirt": 1} if any(a["requested_kind"] for a in queued) else {},
            "by_id": sum(a["requested_kind"] is None for a in queued),
        }
        if queued and self.corrupt_queue:
            queue["by_id"] = 0
        return ToolResponse(
            object_id="availability",
            status="ok",
            data={"queue_depth": queue, "fits_now": []},
            items=[
                ToolResponse.success(
                    str(resource["id"]),
                    "available",
                    data={
                        "schedulable": True,
                        "cap": 1,
                        "in_use": occupied,
                        "headroom": 1 - occupied,
                        "fits": [],
                    },
                )
                for resource in self.resources
            ],
        )


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Boundary:
    b = Boundary()
    monkeypatch.setenv("KDIVE_PROJECT", "funded")
    monkeypatch.setattr(carrier, "_rows", b.rows)
    monkeypatch.setattr(carrier.HttpCaller, "token", lambda self, grants: b.token(grants))
    monkeypatch.setattr(carrier.HttpCaller, "call", lambda self, *a, **kw: b.call(*a, **kw))
    return b


def run(b: Boundary) -> dict[str, object]:
    caller = carrier.HttpCaller("direct", "unused", OidcIssuer("unused"))
    return asyncio.run(
        carrier._availability(caller, Grants("reader", ("funded",)), db_url="unused")
    )


def test_positive_queue_and_restoration(boundary: Boundary) -> None:
    result = run(boundary)
    assert result["queued"] == {"total": 2, "by_kind": {"local-libvirt": 1}, "by_id": 1}
    assert [a["state"] for a in boundary.allocations] == ["released"] * 3
    assert boundary.quota == boundary.original
    releases = [a["allocation_id"] for tool, a in boundary.calls if tool == "allocations.release"]
    assert releases == ["2", "3", "1"]
    writes = [a for tool, a in boundary.calls if tool == "accounting.set_quota"]
    assert writes == [{**boundary.original, "max_pending_allocations": 2}, boundary.original]


def test_split_mismatch_fails_and_cleans(boundary: Boundary) -> None:
    boundary.corrupt_queue = True
    with pytest.raises(AssertionError, match="queue depth"):
        run(boundary)
    assert all(a["state"] == "released" for a in boundary.allocations)
    assert boundary.quota == boundary.original


@pytest.mark.parametrize("failed", ["2", "3"])
def test_withdrawal_failure_retains_blocker_and_restores_cap(
    boundary: Boundary, failed: str
) -> None:
    boundary.fail_release = failed
    with pytest.raises(AssertionError, match="cleanup") as caught:
        run(boundary)
    assert failed in str(caught.value)
    assert boundary.allocations[0]["state"] == "granted"
    assert [a["allocation_id"] for t, a in boundary.calls if t == "allocations.release"] == [
        "2",
        "3",
    ]
    assert boundary.quota == boundary.original


def test_indeterminate_request_retains_blocker_even_after_withdrawal(boundary: Boundary) -> None:
    boundary.lost_response = True
    with pytest.raises(TimeoutError) as caught:
        run(boundary)
    assert boundary.allocations[0]["state"] == "granted"
    assert boundary.allocations[1]["state"] == "released"
    assert "indeterminate" in str(caught.value.__notes__)
    assert boundary.quota == boundary.original


def test_body_failure_still_withdraws_before_release(boundary: Boundary) -> None:
    boundary.fail_held = True
    with pytest.raises(RuntimeError, match="held observation"):
        run(boundary)
    assert all(a["state"] == "released" for a in boundary.allocations)
    assert boundary.quota == boundary.original


def test_restore_failure_cannot_pass(boundary: Boundary) -> None:
    boundary.fail_restore = True
    with pytest.raises(AssertionError, match="quota restore failed"):
        run(boundary)
    assert all(a["state"] == "released" for a in boundary.allocations)


def test_unsuitable_topology_blocks_before_mutation(boundary: Boundary) -> None:
    boundary.resources.append({**boundary.resources[0], "id": "second"})
    with pytest.raises(ScenarioStop):
        run(boundary)
    assert all(t == "resources.availability" for t, _ in boundary.calls)


@pytest.mark.parametrize("fault", ["late_response", "fail_owned"])
def test_uncertain_ownership_cannot_release_blocker(boundary: Boundary, fault: str) -> None:
    setattr(boundary, fault, True)
    if fault == "fail_owned":
        boundary.fail_held = True
    with pytest.raises((TimeoutError, RuntimeError)) as caught:
        run(boundary)
    assert boundary.allocations[0]["state"] == "granted"
    assert "subject=" in str(caught.value.__notes__)
    assert "1" in str(caught.value.__notes__)
    assert boundary.quota == boundary.original


def test_quota_setup_failure_restores_original_caps(boundary: Boundary) -> None:
    boundary.fail_setup = True
    with pytest.raises(RuntimeError, match="quota setup failed"):
        run(boundary)
    assert not boundary.allocations
    assert boundary.quota == boundary.original


def test_existing_pending_cap_is_preserved(boundary: Boundary) -> None:
    boundary.quota["max_pending_allocations"] = 5
    boundary.original = boundary.quota.copy()
    run(boundary)
    assert boundary.quota == boundary.original
    assert all(
        args["max_pending_allocations"] == 5
        for tool, args in boundary.calls
        if tool == "accounting.set_quota"
    )
