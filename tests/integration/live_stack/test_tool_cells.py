"""Unit tests of the ADR-0722 tool-cell harness (no stack needed)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackToolError, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import capability_unsupported
from kdive.providers.ports.traffic import pcap_volume_name
from kdive.providers.shared.runtime_paths import pcap_dir
from kdive.serialization import JsonValue
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Evidence, Outcome
from scripts.coverage_campaign.results import qualify
from tests.integration.live_stack import scenario, tool_cells
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity
from tests.integration.live_stack.remote_lifecycle import RemoteHost, guest_boot_kernel
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cell_fakes import (
    _GRANTS,
    _ITEM,
    _OK,
    _Caller,
    _control,
    _Filtering,
    _list_run,
    _listed,
    _snapshot,
    forge_source,
)
from tests.integration.live_stack.tool_cells import (
    RECOVERY_TOOLS,
    FilteredListControl,
    Grants,
    HttpCaller,
    Rejection,
    _empty_page,
    bindings,
    claims_of,
    configuration_of,
    forge,
    prove_functional,
    prove_refusal,
    prove_rejection,
    rejected_by_validation,
)


def _run(tmp_path: Path, boundary: str) -> CellRun:
    cell = next(
        c
        for c in build_contract().cells
        if c.operation == "tools.search" and c.scenario_id.endswith("/" + boundary)
    )
    return CellRun(cell, EvidenceWriter(tmp_path))


def test_configuration_follows_the_recovery_tools() -> None:
    assert configuration_of(["tools.search"]) == "default"
    assert configuration_of([*RECOVERY_TOOLS, "tools.search"]) == "recovery"
    with pytest.raises(AssertionError, match="neither default nor recovery"):
        configuration_of(["ops.build_uses_list"])


def test_authentication_needs_401(tmp_path: Path) -> None:
    run = _run(tmp_path, "authentication")
    caller = _Caller("gateway")
    asyncio.run(
        prove_rejection(run, caller, "authentication", Rejection({}, _GRANTS), _snapshot(1, 1))
    )
    assert set(run.assertions) == {"authentication", "unchanged-state", "cleanup"}
    assert caller.calls[0][1]["projects"] == ["p"]
    with pytest.raises(AssertionError, match="HTTP 200"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "authentication"),
                _Caller("direct", status=200),
                "authentication",
                Rejection({}, _GRANTS),
                _snapshot(1, 1),
            )
        )
    with pytest.raises(AssertionError, match="also refused"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "authentication"),
                _Caller("direct", refuse_issued=True),
                "authentication",
                Rejection({}, _GRANTS),
                _snapshot(1, 1),
            )
        )


def _invoke_error(detail: str | None, **data: JsonValue) -> ToolResponse:
    return ToolResponse.failure(
        "tools.invoke", ErrorCategory.CONFIGURATION_ERROR, detail=detail, data=data
    )


# The binding-failure detail tools.invoke writes when the caller can see the tool
# (src/kdive/mcp/tools/gateway.py).
_BINDING_DETAIL = (
    "Arguments for 'tools.search' failed schema validation. Call "
    'tools.search(names=["tools.search"], detail="full") for its exact schema.'
)
_FIELD_ERRORS: JsonValue = [{"field": "limit", "kind": "int_parsing"}]


def test_validation_rule_per_exposure() -> None:
    tool_error = LiveStackToolError("tools.search", "1 validation error for call[tools_search]")
    binding = _invoke_error(_BINDING_DETAIL, field_errors=_FIELD_ERRORS)
    assert rejected_by_validation("direct", tool_error)
    assert not rejected_by_validation("gateway", tool_error)
    assert rejected_by_validation("gateway", binding)
    assert rejected_by_validation("direct", _invoke_error(None))
    assert not rejected_by_validation("direct", LiveStackToolError("t", "boom"))
    assert not rejected_by_validation("direct", ToolResponse.success("x", "ok"))


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(
            _invoke_error(
                "No tool named 'tools.search' is registered or enabled; "
                "discover available tools with tools.search."
            ),
            id="not-found",
        ),
        pytest.param(_invoke_error("project 'p' has no build host"), id="inner-categorized-error"),
        pytest.param(
            _invoke_error("Arguments for 'tools.search' failed schema validation."),
            id="bare-pydantic-validation-error",
        ),
        pytest.param(_invoke_error(_BINDING_DETAIL), id="binding-without-field-errors"),
        pytest.param(_invoke_error(_BINDING_DETAIL, field_errors=[]), id="empty-field-errors"),
        pytest.param(
            _invoke_error("project 'p' has no build host", field_errors=_FIELD_ERRORS),
            id="field-errors-without-schema-detail",
        ),
    ],
)
def test_gateway_validation_needs_the_binding_failure(envelope: ToolResponse) -> None:
    assert not rejected_by_validation("gateway", envelope)


def test_category_boundaries_use_the_closed_set(tmp_path: Path) -> None:
    denied = ToolResponse.failure("x", ErrorCategory.AUTHORIZATION_DENIED)
    for boundary in ("authorization", "project-isolation"):
        run = _run(tmp_path, "validation")
        asyncio.run(
            prove_rejection(
                run, _Caller("direct", denied), boundary, Rejection({}, _GRANTS), _snapshot(1, 1)
            )
        )
        assert boundary in run.assertions
    narrow = Rejection({}, _GRANTS, frozenset({"not_found"}))
    with pytest.raises(AssertionError, match="authorization_denied"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "validation"),
                _Caller("direct", denied),
                "project-isolation",
                narrow,
                _snapshot(1, 1),
            )
        )
    with pytest.raises(AssertionError, match="was not rejected"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "validation"),
                _Caller("direct", ToolResponse.success("x", "ok")),
                "authorization",
                Rejection({}, _GRANTS),
                _snapshot(1, 1),
            )
        )


def _capability_refusal() -> ToolResponse:
    return capability_unsupported(
        "sys-1", capability="snapshot", provider="local-libvirt", supported=[]
    )


@pytest.mark.parametrize("exposure", ["direct", "gateway"])
def test_capability_boundary_needs_the_capability_refusal(tmp_path: Path, exposure: str) -> None:
    run = _run(tmp_path, "validation")
    asyncio.run(
        prove_rejection(
            run,
            _Caller(exposure, _capability_refusal()),
            "capability",
            Rejection({}, _GRANTS),
            _snapshot(1, 1),
        )
    )
    proof = _artifact(run, "capability")
    assert proof["capability"] == "snapshot"
    assert proof["rejected"] == {"status": "error", "error_category": "configuration_error"}


@pytest.mark.parametrize(
    "answer",
    [
        ToolResponse.failure("sys-1", ErrorCategory.CONFIGURATION_ERROR),
        ToolResponse.failure(
            "sys-1", ErrorCategory.CONFIGURATION_ERROR, data={"reason": "invalid_snapshot_name"}
        ),
        ToolResponse.failure(
            "sys-1", ErrorCategory.AUTHORIZATION_DENIED, data={"reason": "capability_unsupported"}
        ),
        ToolResponse.success("sys-1", "queued"),
        LiveStackToolError("systems.snapshot", "1 validation error"),
    ],
    ids=["bare-configuration-error", "other-reason", "other-category", "success", "tool-error"],
)
def test_capability_boundary_refuses_any_other_answer(
    tmp_path: Path, answer: ToolResponse | LiveStackToolError
) -> None:
    with pytest.raises(AssertionError, match="was not refused for capability"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "validation"),
                _Caller("direct", answer),
                "capability",
                Rejection({}, _GRANTS),
                _snapshot(1, 1),
            )
        )


_DENIED_PAGE = ToolResponse.collection(
    "investigations",
    "ok",
    [],
    suggested_next_actions=["investigations.get", "investigations.open"],
    data={"truncated": False, "next_cursor": None},
)


def _filtered(tmp_path: Path, caller: _Caller, control: FilteredListControl) -> CellRun:
    """Prove ``investigations.list``'s authorization cell against ``caller``."""
    run = _list_run(tmp_path)
    rejection = Rejection({"request": {"project": "p"}}, _GRANTS, filtered=control)
    asyncio.run(prove_rejection(run, caller, "authorization", rejection, _snapshot(1, 1)))
    return run


