"""The authority-owned System frame's orchestration, with its host and HTTP boundaries faked."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration._seed import seed_granted_allocation, seed_running_run, seed_system
from tests.integration.conftest import open_pool
from tests.integration.live_stack import authority_lane
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity
from tests.integration.live_stack.remote_lifecycle import REMOTE_REPRESENTATIVES, RemoteHost
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import _lane_contexts, tool_cells
from tests.live_vm.installed_remote_authority_support import (
    RemoteAuthorityInventory,
    RemoteAuthorityProofConfig,
    RemoteDomain,
)

_REVISION = "a" * 40
_SYSTEM = "11111111-2222-4333-8444-555555555555"
_DIGEST = "sha256:" + "d" * 64
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
    # Operator decision D1 (#3186): every local record discloses the fixture-mode System.
    effect = json.loads((tmp_path / "artifacts" / run.assertions["effect"]).read_text())
    assert effect["system_provenance"] == authority_lane.FIXTURE_PROVENANCE
    assert "#3144" in effect["system_provenance"]
    assert "real authority" in effect["system_provenance"]


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


def test_observer_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    roots = {authority_lane._AUTHORITY_ROOTFS: ["residue"], authority_lane._AUTHORITY_CONSOLE: []}
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"domains": ["unrelated"], "roots": roots, "system": None}), ""
        )

    monkeypatch.setattr(authority_lane.subprocess, "run", run)
    assert authority_lane.authority_inventory(_SYSTEM).roots == roots
    assert seen[0][0] == [
        "sudo",
        "-n",
        "-u",
        "kdive-authority-observer",
        "--",
        "/usr/bin/python3",
        "-I",
        "-S",
        "/usr/libexec/kdive-authority-inventory-client.py",
        _SYSTEM,
    ]
    assert seen[0][1]["timeout"] == 120.0
    with pytest.raises(ValueError):
        authority_lane.authority_inventory("not-a-uuid")
    assert len(seen) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "missing-root",
        "extra-root",
        "duplicate",
        "nested-extra",
        "active-int",
        "empty-type",
        "missing-system",
        "unexpected-system",
        "root-type",
    ],
)
def test_observer_schema(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    data: Any = {
        "domains": ["kdive-" + _SYSTEM],
        "roots": {authority_lane._AUTHORITY_ROOTFS: [], authority_lane._AUTHORITY_CONSOLE: []},
        "system": {"active": False, "type": "kvm"},
    }
    if fault == "missing-root":
        del data["roots"][authority_lane._AUTHORITY_ROOTFS]
    elif fault == "extra-root":
        data["roots"]["/other"] = []
    elif fault == "nested-extra":
        data["system"]["other"] = False
    elif fault == "active-int":
        cast(Any, data["system"])["active"] = 1
    elif fault == "empty-type":
        data["system"]["type"] = ""
    elif fault == "missing-system":
        data["system"] = None
    elif fault == "unexpected-system":
        data["domains"] = []
    elif fault == "root-type":
        data["roots"][authority_lane._AUTHORITY_ROOTFS] = [1]
    raw = json.dumps(data)
    if fault == "duplicate":
        raw = raw.replace('"active": false', '"active": true, "active": false')
    monkeypatch.setattr(
        authority_lane.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, raw, ""),
    )
    with pytest.raises(AssertionError):
        authority_lane.authority_inventory(_SYSTEM)


def test_observer_cleanup_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    cleanup = OSError("private observer failure")
    observations = iter([dict(_INVENTORY), cleanup])

    def inventory():
        result = next(observations)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(authority_lane, "lane_inventory", inventory)
    primary = RuntimeError("primary")

    async def body(_system):
        raise primary

    run = _run(tmp_path)
    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(
            authority_lane.on_authority_system(
                run, "http://lane", cast(Any, None), "db", project="project", body=body
            )
        )
    assert caught.value.exceptions == (primary, cleanup)
    assert "allocations.request" in calls
    assert "private observer failure" not in str(run.artifacts)


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


_FILE_PREFIX = "kdive-2151-" + "a" * 12 + "-00000000"
_REMOTE_INVENTORY = {"remote_authority_domains": ["kdive-other"], "remote_authority_volumes": []}


def _remote_config(tmp_path: Path) -> Path:
    path = tmp_path / "remote.json"
    document = {
        "installed_revision": _REVISION,
        "project": "kdive-2151-project",
        "resource_name": "remote-authority",
        "authority_instance": "remote-instance",
        "ownership_prefix": _FILE_PREFIX,
        "ssh_target": "authority-host",
        "authority_service": "kdive-external-boot-authority.service",
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


def _route(instance: str | None) -> Any:
    authority = None if instance is None else SimpleNamespace(authority_instance=instance)
    return lambda _name: SimpleNamespace(authority=authority)


def _fake_remote_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seen: list[authority_lane.AuthoritySystem]
) -> tuple[list[str], authority_lane.AuthorityBody]:
    """Replace the remote host, database and HTTP boundaries; return the call log and a body."""
    calls: list[str] = []
    monkeypatch.setenv(authority_lane.REMOTE_CONFIG_ENV, str(_remote_config(tmp_path)))
    inventory = tmp_path / "systems.toml"
    inventory.write_text("", encoding="utf-8")
    monkeypatch.setenv(authority_lane.SYSTEMS_TOML_ENV, str(inventory))
    monkeypatch.setattr(authority_lane, "VOLUME_SWEEP_DEADLINE_S", 0.2)
    monkeypatch.setattr(authority_lane, "VOLUME_SWEEP_POLL_S", 0.0)

    def arecord(name: str, result: object = None) -> Any:
        async def coroutine(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            return result

        return coroutine

    def observe(_config: object, system_id: str | None = None) -> RemoteAuthorityInventory:
        calls.append("remote_inventory" if system_id is None else "remote_inventory(system)")
        torn = calls.count("systems.teardown") == calls.count("systems.provision")
        running = system_id is not None and not torn
        return RemoteAuthorityInventory(
            "x86_64", ["kdive-other"], [], RemoteDomain(True, "kvm") if running else None
        )

    def require(config: Any, _url: str) -> None:
        calls.append("require_remote_lane")
        assert config.ownership_prefix != _FILE_PREFIX

    @asynccontextmanager
    async def over_http(_url: str, _token: str) -> AsyncIterator[_Op]:
        yield _Op(calls)

    async def body(system: authority_lane.AuthoritySystem) -> dict[str, object]:
        calls.append("body")
        seen.append(system)
        return {"observed": True}

    patches: dict[str, Any] = {
        "remote_config_for_resource": _route("remote-instance"),
        "require_remote_lane": require,
        "require_evidence_read_access": arecord("require_evidence_read_access"),
        "remote_root": arecord("remote_root", ("resource-id", "image", "sha256:" + "c" * 64)),
        "remote_authority_inventory": observe,
        "mint_role_token": lambda *_a, **_k: "token",
        "seed_metering": arecord("seed_metering"),
        "capacity_in_use": arecord("capacity_in_use", 0),
        "await_system_state": arecord("await_system_state"),
        "require_root_provenance": arecord("require_root_provenance"),
        "drain_system_jobs": arecord("drain_system_jobs", ["succeeded"]),
        "_await_released": arecord("released"),
    }
    for name, value in patches.items():
        monkeypatch.setattr(authority_lane, name, value)
    monkeypatch.setattr(authority_lane.LiveStackClient, "over_http", over_http)
    return calls, body


def _remote(tmp_path: Path) -> CellRun:
    return _run(tmp_path, "remote-libvirt")


def test_remote_success_orders_effects_and_proves_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[authority_lane.AuthoritySystem] = []
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, seen)
    run = _remote(tmp_path)
    _frame(run, body)
    order = [
        "require_remote_lane",
        "require_evidence_read_access",
        "remote_inventory",
        "remote_root",
        "allocations.request",
        "systems.provision",
        "require_root_provenance",
        "remote_inventory(system)",
        "body",
        "drain_system_jobs",
        "systems.teardown",
        "remote_inventory(system)",
        "allocations.release",
        "remote_inventory",
    ]
    assert [c for c in calls if c in set(order)] == order
    assert set(run.assertions) == {"effect", "cleanup"}
    assert run.observed["accelerator"] == "kvm"
    assert run.observed["image_sha256"] == "c" * 64
    assert seen[0].system_id == _SYSTEM
    assert isinstance(seen[0].config, RemoteAuthorityProofConfig)


def test_remote_prefix_is_minted_per_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[authority_lane.AuthoritySystem] = []
    _calls, body = _fake_remote_lane(monkeypatch, tmp_path, seen)
    _frame(_remote(tmp_path), body)
    _frame(_remote(tmp_path), body)
    prefixes = [system.config.ownership_prefix for system in seen]
    assert len(set(prefixes)) == 2 and _FILE_PREFIX not in prefixes
    assert all(p.startswith("kdive-2151-" + "a" * 12 + "-") for p in prefixes)
    assert all(system.ledger.prefix == system.config.ownership_prefix for system in seen)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("unset", authority_lane.REMOTE_CONFIG_ENV),
        ("no-inventory", authority_lane.SYSTEMS_TOML_ENV),
        ("unrouted", "authority_instance"),
        ("other-instance", "authority_instance"),
        ("undeclared", "authority_instance"),
    ],
)
def test_remote_selection_stops_before_anything_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str, match: str
) -> None:
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])
    if change == "unset":
        monkeypatch.delenv(authority_lane.REMOTE_CONFIG_ENV)
    elif change == "no-inventory":
        monkeypatch.delenv(authority_lane.SYSTEMS_TOML_ENV)
    elif change == "undeclared":

        def undeclared(_name: str) -> object:
            raise CategorizedError("no instance", category=ErrorCategory.CONFIGURATION_ERROR)

        monkeypatch.setattr(authority_lane, "remote_config_for_resource", undeclared)
    else:
        instance = None if change == "unrouted" else "another-instance"
        monkeypatch.setattr(authority_lane, "remote_config_for_resource", _route(instance))
    with pytest.raises(ScenarioStop, match=match) as stop:
        _frame(_remote(tmp_path), body)
    assert stop.value.outcome is Outcome.BLOCKED
    assert calls == []


def test_remote_foreign_host_arch_is_blocked_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        authority_lane,
        "remote_authority_inventory",
        lambda *_a: RemoteAuthorityInventory("ppc64le", [], [], None),
    )
    with pytest.raises(ScenarioStop, match="ppc64le") as stop:
        _frame(_remote(tmp_path), body)
    assert stop.value.outcome is Outcome.BLOCKED
    assert "allocations.request" not in calls


def test_remote_missing_root_is_blocked_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])

    async def no_root(*_args: object) -> tuple[str, str, str]:
        raise ScenarioStop(Outcome.BLOCKED, "no root")

    monkeypatch.setattr(authority_lane, "remote_root", no_root)
    with pytest.raises(ScenarioStop, match="no root"):
        _frame(_remote(tmp_path), body)
    assert "allocations.request" not in calls


@pytest.mark.parametrize(
    ("left", "match"),
    [
        (RemoteAuthorityInventory("x86_64", [], [], RemoteDomain(False, "kvm")), "domain remains"),
        (
            RemoteAuthorityInventory("x86_64", [], [f"kdive-{_SYSTEM}-overlay.qcow2"], None),
            "volumes remain",
        ),
    ],
)
def test_remote_leftover_after_teardown_fails_before_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    left: RemoteAuthorityInventory,
    match: str,
) -> None:
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])
    observe = authority_lane.remote_authority_inventory

    def leftover(config: Any, system_id: str | None = None) -> RemoteAuthorityInventory:
        if system_id is not None and "systems.teardown" in calls:
            return left
        return observe(config, system_id)

    monkeypatch.setattr(authority_lane, "remote_authority_inventory", leftover)
    run = _remote(tmp_path)
    with pytest.raises(AssertionError, match=match):
        _frame(run, body)
    assert "allocations.release" not in calls
    assert "cleanup" not in run.assertions


def test_remote_reap_markers_that_the_sweep_removes_do_not_fail_the_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The marker volumes outlive the teardown until the sweep runs, so the frame waits."""
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])
    observe = authority_lane.remote_authority_inventory
    markers = [f"kdive-module-{_SYSTEM}-run-nonce-reaping.journal"]
    pending = [True, True]

    def sweeping(config: Any, system_id: str | None = None) -> RemoteAuthorityInventory:
        if system_id is not None and "systems.teardown" in calls and pending:
            pending.pop()
            return RemoteAuthorityInventory("x86_64", ["kdive-other"], markers, None)
        return observe(config, system_id)

    monkeypatch.setattr(authority_lane, "remote_authority_inventory", sweeping)
    run = _remote(tmp_path)

    _frame(run, body)

    assert pending == []
    assert set(run.assertions) == {"effect", "cleanup"}


