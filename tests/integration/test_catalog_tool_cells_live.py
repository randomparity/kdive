"""Prove the catalog and configuration MCP tool cells over HTTP (#3095, ADR-0722).

``live_stack``-marked. One parameter per contract cell of owner group 3095's 11 tools, framed by
:func:`~tests.integration.live_stack.tool_cells.run_tool_cell` like the #2811 core cells. A
functional cell compares the tool's answer with a source the server did not produce: the evidence
database read in a read-only session, ``systems.toml`` and the build-fs siblings of its staged
image, this host's CPU count and memory, or the bytes the cell uploaded. Cells that write catalog
state remove it and prove so with :func:`_catalog_state`, which adds the owner-keyed private images
and the global shapes catalog to the per-project snapshot. ``images.upload`` sources a quarantined
object the cell writes with the metadata the ADR-0048 upload reassembly sets.
``docs/operating/runbooks/live-testing.md`` covers the precondition and the run.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, LiteralString, cast

import httpx
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.images.rootfs.staged_provenance import config_sibling_path
from kdive.inventory.model import ImageEntry
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.catalog import (
    _SHAPES,
    _UPLOAD_NAME,
    _allocation,
    _availability,
    _availability_expected,
    _cleaning,
    _funded_project,
    _operator,
    _register,
    _release,
    _rows,
    _s3,
    _sha256_file,
    _staged_image,
    _uploading,
    _visible_resources,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Functional,
    Grants,
    HttpCaller,
    Rejection,
    boundary_of,
    caller_for,
    one,
    project_state,
    prove_functional,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "images.delete",
    "images.describe",
    "images.kernel_config",
    "images.list",
    "images.upload",
    "resources.availability",
    "resources.describe",
    "resources.list",
    "shapes.delete",
    "shapes.list",
    "shapes.set",
)
_ABSENT_ID = "00000000-0000-4000-8000-000000000000"
_DIGEST: LiteralString = (
    "SELECT count(*) AS n, encode(sha256(convert_to(coalesce("
    "string_agg(t::text, ',' ORDER BY t::text), ''), 'UTF8')), 'hex') AS h "
)


async def _catalog_state(db_url: str, project: str) -> dict[str, list[object]]:
    """The per-project snapshot plus ``project``'s private images and every shape preset."""
    state = await project_state(db_url, project)
    owned = await _rows(db_url, _DIGEST + "FROM image_catalog t WHERE t.owner = %s", (project,))
    shapes = await _rows(db_url, _DIGEST + "FROM system_shapes t")
    state["image_catalog:owner"] = [owned[0]["n"], owned[0]["h"]]
    state["system_shapes"] = [shapes[0]["n"], shapes[0]["h"]]
    return state


def _viewer(project: str) -> Grants:
    return Grants(f"{project}-viewer", (project,), {project: "viewer"})


def _platform_operator(project: str) -> Grants:
    return Grants(f"{project}-platform", (project,), {project: "viewer"}, ("platform_operator",))


async def _image_id(db_url: str, entry: ImageEntry) -> str:
    rows = await _rows(
        db_url,
        "SELECT id FROM image_catalog WHERE provider = %s AND name = %s AND arch = %s "
        "AND visibility = 'public' AND state = 'registered'",
        (entry.provider, entry.name, entry.arch),
    )
    if len(rows) != 1:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{entry.name} is declared but not a registered catalog image"
        )
    return str(rows[0]["id"])


async def _pages(caller: HttpCaller, tool: str, token: str) -> list[ToolResponse]:
    """Every item of a paginated collection tool."""
    items: list[ToolResponse] = []
    request: dict[str, object] = {}
    while True:
        args = {"request": request} if request else {}
        env = one(await caller.call(tool, args, token, discover=True))
        items.extend(env.items)
        cursor = env.data.get("next_cursor")
        if not env.data.get("truncated") or not cursor:
            return items
        request = {"cursor": cursor}


def _not_found(result: ToolResponse | list[ToolResponse]) -> bool:
    return (
        isinstance(result, ToolResponse) and result.error_category == ErrorCategory.NOT_FOUND.value
    )


