"""Functional authority tool cell framing (#3186) with its frame, lanes and HTTP faked."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack import authority_cells
from tests.integration.live_stack.authority_lane import AuthoritySystem
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import Grants, tool_cells
from tests.live_vm.installed_local_authority_support import (
    ActivationJob,
    OwnedResource,
    ResourceLedger,
)
from tests.live_vm.installed_remote_authority_support import RemoteAuthorityProofConfig

_SYSTEM = "11111111-2222-4333-8444-555555555555"
_PREFIX = "kdive-2151-aaaaaaaaaaaa-0123abcd"
_TREE = Path("/fixture/tree")
_MANIFEST: dict[str, Any] = {"arch": "x86_64"}


@cache
def _cell(tool: str, provider: str) -> Cell:
    return next(
        c
        for c in tool_cells((tool,))
        if c.kind == "functional" and c.provider == provider and c.guest_arch == "x86_64"
    )


def _async(result: object = None) -> Callable[..., Any]:
    async def coroutine(*_args: object, **_kwargs: object) -> object:
        return result

    return coroutine


def _system(remote: bool = False) -> AuthoritySystem:
    config: Any = type("Config", (), {"ownership_prefix": _PREFIX})()
    if remote:
        config = RemoteAuthorityProofConfig.model_construct(ownership_prefix=_PREFIX)
    return AuthoritySystem(
        cast(Any, object()), "cov-t", "alloc", _SYSTEM, config, ResourceLedger(_PREFIX)
    )


def _fake_lanes(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fixture() -> tuple[Path, dict[str, Any]]:
        calls.append("fixture")
        return _TREE, _MANIFEST

    async def frame(run: CellRun, *_args: object, project: str, body: Any) -> None:
        calls.append(f"frame:{project}")
        calls.append(f"effect:{sorted(await body(_system(run.cell.provider != 'local-libvirt')))}")

    async def observe_remote(*_args: object) -> None:
        calls.append("observe_remote_authority")

    monkeypatch.setattr(authority_cells, "_fixture", fixture)
    monkeypatch.setattr(authority_cells, "on_authority_system", frame)
    monkeypatch.setattr(authority_cells, "observe_remote_authority", observe_remote)


@pytest.mark.parametrize(
    ("tool", "provider"),
    [
        ("runs.release_external_boot", "local-libvirt"),
        ("runs.release_external_boot", "remote-libvirt"),
        ("ops.force_release", "local-libvirt"),
        ("ops.force_release", "remote-libvirt"),
        ("ops.resolve_recovery_orphan", "local-libvirt"),
        ("ops.resolve_recovery_orphan", "remote-libvirt"),
        ("systems.resolve_external_boot_conflict", "local-libvirt"),
        ("systems.resolve_external_boot_conflict", "remote-libvirt"),
    ],
)
def test_a_runnable_cell_frames_its_body_with_the_bound_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, provider: str
) -> None:
    calls: list[str] = []
    _fake_lanes(monkeypatch, calls)
    run = CellRun(_cell(tool, provider), EvidenceWriter(tmp_path))
    seen: list[object] = []

    async def body(_system: AuthoritySystem, tree: Path, manifest: object) -> dict[str, object]:
        seen.append((tree, manifest))
        return {"effect": True}

    asyncio.run(
        authority_cells.run_authority_cell(run, "url", cast(Any, None), "db", body, project="p")
    )
    remote = ["observe_remote_authority"] if provider == "remote-libvirt" else []
    assert calls == ["fixture", "frame:p", *remote, "effect:['effect']"]
    assert seen == [(_TREE, _MANIFEST)]


class _Caller:
    """An exposure that records its grants and answers with fixed envelopes."""

    def __init__(self, answer: ToolResponse | list[ToolResponse]) -> None:
        self.answer = answer
        self.grants: list[Grants] = []
        self.discovered: list[bool] = []

    @property
    def exposure(self) -> str:
        return "gateway"

    def token(self, grants: Grants) -> str:
        self.grants.append(grants)
        return "token"

    async def call(
        self, _tool: str, _args: Mapping[str, object], _token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        self.discovered.append(discover)
        return self.answer

    async def post(self, _tool: str, _args: Mapping[str, object], _token: str) -> int:
        raise AssertionError("not used")


def test_exposed_returns_error_envelopes_unchanged_after_discovery() -> None:
    refusal = ToolResponse(object_id="a", status="error", error_category="conflict")
    caller = _Caller(refusal)
    call = authority_cells.exposed(cast(Any, caller), "token")
    assert asyncio.run(call("ops.force_release", {})) is refusal
    assert caller.discovered == [True]
    many = authority_cells.exposed(cast(Any, _Caller([refusal, refusal])), "token")
    with pytest.raises(AssertionError, match="2 envelopes"):
        asyncio.run(many("ops.force_release", {}))


def test_the_release_is_called_by_a_contributor_of_the_frame_project() -> None:
    grants = authority_cells.contributor_grants("cov-1")
    assert (grants.projects, dict(grants.roles), grants.platform_roles) == (
        ("cov-1",),
        {"cov-1": "contributor"},
        (),
    )


def test_the_ops_tools_are_called_by_an_outside_platform_admin() -> None:
    grants = authority_cells.platform_admin_grants()
    assert (dict(grants.roles), grants.platform_roles) == ({}, ("platform_admin",))


def _fake_release(
    monkeypatch: pytest.MonkeyPatch,
    *,
    job_state: str = "succeeded",
    after: dict[str, object] | None = None,
    receipt: bool = False,
) -> list[str]:
    calls: list[str] = []
    before: dict[str, object] = {"rootfs": ["overlay"], "domain": True}
    states = iter([before, after or before])

    def write_upload(upload: Path) -> None:
        upload.mkdir()
        (upload / "upload.json").write_text(json.dumps({"build_id": "c" * 40}))
        (upload / "effective_config").write_text("CONFIG_X=y\n")

    async def activate(system: AuthoritySystem, **kwargs: Any) -> ActivationJob:
        write_upload(kwargs["evidence_dir"])
        system.ledger.record(OwnedResource(kind="run", identity="run-1"))
        calls.append(f"activate:{kwargs['kernel_tree']}")
        return ActivationJob("inv", "run-1", "activate-job")

    patches: dict[str, Any] = {
        "_authority_state": lambda _system: next(states),
        "activate": activate,
        "drain_job": lambda *a: _async()(calls.append(f"drain:{a[2]}")),
        "job_claim": _async((job_state, 1)),
        "assert_root_release_completion": _async(),
        "activation_of": _async("act-1"),
        "receipt_state": lambda *_a: type("Receipt", (), {"present": receipt})(),
        "uploaded_kernel_inputs": lambda *_a: {"kernel_build_id": "c" * 40},
    }
    for name, value in patches.items():
        monkeypatch.setattr(authority_cells, name, value)
    return calls


def _released(
    tmp_path: Path, answer: ToolResponse | Exception
) -> tuple[CellRun, dict[str, object]]:
    run = CellRun(_cell("runs.release_external_boot", "local-libvirt"), EvidenceWriter(tmp_path))

    async def call(tool: str, args: dict[str, object]) -> ToolResponse:
        assert (tool, args) == ("runs.release_external_boot", {"run_id": "run-1"})
        if isinstance(answer, Exception):
            raise answer
        return answer

    effect = asyncio.run(
        authority_cells.released_external_boot(run, call, "db", _system(), _TREE, _MANIFEST)
    )
    return run, effect


_ADMITTED = ToolResponse(object_id="release-job", status="queued")


def test_release_effect_is_observed_outside_the_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_release(monkeypatch)
    run, effect = _released(tmp_path, _ADMITTED)
    assert calls == [f"activate:{_TREE}", "drain:release-job"]
    assert effect == {
        "release_job": "succeeded",
        "root_release": "complete",
        "cleanup_receipt": "absent",
        "authority_state": "restored",
    }
    assert run.observed["kernel_build_id"] == "c" * 40


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ({"job_state": "failed"}, "release job is failed"),
        ({"after": {"rootfs": ["overlay", "leftover"], "domain": True}}, "authority state"),
        ({"receipt": True}, "receipt survived"),
    ],
)
def test_release_effect_rejects_an_unreclaimed_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: dict[str, Any], message: str
) -> None:
    _fake_release(monkeypatch, **fault)
    with pytest.raises(AssertionError, match=message):
        _released(tmp_path, _ADMITTED)


@pytest.mark.parametrize(
    ("jobs", "activations", "expected"),
    [
        (["release-job"], 1, ["drain:release-job"]),  # admitted, then the call raised
        ([], 1, ["admin-release", "drain:admin-job"]),  # activated, never released
        ([], 0, []),  # nothing was activated
    ],
)
def test_a_failed_release_settles_from_the_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    jobs: list[str],
    activations: int,
    expected: list[str],
) -> None:
    calls = _fake_release(monkeypatch)
    monkeypatch.setattr(authority_cells, "release_jobs", _async(jobs))
    monkeypatch.setattr(authority_cells, "activated", _async(activations > 0))

    async def admin_release(_op: object, run_id: str) -> ToolResponse:
        calls.append("admin-release")
        assert run_id == "run-1"
        return ToolResponse(object_id="admin-job", status="queued")

    monkeypatch.setattr(authority_cells, "release_external_boot", admin_release)
    with pytest.raises(TimeoutError, match="lost reply"):
        _released(tmp_path, TimeoutError("lost reply"))
    assert calls[1:] == expected