def test_remote_inventory_drift_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])
    observe = authority_lane.remote_authority_inventory

    def drifted(config: Any, system_id: str | None = None) -> RemoteAuthorityInventory:
        found = observe(config, system_id)
        if system_id is None and "allocations.release" in calls:
            return RemoteAuthorityInventory("x86_64", found.domains, ["stray"], None)
        return found

    monkeypatch.setattr(authority_lane, "remote_authority_inventory", drifted)
    with pytest.raises(AssertionError, match="remote_authority_volumes"):
        _frame(_remote(tmp_path), body)


def test_remote_body_failure_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])
    run = _remote(tmp_path)
    with pytest.raises(_BodyFailed):
        _frame(run, _body_fails)
    assert calls.index("systems.teardown") < calls.index("allocations.release")
    assert "cleanup" not in run.assertions
    assert len(run.artifacts) == 1


def test_remote_refused_provision_only_releases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, [])

    class _Refusing(_Op):
        async def call_tool(self, name: str, **args: object) -> ToolResponse:
            if name == "systems.provision":
                self.calls.append(name)
                return ToolResponse(
                    object_id="alloc",
                    status="error",
                    error_category=ErrorCategory.CONFIGURATION_ERROR,
                )
            return await super().call_tool(name, **args)

    @asynccontextmanager
    async def over_http(_url: str, _token: str) -> AsyncIterator[_Op]:
        yield _Refusing(calls)

    monkeypatch.setattr(authority_lane.LiveStackClient, "over_http", over_http)
    run = _remote(tmp_path)
    with pytest.raises(AssertionError):
        _frame(run, body)
    assert "systems.teardown" not in calls and "drain_system_jobs" not in calls
    assert calls[-2:] == ["capacity_in_use", "remote_inventory"]
    assert "allocations.release" in calls
    assert len(run.artifacts) == 1


