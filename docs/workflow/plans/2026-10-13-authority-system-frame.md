# Authority-owned System frame: implementation plan (#3184)

**Goal:** HTTP tool cells get a frame that creates, observes and cleans up an authority-owned
System on the installed local-authority lane.

**Architecture:** a new `tests/integration/live_stack/authority_lane.py` holds the lane config,
selection stops, pre-mutation gates, a root-run inventory of the authority daemon and private
roots, and `on_authority_system`. It reuses the installed-authority helpers in
`tests/live_vm/installed_local_authority_support.py`, whose protected-config reader is shared and
whose two revision/route gates accept any object with `installed_revision`. Spec:
`docs/workflow/specs/2026-10-13-authority-system-frame-design.md`.

**Tech stack:** Python 3.14, pytest, psycopg 3, libvirt-python, pydantic 2.

Expected implementation size: 1000–1150 changed lines (L) — about 35 support-module lines, 560
frame lines, 400 unit-test lines, 90 live self-test lines, 60 runbook lines and a docstring
paragraph, from the file map. The range sits at the L ceiling because review added the
teardown, Run-job drain, partial-create and interruption paths.

## Global Constraints

- Test harness only: no change under `src/`, no migration, no ADR, no production policy.
- The remote authority frame is #3209; public provisioning stays `blocked` naming #3144/#3148.
- Evidence and assertion messages carry no host names, addresses or credential paths; cleanup
  attempt artifacts carry exception type names only.
- Every privileged call is `sudo -n` with a fixed argv; new programs run `python -I -c`.
- Ruff line length 100; `just lint`, `just type` (whole tree) green. The tool shell is zsh: run
  multi-line verification under `bash`; run gates bare.
- Doc style: no "critical", "robust", "comprehensive", "elegant".

## File map

| File | Change |
|------|--------|
| `tests/live_vm/installed_local_authority_support.py` | extract `read_protected_config`; `Revisioned` protocol for two gates |
| `tests/integration/live_stack/authority_lane.py` (new) | lane config, selection, gates, inventory, frame, cleanup |
| `tests/integration/live_stack/test_authority_lane.py` (new) | orchestration unit tests |
| `tests/integration/test_authority_lane_frame_live.py` (new) | gated live self-test |
| `tests/integration/live_stack/tool_cells.py` | docstring names the third frame |
| `docs/operating/runbooks/live-testing.md` | frame config, mode, exclusivity, manual cleanup |

## Task 1 — shared protected reader and revision protocol

Files: modify `tests/live_vm/installed_local_authority_support.py`; test
`tests/live_vm/test_installed_local_authority_support.py` (existing).

Interfaces produced:
- `read_protected_config(raw: str, label: str) -> bytes`
- `class Revisioned(Protocol): installed_revision: str` (read-only property)
- `require_deployed_revision(config: Revisioned, base_url, running_workers, *, fetch, resolve)`
- `require_installed_authority_routes(_config: Revisioned, running_workers)`

Verification:
- Contract: `load_config` keeps its metadata and size refusals. Mode: focused-test —
  existing `test_installed_local_authority_support.py` cases (`unsafe metadata`); red when the
  reader drops the `st_uid` check; green:
  `uv run python -m pytest tests/live_vm/test_installed_local_authority_support.py -q`.
- Contract: the gates accept the lane config. Mode: task-test-not-applicable for the
  annotation itself; `just type` checks the call sites in Task 2.

Steps:
1. Replace the body of `load_config` after `raw is None` with
   `return NativeAuthorityConfig.model_validate_json(read_protected_config(raw, "local authority proof config"))`
   and add:

```python
def read_protected_config(raw: str, label: str) -> bytes:
    """The bytes of an owner-only mode-0400/0600 regular file of at most 16384 bytes."""
    descriptor = os.open(Path(raw), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600)
            or not 0 < metadata.st_size <= 16_384
        ):
            raise ValueError(f"{label} has unsafe metadata")
        data = os.read(descriptor, 16_385)
    finally:
        os.close(descriptor)
    if len(data) > 16_384:
        raise ValueError(f"{label} exceeds 16384 bytes")
    return data


class Revisioned(Protocol):
    """An operator config that names the installed candidate revision."""

    @property
    def installed_revision(self) -> str: ...
```