def test_filtered_list_proves_denied_visibility(tmp_path: Path) -> None:
    caller = _Filtering("gateway", _DENIED_PAGE)
    run = _filtered(tmp_path, caller, _control())
    assert set(run.assertions) == {"authorization", "unchanged-state", "cleanup"}
    assert [args["subject"] for _, args in caller.calls] == ["viewer", "agent"]
    assert all(args["request"] == {"project": "p"} for _, args in caller.calls)
    proof = _artifact(run, "authorization")
    assert proof["exposure"] == "gateway"
    assert proof["denied_visibility"] == {"status": "ok", "canonical_empty_page": True}
    assert proof["viewer_control"] == {
        "listed": 1,
        "item_sha256": hashlib.sha256(_ITEM.encode()).hexdigest(),
    }
    artifacts = _list_run(tmp_path, "artifacts.list", "project-isolation")
    page = ToolResponse.collection(
        "sys-1",
        "ok",
        [],
        suggested_next_actions=["artifacts.get"],
        data={"truncated": False, "next_cursor": None},
    )
    listed = page.model_copy(update={"items": [ToolResponse.success(_ITEM, "redacted")]})
    rejection = Rejection({"system_id": "sys-1"}, _GRANTS, filtered=_control())
    asyncio.run(
        prove_rejection(
            artifacts,
            _Filtering("direct", page, control=listed),
            "project-isolation",
            rejection,
            _snapshot(1, 1),
        )
    )
    assert "project-isolation" in artifacts.assertions


@pytest.mark.parametrize(
    ("control", "item", "match"),
    [
        (_listed(), _ITEM, "did not list the known item"),
        (_listed("inv-2"), _ITEM, "did not list the known item"),
        (_listed(_ITEM), "", "no independently known protected item"),
        (ToolResponse.failure("x", ErrorCategory.NOT_FOUND), _ITEM, "control call failed"),
    ],
    ids=["empty", "unrelated-item", "no-oracle", "control-error"],
)
def test_filtered_list_requires_known_visible_item(
    tmp_path: Path, control: ToolResponse, item: str, match: str
) -> None:
    caller = _Filtering("direct", _DENIED_PAGE, control=control)
    with pytest.raises(AssertionError, match=match):
        _filtered(tmp_path, caller, _control(item))
    assert all(args["subject"] == "viewer" for _, args in caller.calls)


_LEAKS = {
    "item": _listed(_ITEM),
    "cursor": _DENIED_PAGE.model_copy(
        update={"data": {"truncated": True, "next_cursor": "c", "count": 0}}
    ),
    "extra-data": _DENIED_PAGE.model_copy(update={"data": {**_DENIED_PAGE.data, "p": "x"}}),
    "refs": _DENIED_PAGE.model_copy(update={"refs": {"project": "p"}}),
    "detail": _DENIED_PAGE.model_copy(update={"detail": "hidden"}),
    "object-id": _DENIED_PAGE.model_copy(update={"object_id": "p"}),
    "actions": _DENIED_PAGE.model_copy(update={"suggested_next_actions": ["investigations.get"]}),
    "status": _DENIED_PAGE.model_copy(update={"status": "partial"}),
    "failure": ToolResponse.failure("investigations", ErrorCategory.AUTHORIZATION_DENIED),
}


