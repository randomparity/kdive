# Investigation and artifact tool cells (#3096) — implementation plan

Goal: bind and prove the 260 contract cells of owner group 3096's 13 tools on the #2811 tool-cell
harness, in both configurations and both exposures.

Architecture: one new live carrier, `tests/integration/test_investigation_tool_cells_live.py`,
parametrized over the group's cells and framed by the unchanged `tool_cells.py` harness. It adds a
live-state snapshot, investigation, Run and session-System fixtures, and one functional body per
tool. There are 65 `[implementations]` entries, a contract-test update and a runbook section.
Design: [spec](../specs/2026-10-02-investigation-tool-cells-design.md); decision record ADR-0722.

Tech stack: Python 3.14, pytest, the fastmcp client (`kdive.mcp.dev_harness`), psycopg 3, boto3,
httpx and libvirt-python. All are already dependencies (`pyproject.toml`); nothing is added.

Expected implementation size: 1060–1130 changed lines (L). The range is derived from the file
map: the carrier (~975 lines), 65 binding lines, the contract test (~15) and the runbook (~29).

## Global Constraints

- No product source change, no `contract.py` change, no harness (`tool_cells.py`) change and no
  new dependency.
- ADR-0722 (with its 2026-10-02 amendment) governs exposure, configuration proof, rejection rules
  and the protected-state snapshot. The #2811 and #3095 cells keep their exact behaviour.
- Line length 100. `just lint`, `just type` and `just test-changed` must be green before each
  commit. Run `git fetch origin main && just records` before pushing.
- Live proof runs only on the disposable lab host, in both lanes, and the stack is wiped
  afterwards.
- Public text names no host, address, user or lab identifier.
- Deferred: #3108, which decides what isolation evidence a filtering list tool owes. Its 12
  cells stay bound and fail.

## File map

| File | Today | After |
|---|---|---|
| `tests/integration/test_investigation_tool_cells_live.py` | — | the group-3096 carrier (criteria 1-4) |
| `scripts/coverage_campaign/obligations.toml` | core and catalog bindings | + 65 group-3096 bindings (criterion 5) |
| `tests/scripts/test_coverage_contract.py` | core and catalog nodes asserted | + the investigation node and tools (criterion 5) |
| `docs/operating/runbooks/live-testing.md` | core and catalog tool-cell sections | + the investigation carrier section (criterion 6) |

No ownership transition is involved: the harness is reused as-is, and no path becomes obsolete.

## Task 1 — bindings and the contract test

Files: modify `scripts/coverage_campaign/obligations.toml` and
`tests/scripts/test_coverage_contract.py`.

Interfaces: the node id
`tests/integration/test_investigation_tool_cells_live.py::test_investigation_tool_cell`, which
Task 2 defines.

Verification:

- Group-3096 bindings. Mode: focused-test. Contract: all 260 cells of `_SPLIT[3096]` carry the
  node id, and no unbound tool gains a node. Test:
  `test_pending_cells_have_owned_assertions_but_no_invented_nodes`. Red before the bindings:
  `assert {None} == {'tests/integration/test_investigation_tool_cells_live.py::...'}`.
  Green: `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`, which reports
  `14 passed`.

Steps:

1. Apply this diff to the contract test, then run the focused command. It is red.

```diff
diff --git a/tests/scripts/test_coverage_contract.py b/tests/scripts/test_coverage_contract.py
index 6c4fdc071..419871fd4 100644
--- a/tests/scripts/test_coverage_contract.py
+++ b/tests/scripts/test_coverage_contract.py
@@ -58,6 +58,9 @@ def test_native_deep_families_and_foreign_tcg_stay_distinct(inventory: Inventory
 _IMAGE_SMOKE_NODE = "tests/integration/test_image_smoke_live.py::test_image_smoke"
 _CORE_NODE = "tests/integration/test_core_tool_cells_live.py::test_core_tool_cell"
 _CATALOG_NODE = "tests/integration/test_catalog_tool_cells_live.py::test_catalog_tool_cell"
+_INVESTIGATION_NODE = (
+    "tests/integration/test_investigation_tool_cells_live.py::test_investigation_tool_cell"
+)
 _DEEP_NODE = "tests/integration/test_deep_lifecycle_live.py::test_deep_lifecycle"
 _REMOTE_DEEP_NODE = (
     "tests/integration/test_remote_deep_lifecycle_live.py::test_remote_deep_lifecycle"
@@ -123,7 +126,17 @@ def test_pending_cells_have_owned_assertions_but_no_invented_nodes(inventory: In
     assert len(core) == 56 and {c.node_id for c in core} == {_CORE_NODE}
     catalog = [c for c in contract.cells if c.operation in _SPLIT[3095]]
     assert len(catalog) == 152 and {c.node_id for c in catalog} == {_CATALOG_NODE}
-    bound = {"image-smoke", "deep-lifecycle", "host-install", *_CORE_TOOLS, *_SPLIT[3095]}
+    investigation = [c for c in contract.cells if c.operation in _SPLIT[3096]]
+    assert len(investigation) == 260
+    assert {c.node_id for c in investigation} == {_INVESTIGATION_NODE}
+    bound = {
+        "image-smoke",
+        "deep-lifecycle",
+        "host-install",
+        *_CORE_TOOLS,
+        *_SPLIT[3095],
+        *_SPLIT[3096],
+    }
     assert all(c.node_id is None for c in contract.cells if c.operation not in bound)
     assert len({c.id for c in contract.cells}) == len(contract.cells)
     recovery = [c for c in contract.cells if c.operation == "ops.recover_build_use"]
```

2. Insert the 65 scenario bindings into `[implementations]`, directly before
   `"deep-lifecycle/remote-libvirt/longterm"`. They are generated from the contract so that none
   is mistyped:

```sh
uv run python - <<'PY'
from scripts.coverage_campaign.contract import build_contract
from tests.scripts.test_coverage_contract import _SPLIT
node = "tests/integration/test_investigation_tool_cells_live.py::test_investigation_tool_cell"
ids = sorted({c.scenario_id for c in build_contract().cells if c.operation in _SPLIT[3096]})
path = "scripts/coverage_campaign/obligations.toml"
text = open(path).read()
anchor = '"deep-lifecycle/remote-libvirt/longterm"'
lines = "".join(f'"{s}" = "{node}"\n' for s in ids)
open(path, "w").write(text.replace(anchor, lines + anchor, 1))
PY
```

3. Run the focused command again. It is green (`14 passed`). Commit
   `test(coverage): bind group 3096's investigation and artifact tool cells`.

## Task 2 — the carrier

Files: create `tests/integration/test_investigation_tool_cells_live.py`.

Interfaces, consumed from the existing code (all exist on `main`):