def test_remote_root_selects_the_one_verified_catalog_image(migrated_url: str) -> None:
    async def scenario() -> list[object]:
        outcomes: list[object] = []
        async with open_pool(migrated_url) as pool:
            allocation = await seed_granted_allocation(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET kind='remote-libvirt', name='remote-a' WHERE id=("
                    "SELECT resource_id FROM allocations WHERE id=%s)",
                    (allocation,),
                )

            async def image(name: str, provenance: dict[str, object]) -> None:
                async with pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO image_catalog (id, provider, name, arch, format, "
                        "root_device, object_key, digest, provenance, visibility, state) VALUES "
                        "(%s, 'remote-libvirt', %s, 'x86_64', 'qcow2', '/dev/vda', %s, %s, %s, "
                        "'public', 'registered')",
                        (uuid4(), name, f"{name}.qcow2", _DIGEST, Jsonb(provenance)),
                    )

            await image("unverified", {})
            await image("verified", {"root_spec": {"schema": "root-spec-v1"}})
            for resource in ("remote-a", "remote-b"):
                try:
                    outcomes.append(
                        await authority_lane.remote_root(migrated_url, resource, "x86_64")
                    )
                except ScenarioStop as stop:
                    outcomes.append(str(stop))
            await image("second", {"root_spec": {"schema": "root-spec-v1"}})
            try:
                await authority_lane.remote_root(migrated_url, "remote-a", "x86_64")
            except ScenarioStop as stop:
                outcomes.append(str(stop))
        return outcomes

    selected, missing, ambiguous = asyncio.run(scenario())
    assert isinstance(selected, tuple) and selected[1:] == ("verified", _DIGEST)
    assert "no remote-libvirt resource" in str(missing) and "remote-b" not in str(missing)
    assert "found 2" in str(ambiguous)


class _BodyCompleted(_Op):
    """A body that already tore its System down and released its allocation (#3185)."""

    async def call_tool(self, name: str, **args: object) -> ToolResponse:
        if name == "systems.get":
            self.calls.append(name)
            return ToolResponse(object_id=_SYSTEM, status="torn_down")
        if name == "allocations.wait":
            self.calls.append(name)
            return ToolResponse(object_id="alloc", status="released")
        return await super().call_tool(name, **args)


def _completed_over_http(calls: list[str]) -> Any:
    @asynccontextmanager
    async def over_http(_url: str, _token: str) -> AsyncIterator[_Op]:
        yield _BodyCompleted(calls)

    return over_http


def test_cleanup_accepts_a_body_completed_teardown_and_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_lane(monkeypatch, tmp_path)
    monkeypatch.setattr(authority_lane.LiveStackClient, "over_http", _completed_over_http(calls))
    run = _run(tmp_path)
    _frame(run, _body_ok)
    assert "systems.teardown" not in calls
    assert "allocations.release" not in calls
    assert set(run.assertions) == {"effect", "cleanup"}


def test_remote_cleanup_accepts_a_body_completed_teardown_and_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[authority_lane.AuthoritySystem] = []
    calls, body = _fake_remote_lane(monkeypatch, tmp_path, seen)
    monkeypatch.setattr(authority_lane.LiveStackClient, "over_http", _completed_over_http(calls))
    monkeypatch.setattr(
        authority_lane,
        "remote_authority_inventory",
        lambda _config, system_id=None: RemoteAuthorityInventory(
            "x86_64",
            ["kdive-other"],
            [],
            RemoteDomain(True, "kvm") if system_id is not None and "body" not in calls else None,
        ),
    )
    run = _remote(tmp_path)
    _frame(run, body)
    assert "systems.teardown" not in calls
    assert "allocations.release" not in calls
    assert set(run.assertions) == {"effect", "cleanup"}


def test_lane_config_admits_only_the_fixed_barrier_socket(tmp_path: Path) -> None:
    fixed = "/run/kdive/provider-authority/proof-control/control.sock"
    config = authority_lane.load_lane_config(
        {authority_lane.CONFIG_ENV: str(_config(tmp_path, barrier_socket=fixed))}
    )
    assert config is not None and config.barrier_socket == Path(fixed)
    with pytest.raises(ValueError, match="fixed authority proof socket"):
        authority_lane.load_lane_config(
            {authority_lane.CONFIG_ENV: str(_config(tmp_path, barrier_socket="/tmp/x.sock"))}
        )


@pytest.mark.parametrize("fault", ["missing", "stale", "owner", "mode"])
def test_collector_revision_gate(monkeypatch, fault):
    import stat
    from unittest.mock import Mock

    revision = Mock()
    revision.lstat.return_value = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o444)
    revision.read_text.return_value = _REVISION
    if fault == "missing":
        revision.lstat.side_effect = FileNotFoundError()
    elif fault == "stale":
        revision.read_text.return_value = "b" * 40
    elif fault == "owner":
        revision.lstat.return_value.st_uid = 123
    else:
        revision.lstat.return_value.st_mode = stat.S_IFREG | 0o644
    monkeypatch.setattr(authority_lane, "_COLLECTOR_REVISION", revision)
    monkeypatch.setattr(
        authority_lane,
        "_output",
        lambda *args: subprocess.CompletedProcess(
            [], 0, "active" if args[0] == "systemctl" else _REVISION, ""
        ),
    )
    deployed = Mock(side_effect=AssertionError("must stop before stack probe"))
    monkeypatch.setattr(authority_lane, "require_deployed_revision", deployed)
    config = authority_lane.AuthorityLaneConfig(
        installed_revision=_REVISION,
        authority_service="kdive-external-boot-authority.service",
        system_mode="fixture",
    )
    with pytest.raises((AssertionError, FileNotFoundError)):
        authority_lane.require_lane(config, "http://lane")
    deployed.assert_not_called()