@pytest.mark.parametrize("leak", sorted(_LEAKS))
def test_filtered_list_requires_canonical_empty_envelope(tmp_path: Path, leak: str) -> None:
    with pytest.raises(AssertionError, match="not the empty page"):
        _filtered(tmp_path, _Filtering("direct", _LEAKS[leak]), _control())


def test_debug_sessions_denied_page_and_unconfigured_pages(tmp_path: Path) -> None:
    page = ToolResponse.collection(
        "debug_sessions",
        "ok",
        [],
        suggested_next_actions=["debug.get_session"],
        data={"truncated": False, "next_cursor": None},
    )
    listed = page.model_copy(update={"items": [ToolResponse.success(_ITEM, "detached")]})
    run = _list_run(tmp_path, "debug.list_sessions", "project-isolation")
    rejection = Rejection({"request": {"run_id": "r"}}, _GRANTS, filtered=_control())
    caller = _Filtering("direct", page, control=listed)
    asyncio.run(prove_rejection(run, caller, "project-isolation", rejection, _snapshot(1, 1)))
    assert "project-isolation" in run.assertions
    # A tool whose carrier has not configured its page fails rather than borrowing another's.
    with pytest.raises(AssertionError, match="no canonical empty page"):
        _empty_page("cov.unconfigured_list", {"request": {}})


def test_allocations_list_page_names_the_project_and_no_actions(tmp_path: Path) -> None:
    page = ToolResponse.collection(
        "allocations",
        "ok",
        [],
        suggested_next_actions=[],
        data={"truncated": False, "next_cursor": None, "project": "p"},
    )
    assert _empty_page("allocations.list", {"request": {"project": "p"}}) == page
    listed = page.model_copy(update={"items": [ToolResponse.success(_ITEM, "granted")]})
    rejection = Rejection({"request": {"project": "p"}}, _GRANTS, filtered=_control())
    for boundary in ("authorization", "project-isolation"):
        run = _list_run(tmp_path, "allocations.list", boundary)
        caller = _Filtering("gateway", page, control=listed)
        asyncio.run(prove_rejection(run, caller, boundary, rejection, _snapshot(1, 1)))
        assert boundary in run.assertions
    for leak in (
        page.model_copy(update={"data": {"truncated": False, "next_cursor": None}}),
        page.model_copy(update={"suggested_next_actions": ["allocations.wait"]}),
    ):
        with pytest.raises(AssertionError, match="not the empty page"):
            asyncio.run(
                prove_rejection(
                    _list_run(tmp_path, "allocations.list", "authorization"),
                    _Filtering("direct", leak, control=listed),
                    "authorization",
                    rejection,
                    _snapshot(1, 1),
                )
            )
    unnamed = _Filtering("direct", page, control=listed)
    with pytest.raises(AssertionError, match="must name a project"):
        asyncio.run(
            prove_rejection(
                _list_run(tmp_path, "allocations.list", "authorization"),
                unnamed,
                "authorization",
                Rejection({"request": {}}, _GRANTS, filtered=_control()),
                _snapshot(1, 1),
            )
        )
    assert unnamed.calls == []


def test_filtered_exception_is_closed(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="FILTERED_LIST_BOUNDARIES"):
        asyncio.run(
            prove_rejection(
                _list_run(tmp_path),
                _Filtering("direct", _DENIED_PAGE),
                "authorization",
                Rejection({}, _GRANTS),
                _snapshot(1, 1),
            )
        )
    unlisted = _list_run(tmp_path, "artifacts.list", "authorization")
    with pytest.raises(AssertionError, match="FILTERED_LIST_BOUNDARIES"):
        asyncio.run(
            prove_rejection(
                unlisted,
                _Filtering("direct", _DENIED_PAGE),
                "authorization",
                Rejection({"system_id": "s"}, _GRANTS, filtered=_control()),
                _snapshot(1, 1),
            )
        )


def test_filtered_list_brackets_both_calls_and_cleans_up(tmp_path: Path) -> None:
    state = [0]

    @asynccontextmanager
    async def setup() -> AsyncIterator[Mapping[str, object]]:
        state[0] += 1
        try:
            yield {}
        finally:
            state[0] -= 1

    async def snap() -> object:
        return list(state)

    failing = Rejection({}, _GRANTS, filtered=_control())
    with pytest.raises(AssertionError, match="did not list"):
        asyncio.run(
            prove_rejection(
                _list_run(tmp_path),
                _Filtering("direct", _DENIED_PAGE, control=_listed()),
                "authorization",
                failing,
                snap,
                setup=setup,
            )
        )
    assert state == [0]
    with pytest.raises(AssertionError, match="protected state changed"):
        asyncio.run(
            prove_rejection(
                _list_run(tmp_path),
                _Filtering("direct", _DENIED_PAGE),
                "authorization",
                failing,
                _snapshot({"t": [0]}, {"t": [1]}),
            )
        )


@dataclass
class _Twin(_Caller):
    """Answers the absent owner's arguments with ``absent``, everything else with ``result``."""

    absent: ToolResponse = field(default_factory=lambda: _OK)

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        self.calls.append((tool, args))
        return (
            self.absent
            if args.get("investigation_id") == "absent"
            else cast(ToolResponse, self.result)
        )


def _owner_error(owner: str, **data: JsonValue) -> ToolResponse:
    return ToolResponse.failure(owner, ErrorCategory.CONFIGURATION_ERROR, data=data)