- `tests/integration/live_stack/tool_cells.py`: `Boundary`, `Exposure`, `Functional`, `Grants`,
  `HttpCaller`, `Rejection`, `Snapshot`, `boundary_of`, `one`, `project_state`,
  `prove_functional(run, caller, grants, body, snapshot)`,
  `prove_rejection(run, caller, boundary, rejection, snapshot, *, setup=None)`,
  `run_tool_cell(cell, scenario)` and `tool_cells(tools)`.
- `tests/integration/live_stack/scenario.py`: `CellRun`, `acquire_image(op, name, arch)`,
  `catalog_profile(entry, name, unread_ref)` and `domain_xml(system_id)`.
- `tests/integration/live_stack/spine.py`: `LOCAL_ALLOCATION_DISK_GB` and
  `provision_to_ready(client, *, allocation_id, profile, phase_name)`.
- `tests/integration/live_stack/cleanup.py`: `capacity_in_use(client)`, `domain_disks(xml)` and
  `release_and_verify(client, *, allocation_id, system_id, domain, disks, in_use_before)`.
- `tests/mcp/complete_build_support.py`: `valid_combined_kernel_tar()`.
- `kdive.providers.shared.runtime_paths.domain_name_for(UUID)` and
  `kdive.images.rootfs.catalog.load_rootfs_catalog()`.

The task produces `test_investigation_tool_cell(cell)`, the node Task 1 binds.

Verification:

- Parametrization. Mode: focused-test. Contract: the node collects one parameter per group-3096
  cell. Red before the file exists: `ERROR: file or directory not found`. Green:
  `uv run python -m pytest tests/integration/test_investigation_tool_cells_live.py --co -q`,
  which reports `260 tests collected`.
- Live snapshot, fixtures, functional bodies and rejection tables. Mode:
  task-test-not-applicable. Every one of them acts only against a live stack's database, object
  store, MCP endpoint and libvirt host; there is nothing to observe without one. Task 4's
  two-lane run proves them.

Steps:

1. Run the collection command. It is red.
2. Create the file with exactly this content:

