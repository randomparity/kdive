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
import json
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, LiteralString, cast

import boto3
import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.errors import ErrorCategory
from kdive.images.rootfs.staged_provenance import config_sibling_path, sidecar_path
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import ImageEntry, StagedPathSource
from kdive.inventory.path import systems_toml_path
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import (
    Boundary,
    Exposure,
    Functional,
    Grants,
    HttpCaller,
    Rejection,
    boundary_of,
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
Rows = list[dict[str, Any]]
_UPLOAD_NAME = "cov-upload"
# What the ADR-0048 upload reassembly stamps on its object
# (src/kdive/artifacts/uploads/reassembly.py); the upload service refuses an object without it.
_UPLOAD_METADATA = {"sensitivity": "sensitive", "retention-class": "build"}
_ABSENT_ID = "00000000-0000-4000-8000-000000000000"
# Allocation states that hold a host slot, and the queued one (ADR-0069).
_OCCUPYING = ["granted", "active", "releasing"]
_REQUESTED = "requested"
_DIGEST: LiteralString = (
    "SELECT count(*) AS n, encode(sha256(convert_to(coalesce("
    "string_agg(t::text, ',' ORDER BY t::text), ''), 'UTF8')), 'hex') AS h "
)


async def _rows(db_url: str, query: LiteralString, params: tuple[object, ...] = ()) -> Rows:
    """``query``'s rows from a read-only session on the evidence database."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return list(await cursor.fetchall())


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


def _operator(project: str) -> Grants:
    return Grants(f"{project}-operator", (project,), {project: "operator"})


def _platform_operator(project: str) -> Grants:
    return Grants(f"{project}-platform", (project,), {project: "viewer"}, ("platform_operator",))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class _Image:
    """The precondition: a public staged-path image with its build-fs siblings."""

    entry: ImageEntry
    qcow2: Path

    @property
    def provenance(self) -> dict[str, object]:
        doc = json.loads(sidecar_path(self.qcow2).read_text(encoding="utf-8"))
        return cast(dict[str, object], doc["provenance"])


def _staged_image() -> _Image:
    """The first public local-libvirt staged-path image of ``systems.toml``; blocked if none."""
    doc = load_inventory_optional(systems_toml_path())
    for entry in doc.image if doc is not None else []:
        source = entry.source
        if (
            entry.provider == "local-libvirt"
            and entry.visibility is ImageVisibility.PUBLIC
            and isinstance(source, StagedPathSource)
        ):
            qcow2 = Path(source.path)
            siblings = (qcow2, sidecar_path(qcow2), config_sibling_path(qcow2))
            if all(path.is_file() for path in siblings):
                return _Image(entry, qcow2)
    raise ScenarioStop(
        Outcome.BLOCKED,
        "the exported KDIVE_SYSTEMS_TOML declares no public local-libvirt staged-path image with "
        "its build-fs siblings (a live tier reads only an exported path); stage one with "
        "examples/local-libvirt/build-image.sh fedora-kdive-ready-44",
    )


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


def _s3() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ["KDIVE_S3_ENDPOINT_URL"],
        region_name=os.environ.get("KDIVE_S3_REGION", "us-east-1"),
    )


def _put_quarantined(source: Path, key: str) -> None:
    extra = {"Metadata": _UPLOAD_METADATA}
    _s3().upload_file(str(source), os.environ["KDIVE_S3_BUCKET"], key, ExtraArgs=extra)


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
class _Upload:
    """A quarantined object for ``project`` and the object keys a cell must remove."""

    project: str
    arch: str
    quarantine_key: str
    object_keys: set[str]


@asynccontextmanager
async def _uploading(
    base_url: str, issuer: OidcIssuer, db_url: str, project: str
) -> AsyncIterator[_Upload]:
    """Quarantine the staged image's bytes for ``project``; on exit remove its images, objects."""
    image = _staged_image()
    key = f"uploads/q/{project}/{_UPLOAD_NAME}.qcow2"
    await asyncio.to_thread(_put_quarantined, image.qcow2, key)
    upload = _Upload(project, image.entry.arch, key, {key})
    remove = partial(_remove_upload, base_url, issuer, db_url, upload)
    async with _cleaning(remove, f"{project}'s upload"):
        yield upload


async def _remove_upload(
    base_url: str, issuer: OidcIssuer, db_url: str, upload: _Upload
) -> list[str]:
    """Delete ``upload.project``'s images, then purge every owned object; what could not go."""
    problems: list[str] = []
    rows = await _rows(
        db_url, "SELECT id, object_key FROM image_catalog WHERE owner = %s", (upload.project,)
    )
    operator = HttpCaller("direct", base_url, issuer)
    token = operator.token(_operator(upload.project))
    for row in rows:
        upload.object_keys.add(str(row["object_key"]))
        result = await operator.call("images.delete", {"image_id": str(row["id"])}, token)
        if not isinstance(result, ToolResponse) or result.error_category is not None:
            problems.append(f"images.delete {row['id']}: {getattr(result, 'detail', result)}")
    for object_key in sorted(upload.object_keys):
        if not await asyncio.to_thread(_purge, object_key):
            problems.append(f"object {object_key} still has versions")
    return problems


async def _register(
    caller: HttpCaller, token: str, upload: _Upload, db_url: str, *, discover: bool = False
) -> str:
    """``images.upload`` ``upload``'s object; return the image id and track its object key."""
    args = {
        "project": upload.project,
        "name": _UPLOAD_NAME,
        "arch": upload.arch,
        "quarantine_key": upload.quarantine_key,
    }
    env = one(await caller.call("images.upload", args, token, discover=discover))
    rows = await _rows(
        db_url, "SELECT object_key FROM image_catalog WHERE id = %s", (env.object_id,)
    )
    upload.object_keys.update(str(row["object_key"]) for row in rows)
    expected = {"name": _UPLOAD_NAME, "visibility": "private", "owner": upload.project}
    assert {k: env.data.get(k) for k in expected} == expected, f"upload answered {env.data}"
    return env.object_id


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
    response.raise_for_status()
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
        mine = {i.object_id for i in await _pages(caller, "images.list", caller.token(grants))}
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


_SHAPES: LiteralString = (
    "SELECT name, vcpus, memory_mb, disk_gb, pcie_match FROM system_shapes ORDER BY name"
)


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


async def _visible_resources(db_url: str, projects: tuple[str, ...]) -> Rows:
    """The ``resources`` rows any of ``projects`` may see: global, owned or allow-listed."""
    rows = await _rows(db_url, "SELECT * FROM resources ORDER BY created_at, id")
    return [
        row
        for row in rows
        if row["owner_project"] is None
        or row["owner_project"] in projects
        or set(row["affinity_allowlist"] or []) & set(projects)
    ]


def _cap(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def _availability_expected(db_url: str, projects: tuple[str, ...]) -> dict[str, object]:
    """Per-host availability and queue depth derived from the database alone."""
    occupancy = {
        str(r["resource_id"]): int(r["n"])
        for r in await _rows(
            db_url,
            "SELECT resource_id, count(*) AS n FROM allocations "
            "WHERE resource_id IS NOT NULL AND state = ANY(%s) GROUP BY resource_id",
            (_OCCUPYING,),
        )
    }
    queued = (
        await _rows(db_url, "SELECT count(*) AS n FROM allocations WHERE state = %s", (_REQUESTED,))
    )[0]["n"]
    shapes = await _rows(db_url, _SHAPES)
    hosts: dict[str, dict[str, object]] = {}
    for row in await _visible_resources(db_url, projects):
        caps = cast(dict[str, object], row["capabilities"])
        cap, vcpus, memory = (
            _cap(caps.get(k)) for k in ("concurrent_allocation_cap", "vcpus", "memory_mb")
        )
        schedulable = (
            cap is not None
            and vcpus is not None
            and memory is not None
            and row["status"] == "available"
            and not row["cordoned"]
        )
        in_use = occupancy.get(str(row["id"]), 0)
        headroom = max(cap - in_use, 0) if cap is not None else 0
        fits = {
            str(s["name"])
            for s in shapes
            if schedulable
            and headroom >= 1
            and s["pcie_match"] is None
            and s["vcpus"] <= cast(int, vcpus)
            and s["memory_mb"] <= cast(int, memory)
        }
        hosts[str(row["id"])] = {
            "schedulable": schedulable,
            "cap": cap if cap is not None else 0,
            "in_use": in_use,
            "headroom": headroom,
            "fits": fits,
            "vcpus": vcpus,
            "memory_mb": memory,
        }
    pcie = {str(s["name"]) for s in shapes if s["pcie_match"] is not None}
    return {"hosts": hosts, "queued": queued, "pcie_shapes": pcie}


def _funded_project() -> str:
    """The project the bring-up onboarded with budget and quota (``KDIVE_PROJECT``)."""
    project = os.environ.get("KDIVE_PROJECT")
    assert project, "KDIVE_PROJECT is unset; source examples/local-libvirt/env.sh"
    return project


async def _allocation(db_url: str, allocation_id: str) -> dict[str, Any]:
    rows = await _rows(db_url, "SELECT * FROM allocations WHERE id = %s", (allocation_id,))
    assert len(rows) == 1, f"allocation {allocation_id} has {len(rows)} rows"
    return rows[0]


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


async def _release(caller: HttpCaller, token: str, db_url: str, allocation_id: str) -> list[str]:
    """Release ``allocation_id``; what kept it from reaching ``released``."""
    result = await caller.call("allocations.release", {"allocation_id": allocation_id}, token)
    state = (await _allocation(db_url, allocation_id))["state"]
    if isinstance(result, ToolResponse) and result.status == "released" and state == "released":
        return []
    return [f"release of {allocation_id} answered {getattr(result, 'detail', result)}, {state}"]


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


async def _compare_availability(
    caller: HttpCaller, token: str, db_url: str, projects: tuple[str, ...]
) -> dict[str, object]:
    """One ``resources.availability`` read compared with the database; the expectation."""
    env = one(await caller.call("resources.availability", {}, token, discover=True))
    expected = await _availability_expected(db_url, projects)
    hosts = cast(dict[str, dict[str, Any]], expected["hosts"])
    pcie = cast(set[str], expected["pcie_shapes"])
    fields = ("schedulable", "cap", "in_use", "headroom")
    observed = {
        item.object_id: {
            **{k: item.data.get(k) for k in fields},
            "fits": set(cast(list[str], item.data.get("fits"))) - pcie,
        }
        for item in env.items
    }
    wanted = {rid: {**{k: h[k] for k in fields}, "fits": h["fits"]} for rid, h in hosts.items()}
    assert observed == wanted, f"availability {observed} != database {wanted}"
    queue = cast(dict[str, object], env.data.get("queue_depth"))
    assert queue.get("total") == expected["queued"], f"queue depth {queue} != {expected['queued']}"
    union = set().union(*(h["fits"] for h in hosts.values()))
    assert set(cast(list[str], env.data.get("fits_now"))) - pcie == union
    return expected


async def _availability(caller: HttpCaller, grants: Grants, *, db_url: str) -> dict[str, object]:
    """Compare the idle fleet, then the fleet holding one allocation this cell controls."""
    token = caller.token(grants)
    idle = await _compare_availability(caller, token, db_url, grants.projects)
    if not _open_hosts(idle):
        raise ScenarioStop(Outcome.BLOCKED, "no schedulable host with headroom to allocate on")
    sizing = {"vcpus": 1, "memory_gb": 1, "disk_gb": 1}
    async with _granted(caller.base_url, caller.issuer, db_url, sizing) as grant:
        held = await _compare_availability(caller, token, db_url, grants.projects)
        host = str(grant["resource_id"])
        before = cast(dict[str, dict[str, Any]], idle["hosts"])[host]
        during = cast(dict[str, dict[str, Any]], held["hosts"])[host]
        assert during["in_use"] == before["in_use"] + 1, "the grant did not occupy its host"
        allocation = str(grant["id"])
    after = await _compare_availability(caller, token, db_url, grants.projects)
    assert after["hosts"] == idle["hosts"], "the released allocation did not free its host"
    return {
        "hosts": len(cast(dict[str, object], idle["hosts"])),
        "held_host": host,
        "queued": idle["queued"],
        "owned": [allocation],
    }


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
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
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