def test_absent_twin_must_answer_identically(tmp_path: Path) -> None:
    run = _run(tmp_path, "validation")
    twin = Rejection(
        {"investigation_id": "inv-1"},
        _GRANTS,
        frozenset({"configuration_error"}),
        absent_twin={"investigation_id": "absent"},
    )
    caller = _Twin("direct", _owner_error("inv-1"), absent=_owner_error("absent"))
    asyncio.run(prove_rejection(run, caller, "project-isolation", twin, _snapshot(1, 1)))
    assert [args for _, args in caller.calls] == [
        {"investigation_id": "inv-1"},
        {"investigation_id": "absent"},
    ]
    assert "absent_owner_twin" in _artifact(run, "project-isolation")
    other = _Twin(
        "direct", _owner_error("inv-1", reason="no_upload_manifest"), absent=_owner_error("absent")
    )
    with pytest.raises(AssertionError, match="an absent owner"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "validation"), other, "project-isolation", twin, _snapshot(1, 1)
            )
        )


def test_changed_state_fails_the_rejection(tmp_path: Path) -> None:
    run = _run(tmp_path, "authentication")
    with pytest.raises(AssertionError, match="protected state changed"):
        asyncio.run(
            prove_rejection(
                run,
                _Caller("direct"),
                "authentication",
                Rejection({}, _GRANTS),
                _snapshot({"t": [0]}, {"t": [1]}),
            )
        )
    assert "unchanged-state" not in run.assertions


def _artifact(run: CellRun, assertion: str) -> dict[str, object]:
    path = run.writer.root / "artifacts" / run.assertions[assertion]
    return cast(dict[str, object], json.loads(path.read_text()))


def test_functional_owned_moves_to_cleanup(tmp_path: Path) -> None:
    run = _run(tmp_path, "functional")

    async def body(caller: HttpCaller, grants: Grants) -> dict[str, object]:
        return {"rows": 2, "owned": ["cov-shape"]}

    caller = cast(HttpCaller, _Caller("direct"))
    asyncio.run(prove_functional(run, caller, _GRANTS, body, _snapshot(1, 1)))
    assert _artifact(run, "cleanup")["owned"] == ["cov-shape"]
    assert "owned" not in _artifact(run, "effect") and _artifact(run, "effect")["rows"] == 2


def _setup(state: list[int], *, leak: bool = False) -> tool_cells.Setup:
    @asynccontextmanager
    async def setup() -> AsyncIterator[Mapping[str, object]]:
        state.append(1)
        yield {"image_id": "img-1"}
        if not leak:
            state.pop()

    return setup


def test_setup_overrides_reach_the_call(tmp_path: Path) -> None:
    run, state = _run(tmp_path, "authentication"), []
    caller = _Caller("direct", result=ToolResponse.denied("x"))

    async def snap() -> object:
        return list(state)

    asyncio.run(
        prove_rejection(
            run, caller, "authorization", Rejection({"a": 1}, _GRANTS), snap, setup=_setup(state)
        )
    )
    assert caller.calls == [("tools.search", {"a": 1, "image_id": "img-1"})]
    assert _artifact(run, "cleanup")["owned"] == ["img-1"]
    assert set(run.assertions) == {"authorization", "unchanged-state", "cleanup"}


def test_setup_left_state_fails_cleanup(tmp_path: Path) -> None:
    run, state = _run(tmp_path, "authentication"), []
    caller = _Caller("direct", result=ToolResponse.denied("x"))

    async def snap() -> object:
        return list(state)

    with pytest.raises(AssertionError, match="left state behind"):
        asyncio.run(
            prove_rejection(
                run,
                caller,
                "authorization",
                Rejection({}, _GRANTS),
                snap,
                setup=_setup(state, leak=True),
            )
        )
    assert "unchanged-state" in run.assertions and "cleanup" not in run.assertions


def test_setup_call_changing_state_fails_unchanged_state(tmp_path: Path) -> None:
    run, state = _run(tmp_path, "authentication"), []

    class _Writing(_Caller):
        async def call(
            self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
        ) -> ToolResponse | list[ToolResponse]:
            state.append(2)
            return ToolResponse.denied("x")

    async def snap() -> object:
        return list(state)

    with pytest.raises(AssertionError, match="protected state changed"):
        asyncio.run(
            prove_rejection(
                run,
                _Writing("direct"),
                "authorization",
                Rejection({}, _GRANTS),
                snap,
                setup=_setup(state),
            )
        )
    assert "unchanged-state" not in run.assertions and "cleanup" not in run.assertions


def test_forge_keeps_claims_and_changes_signature() -> None:
    token = forge_source(_GRANTS)
    forged = forge(token)
    assert claims_of(forged) | {"iat": 0, "exp": 0} == claims_of(token) | {"iat": 0, "exp": 0}
    assert forged.split(".")[2] != token.split(".")[2]
    header = json.loads(base64.urlsafe_b64decode(forged.split(".")[0] + "=="))
    assert header["kid"] == "issuer-key-1"


def _cell(operation: str, provider: str, arch: str, kind: str = "functional") -> Cell:
    return next(
        c
        for c in build_contract().cells
        if c.operation == operation
        and c.provider == provider
        and c.guest_arch == arch
        and c.kind == kind
    )


def _bound(cell: Cell) -> Cell:
    return replace(cell, node_id="tests/x.py::test_x")