```python
"""Prove the investigation and artifact MCP tool cells over HTTP (#3096, ADR-0722).

``live_stack``-marked. One parameter per contract cell of owner group 3096's 13 tools, framed by
:func:`~tests.integration.live_stack.tool_cells.run_tool_cell` like the #2811 and #3095 cells. A
functional cell compares the tool's answer with the evidence database (read-only session), the
object store, the bytes the cell generated, or the synthetic build it uploaded. Investigations
cannot be deleted, so :func:`_live_state` replaces the per-project snapshot's ``investigations``
and ``runs`` tables with their live rows plus the upload manifests and artifact rows those live
owners hold: a cell's investigation closed and its Runs ended leave the snapshot as found.
``artifacts.list`` and ``artifacts.get`` read the console parts of one System per session,
provisioned in ``KDIVE_PROJECT`` and released at session end.
``docs/operating/runbooks/live-testing.md`` covers the precondition and the run.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import os
import secrets
import struct
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, LiteralString, cast
from uuid import UUID

import boto3
import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from kdive.domain.errors import ErrorCategory
from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.providers.shared.runtime_paths import domain_name_for
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.cleanup import capacity_in_use, domain_disks, release_and_verify
from tests.integration.live_stack.scenario import (
    CellRun,
    acquire_image,
    catalog_profile,
    domain_xml,
)
from tests.integration.live_stack.spine import LOCAL_ALLOCATION_DISK_GB, provision_to_ready
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Exposure,
    Functional,
    Grants,
    HttpCaller,
    Rejection,
    Snapshot,
    boundary_of,
    one,
    project_state,
    prove_functional,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)
from tests.mcp.complete_build_support import valid_combined_kernel_tar

pytestmark = pytest.mark.live_stack

TOOLS = (
    "artifacts.create_investigation_upload",
    "artifacts.create_run_upload",
    "artifacts.fetch_raw",
    "artifacts.get",
    "artifacts.list",
    "investigations.close",
    "investigations.complete_rootfs_upload",
    "investigations.get",
    "investigations.link",
    "investigations.list",
    "investigations.open",
    "investigations.set",
    "investigations.unlink",
)
Rows = list[dict[str, Any]]
_ABSENT_ID = "00000000-0000-4000-8000-000000000000"
_LIVE_INVESTIGATIONS = ["open", "active"]
_LIVE_RUNS = ["created", "running"]
_REF_A = {"tracker": "cov", "id": "A-1", "url": "https://tracker.invalid/A-1"}
_REF_B = {"tracker": "cov", "id": "B-2", "url": "https://tracker.invalid/B-2"}
_BUILD_PROFILE = {"schema_version": 1, "arch": "x86_64"}
# The build-id note the synthetic bundle's ELF carries (tests/mcp/complete_build_support.py).
_BUILD_ID = "0123456789abcdef"
_IMAGE = "fedora-kdive-ready-44"
_PART_SETTLE_S = 60.0
_PART_DEADLINE_S = 600.0
_DIGEST: LiteralString = (
    "SELECT count(*) AS n, encode(sha256(convert_to(coalesce("
    "string_agg(t::text, ',' ORDER BY t::text), ''), 'UTF8')), 'hex') AS h "
)
_LIVE_OWNERS: LiteralString = (
    "(SELECT id FROM investigations WHERE project = %(p)s AND state = ANY(%(inv)s) "
    "UNION SELECT id FROM runs WHERE project = %(p)s AND state = ANY(%(run)s))"
)
_LIVE_QUERIES: dict[str, LiteralString] = {
    "investigations:live": "FROM investigations t WHERE t.project = %(p)s "
    "AND t.state = ANY(%(inv)s)",
    "runs:live": "FROM runs t WHERE t.project = %(p)s AND t.state = ANY(%(run)s)",
    "upload_manifests:live": "FROM upload_manifests t WHERE t.owner_id IN " + _LIVE_OWNERS,
    "artifacts:live": "FROM artifacts t WHERE t.owner_id IN " + _LIVE_OWNERS,
}
_SYSTEM_PARTS: LiteralString = (
    "SELECT id, object_key FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s "
    "AND sensitivity = 'redacted' ORDER BY created_at DESC, id DESC"
)


async def _rows(db_url: str, query: LiteralString, params: Any = ()) -> Rows:
    """``query``'s rows from a read-only session on the evidence database."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return list(await cursor.fetchall())


async def _live_state(db_url: str, project: str) -> dict[str, list[object]]:
    """The per-project snapshot with live investigations and Runs in place of both tables.

    A closed investigation and an ended Run are history (ADR-0722 §4 permits a narrower
    snapshot); the live ones, and the manifests and artifact rows they own, stay observed.
    """
    state = await project_state(db_url, project)
    del state["investigations"], state["runs"]
    params = {"p": project, "inv": _LIVE_INVESTIGATIONS, "run": _LIVE_RUNS}
    for key, where in _LIVE_QUERIES.items():
        row = (await _rows(db_url, _DIGEST + where, params))[0]
        state[key] = [row["n"], row["h"]]
    return state


def _viewer(project: str) -> Grants:
    return Grants(f"{project}-viewer", (project,), {project: "viewer"})


def _member(project: str) -> Grants:
    """A member of ``project`` holding no role in it."""
    return Grants(f"{project}-member", (project,), {})


def _contributor(project: str) -> Grants:
    return Grants(f"{project}-contributor", (project,), {project: "contributor"})


def _stranger() -> Grants:
    other = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{other}-operator", (other,), {other: "operator"})


def _funded_project() -> str:
    """The project the bring-up onboarded with budget and quota (``KDIVE_PROJECT``)."""
    project = os.environ.get("KDIVE_PROJECT")
    assert project, "KDIVE_PROJECT is unset; source examples/local-libvirt/env.sh"
    return project


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _s3() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ["KDIVE_S3_ENDPOINT_URL"],
        region_name=os.environ.get("KDIVE_S3_REGION", "us-east-1"),
    )


def _stored(key: str) -> tuple[bytes, dict[str, str]]:
    """The object's bytes and user metadata."""
    obj = _s3().get_object(Bucket=os.environ["KDIVE_S3_BUCKET"], Key=key)
    return obj["Body"].read(), dict(obj.get("Metadata", {}))


def _versions(key: str) -> list[str]:
    """The version ids, delete markers included, stored under exactly ``key``."""
    listing = _s3().list_object_versions(Bucket=os.environ["KDIVE_S3_BUCKET"], Prefix=key)
    items = [*listing.get("Versions", []), *listing.get("DeleteMarkers", [])]
    return [str(item["VersionId"]) for item in items if item["Key"] == key]


def _purge(key: str) -> bool:
    """Delete every version of ``key``; whether none is left."""
    client, bucket = _s3(), os.environ["KDIVE_S3_BUCKET"]
    for version in _versions(key):
        client.delete_object(Bucket=bucket, Key=key, VersionId=version)
    return not _versions(key)


async def _put(item: ToolResponse, data: bytes) -> None:
    """PUT ``data`` to an upload item's presigned URL with exactly its required headers."""
    headers = cast(dict[str, str], item.data["required_headers"])
    async with httpx.AsyncClient(timeout=60.0) as http:
        response = await http.put(item.refs["upload_url"], content=data, headers=headers)
    # The status alone: an HTTPStatusError would echo the presigned URL and its signature.
    assert response.status_code == 200, f"presigned PUT answered HTTP {response.status_code}"


def _declaration(name: str, data: bytes) -> dict[str, object]:
    return {"name": name, "sha256": _b64_sha256(data), "size_bytes": len(data)}


def _failure(result: object) -> str:
    return str(getattr(result, "detail", None) or getattr(result, "error_category", result))


Removal = Callable[[], Awaitable[list[str]]]


async def _attempt(remove: Removal) -> list[str]:
    try:
        return await remove()
    except Exception as exc:  # noqa: BLE001 - reported as a cleanup problem, never raised over the cell
        return [repr(exc)]


@asynccontextmanager
async def _cleaning(remove: Removal, what: str) -> AsyncIterator[None]:
    """Run ``remove`` on exit; a failed body keeps its error and gains the cleanup problems."""
    try:
        yield
    except BaseException as exc:
        problems = await _attempt(remove)
        if problems:
            exc.add_note(f"cleanup of {what} also failed: {problems}")
        raise
    problems = await _attempt(remove)
    assert not problems, f"cleanup of {what} failed: {problems}"


@dataclass
class _Investigation:
    """A fixture investigation, the Runs to cancel and the unadopted object keys to purge."""

    id: str
    caller: HttpCaller
    token: str
    runs: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)


async def _settle(inv: _Investigation) -> list[str]:
    """Purge ``inv``'s unadopted objects, cancel its Runs and close it; what could not end.

    Each step runs whatever an earlier one raised, so a store fault still closes ``inv``.
    """

    owned = (f"local/investigations/{inv.id}/", *(f"local/runs/{run}/" for run in inv.runs))

    async def purge(key: str) -> list[str]:
        # A key outside the cell's own owners is never deleted, whatever the server returned.
        if not key.startswith(owned):
            return [f"object {key} is outside {owned}; not purged"]
        gone = await asyncio.to_thread(_purge, key)
        return [] if gone else [f"object {key} still has versions"]

    async def call(tool: str, args: Mapping[str, object], status: str) -> list[str]:
        result = await inv.caller.call(tool, args, inv.token)
        if isinstance(result, ToolResponse) and result.status == status:
            return []
        return [f"{tool} {args}: {_failure(result)}"]

    steps = [partial(purge, key) for key in sorted(inv.keys)]
    steps += [partial(call, "runs.cancel", {"run_id": run}, "canceled") for run in inv.runs]
    closing = {"investigation_id": inv.id, "summary": "coverage cell finished"}
    steps.append(partial(call, "investigations.close", closing, "closed"))
    problems: list[str] = []
    for step in steps:
        problems += await _attempt(step)
    return problems


@asynccontextmanager
async def _closing(inv: _Investigation) -> AsyncIterator[_Investigation]:
    async with _cleaning(partial(_settle, inv), f"investigation {inv.id}"):
        yield inv


@asynccontextmanager
async def _investigation(
    base_url: str, issuer: OidcIssuer, project: str, *, refs: tuple[dict[str, str], ...] = (_REF_A,)
) -> AsyncIterator[_Investigation]:
    """An open investigation of ``project`` with a description and ``refs``; closed on exit."""
    fixture = HttpCaller("direct", base_url, issuer)
    token = fixture.token(_contributor(project))
    args = {
        "project": project,
        "title": "cov investigation",
        "description": "cov description",
        "external_refs": list(refs),
    }
    env = one(await fixture.call("investigations.open", args, token))
    async with _closing(_Investigation(env.object_id, fixture, token)) as inv:
        yield inv


async def _unbound_run(inv: _Investigation, *, cancel: bool = True) -> str:
    """An unbound Run of ``inv``; canceled when ``inv`` settles unless ``cancel`` is false."""
    args = {
        "investigation_id": inv.id,
        "build_profile": _BUILD_PROFILE,
        "target_kind": "local-libvirt",
    }
    env = one(await inv.caller.call("runs.create", args, inv.token))
    if cancel:
        inv.runs.append(env.object_id)
    return env.object_id


def _synthetic_build() -> tuple[bytes, bytes]:
    """The synthetic kernel bundle and a vmlinux carrying its build-id note.

    The bundle's ``boot/vmlinuz`` embeds a gzip-compressed ELF with only a ``PT_NOTE`` program
    header; ``runs.complete_build`` reads a vmlinux's build-id through its section headers, so
    the vmlinux is that ELF with a two-entry table: the null section and an ``SHT_NOTE`` over the
    note at offset 176 (64-byte ELF header + two 56-byte program headers), 28 bytes long.
    """
    bundle = valid_combined_kernel_tar()
    with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
        member = archive.extractfile("boot/vmlinuz")
        assert member is not None, "the synthetic bundle has no boot/vmlinuz"
        elf = bytearray(gzip.decompress(member.read()[0x400:]))
    table = bytearray(128)
    struct.pack_into("<IIQQQQIIQQ", table, 64, 0, 7, 0, 0, 176, 28, 0, 0, 4, 0)
    struct.pack_into("<Q", elf, 0x28, len(elf))
    struct.pack_into("<HH", elf, 0x3A, 64, 2)
    return bundle, bytes(elf + table)


async def _built_run(inv: _Investigation) -> tuple[str, bytes]:
    """A Run of ``inv`` whose synthetic build ``runs.complete_build`` accepted, and its vmlinux.

    Until the build succeeds the Run is canceled and its objects purged when ``inv`` settles; a
    succeeded Run is history to the live snapshot, and ``inv``'s close hands its build to the
    reconciler's build GC (``cleanup_pending_at``).
    """
    run_id = await _unbound_run(inv)
    bundle, vmlinux = _synthetic_build()
    declared = [_declaration("kernel", bundle), _declaration("vmlinux", vmlinux)]
    args = {"run_id": run_id, "artifacts": declared}
    env = one(await inv.caller.call("artifacts.create_run_upload", args, inv.token))
    keys = {item.object_id for item in env.items}
    inv.keys |= keys
    for item in env.items:
        await _put(item, bundle if item.data.get("name") == "kernel" else vmlinux)
    # Untracked before the adopting call: a response lost after the build committed must not
    # purge objects its artifact rows now own. An unadopted upload is left to the manifest reaper.
    inv.keys -= keys
    args = {"run_id": run_id, "build_id": _BUILD_ID}
    built = one(await inv.caller.call("runs.complete_build", args, inv.token))
    assert built.status == "succeeded", f"runs.complete_build answered {built.status}"
    # The close hands the succeeded build to the build GC.
    inv.runs.remove(run_id)
    return run_id, vmlinux


@dataclass
class _System:
    """The session's System in ``KDIVE_PROJECT``; ``id`` is empty until it is ``ready``."""

    base_url: str
    issuer: OidcIssuer
    project: str
    allocation: str
    in_use_before: int
    id: str = ""
    disks: list[str] = field(default_factory=list)
    settled: bool = False


_SYSTEMS: dict[str, _System] = {}


def _operator_token(caller: HttpCaller, project: str) -> str:
    return caller.token(Grants(f"{project}-cov", (project,), {project: "operator"}))


async def _session_system(base_url: str, issuer: OidcIssuer, db_url: str) -> _System:
    """The session's ``ready`` System with settled console parts, provisioned on first use."""
    if base_url in _SYSTEMS:
        system = _SYSTEMS[base_url]
        assert system.settled, "the session System failed to provision in an earlier cell"
        return system
    project = _funded_project()
    entry = load_rootfs_catalog()[_IMAGE]
    operator = HttpCaller("direct", base_url, issuer)
    async with LiveStackClient.over_http(base_url, _operator_token(operator, project)) as op:
        await acquire_image(op, _IMAGE, entry.arch)
        in_use = await capacity_in_use(op)
        sizing = {"vcpus": 1, "memory_gb": 1, "disk_gb": LOCAL_ALLOCATION_DISK_GB}
        allocation = one(await op.call_tool("allocations.request", project=project, **sizing))
        system = _System(base_url, issuer, project, allocation.object_id, in_use)
        _SYSTEMS[base_url] = system
        profile = {**catalog_profile(entry, _IMAGE, f"{project}-unread"), "vcpu": 1}
        profile["memory_mb"] = 1024
        system_id = await provision_to_ready(
            op, allocation_id=system.allocation, profile=profile, phase_name="session-system"
        )
    system.id = system_id
    system.disks = domain_disks(await asyncio.to_thread(domain_xml, system_id))
    await _settled_parts(db_url, system_id)
    system.settled = True
    return system


async def _settled_parts(db_url: str, system_id: str) -> None:
    """Wait until the System holds a redacted console part and the set stopped changing."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PART_DEADLINE_S
    seen: list[str] = []
    stable_since = loop.time()
    while loop.time() < deadline:
        ids = [str(row["id"]) for row in await _rows(db_url, _SYSTEM_PARTS, (system_id,))]
        if ids != seen:
            seen, stable_since = ids, loop.time()
        elif seen and loop.time() - stable_since >= _PART_SETTLE_S:
            return
        await asyncio.sleep(10.0)
    raise AssertionError(f"System {system_id} console parts did not settle: {len(seen)} part(s)")


async def _release(system: _System) -> None:
    """Release the session System and prove its domain, disks and capacity were reclaimed."""
    operator = HttpCaller("direct", system.base_url, system.issuer)
    token = _operator_token(operator, system.project)
    async with LiveStackClient.over_http(system.base_url, token) as op:
        if not system.id:
            one(await op.call_tool("allocations.release", allocation_id=system.allocation))
            return
        await release_and_verify(
            op,
            allocation_id=system.allocation,
            system_id=system.id,
            domain=domain_name_for(UUID(system.id)),
            disks=system.disks,
            in_use_before=system.in_use_before,
        )


@pytest.fixture(scope="module", autouse=True)
def _release_session_systems() -> Iterator[None]:
    """Release every session System when the module's cells are done."""
    yield
    systems = list(_SYSTEMS.values())
    _SYSTEMS.clear()
    for system in systems:
        asyncio.run(_release(system))


async def _oldest_part(db_url: str, system_id: str) -> dict[str, Any]:
    rows = await _rows(db_url, _SYSTEM_PARTS, (system_id,))
    assert rows, f"System {system_id} has no redacted console part"
    return rows[-1]


def _not_found(result: object) -> bool:
    return (
        isinstance(result, ToolResponse) and result.error_category == ErrorCategory.NOT_FOUND.value
    )


_FIELDS = ("project", "title", "description", "summary", "external_refs", "state")


async def _investigation_row(db_url: str, investigation_id: str) -> dict[str, Any]:
    rows = await _rows(db_url, "SELECT * FROM investigations WHERE id = %s", (investigation_id,))
    assert len(rows) == 1, f"investigation {investigation_id} has {len(rows)} rows"
    return rows[0]


def _row_view(row: Mapping[str, Any]) -> dict[str, object]:
    return {k: row[k] for k in _FIELDS}


def _refs(refs: object) -> list[dict[str, str]]:
    """External refs in a stable order, so an upsert's position does not matter."""
    listed = cast(list[dict[str, str]], refs)
    return sorted(listed, key=lambda ref: (ref["tracker"], ref["id"]))


async def _pages(
    caller: HttpCaller, tool: str, token: str, args: Mapping[str, object], cursor_in: str
) -> list[str]:
    """Every item id of a paginated tool; ``cursor_in`` names where the cursor goes."""
    ids: list[str] = []
    seen: set[object] = set()
    request = dict(args)
    while True:
        call = {"request": request} if cursor_in == "request" else request
        env = one(await caller.call(tool, call, token, discover=not ids))
        ids.extend(item.object_id for item in env.items)
        cursor = env.data.get("next_cursor")
        if not env.data.get("truncated") or not cursor:
            return ids
        assert cursor not in seen, f"{tool} repeated a cursor"
        seen.add(cursor)
        request = {**request, "cursor": cursor}


async def _open(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project = grants.projects[0]
    args = {
        "project": project,
        "title": "cov open",
        "description": "cov opened",
        "external_refs": [_REF_A],
    }
    env = one(await caller.call("investigations.open", args, caller.token(grants), discover=True))
    fixture = HttpCaller("direct", caller.base_url, caller.issuer)
    inv = _Investigation(env.object_id, fixture, fixture.token(_contributor(project)))
    async with _closing(inv):
        expected = {**args, "summary": None, "state": "open"}
        assert _row_view(await _investigation_row(db_url, inv.id)) == expected
        stranger = caller.token(_viewer(f"cov-{secrets.token_hex(4)}"))
        hidden = await caller.call("investigations.get", {"investigation_id": inv.id}, stranger)
        assert _not_found(hidden), "another project can read the new investigation"
        listed = await _pages(caller, "investigations.list", stranger, {}, "request")
        assert inv.id not in listed, "another project lists the new investigation"
    return {
        "row_equals_arguments": sorted(expected),
        "other_project_hidden": True,
        "owned": [inv.id],
    }


async def _get(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project = grants.projects[0]
    async with _investigation(
        caller.base_url, caller.issuer, project, refs=(_REF_A, _REF_B)
    ) as inv:
        run_id = await _unbound_run(inv)
        args = {"investigation_id": inv.id}
        env = one(
            await caller.call("investigations.get", args, caller.token(grants), discover=True)
        )
        row = await _investigation_row(db_url, inv.id)
        assert row["state"] == "active", f"a Run left the investigation {row['state']}"
        observed = {k: env.data.get(k) for k in _FIELDS}
        assert observed == _row_view(row), f"get {observed} != row {_row_view(row)}"
        bound = await _rows(db_url, "SELECT id FROM runs WHERE investigation_id = %s", (inv.id,))
        runs = [str(r["id"]) for r in bound]
        assert runs == [run_id] and env.data.get("runs") == runs, f"runs {env.data.get('runs')}"
    return {"fields_equal": list(_FIELDS), "runs": len(runs), "owned": [inv.id, run_id]}


async def _list(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project, token = grants.projects[0], caller.token(grants)
    base, issuer = caller.base_url, caller.issuer
    async with (
        _investigation(base, issuer, project) as first,
        _investigation(base, issuer, project) as second,
        _investigation(base, issuer, project) as third,
    ):
        closing = {"investigation_id": first.id, "summary": "cov closed early"}
        one(await first.caller.call("investigations.close", closing, first.token))
        query: LiteralString = (
            "SELECT id FROM investigations WHERE project = %s ORDER BY created_at DESC, id DESC"
        )
        expected = [str(r["id"]) for r in await _rows(db_url, query, (project,))]
        assert sorted(expected) == sorted([first.id, second.id, third.id])
        listed = await _pages(caller, "investigations.list", token, {"project": project}, "request")
        assert listed == expected, f"listed {listed} != newest-first {expected}"
        visible = await _pages(caller, "investigations.list", token, {}, "request")
        assert visible == expected, "the caller lists investigations of a project it cannot view"
        state = {"project": project, "state": "closed"}
        closed = await _pages(caller, "investigations.list", token, state, "request")
        assert closed == [first.id], f"state=closed listed {closed}"
        paged_args = {"project": project, "limit": 1}
        paged = await _pages(caller, "investigations.list", token, paged_args, "request")
        assert paged == expected, f"limit=1 pages listed {paged}"
    return {"listed": len(expected), "closed_filter": 1, "page_size": 1, "owned": expected}


async def _set(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    async with _investigation(caller.base_url, caller.issuer, grants.projects[0]) as inv:
        before = await _investigation_row(db_url, inv.id)
        args = {"investigation_id": inv.id, "title": "cov retitled", "description": "cov edited"}
        one(await caller.call("investigations.set", args, caller.token(grants), discover=True))
        after = await _investigation_row(db_url, inv.id)
        assert (after["title"], after["description"]) == ("cov retitled", "cov edited")
        kept = ("project", "summary", "external_refs", "state", "created_at")
        assert {k: after[k] for k in kept} == {k: before[k] for k in kept}, "set changed more"
    return {"changed": ["title", "description"], "unchanged": list(kept), "owned": [inv.id]}


async def _ref_edit(
    caller: HttpCaller,
    grants: Grants,
    *,
    db_url: str,
    tool: str,
    seeded: tuple[dict[str, str], ...],
    ref: Mapping[str, str],
    expected: list[dict[str, str]],
) -> dict[str, object]:
    """Edit ``ref`` on one of two investigations; only that one's refs change, to ``expected``."""
    project, base, issuer = grants.projects[0], caller.base_url, caller.issuer
    async with (
        _investigation(base, issuer, project, refs=seeded) as inv,
        _investigation(base, issuer, project, refs=seeded) as other,
    ):
        untouched = await _investigation_row(db_url, other.id)
        args = {"investigation_id": inv.id, "ref": dict(ref)}
        one(await caller.call(tool, args, caller.token(grants), discover=True))
        row = await _investigation_row(db_url, inv.id)
        assert _refs(row["external_refs"]) == _refs(expected), f"refs {row['external_refs']}"
        assert await _investigation_row(db_url, other.id) == untouched, "the other one changed"
    return {"refs": len(expected), "other_unchanged": True, "owned": [inv.id, other.id]}


async def _close(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    summary = "cov closing summary"
    async with _investigation(caller.base_url, caller.issuer, grants.projects[0]) as inv:
        args = {"investigation_id": inv.id, "summary": summary}
        token = caller.token(grants)
        env = one(await caller.call("investigations.close", args, token, discover=True))
        assert env.status == "closed", f"investigations.close answered {env.status}"
        row = await _investigation_row(db_url, inv.id)
        assert (row["state"], row["summary"]) == ("closed", summary), "close was not persisted"
        got = one(await caller.call("investigations.get", {"investigation_id": inv.id}, token))
        assert (got.status, got.data.get("summary")) == ("closed", summary)
    return {"state": "closed", "summary_persisted": True, "owned": [inv.id]}


async def _manifest(db_url: str, owner_id: str) -> dict[str, Any]:
    rows = await _rows(db_url, "SELECT * FROM upload_manifests WHERE owner_id = %s", (owner_id,))
    assert len(rows) == 1, f"{owner_id} has {len(rows)} upload manifests"
    return rows[0]


async def _upload(
    caller: HttpCaller, token: str, inv: _Investigation, tool: str, args: Mapping[str, object]
) -> tuple[ToolResponse, bytes]:
    """Mint one upload window for fresh bytes, PUT them and track the key for purging."""
    data = secrets.token_bytes(4096)
    name = "rootfs" if tool == "artifacts.create_investigation_upload" else "kernel"
    call = {**args, "artifacts": [_declaration(name, data)]}
    env = one(await caller.call(tool, call, token, discover=True))
    # Scalars only: an assertion on the envelope would print its presigned upload URL.
    status, count = env.status, len(env.items)
    assert status == "upload_ready" and count == 1, f"{tool} answered {status} with {count} items"
    item = env.items[0]
    inv.keys.add(item.object_id)
    await _put(item, data)
    return item, data


async def _check_upload(
    db_url: str, item: ToolResponse, data: bytes, *, owner_kind: str, owner_id: str
) -> str:
    """The stored bytes are ``data`` under the owner's prefix and its manifest declares them."""
    key = item.object_id
    stored, _ = await asyncio.to_thread(_stored, key)
    assert stored == data, "the stored object is not the uploaded bytes"
    manifest = await _manifest(db_url, owner_id)
    assert manifest["owner_kind"] == owner_kind, f"manifest owner {manifest['owner_kind']}"
    assert key.startswith(manifest["prefix"]), f"{key} is outside {manifest['prefix']}"
    assert manifest["prefix"] == f"local/{owner_kind}/{owner_id}/", manifest["prefix"]
    assert _b64_sha256(data) in str(manifest["manifest"]), "the manifest does not declare them"
    return key


async def _create_investigation_upload(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    async with _investigation(caller.base_url, caller.issuer, grants.projects[0]) as inv:
        tool, args = "artifacts.create_investigation_upload", {"investigation_id": inv.id}
        item, data = await _upload(caller, caller.token(grants), inv, tool, args)
        key = await _check_upload(db_url, item, data, owner_kind="investigations", owner_id=inv.id)
    return {"stored_equal": True, "owner": "investigations", "owned": [inv.id, key]}


async def _create_run_upload(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    async with _investigation(caller.base_url, caller.issuer, grants.projects[0]) as inv:
        run_id = await _unbound_run(inv)
        tool, args = "artifacts.create_run_upload", {"run_id": run_id}
        item, data = await _upload(caller, caller.token(grants), inv, tool, args)
        key = await _check_upload(db_url, item, data, owner_kind="runs", owner_id=run_id)
        assert key == f"local/runs/{run_id}/kernel", key
    return {"stored_equal": True, "owner": "runs", "owned": [inv.id, run_id, key]}


async def _complete_rootfs_upload(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    async with _investigation(caller.base_url, caller.issuer, grants.projects[0]) as inv:
        tool, args = "artifacts.create_investigation_upload", {"investigation_id": inv.id}
        item, data = await _upload(inv.caller, inv.token, inv, tool, args)
        finalize = {"investigation_id": inv.id}
        token = caller.token(grants)
        # Untracked before the adopting call, as in _built_run; the close hands the finalized
        # object to the rootfs reclaim.
        inv.keys.discard(item.object_id)
        env = one(
            await caller.call(
                "investigations.complete_rootfs_upload", finalize, token, discover=True
            )
        )
        key = str(env.data.get("object_key"))
        assert env.data.get("checksum_sha256") == _b64_sha256(data), "handle is not the digest"
        assert key == item.object_id and key.startswith(f"local/investigations/{inv.id}/")
        stored, _ = await asyncio.to_thread(_stored, key)
        assert stored == data, "the finalized object is not the uploaded bytes"
        rows = await _rows(
            db_url,
            "SELECT owner_kind, retention_class FROM artifacts WHERE owner_id = %s "
            "AND object_key = %s",
            (inv.id, key),
        )
        assert [(r["owner_kind"], r["retention_class"]) for r in rows] == [
            ("investigations", "rootfs")
        ], f"finalized rows {rows}"
        manifests = await _rows(
            db_url, "SELECT 1 FROM upload_manifests WHERE owner_id = %s", (inv.id,)
        )
        assert not manifests, "the finalize left its upload manifest"
    pending = (await _investigation_row(db_url, inv.id))["rootfs_cleanup_pending_at"]
    assert pending is not None, "the close did not mark the rootfs reclaim pending"
    return {"handle_is_digest": True, "close_marked_pending": True, "owned": [inv.id, key]}


async def _fetch_raw(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    async with _investigation(caller.base_url, caller.issuer, grants.projects[0]) as inv:
        run_id, vmlinux = await _built_run(inv)
        args = {"run_id": run_id, "asset": "vmlinux"}
        env = one(
            await caller.call("artifacts.fetch_raw", args, caller.token(grants), discover=True)
        )
        # Scalars only from here: an assertion on the envelope would print its presigned URL.
        url, answered = (
            env.refs["download_uri"],
            (env.data.get("asset"), env.data.get("size_bytes")),
        )
        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.get(url)
        # The status alone: an HTTPStatusError would echo the presigned URL and its signature.
        assert response.status_code == 200, f"download answered HTTP {response.status_code}"
        assert response.content == vmlinux, "the downloaded vmlinux is not the uploaded one"
        assert answered == ("vmlinux", len(vmlinux)), f"fetch_raw answered {answered}"
    run = await _rows(db_url, "SELECT state FROM runs WHERE id = %s", (run_id,))
    pending = (await _investigation_row(db_url, inv.id))["cleanup_pending_at"]
    assert run[0]["state"] == "succeeded" and pending is not None, (
        "the build did not succeed or the close marked no GC"
    )
    return {
        "bytes_equal": len(vmlinux),
        "close_marked_pending": True,
        "owned": [inv.id, run_id],
    }


async def _artifacts_list(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    system = await _session_system(caller.base_url, caller.issuer, db_url)
    token = caller.token(grants)
    args = {"system_id": system.id, "limit": 1}
    for _ in range(2):
        before = [str(r["id"]) for r in await _rows(db_url, _SYSTEM_PARTS, (system.id,))]
        listed = await _pages(caller, "artifacts.list", token, args, "flat")
        after = [str(r["id"]) for r in await _rows(db_url, _SYSTEM_PARTS, (system.id,))]
        if before == after:
            break
    else:
        raise AssertionError("console rotation changed the System's artifacts twice in a row")
    assert listed == before, f"listed {listed} != newest-first {before}"
    return {"artifacts": len(listed), "page_size": 1}


def _literal(body: bytes) -> str:
    """A printable literal from ``body`` that ``find`` accepts (no ``|``, no NUL)."""
    for line in body.split(b"\n"):
        text = line.strip()
        if len(text) >= 12 and text.isascii() and text.decode().isprintable() and b"|" not in text:
            return text[:24].decode()
    raise AssertionError("the console part has no printable line to search for")


async def _artifacts_get(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    system = await _session_system(caller.base_url, caller.issuer, db_url)
    part = await _oldest_part(db_url, system.id)
    raw, metadata = await asyncio.to_thread(_stored, str(part["object_key"]))
    assert metadata.get("sensitivity") == "redacted", f"stored sensitivity {metadata}"
    body = gzip.decompress(raw) if metadata.get("content-encoding") == "gzip" else raw
    token, artifact = caller.token(grants), str(part["id"])

    async def get(**request: object) -> dict[str, object]:
        """The answer's ``data`` alone: its ``refs`` carry a presigned download URL."""
        call = {"request": {"artifact_id": artifact, **request}}
        return dict(one(await caller.call("artifacts.get", call, token, discover=True)).data)

    forward = await get(max_bytes=512)
    assert forward.get("size_bytes") == len(body), "size_bytes is not the stored body's"
    assert forward.get("content") == body[:512].decode("utf-8", errors="replace")
    backward = await get(max_bytes=512, direction="backward")
    assert backward.get("content") == body[-512:].decode("utf-8", errors="replace")
    term = _literal(body)
    offset = body.find(term.encode())
    hit = await get(find=term)
    expected = {"match_found": True, "match_offset": offset}
    expected["match_line"] = body.count(b"\n", 0, offset) + 1
    assert {k: hit.get(k) for k in expected} == expected, f"find answered {hit}"
    absent = f"cov-absent-{secrets.token_hex(8)}"
    assert absent.encode() not in body
    miss = await get(find=absent)
    assert miss.get("match_found") is False, f"find of an absent term answered {miss}"
    return {"bytes": len(body), "windows": ["forward", "backward"], "find": "hit-and-miss"}


_FUNCTIONAL: dict[str, Callable[..., Awaitable[dict[str, object]]]] = {
    "artifacts.create_investigation_upload": _create_investigation_upload,
    "artifacts.create_run_upload": _create_run_upload,
    "artifacts.fetch_raw": _fetch_raw,
    "artifacts.get": _artifacts_get,
    "artifacts.list": _artifacts_list,
    "investigations.close": _close,
    "investigations.complete_rootfs_upload": _complete_rootfs_upload,
    "investigations.get": _get,
    "investigations.link": partial(
        _ref_edit,
        tool="investigations.link",
        seeded=(_REF_A,),
        ref=_REF_B,
        expected=[_REF_A, _REF_B],
    ),
    "investigations.list": _list,
    "investigations.open": _open,
    "investigations.set": _set,
    "investigations.unlink": partial(
        _ref_edit,
        tool="investigations.unlink",
        seeded=(_REF_A, _REF_B),
        ref={"tracker": _REF_A["tracker"], "id": _REF_A["id"]},
        expected=[_REF_B],
    ),
}
_SYSTEM_TOOLS = frozenset({"artifacts.get", "artifacts.list"})
_READERS = frozenset({"investigations.get", "investigations.list", *_SYSTEM_TOOLS})
_RUN_TARGETS = frozenset({"artifacts.create_run_upload", "artifacts.fetch_raw"})
# A non-member gets the answer an absent owner gets (artifacts/uploads.py _create_upload,
# complete_rootfs_upload.py); investigations.open names its project, so it is denied outright.
_ISOLATION = {
    "artifacts.create_investigation_upload": ErrorCategory.CONFIGURATION_ERROR,
    "artifacts.create_run_upload": ErrorCategory.CONFIGURATION_ERROR,
    "investigations.complete_rootfs_upload": ErrorCategory.CONFIGURATION_ERROR,
    "investigations.open": ErrorCategory.AUTHORIZATION_DENIED,
}


def _functional_grants(tool: str, project: str) -> Grants:
    if tool in _SYSTEM_TOOLS:
        funded = _funded_project()
        return Grants(f"{project}-viewer", (funded,), {funded: "viewer"})
    return _viewer(project) if tool in _READERS else _contributor(project)


def _valid(tool: str, project: str) -> dict[str, object]:
    """Arguments that change nothing for the cell's issued-token control call."""
    table: dict[str, dict[str, object]] = {
        "artifacts.create_investigation_upload": {
            "investigation_id": _ABSENT_ID,
            "artifacts": [_declaration("rootfs", b"cov")],
        },
        "artifacts.create_run_upload": {
            "run_id": _ABSENT_ID,
            "artifacts": [_declaration("kernel", b"cov")],
        },
        "artifacts.fetch_raw": {"run_id": _ABSENT_ID, "asset": "vmlinux"},
        "artifacts.get": {"request": {"artifact_id": _ABSENT_ID}},
        "artifacts.list": {"system_id": _ABSENT_ID},
        "investigations.close": {"investigation_id": _ABSENT_ID, "summary": "cov denied"},
        "investigations.complete_rootfs_upload": {"investigation_id": _ABSENT_ID},
        "investigations.get": {"investigation_id": _ABSENT_ID},
        "investigations.link": {"investigation_id": _ABSENT_ID, "ref": _REF_B},
        "investigations.list": {"request": {"project": project}},
        # Only a viewer's token reaches the authentication control, so nothing is opened.
        "investigations.open": {"project": project, "title": "cov denied"},
        "investigations.set": {"investigation_id": _ABSENT_ID, "title": "cov denied"},
        "investigations.unlink": {
            "investigation_id": _ABSENT_ID,
            "ref": {"tracker": _REF_A["tracker"], "id": _REF_A["id"]},
        },
    }
    return table[tool]


def _invalid(tool: str, project: str) -> dict[str, object]:
    """Schema-invalid arguments: a missing required one, a wrong type or an unknown value."""
    table: dict[str, dict[str, object]] = {
        "artifacts.create_investigation_upload": {"investigation_id": _ABSENT_ID},
        "artifacts.create_run_upload": {"run_id": _ABSENT_ID},
        "artifacts.fetch_raw": {"run_id": _ABSENT_ID, "asset": "core"},
        "artifacts.get": {"request": {"artifact_id": _ABSENT_ID, "direction": "sideways"}},
        "artifacts.list": {"limit": 5},
        "investigations.close": {"investigation_id": _ABSENT_ID},
        "investigations.complete_rootfs_upload": {},
        "investigations.get": {},
        "investigations.link": {
            "investigation_id": _ABSENT_ID,
            "ref": {"tracker": "cov", "id": "B-2"},
        },
        "investigations.list": {"request": {"limit": "many"}},
        "investigations.open": {"project": project},
        "investigations.set": {"title": "cov invalid"},
        "investigations.unlink": {"investigation_id": _ABSENT_ID, "ref": "A-1"},
    }
    return table[tool]


def _rejection(tool: str, boundary: Boundary, project: str) -> Rejection:
    if boundary == "validation":
        grants = _viewer(project) if tool in _READERS else _contributor(project)
        return Rejection(_invalid(tool, project), grants)
    args = _valid(tool, project)
    if boundary == "authentication":
        return Rejection(args, _viewer(project))
    if boundary == "project-isolation":
        category = _ISOLATION.get(tool, ErrorCategory.NOT_FOUND)
        return Rejection(args, _stranger(), frozenset({category.value}))
    if tool in _SYSTEM_TOOLS:
        return Rejection(args, _member(_funded_project()))
    return Rejection(args, _member(project) if tool in _READERS else _viewer(project))


def _target_setup(
    base_url: str, issuer: OidcIssuer, project: str, tool: str
) -> Callable[[], AbstractAsyncContextManager[Mapping[str, object]]]:
    """A live investigation (or unbound Run) of ``project`` for the rejected call to aim at."""

    @asynccontextmanager
    async def setup() -> AsyncIterator[Mapping[str, object]]:
        async with _investigation(base_url, issuer, project) as inv:
            if tool == "investigations.complete_rootfs_upload":
                # A window holding bytes: a finalize that skipped the boundary would adopt them
                # and change the snapshot, so configuration_error alone cannot pass the cell.
                tool_args = {"investigation_id": inv.id}
                window = "artifacts.create_investigation_upload"
                await _upload(inv.caller, inv.token, inv, window, tool_args)
            if tool in _RUN_TARGETS:
                yield {"run_id": await _unbound_run(inv)}
            elif tool == "investigations.list":
                yield {}
            else:
                yield {"investigation_id": inv.id}

    return setup


async def _system_args(tool: str, db_url: str, system: _System) -> dict[str, object]:
    if tool == "artifacts.list":
        return {"system_id": system.id}
    part = await _oldest_part(db_url, system.id)
    return {"request": {"artifact_id": str(part["id"])}}


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell, tool = run.cell, run.cell.operation
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    project = f"cov-{secrets.token_hex(4)}"
    targeted = cell.kind == "functional" or boundary_of(cell) in (
        "authorization",
        "project-isolation",
    )
    snapshot: Snapshot = partial(_live_state, db_url, project)
    system = None
    if tool in _SYSTEM_TOOLS and targeted:
        # Console rotation adds parts to the System whenever its guest writes enough, so its
        # rows stay out of the snapshot; artifacts.list compares them inside its body.
        system = await _session_system(base_url, issuer, db_url)
    if cell.kind == "functional":
        body = cast(Functional, partial(_FUNCTIONAL[tool], db_url=db_url))
        await prove_functional(run, caller, _functional_grants(tool, project), body, snapshot)
        return
    boundary = boundary_of(cell)
    rejection = _rejection(tool, boundary, project)
    setup = None
    if system is not None:
        rejection = Rejection(
            await _system_args(tool, db_url, system), rejection.grants, rejection.categories
        )
    elif targeted and tool != "investigations.open":
        setup = _target_setup(base_url, issuer, project, tool)
    await prove_rejection(run, caller, boundary, rejection, snapshot, setup=setup)


@pytest.mark.parametrize("cell", tool_cells(TOOLS), ids=lambda cell: cell.id)
def test_investigation_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of an investigation or artifact tool."""
    run_tool_cell(cell, _scenario)
```

