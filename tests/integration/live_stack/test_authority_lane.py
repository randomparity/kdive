"""The authority-owned System frame's orchestration, with its host and HTTP boundaries faked."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import SystemState
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration._seed import seed_granted_allocation, seed_running_run, seed_system
from tests.integration.conftest import open_pool
from tests.integration.live_stack import authority_lane
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun, ScenarioStop

_REVISION = "a" * 40
_SYSTEM = "11111111-2222-4333-8444-555555555555"
_LOCAL_ENTRY = """
[[local_libvirt]]
name = "lane-local"
cost_class = "local"
host_uri = "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
"""
_PLAIN_INVENTORY = "schema_version = 2\n" + _LOCAL_ENTRY
_ROUTED_INVENTORY = _PLAIN_INVENTORY + 'authority_instance = "lane-authority"\n'
_INVENTORY = {"authority_domains": ["kdive-other"], "authority_roots": {}, "worker_domains": []}


def _cell(provider: str = "local-libvirt") -> Cell:
    return Cell(
        "frame",
        "tool/frame",
        3184,
        "frame",
        "frame",
        ("effect", "cleanup"),
        provider=provider,
        guest_arch="x86_64",
    )


def _run(tmp_path: Path, provider: str = "local-libvirt") -> CellRun:
    return CellRun(_cell(provider), EvidenceWriter(tmp_path))


def _config(tmp_path: Path, mode: str = "fixture", **extra: object) -> Path:
    path = tmp_path / "lane.json"
    document = {
        "installed_revision": _REVISION,
        "authority_service": "kdive-external-boot-authority.service",
        "system_mode": mode,
        **extra,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


class _Op:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def call_tool(self, name: str, **_args: object) -> ToolResponse:
        self.calls.append(name)
        if name == "systems.provision":
            return ToolResponse(object_id="job", status="queued", data={"system_id": _SYSTEM})
        if name == "jobs.wait":
            return ToolResponse(object_id="job", status="succeeded")
        if name == "investigations.get":
            return ToolResponse(object_id="inv", status="active")
        return ToolResponse(object_id="alloc", status="ok")


def _fake_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, mode: str = "fixture"
) -> list[str]:
    """Replace every host, database and HTTP boundary with a recorder; return the call log."""
    calls: list[str] = []
    monkeypatch.setenv(authority_lane.CONFIG_ENV, str(_config(tmp_path, mode)))
    inventory = tmp_path / "systems.toml"
    inventory.write_text(_PLAIN_INVENTORY, encoding="utf-8")
    monkeypatch.setenv(authority_lane.SYSTEMS_TOML_ENV, str(inventory))

    def record(name: str, result: object = None) -> Any:
        def sync(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            return result

        return sync

    def arecord(name: str, result: object = None) -> Any:
        async def coroutine(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            return result

        return coroutine

    @asynccontextmanager
    async def over_http(_url: str, _token: str) -> AsyncIterator[_Op]:
        yield _Op(calls)

    async def fixture(_db: str, config: Any, ledger: Any) -> None:
        calls.append(f"fixture-{config.fixture_mode}")
        if config.fixture_mode == "create":
            for name in authority_lane.fixture_names(_SYSTEM):
                ledger.record(authority_lane.OwnedResource(kind="authority-fixture", identity=name))

    entry = type("Entry", (), {"arch": "x86_64", "distro": "fedora", "version": "44"})()
    patches: dict[str, Any] = {
        "lane_image": lambda: ("lane", entry),
        "require_lane": record("require_lane", "running"),
        "require_evidence_read_access": arecord("require_evidence_read_access"),
        "lane_inventory": record("lane_inventory", dict(_INVENTORY)),
        "mint_role_token": lambda *_a, **_k: "token",
        "seed_metering": arecord("seed_metering"),
        "acquire_image": arecord("acquire_image", {"digest": "sha256:" + "b" * 64}),
        "capacity_in_use": arecord("capacity_in_use", 0),
        "await_system_state": arecord("await_system_state"),
        "domain_xml": record("domain_xml", "<domain/>"),
        "domain_disks": lambda _xml: ["/worker/overlay.qcow2"],
        "require_root_provenance": arecord("require_root_provenance"),
        "allocated_resource": arecord("allocated_resource", "local"),
        "local_authority_instance_for_resource": lambda _name: None,
        "provision_authority_fixture": fixture,
        "require_authority_artifact_confinement": record("confinement"),
        "authority_inventory": record(
            "authority_inventory",
            authority_lane.AuthorityInventory([], {}, authority_lane.DomainFacts(True, "kvm")),
        ),
        "worker_domains": record("worker_domains", []),
        "drain_system_jobs": arecord("drain_system_jobs", ["succeeded"]),
        "remove_authority_fixture": _removal(calls),
        "release_and_verify": arecord("release_and_verify", {"system": "torn_down"}),
    }
    for name, value in patches.items():
        monkeypatch.setattr(authority_lane, name, value)
    monkeypatch.setattr(authority_lane.LiveStackClient, "over_http", over_http)
    return calls


def record_async(calls: list[str], name: str) -> Any:
    async def coroutine(*_args: object, **_kwargs: object) -> None:
        calls.append(name)

    return coroutine


def _removal(calls: list[str]) -> Any:
    def remove(_config: object, ledger: Any) -> None:
        calls.append("remove_authority_fixture")
        calls.extend(f"remove {r.identity}" for r in reversed(ledger.resources))

    return remove


async def _body_ok(system: authority_lane.AuthoritySystem) -> dict[str, object]:
    assert system.system_id == _SYSTEM
    return {"observed": True}


class _BodyFailed(Exception):
    pass


async def _body_fails(_system: authority_lane.AuthoritySystem) -> dict[str, object]:
    raise _BodyFailed


def _frame(run: CellRun, body: authority_lane.AuthorityBody) -> None:
    asyncio.run(
        authority_lane.on_authority_system(
            run, "u", cast(OidcIssuer, object()), "db", project="cov-t", body=body
        )
    )


def test_remote_cell_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    with pytest.raises(ScenarioStop, match="#3209") as stop:
        _frame(_run(tmp_path, "remote-libvirt"), _body_ok)
    assert stop.value.outcome is Outcome.BLOCKED
    assert calls == []


def test_unset_config_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    monkeypatch.delenv(authority_lane.CONFIG_ENV)
    with pytest.raises(ScenarioStop, match=authority_lane.CONFIG_ENV):
        _frame(_run(tmp_path), _body_ok)
    assert calls == []


def test_public_mode_is_blocked_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_lane(monkeypatch, tmp_path, mode="public")
    with pytest.raises(ScenarioStop) as stop:
        _frame(_run(tmp_path), _body_ok)
    assert stop.value.outcome is Outcome.BLOCKED
    assert "#3144" in str(stop.value) and "#3148" in str(stop.value)
    assert calls == []


def test_missing_lane_inventory_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    monkeypatch.delenv(authority_lane.SYSTEMS_TOML_ENV)
    with pytest.raises(ScenarioStop, match=authority_lane.SYSTEMS_TOML_ENV) as stop:
        _frame(_run(tmp_path), _body_ok)
    assert stop.value.outcome is Outcome.BLOCKED
    assert calls == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [(_ROUTED_INVENTORY, "authority_instance"), ("schema_version = 1\n", "is invalid")],
    ids=["all-routed", "invalid"],
)
def test_unusable_lane_inventory_is_blocked_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str, reason: str
) -> None:
    """#3215: an all-routed or invalid inventory stops before any read, allocation or write."""
    calls = _fake_lane(monkeypatch, tmp_path)
    (tmp_path / "systems.toml").write_text(content, encoding="utf-8")
    with pytest.raises(ScenarioStop) as stop:
        _frame(_run(tmp_path), _body_ok)
    assert stop.value.outcome is Outcome.BLOCKED
    assert reason in str(stop.value)
    assert calls == []