2. Change the first parameter annotation of `require_deployed_revision` and
   `require_installed_authority_routes` to `Revisioned`; add `Protocol` to the `typing` import.
3. Run the focused command (expect pass) and `just type`; commit
   `refactor(test): share the protected authority config reader`.

## Task 2 — the authority frame

Files: create `tests/integration/live_stack/authority_lane.py` and
`tests/integration/live_stack/test_authority_lane.py`; edit the `tool_cells.py` docstring.

Interfaces consumed: Task 1's names; existing `NativeAuthorityConfig`, `OwnedResource`,
`ResourceLedger`,
`provision_authority_fixture(db_url, config, ledger)`, `remove_authority_fixture(config, ledger)`,
`require_authority_artifact_confinement(config, running_workers)`,
`require_evidence_read_access(db_url)`; `acquire_image(op, name, arch)`, `domain_xml(system_id)`,
`ACCELERATORS`, `CellRun`, `ScenarioStop` (scenario.py); `capacity_in_use`, `domain_disks`,
`release_and_verify`, `_await_released` (cleanup.py); `await_system_state`, `mint_role_token`,
`ok`, `scalar`, `seed_metering`, `worker_libvirt_uri`, `LOCAL_ALLOCATION_DISK_GB`,
`DRAIN_DEADLINE_S`, `POLL_INTERVAL_S` (spine.py); `lane_image()` (tool_cells.py);
`data_str` (tests/mcp/json_data.py); `domain_name_for`, `local_authority_instance_for_resource`
(`kdive.providers.local_libvirt.config`), `TERMINAL_INVESTIGATION_STATES`
(`kdive.services.investigations.common`); for the database test `migrated_url`
(tests/db/conftest.py via tests/integration/conftest.py), `open_pool`, and
`seed_granted_allocation`, `seed_system`, `seed_running_run` (tests/integration/_seed.py).

