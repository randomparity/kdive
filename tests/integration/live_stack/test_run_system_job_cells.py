"""Behavioral checks for #3097's independent assertions; no live stack required."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import FILTERED_LIST_BOUNDARIES, build_contract
from tests.integration.live_stack import run_system_job_cells as carrier
from tests.integration.live_stack import tool_cells
from tests.integration.live_stack.tool_cell_fakes import (
    _GRANTS,
    _ITEM,
    _control,
    _Filtering,
    _list_run,
    _snapshot,
)
from tests.integration.live_stack.tool_cells import Rejection, prove_rejection


@pytest.mark.parametrize(
    "tool,actions",
    [
        ("runs.list", ["runs.get", "runs.create"]),
        ("systems.list", ["systems.get", "runs.create"]),
        ("jobs.list", ["jobs.wait", "jobs.cancel"]),
    ],
)
def test_canonical_pages_and_controls(tool: str, actions: list[str], tmp_path: Path) -> None:
    expected = ToolResponse.collection(
        tool.split(".")[0],
        "ok",
        [],
        suggested_next_actions=actions,
        data={"truncated": False, "next_cursor": None},
    )
    assert tool_cells._empty_page(tool, {}) == expected
    visible = expected.model_copy(update={"items": [ToolResponse.success(_ITEM, "ok")]})
    rejection = Rejection({"request": {}}, _GRANTS, filtered=_control())
    for bad in ("control", "metadata"):
        caller = _Filtering("gateway", expected, control=visible)
        if bad == "control":
            caller.control = expected
        else:
            caller.result = expected.model_copy(
                update={"data": {"truncated": True, "next_cursor": None}}
            )
        with pytest.raises(AssertionError, match="known item|empty page"):
            asyncio.run(
                prove_rejection(
                    _list_run(tmp_path, tool), caller, "authorization", rejection, _snapshot(1, 1)
                )
            )
    caller = _Filtering("gateway", expected, control=visible)
    asyncio.run(
        prove_rejection(
            _list_run(tmp_path, tool), caller, "authorization", rejection, _snapshot(1, 1)
        )
    )


def test_digest_assertion_rejects_wrong_equal_length_bytes() -> None:
    carrier._assert_digest(b"build", b"build")
    for bad in (b"BUILD", b"buildx", b""):
        with pytest.raises(AssertionError, match="digest"):
            carrier._assert_digest(bad, b"build")


def test_notes_preserve_lifecycle_and_clear() -> None:
    original = {
        "outcome_note": None,
        "state": "succeeded",
        "build_ref": "build-a",
        "label": "label-a",
        "updated_at": 1,
    }
    good = {**original, "outcome_note": "note", "updated_at": 2}
    carrier._assert_note(original, good, "note")
    carrier._assert_note(original, {**good, "outcome_note": None}, None)
    for name, value in (
        ("state", "canceled"),
        ("build_ref", "build-b"),
        ("label", "label-b"),
        ("outcome_note", "wrong"),
    ):
        with pytest.raises(AssertionError):
            carrier._assert_note(original, {**good, name: value}, "note")


@pytest.mark.parametrize("actual", [["b", "a"], ["a"], ["a", "a"], ["a", "b", "c"]])
def test_page_assertion_detects_wrong_order_missing_duplicate_extra(actual: list[str]) -> None:
    carrier._assert_page(["a", "b"], ["a", "b"])
    with pytest.raises(AssertionError):
        carrier._assert_page(actual, ["a", "b"])


def test_filter_oracle_composes_and_excludes_controls() -> None:
    rows = [
        {"id": "a", "project": "p", "state": "created", "system_id": "s", "investigation_id": "i"},
        {
            "id": "b",
            "project": "p",
            "state": "canceled",
            "system_id": None,
            "investigation_id": "j",
        },
    ]
    for request in (
        {"system_id": "s"},
        {"investigation_id": "i"},
        {"state": "created"},
        {"project": "p", "state": "created"},
    ):
        assert carrier._selected("runs.list", rows, request) == ["a"]
    assert carrier._selected("runs.list", rows, {"project": "other"}) == []
    jobs = [
        {
            "id": "a",
            "state": "canceled",
            "kind": "install",
            "inv": "i",
            "payload": {"system_id": "s"},
        },
        {
            "id": "b",
            "state": "succeeded",
            "kind": "provision",
            "inv": None,
            "payload": {"system_id": "s"},
        },
    ]
    assert carrier._selected("jobs.list", jobs, {"investigation_id": "i"}) == ["a"]
    assert carrier._selected("jobs.list", jobs, {"system_id": "s"}) == ["a", "b"]


def test_canceled_envelope_does_not_hide_a_claim_or_provider_effect() -> None:
    job = {
        "state": "canceled",
        "attempt": 0,
        "worker_id": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
    }
    carrier._assert_unclaimed(job, "canceled")
    for name, value in (
        ("state", "running"),
        ("attempt", 1),
        ("worker_id", "worker"),
        ("lease_expires_at", "lease"),
        ("heartbeat_at", "heartbeat"),
    ):
        with pytest.raises(AssertionError):
            carrier._assert_unclaimed({**job, name: value}, "canceled")
    retained = {
        "domain": "running",
        "disk_identity": 1,
        "steps": ["build"],
        "uses": [],
        "authority": [],
    }
    carrier._assert_retained(retained, dict(retained))
    for name, value in (
        ("domain", "stopped"),
        ("disk_identity", 2),
        ("steps", ["build", "install"]),
        ("uses", ["pin"]),
        ("authority", ["operation"]),
    ):
        with pytest.raises(AssertionError, match="changed"):
            carrier._assert_retained(retained, {**retained, name: value})


def test_complete_owner_binding_set() -> None:
    cells = [c for c in build_contract().cells if c.owner == 3097]
    assert len(cells) == 220
    assert len({c.scenario_id for c in cells}) == 55
    assert {c.operation for c in cells} == set(carrier.TOOLS)
    assert all(
        c.node_id
        == "tests/integration/test_run_system_job_tool_cells_live.py::test_run_system_job_tool_cell"
        for c in cells
    )
    assert sum(c.kind == "functional" for c in cells) == 44
    assert sum(c.roles == ("server",) for c in cells) == 212
    assert (
        sum(
            (c.operation, tool_cells.boundary_of(c)) in FILTERED_LIST_BOUNDARIES
            for c in cells
            if c.kind == "rejection"
        )
        == 24
    )
    assert {c.configuration for c in cells} == {"default", "recovery"}
    assert {c.exposure for c in cells} == {"direct", "gateway"}
    assert all(
        c.roles == ("server", "worker")
        for c in cells
        if c.operation == "jobs.wait" and c.kind == "functional"
    )
    assert all(
        c.roles == ("server", "worker", "reconciler", "authority")
        for c in cells
        if c.operation == "jobs.cancel" and c.kind == "functional"
    )


def test_version_cleanup_preserves_exact_adopted_version() -> None:
    old = {"Key": "owned/kernel", "VersionId": "adopted"}
    extra = {"Key": "owned/kernel", "VersionId": "unadopted"}
    loose = {"Key": "owned/other", "VersionId": "loose"}
    retained, purge = carrier._partition_versions(
        [old, extra, loose], {"owned/kernel": {"adopted"}}
    )
    assert retained == [old] and purge == [extra, loose]
    assert carrier._partition_versions([], {}) == ([], [])


@pytest.mark.parametrize("changed", ["jobs", "runs", "steps", "builds", "uses"])
def test_protected_snapshot_covers_selected_terminal_owner(
    monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    facts: dict[str, list[dict[str, object]]] = {
        name: [{"id": name, "state": "succeeded", "protected": "before"}]
        for name in ("jobs", "runs", "steps", "builds", "uses")
    }
    tables = {
        "run_steps": "steps",
        "investigation_build_uses": "uses",
        "investigation_builds": "builds",
        "jobs": "jobs",
        "runs": "runs",
    }

    async def rows(_db: str, query: str, _params: object = ()) -> list[dict[str, object]]:
        import copy

        for table, name in tables.items():
            if f"FROM {table} " in query:
                return copy.deepcopy(facts[name])
        raise AssertionError("unexpected evidence query")

    async def live(_db: str, _project: str) -> dict[str, object]:
        return {"stable": []}

    monkeypatch.setattr(carrier, "_rows", rows)
    monkeypatch.setattr(carrier, "_own_versions", lambda _prefix: [])
    monkeypatch.setattr(carrier, "_live_state", live)
    before = asyncio.run(carrier._protected("db", "project"))
    facts[changed][0]["protected"] = "changed"
    after = asyncio.run(carrier._protected("db", "project"))
    assert after != before


class _QueueHttp:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.calls: list[str] = []

    def token(self, grants: object) -> str:
        return "issued"

    async def call(
        self, tool: str, args: object, token: str, *, discover: bool = False
    ) -> ToolResponse:
        self.calls.append(tool)
        if tool == "jobs.cancel":
            self.state["job_state"] = "canceled"
            return ToolResponse.success("job", "canceled")
        assert tool == "ops.set_queue_paused" and self.state["job_state"] == "canceled"
        self.state["paused"] = False
        return ToolResponse.success("queue", "running")


def test_lost_enqueue_response_settles_owned_job_before_queue_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import cast

    from tests.integration.live_stack.tool_cells import Grants, HttpCaller

    state: dict[str, object] = {"job_state": "queued", "paused": True}
    http = _QueueHttp(state)

    async def rows(_db: str, query: str, _params: object = ()) -> list[dict[str, object]]:
        if "FROM idempotency_keys" in query:
            return [{"result": {"envelope": {"object_id": "job"}}}]
        if "FROM jobs" in query:
            return [
                {
                    "id": "job",
                    "state": state["job_state"],
                    "attempt": 0,
                    "worker_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "payload": {"run_id": "run"},
                    "authorizing": {"project": "project"},
                }
            ]
        return [{"queue_paused": state["paused"]}]

    monkeypatch.setattr(carrier, "_rows", rows)
    asyncio.run(
        carrier._finish_queue(
            cast(HttpCaller, http),
            Grants("operator", ("project",), {}),
            "db",
            "project",
            "run",
            "key",
            True,
        )
    )
    assert http.calls == ["jobs.cancel", "ops.set_queue_paused"]
    assert state == {"job_state": "canceled", "paused": False}


def test_unknown_admission_outcome_keeps_queue_held(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import cast

    from tests.integration.live_stack.tool_cells import Grants, HttpCaller

    state: dict[str, object] = {"job_state": "queued", "paused": True}
    http = _QueueHttp(state)

    async def rows(_db: str, _query: str, _params: object = ()) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(carrier, "_rows", rows)
    with pytest.raises(AssertionError, match="outcome unresolved"):
        asyncio.run(
            carrier._finish_queue(
                cast(HttpCaller, http),
                Grants("operator", ("project",), {}),
                "db",
                "project",
                "run",
                "key",
                True,
                deadline_s=0,
            )
        )
    assert state["paused"] is True and http.calls == []


def test_terminal_runs_are_not_canceled_during_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import cast

    from tests.integration.live_stack.tool_cells import HttpCaller

    state: dict[str, object] = {"job_state": "queued", "paused": True}
    http = _QueueHttp(state)

    async def rows(_db: str, query: str, _params: object = ()) -> list[dict[str, object]]:
        if "FROM investigations" in query:
            return [{"state": "closed"}]
        return [{"id": "run", "state": "succeeded"}]

    monkeypatch.setattr(carrier, "_rows", rows)
    asyncio.run(carrier._end_runs(cast(HttpCaller, http), "project", "db", "investigation"))
    assert http.calls == []


@pytest.mark.parametrize("tool, ignored", [("systems.list", "project"), ("jobs.list", "system_id")])
def test_list_detects_a_server_ignoring_filter(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    ignored: str,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from typing import Any, cast

    facts = [
        {"id": "current", "project": "p", "state": "ready", "allocation_id": "a", "shape": None},
        {"id": "old", "project": "p", "state": "torn_down", "allocation_id": "b", "shape": None},
    ]

    if tool == "jobs.list":
        facts = [
            {
                "id": "install",
                "state": "canceled",
                "kind": "install",
                "inv": "investigation",
                "payload": {"system_id": "current"},
            },
            {
                "id": "provision",
                "state": "succeeded",
                "kind": "provision",
                "inv": None,
                "payload": {"system_id": "current"},
            },
        ]

    @asynccontextmanager
    async def owned(*args: Any):
        yield SimpleNamespace(id="investigation", caller=None)

    async def queued(*args: Any, **kwargs: Any):
        return {}

    async def rows(*args: Any):
        return facts

    async def pages(_c: Any, _g: Any, tool: str, request: dict[str, object]):
        # The HTTP boundary violates only the named narrowing, retaining all other filters.
        return carrier._selected(tool, facts, {k: v for k, v in request.items() if k != ignored})

    monkeypatch.setattr(carrier, "_owned", owned)
    monkeypatch.setattr(carrier, "_rows", rows)
    monkeypatch.setattr(carrier, "_pages", pages)
    monkeypatch.setattr(carrier, "_queued_install", queued)
    with pytest.raises(AssertionError, match="filtered page"):
        asyncio.run(
            carrier._list_body(
                cast(carrier.HttpCaller, None),
                carrier._viewer("p"),
                tool=tool,
                db="db",
                system=SimpleNamespace(system_id="current"),
            )
        )


def test_build_step_matches_exact_adopted_versions() -> None:
    build = {
        "build_ref": "build-ref",
        "artifacts": {"kernel": {"version_id": "kernel-v"}, "vmlinux": {"version_id": "debug-v"}},
    }
    versions = {"kernel": "kernel-v", "vmlinux": "debug-v"}
    correct = {"build_ref": "build-ref", "artifact_versions": versions}
    carrier._assert_build_versions(correct, build)
    for bad in (
        {"build_ref": "build-ref"},
        {**correct, "artifact_versions": {"kernel": "kernel-v"}},
        {**correct, "artifact_versions": {"kernel": "other", "vmlinux": "debug-v"}},
        {**correct, "artifact_versions": {**versions, "extra": "version"}},
        {**correct, "build_ref": "other-generation"},
    ):
        with pytest.raises(AssertionError, match="adopted generation"):
            carrier._assert_build_versions(bad, build)


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("tool", ["jobs.cancel", "systems.list"])
def test_frame_evidence_survives_without_changing_service_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails: bool, tool: str
) -> None:
    import json
    from typing import Any, cast

    from tests.integration.live_stack.evidence import EvidenceWriter

    cell = next(c for c in build_contract().cells if c.operation == tool and c.kind == "functional")
    run = carrier.CellRun(cell, EvidenceWriter(tmp_path))
    cleanup_refs: list[str] = []
    proof_refs: list[str] = []
    observed = {"accelerator": "kvm", "guest_arch": "x86_64", "guest_os": "fedora:44"}

    async def frame(fixture: carrier.CellRun, *args: Any, **kwargs: Any) -> None:
        fixture.observed.update(observed)
        cleanup_ref = fixture.writer.artifact(
            {"cleanup-attempt": {"restored": True, "frame": len(cleanup_refs)}}
        )
        cleanup_refs.append(cleanup_ref)
        fixture.artifacts.append(cleanup_ref)
        if fails and (tool == "jobs.cancel" or len(cleanup_refs) == 2):
            raise AssertionError("scenario failed")
        fixture.prove("effect", {"effect": True})
        fixture.prove("cleanup", {"restored": True, "frame": len(cleanup_refs)})
        proof_refs.extend(fixture.assertions.values())

    monkeypatch.setattr(carrier, "on_authority_system", frame)
    monkeypatch.setattr(tool_cells, "on_lane_system", frame)
    issuer = cast(carrier.OidcIssuer, None)
    if fails:
        with pytest.raises(AssertionError, match="scenario failed"):
            asyncio.run(carrier._scenario_body(run, "http://localhost", issuer, "db"))
        assert not {"effect", "cleanup"}.intersection(run.assertions)
    else:
        asyncio.run(carrier._scenario_body(run, "http://localhost", issuer, "db"))
        assert {"effect", "cleanup"} <= run.assertions.keys()
    assert run.observed == {"accelerator": "none"}
    assert set(cleanup_refs + proof_refs) <= set(run.artifacts)
    retained = [json.loads((tmp_path / "artifacts" / ref).read_text()) for ref in run.artifacts]
    assert any(item.get("fixture_observed") == observed for item in retained)


@pytest.mark.parametrize("damage", ["valid", "kernel", "vmlinux", "missing-vmlinux"])
def test_complete_observation_uses_production_response(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from types import SimpleNamespace
    from typing import Any, cast
    from uuid import UUID

    from kdive.mcp.tools.lifecycle.runs.complete_build import _complete_envelope
    from kdive.services.runs.steps import BuildStepResult

    run_id = "00000000-0000-4000-8000-000000000001"
    versions = {"kernel": "kernel-version", "vmlinux": "debug-version"}
    result = BuildStepResult(
        "kernel-key", "debug-key", carrier._BUILD_ID, build_ref="build", artifact_versions=versions
    )
    envelope = _complete_envelope(UUID(run_id), result, server_time="2026-10-16T00:00:00Z")
    if damage != "valid":
        refs = dict(envelope.refs)
        if damage == "missing-vmlinux":
            del refs["vmlinux"]
        else:
            refs[damage] = "wrong-key"
        envelope = envelope.model_copy(update={"refs": refs})
    run = {
        "state": "succeeded",
        "build_ref": "build",
        "kernel_ref": "kernel-key",
        "debuginfo_ref": "debug-key",
    }

    async def stage(*args: Any):
        return run_id, {"kernel-key", "debug-key"}

    async def call(*args: Any, **kwargs: Any):
        return envelope

    async def rows(_db: str, query: str, _params: object = ()):
        if query == carrier._RUN:
            return [run]
        if query == carrier._STEPS:
            return [{"step": "build", "state": "succeeded", "result": result.dump()}]
        if "FROM investigation_builds" in query:
            return [
                {
                    "build_ref": "build",
                    "artifacts": {
                        name: {"version_id": version} for name, version in versions.items()
                    },
                }
            ]
        assert "FROM artifacts" in query
        return [
            {"object_key": key, "owner_kind": "investigations", "owner_id": "inv"}
            for key in ("kernel-key", "debug-key")
        ]

    monkeypatch.setattr(carrier, "_stage", stage)
    monkeypatch.setattr(carrier, "_call", call)
    monkeypatch.setattr(carrier, "_rows", rows)
    inv = cast(carrier._Investigation, SimpleNamespace(id="inv", runs=[run_id]))
    pending = carrier._complete(
        inv, cast(carrier.HttpCaller, None), carrier._contributor("p"), "db"
    )
    if damage == "valid":
        assert asyncio.run(pending) == run_id
    else:
        with pytest.raises((AssertionError, KeyError)):
            asyncio.run(pending)