def test_inventory_without_local_entries_proceeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    (tmp_path / "systems.toml").write_text("schema_version = 2\n", encoding="utf-8")
    _frame(_run(tmp_path), _body_ok)
    assert "allocations.request" in calls


def test_gate_failure_creates_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)

    def refuse(*_args: object) -> str:
        raise AssertionError("deployed worker revision does not match")

    monkeypatch.setattr(authority_lane, "require_lane", refuse)
    with pytest.raises(AssertionError, match="does not match"):
        _frame(_run(tmp_path), _body_ok)
    assert "allocations.request" not in calls


def test_success_orders_effects_and_proves_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    run = _run(tmp_path)
    _frame(run, _body_ok)
    order = [
        "require_lane",
        "require_evidence_read_access",
        "lane_inventory",
        "allocations.request",
        "systems.provision",
        "require_root_provenance",
        "fixture-create",
        "confinement",
        "fixture-verify-existing",
        "authority_inventory",
        "drain_system_jobs",
        "remove_authority_fixture",
        "systems.teardown",
        "release_and_verify",
        "lane_inventory",
    ]
    seen = [c for c in calls if c in set(order)]
    assert seen == order
    assert set(run.assertions) == {"effect", "cleanup"}
    assert run.observed["accelerator"] == "kvm"
    assert run.observed["image_sha256"] == "b" * 64


def test_routed_resource_is_blocked_and_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    monkeypatch.setattr(authority_lane, "local_authority_instance_for_resource", lambda _n: "a")
    monkeypatch.setattr(authority_lane, "_await_released", record_async(calls, "released"))
    with pytest.raises(ScenarioStop, match="authority_instance") as stop:
        _frame(_run(tmp_path), _body_ok)
    assert stop.value.outcome is Outcome.BLOCKED
    assert "systems.provision" not in calls
    assert calls.index("allocations.request") < calls.index("allocations.release")
    assert calls[-1] == "lane_inventory"


def test_partial_fixture_create_removes_the_fixed_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)

    async def failed_create(_db: str, _config: object, _ledger: object) -> None:
        raise RuntimeError("authority fixture create failed")

    monkeypatch.setattr(authority_lane, "provision_authority_fixture", failed_create)
    with pytest.raises(RuntimeError, match="create failed"):
        _frame(_run(tmp_path), _body_ok)
    removed = [
        c.removeprefix("remove ")
        for c in calls
        if c.startswith("remove /") or c.startswith("remove kdive-")
    ]
    assert removed == list(reversed(authority_lane.fixture_names(_SYSTEM)))
    assert calls.index("remove_authority_fixture") < calls.index("systems.teardown")