3. Run the collection command; it is green with 260 tests collected. Then run `just lint` and
   `just type`; both are green. Commit
   `test(live): prove the investigation and artifact tool cells (#3096)`.

## Task 3 — runbook section

Files: modify `docs/operating/runbooks/live-testing.md`.

Verification:

- Runbook section. Mode: task-test-not-applicable. It is operator prose with no executable
  consumer; `just docs-check` validates its links and paths.

Steps:

1. After the catalog section's final paragraph (`Queue depth is compared but not driven
   (#3106).`), add a `#### Investigation and artifact tool cells (#3096)` section that states:
   - the node and the 13 tools, and that all three carriers run in each lane's pytest command;
   - a link to the design;
   - the shared environment (`env.sh`, `KDIVE_DATABASE_URL`, `KDIVE_SYSTEMS_TOML`), the staged
     `fedora-kdive-ready-44` image and KVM;
   - the cleanup model: closed investigations, ended Runs, the live snapshot;
   - the upload, build and session-System fixtures and their reclaim;
   - the 12 cells that fail pending #3108;
   - that the wipe clears everything a killed cell leaves behind.
2. Run `just docs-check`; it is green. Commit `docs(runbook): run the investigation tool cells`.

## Task 4 — live proof on the lab host

Verification:

- Live cells. Mode: task-test-not-applicable. They need the live stack; the evidence is the
  `qualify` report.

Steps:

1. Ship the branch head to the lab host as a git bundle, then check it out there.
2. Run `just sync` and `just build-capture-bootstrap-manifest`. Write the bindings with
   `uv run --no-sync python -m tests.integration.live_stack.tool_cells bindings --candidate
   <head> --out ~/inputs-3096.json`.
3. For each lane (`default`, then `recovery` with `KDIVE_WORKER_DEATH_VERIFIER=docker`):
   1. Bring the stack up with `examples/local-libvirt/demo-up.sh` and confirm that all three
      `/readyz` endpoints report the head commit.
   2. Run pytest `-m live_stack` over the core, catalog and investigation carriers into one
      evidence root, with `KDIVE_SYSTEMS_TOML` and
      `KDIVE_DATABASE_URL=$KDIVE_MIGRATION_DATABASE_URL` exported.

   Expected per lane, out of 468 collected cells: 228 passed, 6 failed (the #3108 cells) and
   234 skipped (the other configuration).
4. Assemble the evidence (`python -m tests.integration.live_stack.evidence assemble`), then
   qualify (`python -m scripts.coverage_campaign qualify`). Expected:
   - #2811's 56 cells and #3095's 152 cells are `yes`;
   - group 3096's 248 cells are `yes`;
   - the 12 #3108 cells are not.
5. Check for leftovers:
   - no libvirt domain;
   - no open `cov-` investigation or `created` Run;
   - the funded project's session allocation is `released`.

   Then wipe with `examples/local-libvirt/demo-down.sh --wipe --yes` and update the private lab
   state note.

## Spec coverage

| Spec requirement | Task |
|---|---|
| carrier, live snapshot, fixtures, functional effects, rejection cells | 2 |
| 65 bindings and the contract test | 1 |
| runbook section | 3 |
| two-lane live proof re-proving #2811 and #3095 | 4 |