Interfaces produced (for #3186 and Task 3): `CONFIG_ENV`, `AuthorityLaneConfig`,
`load_lane_config(environment=None)`, `AuthoritySystem`, `AuthorityBody`,
`on_authority_system(run, base_url, issuer, db_url, *, project, body)`.

Verification (each focused-test red is observed by commenting out the named behaviour):
- Remote cell stops `blocked` naming #3209 with no gate call — `test_remote_cell_is_blocked`.
- Unset config stops `blocked` — `test_unset_config_is_blocked`.
- `public` stops `blocked` naming #3144 and #3148 before any gate —
  `test_public_mode_is_blocked_before_mutation`.
- A gate failure requests no allocation — `test_gate_failure_creates_nothing`.
- An authority-routed resource stops `blocked`, never provisions, releases its allocation —
  `test_routed_resource_is_blocked_and_released`.
- A failed fixture create removes the five fixed names before teardown —
  `test_partial_fixture_create_removes_the_fixed_names`.
- Cancellation still cleans up — `test_cancellation_still_cleans_up`.
- The drain query names the System's and its Runs' open jobs only (database) —
  `test_open_jobs_names_the_system_and_its_runs` (red when the `run_id` arm is removed).
- Success order and proofs, including `systems.teardown` before `release_and_verify` —
  `test_success_orders_effects_and_proves_cleanup` (red when the confinement call is removed:
  the recorded order differs).
- Body failure still cleans up and re-raises — `test_body_failure_still_cleans_up`.
- Cleanup failure fails — `test_cleanup_failure_fails_the_scenario`.
- Both fail — `test_body_and_cleanup_failures_are_grouped`.
- Inventory drift fails — `test_inventory_drift_fails`.
- Config refusals — `test_lane_config_rejects_unknown_fields_and_unsafe_files`.
- Inventory program call shape and output validation — `test_authority_inventory_is_fixed_and_validated`.
Green: `uv run python -m pytest tests/integration/live_stack/test_authority_lane.py -q`.

Steps:
1. Create `tests/integration/live_stack/test_authority_lane.py`:

```python
"""The authority-owned System frame's orchestration, with its host and HTTP boundaries faked."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from typing import Any, cast

import pytest
from psycopg.types.json import Jsonb

from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.domain.capacity.state import SystemState
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration._seed import seed_granted_allocation, seed_running_run, seed_system
from tests.integration.conftest import open_pool
from tests.integration.live_stack import authority_lane
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun, ScenarioStop

_REVISION = "a" * 40
_SYSTEM = "11111111-2222-4333-8444-555555555555"
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
            "authority_inventory", {"system": {"active": True, "type": "kvm"}}
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
    assert facts == {"domains": ["kdive-x"], "roots": {"/r": ["a"]}, "system": None}
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
```

   Run the green command; expect `ModuleNotFoundError` for `authority_lane`.
2. Create `tests/integration/live_stack/authority_lane.py`:

```python
"""Authority-owned System frame of the HTTP tool cells (#3184, ADR-0722, ADR-0715).

:func:`on_authority_system` is the third System frame beside
:func:`~tests.integration.live_stack.tool_cells.on_lane_system` and
:func:`~tests.integration.live_stack.tool_cells.lane_target`. A carrier selects it explicitly. It
runs on the installed local-authority lane that ``CONFIG_ENV`` names, where the System's domain
lives on the authority's private session daemon and its artifacts under the private roots, so it
observes there and never through SSH: an authority System refuses ordinary mutation, including
``systems.authorize_ssh_key``, before its first activation.

``system_mode`` ``fixture`` provisions a System through HTTP with the fixed pinned rootfs, then
moves it onto the private daemon with ``scripts/live-vm/provision-authority-fixture.py``.
``public`` stops ``blocked`` before any mutation (#3144, #3148); a remote cell stops ``blocked``
too (#3209).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

import libvirt
import psycopg
from pydantic import BaseModel, ConfigDict, Field

from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.local_libvirt.config import local_authority_instance_for_resource
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.services.investigations.common import TERMINAL_INVESTIGATION_STATES
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.cleanup import (
    _await_released,  # the release wait of a frame that never got a System
    capacity_in_use,
    domain_disks,
    release_and_verify,
)
from tests.integration.live_stack.scenario import (
    ACCELERATORS,
    CellRun,
    ScenarioStop,
    acquire_image,
    domain_xml,
)
from tests.integration.live_stack.spine import (
    DRAIN_DEADLINE_S,
    LOCAL_ALLOCATION_DISK_GB,
    POLL_INTERVAL_S,
    await_system_state,
    mint_role_token,
    ok,
    scalar,
    seed_metering,
    worker_libvirt_uri,
)
from tests.integration.live_stack.tool_cells import lane_image
from tests.live_vm.installed_local_authority_support import (
    NativeAuthorityConfig,
    OwnedResource,
    ResourceLedger,
    provision_authority_fixture,
    read_protected_config,
    remove_authority_fixture,
    require_authority_artifact_confinement,
    require_deployed_revision,
    require_evidence_read_access,
    require_installed_authority_routes,
)
from tests.mcp.json_data import data_str

CONFIG_ENV = "KDIVE_LIVE_STACK_LOCAL_AUTHORITY_CONFIG"
# The fixed worker input the fixture script copies onto the private daemon.
FIXTURE_ROOTFS = "/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2"
_AUTHORITY_PYTHON = "/opt/kdive-provider-authority/.venv/bin/python"
_REVISION = "/opt/kdive-provider-authority/revision"
_MAX_INVENTORY_BYTES = 1_048_576
_TERMINAL = frozenset({"succeeded", "failed", "canceled"})
_AUTHORITY_ROOTFS = "/var/lib/kdive/provider-authority/rootfs"
_AUTHORITY_CONSOLE = "/var/lib/kdive/provider-authority/console"
REMOTE_BLOCKED = "the remote authority-owned System frame is #3209"
PUBLIC_BLOCKED = (
    "public authority-owned provisioning is blocked by #3144 (the authority answers "
    "provider-conflict after it defines the domain and fences the allocation from release) and "
    "#3148 (the local System identity never matches libvirt's machine name); "
    "set system_mode to fixture"
)
ROUTED_BLOCKED = (
    "fixture mode needs the lane's plain local-libvirt resource without an authority_instance; "
    "an authority-routed resource makes systems.provision the blocked public path (#3144, #3148)"
)
# Every non-terminal job that names the System or one of its Runs.
_OPEN_JOBS = (
    "SELECT id::text FROM jobs WHERE state NOT IN ('succeeded', 'failed', 'canceled') "
    "AND (payload->>'system_id' = %(system)s OR payload->>'run_id' IN "
    "(SELECT id::text FROM runs WHERE system_id = %(system)s::uuid)) ORDER BY created_at"
)

# Root runs this with the authority's interpreter in isolated mode; its only argument is a UUID.
_INVENTORY = """
import json
import os
import sys
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring

if len(sys.argv) > 2:
    raise SystemExit("inventory takes at most one System UUID")
name = "kdive-" + str(UUID(sys.argv[1])) if len(sys.argv) == 2 else None
roots = ("/var/lib/kdive/provider-authority/rootfs", "/var/lib/kdive/provider-authority/console")
conn = libvirt.open("qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock")
if conn is None:
    raise SystemExit("authority daemon connection returned no handle")
try:
    domains = sorted(domain.name() for domain in conn.listAllDomains(0))
    system = None
    if name in domains:
        domain = conn.lookupByName(name)
        kind = fromstring(domain.XMLDesc(0)).get("type", "")
        system = {"active": domain.isActive() == 1, "type": kind}
finally:
    conn.close()
listing = {root: sorted(os.listdir(root)) for root in roots}
print(json.dumps({"domains": domains, "roots": listing, "system": system}))
"""


class AuthorityLaneConfig(BaseModel):
    """Non-secret operator inputs of the installed local-authority lane."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    installed_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    authority_service: Literal["kdive-external-boot-authority.service"]
    system_mode: Literal["fixture", "public"]


def load_lane_config(environment: Mapping[str, str] | None = None) -> AuthorityLaneConfig | None:
    """The lane config ``CONFIG_ENV`` names, or ``None`` when it is unset."""
    raw = (os.environ if environment is None else environment).get(CONFIG_ENV)
    if raw is None:
        return None
    data = read_protected_config(raw, "local authority lane config")
    return AuthorityLaneConfig.model_validate_json(data)


def _names(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise AssertionError(f"authority inventory {what} is malformed")
    return value


def authority_inventory(system_id: str | None = None) -> dict[str, object]:
    """The authority daemon's domains, the private roots' entries and the System's domain facts."""
    argv = ["sudo", "-n", _AUTHORITY_PYTHON, "-I", "-c", _INVENTORY]
    if system_id is not None:
        argv.append(str(UUID(system_id)))
    result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=120.0, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"authority inventory exited {result.returncode}")
    if len(result.stdout) > _MAX_INVENTORY_BYTES:
        raise AssertionError("authority inventory output exceeds its bound")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or set(data) != {"domains", "roots", "system"}:
        raise AssertionError("authority inventory is malformed")
    roots = data["roots"]
    if not isinstance(roots, dict):
        raise AssertionError("authority inventory roots are malformed")
    system = data["system"]
    if system is not None and not (
        isinstance(system, dict)
        and isinstance(system.get("active"), bool)
        and isinstance(system.get("type"), str)
    ):
        raise AssertionError("authority inventory System facts are malformed")
    return {
        "domains": _names(data["domains"], "domains"),
        "roots": {str(root): _names(names, "root") for root, names in roots.items()},
        "system": system,
    }


def worker_domains() -> list[str]:
    """The ``kdive-`` domains the worker's libvirt defines."""
    conn = libvirt.open(worker_libvirt_uri())
    try:
        return sorted(d.name() for d in conn.listAllDomains(0) if d.name().startswith("kdive-"))
    finally:
        conn.close()


def lane_inventory() -> dict[str, object]:
    """What the frame must leave as it found it: both daemons' domains and the private roots."""
    authority = authority_inventory()
    return {
        "authority_domains": authority["domains"],
        "authority_roots": authority["roots"],
        "worker_domains": worker_domains(),
    }


def _output(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
        argv, capture_output=True, text=True, timeout=60.0, check=False
    )


def require_lane(config: AuthorityLaneConfig, base_url: str) -> str:
    """Refuse unless the lane runs the configured candidate; return the running worker units."""
    active = _output("systemctl", "is-active", config.authority_service).stdout.strip()
    assert active == "active", f"{config.authority_service} is {active!r}, not active"
    installed = _output("sudo", "-n", "cat", _REVISION)
    assert installed.returncode == 0, "cannot read the installed authority revision with sudo -n"
    assert installed.stdout.strip() == config.installed_revision, (
        "the installed authority revision does not match the lane config"
    )
    running = _output(
        "systemctl", "list-units", "kdive-live-worker@*.service", "--state=running", "--no-legend"
    ).stdout.strip()
    require_deployed_revision(config, base_url, running)
    require_installed_authority_routes(config, running)
    return running


def fixture_profile(arch: str, digest: str, unread_ref: str) -> dict[str, object]:
    """A direct-kernel profile booting the fixed worker rootfs pinned to ``digest``."""
    return {
        "schema_version": 1,
        "arch": arch,
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": unread_ref,
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": FIXTURE_ROOTFS, "sha256": digest},
                "crashkernel": "256M",
            }
        },
    }


async def require_root_provenance(db_url: str, system_id: str, digest: str, arch: str) -> None:
    """Require admission to have bound the System's root to the registered image."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT image_digest, architecture FROM system_root_provenance WHERE system_id = %s",
            (system_id,),
        )
        row = await cursor.fetchone()
    assert row is not None, "the System has no root provenance; stage the registered image bytes"
    assert (row[0], row[1]) == (digest, arch), "the System's root provenance is another image"


async def _open_jobs(db_url: str, system_id: str) -> list[str]:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(_OPEN_JOBS, {"system": system_id})
        return [str(row[0]) for row in await cursor.fetchall()]


async def drain_system_jobs(
    op: LiveStackClient, db_url: str, system_id: str, *, deadline_s: float = DRAIN_DEADLINE_S
) -> list[str]:
    """Wait until no job of the System or its Runs is open; return the terminal states seen.

    The query repeats until it finds nothing, so a job queued during the drain is waited on too.
    """
    deadline = time.monotonic() + deadline_s
    states: list[str] = []
    while open_jobs := await _open_jobs(db_url, system_id):
        for job_id in open_jobs:
            env = await scalar(op, "jobs.wait", job_id=job_id, timeout_s=60.0)
            if env.status in _TERMINAL:
                states.append(env.status)
        assert time.monotonic() < deadline, f"job(s) of System {system_id} stayed open"
        await asyncio.sleep(POLL_INTERVAL_S)
    return states


def fixture_names(system_id: str) -> list[str]:
    """The five names a fixture create makes for ``system_id``, in creation order (runbook)."""
    return [
        f"{_AUTHORITY_ROOTFS}/{system_id}-fixture-base.qcow2",
        f"{_AUTHORITY_ROOTFS}/{system_id}-baseline",
        f"{_AUTHORITY_ROOTFS}/{system_id}-overlay.qcow2",
        f"{_AUTHORITY_CONSOLE}/{system_id}.log",
        domain_name_for(UUID(system_id)),
    ]


async def allocated_resource(db_url: str, allocation_id: str) -> str:
    """The name of the Resource ``allocation_id`` holds."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT r.name FROM allocations a JOIN resources r ON r.id = a.resource_id "
            "WHERE a.id = %s",
            (allocation_id,),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0], f"allocation {allocation_id} holds no named resource"
    return str(row[0])


@dataclass(frozen=True)
class AuthoritySystem:
    """A ``ready`` authority-owned System as the frame's body sees it.

    ``config`` and ``ledger`` are the invocation's: a body records each Investigation it opens in
    ``ledger`` (the frame closes any still open) and releases its own Runs.
    """

    op: LiveStackClient
    project: str
    allocation_id: str
    system_id: str
    config: NativeAuthorityConfig
    ledger: ResourceLedger


AuthorityBody = Callable[[AuthoritySystem], Awaitable[dict[str, object]]]


@dataclass
class _Frame:
    """What one invocation owns so far; cleanup removes exactly this."""

    op: LiveStackClient
    db_url: str
    project: str
    allocation: str
    in_use: int
    before: dict[str, object]
    system_id: str | None = None
    worker_disks: list[str] = field(default_factory=list)
    fixture: NativeAuthorityConfig | None = None
    ledger: ResourceLedger | None = None


def select_lane(run: CellRun) -> AuthorityLaneConfig:
    """The runnable lane config, or a ``blocked`` stop before anything is read or written."""
    if run.cell.provider == "remote-libvirt":
        raise ScenarioStop(Outcome.BLOCKED, REMOTE_BLOCKED)
    config = load_lane_config()
    if config is None:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{CONFIG_ENV} unset; name the installed local-authority lane config"
        )
    if config.system_mode == "public":
        raise ScenarioStop(Outcome.BLOCKED, PUBLIC_BLOCKED)
    return config


def _digest(state: object) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


async def on_authority_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    body: AuthorityBody,
) -> None:
    """Create an authority-owned System in ``project``, run ``body``, then prove ``cleanup``.

    ``body`` returns the ``effect`` observation; the frame adds the exposure. Cleanup runs
    whether or not ``body`` raised, including on interruption.
    """
    lane = select_lane(run)
    name, entry = lane_image()
    running = await asyncio.to_thread(require_lane, lane, base_url)
    await require_evidence_read_access(db_url)
    before = await asyncio.to_thread(lane_inventory)
    # systems.teardown, which cleanup needs, is an admin operation.
    token = mint_role_token(issuer, project=project, agent_session=f"{project}-sess", role="admin")
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        digest = str((await acquire_image(op, name, entry.arch))["digest"])
        run.observed |= {
            "image_sha256": digest.removeprefix("sha256:"),
            "guest_os": f"{entry.distro}:{entry.version}",
            "guest_arch": entry.arch,
        }
        in_use = await capacity_in_use(op)
        allocation = ok(
            await scalar(
                op,
                "allocations.request",
                project=project,
                vcpus=2,
                memory_gb=2,
                disk_gb=LOCAL_ALLOCATION_DISK_GB,
                resource={"mode": "kind"},
            ),
            "allocate",
        ).object_id
        frame = _Frame(op, db_url, project, allocation, in_use, before)
        try:
            profile = fixture_profile(entry.arch, digest, f"{project}-unread")
            observed = await _create_and_run(run, frame, lane, running, profile, digest, body)
        except BaseException as primary:
            await _cleanup_after_failure(run, frame, primary)
            raise
        run.prove("effect", {"exposure": run.cell.exposure, **observed})
        run.prove("cleanup", await _cleanup(frame))


async def _move_to_authority(frame: _Frame, config: NativeAuthorityConfig) -> None:
    """Run the fixture create and record its names; a failed create records the fixed five."""
    assert frame.ledger is not None and frame.system_id is not None
    names = fixture_names(frame.system_id)
    created = ResourceLedger(config.ownership_prefix, fixture_system=config.system_id)
    try:
        await provision_authority_fixture(frame.db_url, config, created)
    except BaseException:
        for identity in names:
            frame.ledger.record(OwnedResource(kind="authority-fixture", identity=identity))
        raise
    reported = [resource.identity for resource in created.resources]
    for identity in reported:
        frame.ledger.record(OwnedResource(kind="authority-fixture", identity=identity))
    assert reported == names, "the fixture create reported names other than its five"


async def _create_and_run(
    run: CellRun,
    frame: _Frame,
    lane: AuthorityLaneConfig,
    running: str,
    profile: dict[str, object],
    digest: str,
    body: AuthorityBody,
) -> dict[str, object]:
    """Provision through HTTP, move onto the private daemon, gate, observe, then run ``body``."""
    op = frame.op
    resource = await allocated_resource(frame.db_url, frame.allocation)
    if local_authority_instance_for_resource(resource) is not None:
        raise ScenarioStop(Outcome.BLOCKED, ROUTED_BLOCKED)
    provisioned = ok(
        await scalar(op, "systems.provision", allocation_id=frame.allocation, profile=profile),
        "provision",
    )
    system_id = frame.system_id = data_str(provisioned, "system_id")
    await await_system_state(op, "provision", system_id, "ready")
    frame.worker_disks = domain_disks(await asyncio.to_thread(domain_xml, system_id))
    await require_root_provenance(frame.db_url, system_id, digest, str(profile["arch"]))
    prefix = f"kdive-2151-{lane.installed_revision[:12]}-{secrets.token_hex(4)}"
    config = NativeAuthorityConfig(
        installed_revision=lane.installed_revision,
        system_id=UUID(system_id),
        project=frame.project,
        ownership_prefix=prefix,
        authority_service=lane.authority_service,
    )
    frame.fixture, frame.ledger = config, ResourceLedger(prefix, fixture_system=config.system_id)
    await _move_to_authority(frame, config)
    await asyncio.to_thread(require_authority_artifact_confinement, config, running)
    verify = config.model_copy(update={"fixture_mode": "verify-existing"})
    await provision_authority_fixture(frame.db_url, verify, frame.ledger)
    facts = (await asyncio.to_thread(authority_inventory, system_id))["system"]
    assert isinstance(facts, dict) and facts["active"] is True, (
        "the System's domain is not running on the authority daemon"
    )
    run.observed["accelerator"] = ACCELERATORS.get(str(facts["type"]), "none")
    return await body(
        AuthoritySystem(op, frame.project, frame.allocation, system_id, config, frame.ledger)
    )


async def _close_investigations(op: LiveStackClient, ledger: ResourceLedger) -> None:
    for resource in reversed(ledger.resources):
        if resource.kind != "investigation":
            continue
        current = await scalar(op, "investigations.get", investigation_id=resource.identity)
        if current.status in TERMINAL_INVESTIGATION_STATES:
            continue
        ok(
            await scalar(
                op,
                "investigations.close",
                investigation_id=resource.identity,
                summary="Authority System frame cleanup",
            ),
            "close-investigation",
        )


async def _cleanup(frame: _Frame) -> dict[str, object]:
    """Drain, close, remove the fixture, tear down, release, then compare the inventory."""
    op = frame.op
    owned = [frame.allocation]
    if frame.system_id is None:
        ok(await scalar(op, "allocations.release", allocation_id=frame.allocation), "release")
        await _await_released(op, frame.allocation, DRAIN_DEADLINE_S, POLL_INTERVAL_S)
        in_use = await capacity_in_use(op)
        assert in_use == frame.in_use, f"capacity in use is {in_use}, not {frame.in_use}"
        released: dict[str, object] = {"allocation": "released", "in_use": in_use}
        drained: list[str] = []
    else:
        drained = await drain_system_jobs(op, frame.db_url, frame.system_id)
        if frame.fixture is not None and frame.ledger is not None:
            await _close_investigations(op, frame.ledger)
            owned += [r.identity for r in frame.ledger.resources if r.kind == "authority-fixture"]
            await asyncio.to_thread(remove_authority_fixture, frame.fixture, frame.ledger)
        owned.append(frame.system_id)
        ok(await scalar(op, "systems.teardown", system_id=frame.system_id), "teardown")
        await await_system_state(op, "teardown", frame.system_id, "torn_down")
        released = await release_and_verify(
            op,
            allocation_id=frame.allocation,
            system_id=frame.system_id,
            domain=domain_name_for(UUID(frame.system_id)),
            disks=frame.worker_disks,
            in_use_before=frame.in_use,
        )
    after = await asyncio.to_thread(lane_inventory)
    changed = sorted(key for key in after if after[key] != frame.before.get(key))
    assert not changed, f"the authority lane inventory changed in {changed}"
    return {
        **released,
        "jobs_drained": sorted(drained),
        "owned": owned,
        "inventory_sha256": _digest(after),
    }


async def _cleanup_after_failure(run: CellRun, frame: _Frame, primary: BaseException) -> None:
    """Clean up after a failed creation or body; a failed cleanup joins ``primary``."""
    try:
        result = await _cleanup(frame)
    except Exception as cleanup:  # noqa: BLE001 - grouped with the primary failure below
        # The type only: a message can carry host paths or a libvirt URI (ADR-0715 evidence).
        attempt: dict[str, object] = {"error": type(cleanup).__name__}
        run.artifacts.append(run.writer.artifact({"cell": run.cell.id, "cleanup-attempt": attempt}))
        # A KeyboardInterrupt or cancellation stays a BaseExceptionGroup; two errors narrow to
        # an ExceptionGroup.
        raise BaseExceptionGroup(
            "authority frame and cleanup failures", [primary, cleanup]
        ) from None
    run.artifacts.append(run.writer.artifact({"cell": run.cell.id, "cleanup-attempt": result}))
```

3. Add to the `tool_cells.py` module docstring one paragraph: an authority-owned System uses
   `authority_lane.on_authority_system`, chosen explicitly by its carrier.
4. Run the focused command (expect pass), `just lint`, `just type`; commit
   `feat(test): frame authority-owned Systems for HTTP tool cells`.
5. Bite check: commit first, then in turn comment out the `require_authority_artifact_confinement`
   call, the `_cleanup_after_failure` call, the `systems.teardown` call and the `run_id` arm of
   `_OPEN_JOBS`; observe the named tests red; restore each with
   `git checkout -- tests/integration/live_stack/authority_lane.py`.

## Task 3 — live self-test and runbook

Files: create `tests/integration/test_authority_lane_frame_live.py`; edit
`docs/operating/runbooks/live-testing.md` (a `#### Authority-owned System frame` subsection under
the separate installed authority lanes).

Verification:
- Contract: the frame works on the installed lane. Mode: focused-test, live —
  `test_authority_frame_success` and `test_authority_frame_body_failure`; skip when
  `CONFIG_ENV` is unset; red observed by the unit bite in Task 2 (a live red would need a
  deliberately broken lane). Green on the lane:
  `KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL" KDIVE_LIVE_STACK_LOCAL_AUTHORITY_CONFIG=<file> uv run python -m pytest tests/integration/test_authority_lane_frame_live.py -m live_stack -v`.
- Contract: runbook text. Mode: task-test-not-applicable — prose with no executable consumer.

Steps:
1. Create `tests/integration/test_authority_lane_frame_live.py`:

```python
"""The authority-owned System frame on the installed local-authority lane (#3184).

Gated ``live_stack`` and dormant unless ``KDIVE_LIVE_STACK_LOCAL_AUTHORITY_CONFIG`` names the lane
config: one body that opens an Investigation and passes, and one that raises. Each must leave the
lane's domains, private artifacts and capacity as it found them.
"""

from __future__ import annotations

import asyncio
import os
import platform
import secrets
from pathlib import Path

import pytest

from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.authority_lane import (
    CONFIG_ENV,
    AuthorityBody,
    AuthoritySystem,
    on_authority_system,
)
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.spine import ok, scalar
from tests.live_vm.installed_local_authority_support import OwnedResource

pytestmark = pytest.mark.live_stack


class _BodyFailed(Exception):
    pass


def _run(tmp_path: Path) -> CellRun:
    arch = platform.machine()
    cell = Cell(
        "authority-frame-self-test",
        "tool/authority-frame-self-test",
        3184,
        "authority-frame",
        "frame self-test",
        ("effect", "cleanup"),
        provider="local-libvirt",
        host_arch=arch,
        guest_arch=arch,
    )
    return CellRun(cell, EvidenceWriter(tmp_path))


def _frame(run: CellRun, body: AuthorityBody) -> None:
    if os.environ.get(CONFIG_ENV) is None:
        pytest.skip(f"{CONFIG_ENV} unset; the installed local-authority lane is not configured")
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        pytest.fail('set KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"')
    base_url, issuer = require_stack(), require_issuer()
    project = f"cov-{secrets.token_hex(4)}"
    asyncio.run(on_authority_system(run, base_url, issuer, db_url, project=project, body=body))


def test_authority_frame_success(tmp_path: Path) -> None:
    async def body(system: AuthoritySystem) -> dict[str, object]:
        opened = await scalar(
            system.op,
            "investigations.open",
            project=system.project,
            title=system.config.ownership_prefix,
        )
        investigation = ok(opened, "open-investigation").object_id
        # Left open on purpose: the frame closes every ledger Investigation still open.
        system.ledger.record(OwnedResource(kind="investigation", identity=investigation))
        return {"investigation": "opened"}

    run = _run(tmp_path)
    _frame(run, body)
    assert set(run.assertions) == {"effect", "cleanup"}
    assert run.observed["accelerator"] == "kvm"


def test_authority_frame_body_failure(tmp_path: Path) -> None:
    async def body(_system: AuthoritySystem) -> dict[str, object]:
        raise _BodyFailed

    run = _run(tmp_path)
    with pytest.raises(_BodyFailed):
        _frame(run, body)
    assert "cleanup" not in run.assertions
    assert len(run.artifacts) == 1
```

2. Write the runbook subsection: config fields and mode, the fixed rootfs staging (same as the
   carrier's step 1), exclusivity, the harness DSN, what `blocked` names, manual exact-name
   cleanup after an interrupted run (refer to the existing fixture cleanup section), then
   `systems.teardown`/`allocations.release` of a stranded System.
3. `prek run` on staged files, `just lint`, `just type`; commit
   `test(live): prove the authority System frame on the installed lane`.

## Deferrals

None.