def test_cancellation_still_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)

    async def cancelled(_system: authority_lane.AuthoritySystem) -> dict[str, object]:
        raise asyncio.CancelledError

    run = _run(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        _frame(run, cancelled)
    assert "release_and_verify" in calls
    assert len(run.artifacts) == 1


def test_body_failure_still_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    run = _run(tmp_path)
    with pytest.raises(_BodyFailed):
        _frame(run, _body_fails)
    assert calls.index("remove_authority_fixture") < calls.index("release_and_verify")
    assert "cleanup" not in run.assertions
    assert len(run.artifacts) == 1


def test_cleanup_failure_fails_the_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_lane(monkeypatch, tmp_path)

    async def stranded(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("allocation stayed active, not released")

    monkeypatch.setattr(authority_lane, "release_and_verify", stranded)
    run = _run(tmp_path)
    with pytest.raises(AssertionError, match="not released"):
        _frame(run, _body_ok)
    assert "cleanup" not in run.assertions


def test_body_and_cleanup_failures_are_grouped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_lane(monkeypatch, tmp_path)

    def stuck(*_args: object) -> None:
        raise RuntimeError("authority fixture remove failed")

    monkeypatch.setattr(authority_lane, "remove_authority_fixture", stuck)
    run = _run(tmp_path)
    with pytest.raises(ExceptionGroup) as group:
        _frame(run, _body_fails)
    assert type(group.value) is ExceptionGroup
    first, second = group.value.exceptions
    assert isinstance(first, _BodyFailed) and isinstance(second, RuntimeError)
    assert len(run.artifacts) == 1


def test_inventory_drift_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_lane(monkeypatch, tmp_path)
    states = iter([dict(_INVENTORY), {**_INVENTORY, "authority_domains": ["kdive-other", "x"]}])
    monkeypatch.setattr(authority_lane, "lane_inventory", lambda: next(states))
    with pytest.raises(AssertionError, match="authority_domains"):
        _frame(_run(tmp_path), _body_ok)


def test_lane_config_rejects_unknown_fields_and_unsafe_files(tmp_path: Path) -> None:
    good = _config(tmp_path)
    loaded = authority_lane.load_lane_config({authority_lane.CONFIG_ENV: str(good)})
    assert loaded is not None and loaded.system_mode == "fixture"
    assert authority_lane.load_lane_config({}) is None
    extra = _config(tmp_path, ssh_target="host")
    with pytest.raises(ValueError, match="ssh_target"):
        authority_lane.load_lane_config({authority_lane.CONFIG_ENV: str(extra)})
    good.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe metadata"):
        authority_lane.load_lane_config({authority_lane.CONFIG_ENV: str(good)})


def test_authority_inventory_is_fixed_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    outputs = iter(
        [
            json.dumps({"domains": ["kdive-x"], "roots": {"/r": ["a"]}, "system": None}),
            json.dumps({"domains": "kdive-x", "roots": {}, "system": None}),
        ]
    )

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, next(outputs), "")

    monkeypatch.setattr(authority_lane.subprocess, "run", run)
    facts = authority_lane.authority_inventory(_SYSTEM)
    assert facts == authority_lane.AuthorityInventory(["kdive-x"], {"/r": ["a"]}, None)
    assert seen[0][:5] == ["sudo", "-n", authority_lane._AUTHORITY_PYTHON, "-I", "-c"]
    assert seen[0][-1] == _SYSTEM
    with pytest.raises(AssertionError, match="malformed"):
        authority_lane.authority_inventory()
    with pytest.raises(ValueError):
        authority_lane.authority_inventory("not-a-uuid")


def test_open_jobs_names_the_system_and_its_runs(migrated_url: str) -> None:
    async def scenario() -> list[str]:
        async with open_pool(migrated_url) as pool:
            allocation = await seed_granted_allocation(pool)
            system = await seed_system(pool, allocation, SystemState.READY)
            other = await seed_system(pool, allocation, SystemState.READY)
            run = await seed_running_run(pool, system)
            async with pool.connection() as conn:
                for key, state, payload in (
                    ("system", "queued", {"system_id": system}),
                    ("run", "queued", {"run_id": run}),
                    ("other", "queued", {"system_id": other}),
                    ("done", "canceled", {"system_id": system}),
                ):
                    await conn.execute(
                        "INSERT INTO jobs (id, kind, state, attempt, max_attempts, payload, "
                        "authorizing, dedup_key) VALUES (%s, 'install', %s, 0, 3, %s, %s, %s)",
                        (
                            uuid4(),
                            state,
                            Jsonb(payload),
                            Jsonb({"principal": "user-1", "project": "proj"}),
                            key,
                        ),
                    )
            return await authority_lane._open_jobs(migrated_url, system)

    assert len(asyncio.run(scenario())) == 2