def test_observer_failure_prevents_allocation(monkeypatch, tmp_path):
    calls = _fake_lane(monkeypatch, tmp_path)

    def fail():
        raise OSError("observer unavailable")

    monkeypatch.setattr(authority_lane, "lane_inventory", fail)
    with pytest.raises(OSError):
        asyncio.run(
            authority_lane.on_authority_system(
                _run(tmp_path),
                "http://lane",
                cast(Any, None),
                "db",
                project="project",
                body=cast(Any, None),
            )
        )
    assert "allocations.request" not in calls


def test_fixture_names_match_the_fixture_script(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).resolve().parents[3] / "scripts/live-vm/provision-authority-fixture.py"
    spec = importlib.util.spec_from_file_location("provision_authority_fixture", path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    # The script points the libvirt roots at the authority daemon at import; keep that out of
    # every later test on this worker.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    spec.loader.exec_module(script)
    # The script's private roots come from the authority's environment, so compare the entry
    # names and their order: the frame must expect every artifact the script reports.
    expected = [Path(name).name for name in script._fixture_artifacts(UUID(_SYSTEM))]
    assert [Path(name).name for name in authority_lane.fixture_names(_SYSTEM)] == expected


def _remote_authority_cell() -> Cell:
    return next(
        c
        for c in tool_cells(("ops.force_release",))
        if c.kind == "functional" and c.provider == "remote-libvirt" and c.guest_arch == "x86_64"
    )


def _observe_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image: str
) -> tuple[CellRun, Cell]:
    cell = _remote_authority_cell()
    run = CellRun(cell, EvidenceWriter(tmp_path))
    host = RemoteHost("operator@provider", "pool", "fedora:44", "x86_64", "kvm")
    monkeypatch.setattr(authority_lane, "remote_host", lambda: host)

    async def root(_db: str, resource: str, arch: str) -> tuple[str, str, str]:
        assert (resource, arch) == ("remote-proof-x86", "x86_64")
        return "resource-id", image, "sha256:" + "c" * 64

    monkeypatch.setattr(authority_lane, "remote_root", root)
    config = RemoteAuthorityProofConfig.model_construct(resource_name="remote-proof-x86")
    system = authority_lane.AuthoritySystem(
        cast(Any, object()), "cov-t", "alloc", _SYSTEM, config, cast(Any, object())
    )
    # What the remote frame itself records before it runs the body.
    run.observed |= {"image_sha256": "c" * 64, "guest_arch": "x86_64", "accelerator": "kvm"}
    asyncio.run(authority_lane.observe_remote_authority(run, system, "db"))
    return run, cell