def test_bindings_cover_bound_tool_cells(tmp_path: Path) -> None:
    contract = build_contract()
    whoami = [c for c in contract.cells if c.operation == "session.whoami"]
    bound = [_bound(c) for c in whoami]
    unbound = replace(whoami[0], node_id=None, id="unbound")
    native = _bound(_cell("systems.ssh_info", "local-libvirt", "x86_64"))
    foreign = _bound(_cell("systems.ssh_info", "local-libvirt", "ppc64le"))
    remote = _bound(_cell("systems.ssh_info", "remote-libvirt", "x86_64"))
    image = tmp_path / "image.qcow2"
    image.write_bytes(b"lane")
    inputs = bindings(
        "a" * 40,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix=contract.matrix_sha256,
        cells=[*bound, unbound, native, foreign, remote],
        staged=lambda _name: image,
    )
    assert set(inputs.cells) == {c.id for c in bound} | {native.id}
    assert {inputs.cells[c.id].accelerator for c in bound} == {"none"}
    lane = inputs.cells[native.id]
    assert (lane.guest_os, lane.guest_arch, lane.accelerator) == ("fedora:44", "x86_64", "kvm")
    assert lane.image_sha256 == hashlib.sha256(b"lane").hexdigest()


def test_remote_bindings_carry_the_provider_host() -> None:
    remote = _bound(_cell("systems.ssh_info", "remote-libvirt", "x86_64"))
    foreign = _bound(_cell("systems.ssh_info", "remote-libvirt", "ppc64le"))
    host = RemoteHost("operator@provider.example", "default", "rocky:10.2", "x86_64", "kvm")
    inputs = bindings(
        "a" * 40,
        host_os="ubuntu:26.04",
        host_arch="x86_64",
        matrix="b" * 64,
        cells=[remote, foreign],
        staged=lambda _name: None,
        remote=host,
        digest=lambda *_: "f" * 64,
        volumes=lambda name: f"{name}.qcow2",
    )
    assert set(inputs.cells) == {remote.id}
    bound = inputs.cells[remote.id]
    assert (bound.host_os, bound.host_arch, bound.guest_os, bound.guest_arch) == (
        "rocky:10.2",
        "x86_64",
        "fedora:43",
        "x86_64",
    )
    assert (bound.accelerator, bound.image_sha256) == ("kvm", "f" * 64)


def test_remote_bindings_refuse_a_provider_without_a_lane_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = RemoteHost("operator@provider.example", "default", "rocky:10.2", "aarch64", "kvm")
    monkeypatch.setattr(tool_cells, "remote_host", lambda: host)
    out = tmp_path / "inputs.json"
    argv = ["bindings", "--remote", "--candidate", "a" * 40, "--out", str(out)]
    assert tool_cells.main(argv) == 2
    assert not out.exists()


def test_lane_for_selects_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_cells.platform, "machine", lambda: "x86_64")
    local = tool_cells.lane_for(_cell("systems.ssh_info", "local-libvirt", "x86_64"))
    assert (local.provider, local.entry.distro) == ("local-libvirt", "fedora")
    monkeypatch.delenv("REMOTE_PROVIDER_SSH", raising=False)
    with pytest.raises(ScenarioStop) as stop:
        tool_cells.lane_for(_cell("systems.ssh_info", "remote-libvirt", "x86_64"))
    assert stop.value.outcome is Outcome.BLOCKED


class _Pool:
    def __init__(self, xml: str) -> None:
        self.xml = xml

    def XMLDesc(self, _flags: int) -> str:  # noqa: N802 - libvirt binding name
        return self.xml


class _PoolConn:
    def __init__(self, xml: str) -> None:
        self.pool, self.closed, self.named = _Pool(xml), False, ""

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802 - libvirt binding name
        self.named = name
        return self.pool

    def close(self) -> None:
        self.closed = True


_SYSTEM, _JOB = str(UUID(int=1)), str(UUID(int=2))