async def _describe(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    image = _staged_image()
    entry = image.entry
    image_id = await _image_id(db_url, entry)
    env = one(
        await caller.call(
            "images.describe", {"image_id": image_id}, caller.token(grants), discover=True
        )
    )
    expected: dict[str, object] = {
        "provider": entry.provider,
        "name": entry.name,
        "arch": entry.arch,
        "format": entry.format,
        "root_device": entry.root_device,
        "visibility": "public",
        "owner": "",
        "state": "registered",
        "capabilities": sorted(cap.value for cap in entry.capabilities),
    }
    observed: dict[str, object] = {k: env.data.get(k) for k in expected}
    observed["capabilities"] = sorted(cast(list[str], observed["capabilities"]))
    assert observed == expected, f"describe {observed} != systems.toml {expected}"
    digest = await asyncio.to_thread(_sha256_file, image.qcow2)
    assert env.data.get("digest") == f"sha256:{digest}", "digest is not the qcow2 file's"
    provenance = image.provenance
    assert env.data.get("provenance") == provenance, "provenance is not the sidecar's"
    signals = cast(dict[str, dict[str, object]], env.data.get("capability_signals"))
    computed = {
        "makedumpfile_version": signals["kdump"].get("makedumpfile_version"),
        "drgn_version": signals["live_drgn"].get("drgn_version"),
        "boot_kernel_count": signals["direct_kernel"].get("boot_kernel_count"),
    }
    recorded = {k: provenance.get(k) for k in computed}
    assert computed == recorded, f"capability signals {computed} != sidecar {recorded}"
    return {"image": entry.name, "equal_inventory": sorted(expected), "signals": computed}


async def _kernel_config(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    image = _staged_image()
    image_id = await _image_id(db_url, image.entry)
    env = one(
        await caller.call(
            "images.kernel_config", {"image_id": image_id}, caller.token(grants), discover=True
        )
    )
    async with httpx.AsyncClient(timeout=60.0) as http:
        response = await http.get(env.refs["download_uri"])
    # The status alone: an HTTPStatusError would echo the presigned URL and its signature.
    assert response.status_code == 200, f"config download answered HTTP {response.status_code}"
    expected = config_sibling_path(image.qcow2).read_bytes()
    assert hashlib.sha256(response.content).digest() == hashlib.sha256(expected).digest()
    assert env.data.get("size_bytes") == len(expected), "size_bytes is not the config's"
    version = image.provenance.get("default_kernel_version")
    assert env.data.get("default_kernel_version") == version, "kernel version differs"
    return {"image": image.entry.name, "config_bytes": len(expected), "kernel": version}


_VISIBLE_IMAGES: LiteralString = (
    "SELECT id FROM image_catalog WHERE visibility = 'public' "
    "OR (visibility = 'private' AND owner = ANY(%s))"
)


async def _list(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project, other = grants.projects[0], f"cov-{secrets.token_hex(4)}"
    async with _uploading(caller.base_url, caller.issuer, db_url, project) as upload:
        operator = HttpCaller("direct", caller.base_url, caller.issuer)
        image_id = await _register(operator, operator.token(grants), upload, db_url)
        own = {str(r["id"]) for r in await _rows(db_url, _VISIBLE_IMAGES, ([project],))}
        public = {str(r["id"]) for r in await _rows(db_url, _VISIBLE_IMAGES, ([],))}
        viewer = caller.token(_viewer(project))
        mine = {i.object_id for i in await _pages(caller, "images.list", viewer)}
        theirs = {
            i.object_id for i in await _pages(caller, "images.list", caller.token(_viewer(other)))
        }
        assert image_id in own and image_id not in public
        assert mine == own, f"owner listed {sorted(mine ^ own)} differently from the catalog"
        assert theirs == public, f"other project listed {sorted(theirs ^ public)} differently"
        owned = [image_id, *sorted(upload.object_keys)]
    return {"owner_listed": len(mine), "other_listed": len(theirs), "owned": owned}


async def _upload(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project = grants.projects[0]
    async with _uploading(caller.base_url, caller.issuer, db_url, project) as upload:
        image_id = await _register(caller, caller.token(grants), upload, db_url, discover=True)
        row = (
            await _rows(
                db_url,
                "SELECT state, visibility, owner, digest, provenance, object_key "
                "FROM image_catalog WHERE id = %s",
                (image_id,),
            )
        )[0]
        source = _staged_image().qcow2
        digest = await asyncio.to_thread(_sha256_file, source)
        assert (row["state"], row["visibility"], row["owner"]) == ("registered", "private", project)
        assert row["digest"] == f"sha256:{digest}", "registered digest is not the uploaded bytes'"
        assert row["provenance"]["upload"]["quarantine_key"] == upload.quarantine_key
        head = _s3().head_object(Bucket=os.environ["KDIVE_S3_BUCKET"], Key=row["object_key"])
        assert head["ContentLength"] == source.stat().st_size, "published object size differs"
        stranger = caller.token(_viewer(f"cov-{secrets.token_hex(4)}"))
        hidden = await caller.call("images.describe", {"image_id": image_id}, stranger)
        assert _not_found(hidden), "another project can describe the private image"
        owned = [image_id, *sorted(upload.object_keys)]
    return {"state": "registered", "digest_of_upload": True, "owner_only": True, "owned": owned}


async def _all_image_ids(db_url: str) -> set[str]:
    return {str(r["id"]) for r in await _rows(db_url, "SELECT id FROM image_catalog")}


async def _delete(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    project = grants.projects[0]
    async with _uploading(caller.base_url, caller.issuer, db_url, project) as upload:
        operator = HttpCaller("direct", caller.base_url, caller.issuer)
        image_id = await _register(operator, operator.token(grants), upload, db_url)
        before = await _all_image_ids(db_url)
        token = caller.token(grants)
        env = one(await caller.call("images.delete", {"image_id": image_id}, token, discover=True))
        assert env.status == "deleted", f"images.delete answered {env.status}"
        after = await _all_image_ids(db_url)
        assert after == before - {image_id}, "the delete changed other catalog entries"
        assert _not_found(await caller.call("images.describe", {"image_id": image_id}, token))
        owned = [image_id, *sorted(upload.object_keys)]
    return {"deleted": image_id, "others_unchanged": len(after), "owned": owned}


def _shape_data(row: Mapping[str, object]) -> dict[str, object]:
    """A ``system_shapes`` row as ``shapes.*`` reports it: ``pcie_match`` only when set."""
    return {k: v for k, v in row.items() if v is not None}


async def _shapes_list(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    env = one(await caller.call("shapes.list", {}, caller.token(grants), discover=True))
    expected = [_shape_data(row) for row in await _rows(db_url, _SHAPES)]
    assert [dict(item.data) for item in env.items] == expected, "shapes differ from the catalog"
    return {"shapes": [s["name"] for s in expected]}


@asynccontextmanager
async def _owned_shape(
    base_url: str, issuer: OidcIssuer, db_url: str, project: str
) -> AsyncIterator[str]:
    """A fresh ``cov-`` shape name; removed on exit if the body left it in the catalog."""
    name = f"cov-{secrets.token_hex(4)}"
    operator = HttpCaller("direct", base_url, issuer)
    token = operator.token(_platform_operator(project))

    async def remove() -> list[str]:
        if not await _rows(db_url, "SELECT 1 FROM system_shapes WHERE name = %s", (name,)):
            return []
        result = await operator.call("shapes.delete", {"name": name}, token)
        if isinstance(result, ToolResponse) and result.status == "deleted":
            return []
        return [f"shapes.delete {name}: {getattr(result, 'detail', result)}"]

    async with _cleaning(remove, f"shape {name}"):
        yield name


@asynccontextmanager
async def _granted(
    base_url: str, issuer: OidcIssuer, db_url: str, sizing: Mapping[str, object]
) -> AsyncIterator[dict[str, Any]]:
    """A granted allocation of ``sizing`` in the funded project; released and checked on exit.

    The allocation row stays as history in the funded project (ADR-0069 keeps released rows);
    the cleanup proof is its ``released`` state and the host occupancy it gives back.
    """
    project = _funded_project()
    operator = HttpCaller("direct", base_url, issuer)
    token = operator.token(Grants(f"{project}-cov", (project,), {project: "contributor"}))
    env = one(await operator.call("allocations.request", {"project": project, **sizing}, token))
    assert env.status == "granted", f"allocations.request answered {env.status}"
    remove = partial(_release, operator, token, db_url, env.object_id)
    async with _cleaning(remove, f"allocation {env.object_id}"):
        yield await _allocation(db_url, env.object_id)


async def _set_shape(
    caller: HttpCaller, token: str, shape: Mapping[str, object], db_url: str
) -> None:
    """``shapes.set`` ``shape`` and read it back from the catalog."""
    env = one(await caller.call("shapes.set", dict(shape), token, discover=True))
    assert dict(env.data) == shape, f"shapes.set answered {env.data}"
    rows = await _rows(db_url, _SHAPES)
    assert [_shape_data(r) for r in rows if r["name"] == shape["name"]] == [shape]


def _open_hosts(expected: Mapping[str, object]) -> list[dict[str, Any]]:
    hosts = cast(dict[str, dict[str, Any]], expected["hosts"])
    return [h for h in hosts.values() if h["schedulable"] and h["headroom"] >= 1]


async def _shapes_set(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    funded = _funded_project()
    # Admission places in the funded project, so its ceiling is over the hosts that project sees.
    expected = await _availability_expected(db_url, (funded,))
    if not _open_hosts(expected):
        raise ScenarioStop(Outcome.BLOCKED, "no schedulable host with headroom to admit a shape")
    hosts = cast(dict[str, dict[str, Any]], expected["hosts"]).values()
    ceiling = max(cast(int, h["vcpus"]) for h in hosts if h["vcpus"] is not None)
    admitter = HttpCaller("direct", caller.base_url, caller.issuer)
    request = admitter.token(Grants(f"{funded}-cov", (funded,), {funded: "contributor"}))
    token, project = caller.token(grants), grants.projects[0]
    async with _owned_shape(caller.base_url, caller.issuer, db_url, project) as name:
        over = {"name": name, "vcpus": ceiling + 1, "memory_mb": 1024, "disk_gb": 1}
        await _set_shape(caller, token, over, db_url)
        refused = await admitter.call(
            "allocations.request", {"project": funded, "shape": name}, request
        )
        if isinstance(refused, ToolResponse) and refused.status == "granted":
            problems = await _release(admitter, request, db_url, refused.object_id)
            raise AssertionError(f"admission granted {over} above every ceiling; {problems}")
        assert isinstance(refused, ToolResponse), f"admission answered {refused}"
        assert refused.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert refused.data.get("field") == "vcpus", f"admission refused {refused.data}"
        assert refused.data.get("requested") == str(ceiling + 1)
        await _set_shape(caller, token, {**over, "vcpus": 1}, db_url)
        async with _granted(caller.base_url, caller.issuer, db_url, {"shape": name}) as grant:
            sized = {k: grant[k] for k in ("shape", "requested_vcpus", "requested_memory_gb")}
            assert sized == {"shape": name, "requested_vcpus": 1, "requested_memory_gb": 1}
            assert grant["requested_disk_gb"] == 1, "admission did not size the disk by the shape"
            allocation = str(grant["id"])
    return {"shape": name, "refused_vcpus": ceiling + 1, "owned": [name, allocation]}


async def _shapes_delete(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    token, project = caller.token(grants), grants.projects[0]
    operator = HttpCaller("direct", caller.base_url, caller.issuer)
    async with _owned_shape(caller.base_url, caller.issuer, db_url, project) as name:
        shape = {"name": name, "vcpus": 1, "memory_mb": 1024, "disk_gb": 1}
        one(await operator.call("shapes.set", shape, operator.token(grants)))
        before = await _rows(db_url, _SHAPES)
        env = one(await caller.call("shapes.delete", {"name": name}, token, discover=True))
        assert env.status == "deleted", f"shapes.delete answered {env.status}"
        after = await _rows(db_url, _SHAPES)
        assert after == [r for r in before if r["name"] != name], "other presets changed"
    return {"deleted": name, "remaining": [r["name"] for r in after], "owned": [name]}


def _resource_view(caps: Mapping[str, object]) -> dict[str, object]:
    return {k: caps.get(k) for k in ("arch", "vcpus", "memory_mb")}


async def _resources_list(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    rows = await _visible_resources(db_url, grants.projects)
    expected = {
        str(r["id"]): {
            "kind": r["kind"],
            "status": r["status"],
            **_resource_view(r["capabilities"]),
        }
        for r in rows
    }
    listed = {
        item.object_id: {
            "kind": item.data.get("kind"),
            "status": item.status,
            **_resource_view(item.data),
        }
        for item in await _pages(caller, "resources.list", caller.token(grants))
    }
    assert listed == expected, f"resources.list {listed} != database {expected}"
    return {"resources": len(listed)}


def _mem_total_mb() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    raise AssertionError("/proc/meminfo has no MemTotal")


async def _resources_describe(
    caller: HttpCaller, grants: Grants, *, db_url: str
) -> dict[str, object]:
    local_uri = os.environ.get("KDIVE_LIBVIRT_URI")
    assert local_uri, "KDIVE_LIBVIRT_URI is unset; source examples/local-libvirt/env.sh"
    token, local = caller.token(grants), 0
    rows = await _visible_resources(db_url, grants.projects)
    for row in rows:
        args = {"resource_id": str(row["id"])}
        env = one(await caller.call("resources.describe", args, token, discover=True))
        fields = ("kind", "pool", "cost_class", "host_uri")
        assert {k: env.data.get(k) for k in fields} == {k: row[k] for k in fields}
        caps = cast(dict[str, object], row["capabilities"])
        assert _resource_view(env.data) == _resource_view(caps), "capabilities differ from the row"
        transports = caps.get("transports")
        if isinstance(transports, list):
            assert env.data.get("transports") == ",".join(str(t) for t in transports)
        assert env.status == row["status"], f"status {env.status} != {row['status']}"
        if row["kind"] == "local-libvirt" and row["host_uri"] == local_uri:
            assert env.data.get("vcpus") == os.cpu_count(), "vcpus is not this host's CPU count"
            assert env.data.get("memory_mb") == _mem_total_mb(), "memory is not MemTotal"
            local += 1
    assert local == 1, f"{local} resources describe this host ({local_uri})"
    return {"resources": len(rows), "host_facts_equal": True}


_FUNCTIONAL = {
    "images.delete": _delete,
    "images.describe": _describe,
    "images.kernel_config": _kernel_config,
    "images.list": _list,
    "images.upload": _upload,
    "resources.availability": _availability,
    "resources.describe": _resources_describe,
    "resources.list": _resources_list,
    "shapes.delete": _shapes_delete,
    "shapes.list": _shapes_list,
    "shapes.set": _shapes_set,
}
_WRITERS = {"images.delete", "images.list", "images.upload"}
_GATED = {"images.delete", "images.upload", "shapes.delete", "shapes.set"}


def _functional_grants(tool: str, project: str) -> Grants:
    if tool.startswith("shapes.") and tool != "shapes.list":
        return _platform_operator(project)
    if tool == "resources.availability":
        # The fixture allocation lands on a host the funded project sees; so must the cell.
        funded = _funded_project()
        return Grants(f"{project}-viewer", (project, funded), {project: "viewer", funded: "viewer"})
    return _operator(project) if tool in _WRITERS else _viewer(project)


def _valid(tool: str, project: str) -> dict[str, object]:
    """Arguments that change nothing for the cell's issued-token control call."""
    table: dict[str, dict[str, object]] = {
        "images.delete": {"image_id": _ABSENT_ID},
        "images.describe": {"image_id": _ABSENT_ID},
        "images.kernel_config": {"image_id": _ABSENT_ID},
        "images.upload": {
            "project": project,
            "name": _UPLOAD_NAME,
            "arch": "x86_64",
            "quarantine_key": f"uploads/q/{project}/absent.qcow2",
        },
        "resources.describe": {"resource_id": _ABSENT_ID},
        # Aimed at the cov- shape _shape_setup provides: only the boundary keeps these from
        # rewriting or removing it.
        "shapes.set": {"vcpus": 64, "memory_mb": 65536, "disk_gb": 1},
    }
    return table.get(tool, {})


def _invalid(tool: str, project: str) -> dict[str, object]:
    """Schema-invalid arguments: a missing required argument or a mistyped ``request`` field."""
    table: dict[str, dict[str, object]] = {
        "images.list": {"request": {"limit": "many"}},
        "images.upload": {"project": project},
        "resources.availability": {"request": {"include_devices": "maybe"}},
        "resources.list": {"request": {"limit": "many"}},
        "shapes.set": {"name": "cov-invalid"},
    }
    return table.get(tool, {})


def _rejection_grants(tool: str, boundary: Boundary, project: str) -> Grants:
    if boundary == "validation" and tool in _GATED:
        # tools.invoke reports field_errors only for a tool the token can see (ADR-0722 §3).
        return _functional_grants(tool, project)
    if boundary == "project-isolation":
        return _operator(f"cov-{secrets.token_hex(4)}")
    if boundary == "authorization" and tool.startswith("shapes."):
        return Grants(f"{project}-auditor", (project,), {project: "viewer"}, ("platform_auditor",))
    return _viewer(project)


def _image_setup(
    base_url: str, issuer: OidcIssuer, db_url: str, project: str
) -> AbstractAsyncContextManager[Mapping[str, object]]:
    """A private image of ``project`` for the duration of a rejected ``images.delete``."""

    @asynccontextmanager
    async def setup() -> AsyncIterator[Mapping[str, object]]:
        async with _uploading(base_url, issuer, db_url, project) as upload:
            operator = HttpCaller("direct", base_url, issuer)
            token = operator.token(_operator(project))
            yield {"image_id": await _register(operator, token, upload, db_url)}

    return setup()


def _shape_setup(
    base_url: str, issuer: OidcIssuer, db_url: str, project: str
) -> AbstractAsyncContextManager[Mapping[str, object]]:
    """A ``cov-`` shape for a rejected ``shapes.set``/``shapes.delete`` to aim at."""

    @asynccontextmanager
    async def setup() -> AsyncIterator[Mapping[str, object]]:
        async with _owned_shape(base_url, issuer, db_url, project) as name:
            operator = HttpCaller("direct", base_url, issuer)
            shape = {"name": name, "vcpus": 1, "memory_mb": 1024, "disk_gb": 1}
            one(
                await operator.call(
                    "shapes.set", shape, operator.token(_platform_operator(project))
                )
            )
            yield {"name": name}

    return setup()


_SETUPS = {
    ("images.delete", "authorization"): _image_setup,
    ("images.delete", "project-isolation"): _image_setup,
    ("shapes.delete", "authentication"): _shape_setup,
    ("shapes.delete", "authorization"): _shape_setup,
    ("shapes.set", "authentication"): _shape_setup,
    ("shapes.set", "authorization"): _shape_setup,
}


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell = run.cell
    caller = caller_for(cell.exposure, base_url, issuer)
    project = f"cov-{secrets.token_hex(4)}"
    snapshot = partial(_catalog_state, db_url, project)
    if cell.kind == "functional":
        body = cast(Functional, partial(_FUNCTIONAL[cell.operation], db_url=db_url))
        grants = _functional_grants(cell.operation, project)
        await prove_functional(run, caller, grants, body, snapshot)
        return
    boundary = boundary_of(cell)
    args = _invalid if boundary == "validation" else _valid
    rejection = Rejection(
        args(cell.operation, project), _rejection_grants(cell.operation, boundary, project)
    )
    factory = _SETUPS.get((cell.operation, boundary))
    setup = partial(factory, base_url, issuer, db_url, project) if factory else None
    await prove_rejection(run, caller, boundary, rejection, snapshot, setup=setup)


@pytest.mark.parametrize("cell", tool_cells(TOOLS), ids=lambda cell: cell.id)
def test_catalog_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a catalog tool and record it."""
    run_tool_cell(cell, _scenario)
