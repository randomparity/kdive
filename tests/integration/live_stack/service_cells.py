"""Shared live-stack helpers for the investigation, run and service tool-cell carriers."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import os
import platform
import secrets
import struct
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, LiteralString, cast

import boto3
import httpx
import psycopg
from psycopg.rows import dict_row

from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    load_fixture,
)
from tests.integration.live_stack.scenario import (
    ScenarioStop,
)
from tests.integration.live_stack.tool_cells import (
    Grants,
    HttpCaller,
    one,
    project_state,
)
from tests.mcp.complete_build_support import valid_combined_kernel_tar

Rows = list[dict[str, Any]]


_ABSENT_ID = "00000000-0000-4000-8000-000000000000"


_LIVE_INVESTIGATIONS = ["open", "active"]


_LIVE_RUNS = ["created", "running"]


_REF_A = {"tracker": "cov", "id": "A-1", "url": "https://tracker.invalid/A-1"}


_BUILD_PROFILE = {"schema_version": 1, "arch": "x86_64"}


# The build-id note the synthetic bundle's ELF carries (tests/mcp/complete_build_support.py).
_BUILD_ID = "0123456789abcdef"


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


async def _manifest(db_url: str, owner_id: str) -> dict[str, Any]:
    rows = await _rows(db_url, "SELECT * FROM upload_manifests WHERE owner_id = %s", (owner_id,))
    assert len(rows) == 1, f"{owner_id} has {len(rows)} upload manifests"
    return rows[0]


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


# The kernel fixture every runs.* cell uploads; write the bindings with
# ``--kernel-baseline longterm``.
BASELINE = "longterm"


def _fixture() -> tuple[Path, dict[str, Any]]:
    """The verified ``longterm`` fixture; ``blocked`` before any stack mutation when absent."""
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the {BASELINE} fixture"
        )
    try:
        return load_fixture(Path(root), BASELINE, platform.machine())
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{BASELINE} fixture: {exc}") from None
