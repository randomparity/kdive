"""Unit tests of the ADR-0722 tool-cell harness (no stack needed)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackToolError, OidcIssuer, make_keypair
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import scenario, tool_cells
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    RECOVERY_TOOLS,
    Grants,
    HttpCaller,
    Rejection,
    bindings,
    claims_of,
    configuration_of,
    forge,
    prove_functional,
    prove_rejection,
    rejected_by_validation,
)

_OK = ToolResponse.success("x", "ok")
_GRANTS = Grants("agent", ("p",), {"p": "viewer"}, ("platform_auditor",))


def forge_source(grants: Grants) -> str:
    """A stand-in for a real-issuer token carrying ``grants``."""
    return make_keypair().create_token(
        subject=grants.subject,
        issuer="https://issuer.test",
        audience="kdive",
        additional_claims={"projects": list(grants.projects)},
        kid="issuer-key-1",
    )


@dataclass
class _Caller:
    exposure: str
    result: ToolResponse | Exception = field(default_factory=lambda: _OK)
    status: int = 401
    calls: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)
    issued: set[str] = field(default_factory=set)
    refuse_issued: bool = False

    def token(self, grants: Grants) -> str:
        token = forge_source(grants)
        self.issued.add(token)
        return token

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        self.calls.append((tool, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def post(self, tool: str, args: Mapping[str, object], token: str) -> int:
        self.calls.append((tool, claims_of(token)))
        return 200 if token in self.issued and not self.refuse_issued else self.status


def _run(tmp_path: Path, boundary: str) -> CellRun:
    cell = next(
        c
        for c in build_contract().cells
        if c.operation == "tools.search" and c.scenario_id.endswith("/" + boundary)
    )
    return CellRun(cell, EvidenceWriter(tmp_path))


def _snapshot(*states: object) -> Callable[[], Awaitable[object]]:
    values = iter(states)

    async def snap() -> object:
        return next(values)

    return snap


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


_EMPTY_PAGE = ToolResponse.collection("investigations", "ok", [])


def test_filtering_list_stops_blocked_naming_its_owner(tmp_path: Path) -> None:
    run = _run(tmp_path, "validation")
    filtered = Rejection({}, _GRANTS, filtered_by="#3108")
    with pytest.raises(ScenarioStop, match="#3108") as stop:
        asyncio.run(
            prove_rejection(
                run, _Caller("direct", _EMPTY_PAGE), "authorization", filtered, _snapshot(1, 1)
            )
        )
    assert stop.value.outcome is Outcome.BLOCKED
    assert "authorization" not in run.assertions
    retained = json.loads((run.writer.root / "artifacts" / run.artifacts[-1]).read_text())
    assert retained["filtered_by"] == "#3108" and "empty page" in retained["blocked"]


def test_filtering_list_that_rejects_still_qualifies(tmp_path: Path) -> None:
    run = _run(tmp_path, "validation")
    denied = ToolResponse.failure("x", ErrorCategory.AUTHORIZATION_DENIED)
    filtered = Rejection({}, _GRANTS, filtered_by="#3108")
    asyncio.run(
        prove_rejection(run, _Caller("direct", denied), "authorization", filtered, _snapshot(1, 1))
    )
    assert set(run.assertions) == {"authorization", "unchanged-state", "cleanup"}
    listed = ToolResponse.collection("investigations", "ok", [ToolResponse.success("i", "open")])
    with pytest.raises(AssertionError, match="was not rejected"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "validation"),
                _Caller("direct", listed),
                "project-isolation",
                filtered,
                _snapshot(1, 1),
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
    cell = _bound(_cell("runs.install", "local-libvirt", "x86_64"))
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