def test_a_remote_authority_record_matches_its_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #3186: qualify compares the whole context, so the provider host and guest must be observed.
    representative = REMOTE_REPRESENTATIVES["fedora"]
    run, cell = _observe_remote(tmp_path, monkeypatch, representative.name)
    identity = RunIdentity(_REVISION, "b" * 64, "ubuntu:26.04", "x86_64", True, {})
    bound = _lane_contexts([cell], "fedora:44", "x86_64", representative, "c" * 64, kernel=None)
    assert run.context(identity) == bound[cell.id]


def test_another_remote_root_image_stops_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ScenarioStop) as stop:
        _observe_remote(tmp_path, monkeypatch, "lane-private-image")
    assert stop.value.outcome is Outcome.BLOCKED
    assert "lane-private-image" not in str(stop.value)


class _Clock:
    """A clock the fake sleep advances, so the polling bound is exercised without waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _pool(*volumes: str) -> RemoteAuthorityInventory:
    return RemoteAuthorityInventory("x86_64", [], list(volumes), None)


def _sweep(
    first: RemoteAuthorityInventory,
    reads: list[RemoteAuthorityInventory],
    clock: _Clock,
    *,
    deadline_s: float = 120.0,
    poll_s: float = 10.0,
) -> list[str]:
    async def read() -> RemoteAuthorityInventory:
        return reads.pop(0) if len(reads) > 1 else reads[0]

    return asyncio.run(
        authority_lane._await_volumes_swept(
            first,
            read,
            _SYSTEM,
            deadline_s=deadline_s,
            poll_s=poll_s,
            clock=clock,
            sleep=clock.sleep,
        )
    )


def test_volume_sweep_wait_returns_at_once_when_the_pool_is_already_clear() -> None:
    clock = _Clock()

    assert _sweep(_pool("kdive-other-overlay.qcow2"), [_pool()], clock) == []
    assert clock.sleeps == []


def test_volume_sweep_wait_passes_when_the_markers_clear_within_the_bound() -> None:
    clock = _Clock()
    markers = _pool(f"kdive-module-{_SYSTEM}-run-nonce-reaping.journal")

    stranded = _sweep(markers, [markers, markers, _pool()], clock)

    assert stranded == []
    assert clock.sleeps == [10.0, 10.0, 10.0]


def test_volume_sweep_wait_reports_what_remains_when_the_bound_passes() -> None:
    clock = _Clock()
    markers = _pool(f"kdive-module-{_SYSTEM}-run-nonce-reaped.journal")

    stranded = _sweep(markers, [markers], clock, deadline_s=30.0)

    assert stranded == [f"kdive-module-{_SYSTEM}-run-nonce-reaped.journal"]
    assert clock.now == 30.0


def test_volume_sweep_wait_ignores_volumes_that_do_not_name_the_system() -> None:
    clock = _Clock()

    assert _sweep(_pool("kdive-22222222-overlay.qcow2"), [_pool()], clock) == []
    assert clock.sleeps == []