def test_remote_pcap_spool_reads_the_pool_target_once(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _PoolConn("<pool><target><path>/srv/pool</path></target></pool>")
    monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
    namer = tool_cells.remote_pcap_spool("op@provider.example", "kdive", _SYSTEM)
    assert (conn.named, conn.closed) == ("kdive", True)
    conn.named = ""
    assert namer(_JOB) == f"/srv/pool/{pcap_volume_name(UUID(_SYSTEM), UUID(_JOB))}"
    assert conn.named == "", "naming a job's volume read the pool again"


def test_remote_pcap_spool_needs_a_pool_target(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _PoolConn("<pool><target/></pool>")
    monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
    with pytest.raises(AssertionError, match="no target path"):
        tool_cells.remote_pcap_spool("op@provider.example", "kdive", _SYSTEM)
    assert conn.closed


def test_pool_volume_path_joins_the_pool_target(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _PoolConn("<pool><target><path>/srv/pool</path></target></pool>")
    monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
    path = tool_cells.pool_volume_path("op@provider.example", "kdive", "kdive-host-dump-x.kdump")
    assert path == "/srv/pool/kdive-host-dump-x.kdump"
    assert (conn.named, conn.closed) == ("kdive", True)


def test_pool_volume_path_needs_a_pool_target(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _PoolConn("<pool><target/></pool>")
    monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
    with pytest.raises(AssertionError, match="no target path"):
        tool_cells.pool_volume_path("op@provider.example", "kdive", "v")
    assert conn.closed


def test_local_lane_observes_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_cells.platform, "machine", lambda: "x86_64")
    lane = tool_cells.lane_for(_cell("control.power", "local-libvirt", "x86_64"))
    assert lane.connect is tool_cells.worker_connect
    assert lane.spool(_SYSTEM)(_JOB) == str(pcap_dir(UUID(_SYSTEM)))
    assert lane.installed_kernel is tool_cells.domain_kernel
    assert lane.pool_volume is None


def test_remote_lane_observes_the_provider_host(monkeypatch: pytest.MonkeyPatch) -> None:
    host = RemoteHost("op@provider.example", "kdive", "rocky:10.2", "x86_64", "kvm")
    monkeypatch.setattr(tool_cells, "remote_host", lambda: host)
    monkeypatch.setattr(tool_cells, "staged_base_volume", lambda _image: "base.qcow2")
    seen: list[str] = []
    monkeypatch.setattr(tool_cells, "observer", lambda dest: seen.append(dest) or "conn")
    lane = tool_cells.lane_for(_cell("control.power", "remote-libvirt", "x86_64"))
    assert lane.connect() == "conn" and seen == ["op@provider.example"]
    spool = lane.spool
    assert isinstance(spool, partial) and spool.func is tool_cells.remote_pcap_spool
    assert spool.args == ("op@provider.example", "kdive")
    kernel = lane.installed_kernel
    assert isinstance(kernel, partial) and kernel.func is guest_boot_kernel
    assert kernel.keywords == {"family": "fedora"}
    volume = lane.pool_volume
    assert isinstance(volume, partial) and volume.func is tool_cells.pool_volume_path
    assert volume.args == ("op@provider.example", "kdive")


def test_remote_cell_is_not_skipped_by_control_plane_arch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stack() -> str:
        raise RuntimeError("stack read")

    monkeypatch.setattr(tool_cells, "require_stack", stack)
    monkeypatch.setattr(tool_cells.platform, "machine", lambda: "aarch64")

    async def never(*_: object) -> None:
        raise AssertionError("the scenario must not run")

    cell = _cell("systems.ssh_info", "remote-libvirt", "x86_64")
    try:
        with pytest.raises(RuntimeError, match="stack read"):
            tool_cells.run_tool_cell(cell, never)
    except pytest.skip.Exception as exc:
        pytest.fail(f"the remote cell was skipped by the control plane's arch: {exc}")


def test_lane_target_prepares_once_per_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared: list[str] = []
    issuer = cast(OidcIssuer, object())

    async def prepare(run: CellRun, *_: object) -> object:
        prepared.append(run.cell.provider)
        return tool_cells.LaneTarget("cov-t", "alloc", "sys", {}, ())

    monkeypatch.setattr(tool_cells, "_TARGETS", {})
    monkeypatch.setattr(tool_cells, "_provision_target", prepare)
    local = _cell("systems.ssh_info", "local-libvirt", "x86_64")
    for cell in (_run(tmp_path, "authentication").cell, local, local):
        run = CellRun(cell, EvidenceWriter(tmp_path))
        asyncio.run(tool_cells.lane_target(run, "u", issuer, "db"))
    assert prepared == ["service", "local-libvirt"]


def test_kernel_inputs_bind_only_declaring_cells() -> None:
    kernel = {"kernel_sha256": "c" * 64, "kernel_build_id": "d" * 40}
    install = _bound(_cell("runs.install", "local-libvirt", "x86_64"))
    ssh_info = _bound(_cell("systems.ssh_info", "local-libvirt", "x86_64"))
    inputs = bindings(
        "a" * 40,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix="b" * 64,
        cells=[install, ssh_info],
        staged=lambda _name: None,
        kernel=kernel,
    )
    assert inputs.cells[install.id].kernel_sha256 == "c" * 64
    assert inputs.cells[install.id].kernel_build_id == "d" * 40
    assert inputs.cells[install.id].image_sha256 is None
    assert inputs.cells[ssh_info.id].kernel_sha256 is None


def test_declared_authority_fails_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    roles = {"server": sha, "worker": sha, "reconciler": sha}
    identity = RunIdentity(sha, "b" * 64, "fedora:44", "x86_64", True, roles)
    monkeypatch.setattr(scenario, "require_stack", lambda: "http://stack.test/mcp")
    monkeypatch.setattr(scenario, "run_identity", lambda _url: identity)
    monkeypatch.setattr(scenario, "prerequisites", lambda: (object(), "postgresql://x"))
    monkeypatch.setattr(scenario, "evidence_root", lambda: tmp_path)
    cell = _bound(_cell("runs.release_external_boot", "local-libvirt", "x86_64"))
    assert "authority" in cell.roles

    async def body(run: CellRun, *_: object) -> None:
        for name in cell.assertions:
            run.prove(name, {})

    with pytest.raises(AssertionError, match="missing:authority"):
        scenario.run_cell(cell, body)


def test_foreign_arch_cell_skips_first(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_stack() -> str:
        raise AssertionError("the stack must not be read")

    monkeypatch.setattr(tool_cells, "require_stack", no_stack)
    monkeypatch.setattr(tool_cells.platform, "machine", lambda: "x86_64")
    cell = _cell("systems.ssh_info", "local-libvirt", "ppc64le")

    async def never(*_: object) -> None:
        raise AssertionError("the scenario must not run")

    with pytest.raises(pytest.skip.Exception, match="ppc64le"):
        tool_cells.run_tool_cell(cell, never)


def test_lane_target_prepares_once_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    issuer = cast(OidcIssuer, object())
    target = tool_cells.LaneTarget("cov-t", "alloc", "sys", {"guest_arch": "x86_64"}, ("a" * 64,))

    async def prepared(_run: CellRun, _target: CellRun, url: str, *_: object) -> object:
        calls.append(url)
        return target

    monkeypatch.setattr(tool_cells, "_TARGETS", {})
    monkeypatch.setattr(tool_cells, "_provision_target", prepared)
    for run in (_run(tmp_path, "authentication"), _run(tmp_path, "authentication")):
        assert asyncio.run(tool_cells.lane_target(run, "u1", issuer, "db")) is target
        assert run.observed["guest_arch"] == "x86_64"
        assert run.artifacts == ["a" * 64]
    assert calls == ["u1"]

    async def blocked(_run: CellRun, into: CellRun, *_: object) -> object:
        into.artifacts.append("b" * 64)
        raise ScenarioStop(Outcome.BLOCKED, "no lane image")

    monkeypatch.setattr(tool_cells, "_provision_target", blocked)
    for _ in range(2):
        run = _run(tmp_path, "authentication")
        with pytest.raises(ScenarioStop, match="no lane image"):
            asyncio.run(tool_cells.lane_target(run, "u2", issuer, "db"))
        assert run.artifacts == ["b" * 64]

    async def unsettled(_run: CellRun, into: CellRun, *_: object) -> object:
        into.prove("cleanup", {"system": "torn_down"})
        raise AssertionError("kept changing")

    monkeypatch.setattr(tool_cells, "_provision_target", unsettled)
    run = _run(tmp_path, "authentication")
    with pytest.raises(AssertionError, match="kept changing"):
        asyncio.run(tool_cells.lane_target(run, "u4", issuer, "db"))
    assert len(run.artifacts) == 1

    async def broken(*_: object) -> object:
        raise RuntimeError("provision failed")

    monkeypatch.setattr(tool_cells, "_provision_target", broken)
    with pytest.raises(AssertionError, match="provision failed"):
        asyncio.run(tool_cells.lane_target(_run(tmp_path, "authentication"), "u3", issuer, "db"))


def test_run_cell_records_the_proven_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    identity = RunIdentity(sha, "b" * 64, "fedora:44", "x86_64", True, {"server": sha})
    monkeypatch.setattr(scenario, "require_stack", lambda: "http://stack.test/mcp")
    monkeypatch.setattr(scenario, "run_identity", lambda _url: identity)
    monkeypatch.setattr(scenario, "prerequisites", lambda: (object(), "postgresql://x"))
    monkeypatch.setattr(scenario, "evidence_root", lambda: tmp_path)
    cell = replace(_run(tmp_path, "authentication").cell, node_id="tests/x.py::test_x")

    async def body(run: CellRun, *_: object) -> None:
        for name in cell.assertions:
            run.prove(name, {})

    scenario.run_cell(cell, body, proves=Outcome.REJECTION)
    (record,) = (tmp_path / "records").glob("*.json")
    assert json.loads(record.read_text())["outcome"] == "rejection"


def test_missing_issuer_fails_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_issuer() -> None:
        pytest.skip("KDIVE_OIDC_ISSUER unset")

    monkeypatch.setattr(tool_cells, "require_stack", lambda: "http://stack.test/mcp")
    monkeypatch.setattr(tool_cells, "require_issuer", no_issuer)

    async def never(*_: object) -> None:
        raise AssertionError("the scenario must not run")

    with pytest.raises(pytest.fail.Exception, match="KDIVE_OIDC_ISSUER"):
        tool_cells.run_tool_cell(_run(tmp_path, "authentication").cell, never)


def test_published_image_binds_the_product(tmp_path: Path) -> None:
    image = tmp_path / "image.qcow2"
    image.write_bytes(b"lane")
    cells = [c for c in build_contract().cells if c.operation == "images.publish"]
    publish = {
        c.exposure: _bound(c)
        for c in cells
        if c.kind == "functional" and c.provider == "local-libvirt" and c.guest_arch == "x86_64"
    }
    reject = _bound(_cell("images.publish", "local-libvirt", "x86_64", "rejection"))
    install = _bound(_cell("runs.install", "local-libvirt", "x86_64"))
    inputs = bindings(
        "a" * 40,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix="b" * 64,
        cells=[*publish.values(), reject, install],
        staged=lambda _name: image,
    )
    guests = {e: inputs.cells[c.id].guest_os for e, c in publish.items()}
    assert guests == {"direct": "fedora:43", "gateway": "rocky:9"}
    for cell in publish.values():
        product = inputs.cells[cell.id]
        assert (product.guest_arch, product.accelerator, product.image_sha256) == (
            "x86_64",
            "kvm",
            None,
        )
    lane = hashlib.sha256(b"lane").hexdigest()
    assert inputs.cells[reject.id].guest_os == "fedora:44"
    assert inputs.cells[reject.id].image_sha256 == lane
    assert inputs.cells[install.id].image_sha256 == lane


def _refusal(arch: str, exposure: str = "direct") -> Cell:
    return next(
        c
        for c in build_contract().cells
        if c.operation == "images.publish"
        and c.provider == "remote-libvirt"
        and c.kind == "unsupported"
        and c.host_arch == arch
        and (c.configuration, c.exposure) == ("default", exposure)
    )


def test_admission_refusals_bind_the_server_host_alone() -> None:
    # ADR-0759: the refusal involves no provider host, guest, image or accelerator.
    native, foreign = _bound(_refusal("x86_64")), _bound(_refusal("ppc64le"))
    host = RemoteHost("operator@provider.example", "default", "rocky:10.2", "x86_64", "kvm")

    def no_probe(*_: object) -> str:
        raise AssertionError("an admission refusal must not probe a provider volume")

    for remote in (None, host):
        inputs = bindings(
            "a" * 40,
            host_os="fedora:44",
            host_arch="x86_64",
            matrix="b" * 64,
            cells=[native, foreign],
            staged=lambda _name: None,
            remote=remote,
            digest=no_probe,
            volumes=no_probe,
        )
        assert set(inputs.cells) == {native.id}
        assert inputs.cells[native.id].model_dump(exclude_none=True) == {
            "host_os": "fedora:44",
            "host_arch": "x86_64",
            "accelerator": "none",
        }


def test_admission_refusal_runs_on_its_server_arch(monkeypatch: pytest.MonkeyPatch) -> None:
    def stack() -> str:
        raise RuntimeError("stack read")

    async def never(*_: object) -> None:
        raise AssertionError("the scenario must not run")

    monkeypatch.setattr(tool_cells, "require_stack", stack)
    monkeypatch.setattr(tool_cells.platform, "machine", lambda: "x86_64")
    with pytest.raises(pytest.skip.Exception, match="ppc64le"):
        tool_cells.run_tool_cell(_refusal("ppc64le"), never)
    with pytest.raises(RuntimeError, match="stack read"):
        tool_cells.run_tool_cell(_refusal("x86_64"), never)


def _refusal_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cell: Cell,
    host_arch: str = "x86_64",
    candidate: str = "a" * 40,
    proves: Outcome = Outcome.UNSUPPORTED,
    via_frame: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Record ``cell`` through the real frame on a server-only stack, then qualify it."""
    contract = build_contract()
    contract = replace(contract, cells=(cell,))
    roles = {"server": candidate}
    identity = RunIdentity(candidate, contract.matrix_sha256, "fedora:44", host_arch, True, roles)
    monkeypatch.setattr(tool_cells, "require_stack", lambda: "http://stack.test/mcp")
    monkeypatch.setattr(tool_cells, "require_issuer", lambda: object())
    monkeypatch.setattr(tool_cells, "server_configuration", lambda *_: ("default", []))
    monkeypatch.setattr(tool_cells.platform, "machine", lambda: host_arch)
    monkeypatch.setattr(scenario, "require_stack", lambda: "http://stack.test/mcp")
    monkeypatch.setattr(scenario, "run_identity", lambda _url: identity)
    monkeypatch.setattr(scenario, "prerequisites", lambda: (object(), "postgresql://x"))
    monkeypatch.setattr(scenario, "evidence_root", lambda: tmp_path)

    async def body(run: CellRun, *_: object) -> None:
        for name in cell.assertions:
            run.prove(name, {})

    if via_frame:
        tool_cells.run_tool_cell(cell, body)
    else:
        scenario.run_cell(cell, body, proves=proves)
    (record,) = (tmp_path / "records").glob("*.json")
    result = Evidence.model_validate_json(record.read_text())
    inputs = bindings(
        "a" * 40,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix=contract.matrix_sha256,
        cells=[cell],
        staged=lambda _name: None,
    )
    (verdict,) = qualify(contract, inputs, [result]).cells
    return verdict.qualified, verdict.reasons


def test_admission_refusal_record_qualifies_without_guest_or_kvm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = _bound(_refusal("x86_64"))
    assert _refusal_verdict(tmp_path, monkeypatch, cell=cell, via_frame=True) == (True, ())


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"host_arch": "ppc64le"}, "input-context-mismatch"),
        ({"candidate": "c" * 40}, "candidate-mismatch"),
        ({"proves": Outcome.SUCCESS}, "outcome-kind-mismatch"),
        ({"rule": None}, "unsupported-without-reviewed-rule"),
    ],
)
def test_admission_refusal_record_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object], reason: str
) -> None:
    cell = _bound(_refusal("x86_64"))
    host_arch = str(change.get("host_arch", "x86_64"))
    candidate = str(change.get("candidate", "a" * 40))
    proves = cast(Outcome, change.get("proves", Outcome.UNSUPPORTED))
    if "rule" in change:
        cell = replace(cell, unsupported_reason=None)
    qualified, reasons = _refusal_verdict(
        tmp_path, monkeypatch, cell=cell, host_arch=host_arch, candidate=candidate, proves=proves
    )
    assert not qualified and reason in reasons


_REFUSAL = Rejection({"provider": "remote-libvirt", "name": "img"}, _GRANTS)
_REFUSED = ToolResponse.failure("img", ErrorCategory.NOT_IMPLEMENTED, detail="stage it")


def test_refusal_proves_the_exact_envelope_and_unchanged_state(tmp_path: Path) -> None:
    run = CellRun(_refusal("x86_64"), EvidenceWriter(tmp_path))
    caller = _Caller("gateway", result=_REFUSED)
    asyncio.run(prove_refusal(run, caller, _REFUSAL, "stage it", _snapshot("s", "s")))
    assert set(run.assertions) == {"effect", "cleanup"}
    assert caller.calls == [("images.publish", _REFUSAL.args)]
    assert len(run.artifacts) == 1  # the request, retained before the call


@pytest.mark.parametrize(
    "result",
    [
        _OK,
        ToolResponse.denied("img"),
        ToolResponse.failure("img", ErrorCategory.NOT_IMPLEMENTED, detail="other"),
        ToolResponse.failure("other", ErrorCategory.NOT_IMPLEMENTED, detail="stage it"),
        ToolResponse.failure(
            "img", ErrorCategory.NOT_IMPLEMENTED, detail="stage it", refs={"job": "j"}
        ),
        ToolResponse.failure(
            "img",
            ErrorCategory.NOT_IMPLEMENTED,
            detail="stage it",
            suggested_next_actions=["jobs.wait"],
        ),
        ConnectionError("transport"),
    ],
)
def test_refusal_fails_on_any_other_answer(
    tmp_path: Path, result: ToolResponse | Exception
) -> None:
    run = CellRun(_refusal("x86_64"), EvidenceWriter(tmp_path))
    caller = _Caller("direct", result=result)
    with pytest.raises((AssertionError, ConnectionError)):
        asyncio.run(prove_refusal(run, caller, _REFUSAL, "stage it", _snapshot("s", "s")))
    assert not run.assertions
    assert len(run.artifacts) == 1  # the failed proof still names the request


def test_refusal_fails_when_state_changed(tmp_path: Path) -> None:
    run = CellRun(_refusal("x86_64"), EvidenceWriter(tmp_path))
    caller = _Caller("direct", result=_REFUSED)
    with pytest.raises(AssertionError, match="changed protected state"):
        asyncio.run(prove_refusal(run, caller, _REFUSAL, "stage it", _snapshot("s", "t")))
    assert not run.assertions
