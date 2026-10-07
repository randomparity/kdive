"""Tool-cell frame of the live coverage carriers (#2811, ADR-0722).

A tool cell is one contract cell of a registered MCP tool: a configuration (``default`` or
``recovery``), an exposure (``direct`` or ``gateway``) and a kind (``functional`` or one rejection
boundary). :func:`run_tool_cell` proves the server's configuration from its operator catalog,
skips a cell of the other configuration without writing a record, and otherwise runs the cell
through :func:`~tests.integration.live_stack.scenario.run_cell`. :class:`HttpCaller` reaches a tool
in the cell's exposure; :func:`prove_functional` and :func:`prove_rejection` prove the cell's
assertions, with :func:`project_state` as the default protected-state snapshot.

A provider cell runs on its provider's :class:`Lane`: a local-libvirt cell on the catalog lane
image (:data:`LANE_IMAGES`) of this host's architecture, a remote-libvirt cell on the remote
representative (:data:`REMOTE_LANE_FAMILIES`) of a separate provider host, observed through the
operator's ``REMOTE_PROVIDER_SSH`` access. :func:`on_lane_system` is the functional frame: it
provisions the lane image in the cell's project, observes the guest, proves the body's ``effect``
and the owned ``cleanup``. :func:`lane_target` is the rejection target, one torn-down System per
stack and provider.

``python -m tests.integration.live_stack.tool_cells bindings --candidate SHA --out FILE`` writes
the expected ``Context`` of every bound service tool cell and every bound local-libvirt tool cell
native to this host; ``--remote`` adds the bound remote-libvirt cells of the provider host;
``--kernel-baseline NAME`` binds a verified kernel fixture to the cells that declare kernel inputs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, cast, get_args

import httpx
import psycopg
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from psycopg import sql

import kdive.config as config
from kdive.config.cli_settings import CLI_CLIENT_ID
from kdive.domain.errors import ErrorCategory
from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.mcp.dev_harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
    make_keypair,
    mint_token,
)
from kdive.mcp.exposure import CORE_TOOLS
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings, Outcome
from tests.integration.live_stack.cleanup import disk_absent
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    bound_kernel,
    file_sha256,
    load_fixture,
)
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.image_smoke import (
    PROBE,
    Endpoint,
    GuestIdentity,
    os_matches,
    staged_image,
)
from tests.integration.live_stack.remote_lifecycle import (
    HOST_SSH_ENV,
    REMOTE_REPRESENTATIVES,
    RemoteHost,
    on_remote_system,
    remote_host,
    remote_profile,
    remote_volume_absent,
    remote_xml,
    staged_base_volume,
    staged_volume,
    volume_sha256,
)
from tests.integration.live_stack.scenario import (
    CatalogBody,
    CellRun,
    Provision,
    ScenarioStop,
    authorize_ssh,
    catalog_profile,
    domain_xml,
    on_catalog_system,
    provision_catalog,
    run_cell,
    ssh_probe,
)

Exposure = Literal["direct", "gateway"]
Boundary = Literal["authentication", "authorization", "project-isolation", "validation"]
Result = ToolResponse | list[ToolResponse] | LiveStackToolError
Snapshot = Callable[[], Awaitable[object]]
Setup = Callable[[], AbstractAsyncContextManager[Mapping[str, object]]]

RECOVERY_TOOLS = frozenset({"ops.build_uses_list", "ops.recover_build_use"})
# Rows a rejected or successful call is expected to write (ADR-0722 §4).
_AUDIT_TABLES = frozenset({"audit_log", "platform_audit_log", "tool_invocation"})
_JWT_STANDARD = frozenset({"sub", "iss", "aud", "exp", "iat", "nbf", "jti"})
_CONFIGURATIONS: dict[str, tuple[str, list[str]]] = {}
# The catalog image every provider cell of a host architecture boots.
LANE_IMAGES = {"x86_64": "fedora-kdive-ready-44"}
# The remote representative family every remote provider cell of a guest architecture boots.
REMOTE_LANE_FAMILIES = {"x86_64": "fedora"}
PUBLISH_TOOL = "images.publish"
# The single-kernel catalog image a functional images.publish cell publishes and boots, per
# (architecture, exposure). One image per exposure: images.publish never recycles a finished
# job of the same name, so two cells of one stack cannot both publish one image.
PUBLISHED_IMAGES = {
    ("x86_64", "direct"): "fedora-kdive-ready-43-cloud",
    ("x86_64", "gateway"): "rocky-kdive-ready-9",
}
# The DMI product UUID a KVM guest reports is its libvirt domain UUID.
IDENTITY_PROBE = PROBE + '; printf "product_uuid=%s\\n" "$(cat /sys/class/dmi/id/product_uuid)"'
_SETTLE_S = 15.0
_SETTLE_ATTEMPTS = 8


def configuration_of(catalog: Iterable[str]) -> str:
    """``recovery`` when both build-use recovery tools are listed, ``default`` when neither."""
    listed = RECOVERY_TOOLS & set(catalog)
    if listed == RECOVERY_TOOLS:
        return "recovery"
    if not listed:
        return "default"
    raise AssertionError(
        f"catalog lists only {sorted(listed)} of the build-use recovery tools; "
        "the server's configuration is neither default nor recovery"
    )


@dataclass(frozen=True)
class Grants:
    """The claims one cell's token carries."""

    subject: str
    projects: tuple[str, ...]
    roles: Mapping[str, str] = field(default_factory=dict)
    platform_roles: tuple[str, ...] = ()


def _segment(token: str, index: int) -> dict[str, object]:
    raw = token.split(".")[index]
    decoded = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert isinstance(decoded, dict), "JWT segment is not an object"
    return decoded


def claims_of(token: str) -> dict[str, object]:
    """The unverified payload of a JWT."""
    return _segment(token, 1)


def forge(token: str) -> str:
    """``token`` re-signed by a fresh key the server does not trust (ADR-0722 §3).

    The claims and the header's ``kid`` are kept, so the verifier selects the trusted key and
    fails on the signature itself rather than on key lookup.
    """
    claims = claims_of(token)
    kid = _segment(token, 0).get("kid")
    return make_keypair().create_token(
        subject=str(claims["sub"]),
        issuer=str(claims["iss"]),
        audience=cast(str | list[str] | None, claims.get("aud")),
        additional_claims={k: v for k, v in claims.items() if k not in _JWT_STANDARD},
        kid=kid if isinstance(kid, str) else None,
    )


class Caller(Protocol):
    """What the rejection harness needs from an exposure."""

    @property
    def exposure(self) -> str: ...
    def token(self, grants: Grants) -> str: ...
    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]: ...
    async def post(self, tool: str, args: Mapping[str, object], token: str) -> int: ...


def one(result: ToolResponse | list[ToolResponse]) -> ToolResponse:
    """The single successful envelope ``result`` must be."""
    assert isinstance(result, ToolResponse), f"expected one envelope, got {len(result)}"
    assert result.error_category is None, f"{result.error_category}: {result.detail}"
    return result


def matches(env: ToolResponse) -> list[dict[str, object]]:
    """The ``tools.search`` matches of ``env``."""
    raw = env.data.get("matches")
    assert isinstance(raw, list), f"{env.object_id} carries no matches"
    return [cast(dict[str, object], m) for m in raw if isinstance(m, dict)]


@dataclass(frozen=True)
class HttpCaller:
    """Reach a tool over HTTP in one exposure (ADR-0722 §1)."""

    exposure: Exposure
    base_url: str
    issuer: OidcIssuer

    def token(self, grants: Grants) -> str:
        """A real-issuer token for ``grants``; ``direct`` carries the ``kdivectl`` client id."""
        return mint_token(
            self.issuer,
            subject=grants.subject,
            projects=list(grants.projects),
            roles=dict(grants.roles),
            platform_roles=list(grants.platform_roles),
            agent_session=f"{grants.subject}-sess",
            client_id=config.require(CLI_CLIENT_ID) if self.exposure == "direct" else None,
        )

    def _route(self, tool: str, args: Mapping[str, object]) -> tuple[str, dict[str, object]]:
        if self.exposure == "direct":
            return tool, dict(args)
        return "tools.invoke", {"name": tool, "arguments": dict(args)}

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        """Call ``tool``; ``gateway`` with ``discover`` first finds it with ``tools.search``."""
        async with LiveStackClient.over_http(self.base_url, token) as client:
            if self.exposure == "gateway" and discover:
                found = one(await client.call_tool("tools.search", names=[tool]))
                names = [m.get("name") for m in matches(found)]
                assert names == [tool], f"tools.search did not find {tool}: {names}"
            name, arguments = self._route(tool, args)
            return await client.call_tool(name, **arguments)

    async def post(self, tool: str, args: Mapping[str, object], token: str) -> int:
        """Send one raw ``tools/call`` carrying ``token``; return the HTTP status."""
        name, arguments = self._route(tool, args)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            response = await http.post(self.base_url, json=body, headers=headers)
        return response.status_code


async def operator_catalog(base_url: str, issuer: OidcIssuer, grants: Grants) -> dict[str, object]:
    """Each tool's ``inputSchema`` in the operator-direct catalog for ``grants``.

    Fails unless the catalog is unclipped: a token whose ``azp`` the server does not take for
    ``kdivectl`` gets the agent-gateway profile, clipped to ``CORE_TOOLS`` (ADR-0268), and would
    make every ``direct`` cell name an exposure the run never had.
    """
    token = HttpCaller("direct", base_url, issuer).token(grants)
    transport = StreamableHttpTransport(url=base_url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        catalog = {tool.name: tool.inputSchema for tool in await client.list_tools()}
    assert set(catalog) - CORE_TOOLS, (
        "operator catalog is clipped to CORE_TOOLS: the server does not resolve this process's "
        "KDIVE_CLI_CLIENT_ID as kdivectl; use the same value for the server and the tests"
    )
    return catalog


@dataclass(frozen=True)
class Rejection:
    """One call a boundary must reject: its arguments, grants and accepted categories.

    ``filtered_by`` names the issue that owns a list tool known to answer an empty page instead
    of rejecting: that answer stops the cell ``blocked``, and a real rejection still qualifies.
    ``absent_twin`` holds arguments naming an owner that does not exist: the rejected answer
    must be indistinguishable from the twin's, so a category shared with an unrelated failure
    cannot pass the cell.
    """

    args: Mapping[str, object]
    grants: Grants
    categories: frozenset[str] = frozenset({ErrorCategory.AUTHORIZATION_DENIED.value})
    filtered_by: str | None = None
    absent_twin: Mapping[str, object] | None = None


def rejected_by_validation(exposure: str, result: Result) -> bool:
    """A schema rejection of the call's arguments (ADR-0722 §3).

    ``direct`` accepts a ``configuration_error`` envelope or a tool error naming validation.
    ``gateway`` accepts only ``tools.invoke``'s argument-binding failure: ``tools.invoke`` also
    answers ``configuration_error`` for an unknown tool, an inner handler's error and a tool body
    re-validating its own data, and only the binding failure carries non-empty
    ``data.field_errors`` beside its "failed schema validation" detail
    (``src/kdive/mcp/tools/gateway.py``). A tool whose binding failure ``BindingErrorMiddleware``
    re-envelopes (``src/kdive/mcp/middleware/binding_errors.py``) never reaches that branch, so
    its ``gateway`` validation cell fails here rather than recording ambiguous evidence.
    """
    if isinstance(result, LiveStackToolError):
        return exposure == "direct" and "validation error" in result.message.lower()
    if not (
        isinstance(result, ToolResponse)
        and result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    ):
        return False
    if exposure == "direct":
        return True
    return bool(result.data.get("field_errors")) and "failed schema validation" in (
        result.detail or ""
    )


def _shape(result: Result) -> dict[str, object]:
    """A message-free description of ``result`` for evidence."""
    if isinstance(result, LiveStackToolError):
        return {
            "tool_error": True,
            "names_validation": "validation error" in result.message.lower(),
        }
    if isinstance(result, list):
        return {"envelopes": len(result)}
    return {"status": result.status, "error_category": result.error_category}


async def _attempt(
    caller: Caller, tool: str, rejection: Rejection, args: Mapping[str, object] | None = None
) -> Result:
    try:
        call = rejection.args if args is None else args
        return await caller.call(tool, call, caller.token(rejection.grants))
    except LiveStackToolError as exc:
        return exc


def _filtered(result: Result) -> bool:
    """A successful envelope listing nothing: a list tool filtering instead of rejecting."""
    return isinstance(result, ToolResponse) and result.error_category is None and not result.items


def _answer(result: Result, args: Mapping[str, object]) -> dict[str, object]:
    """``result`` with every string argument masked, to compare a call with its absent twin."""
    if not isinstance(result, ToolResponse):
        return _shape(result)
    text = json.dumps(
        [result.object_id, result.status, result.error_category, result.detail, result.data],
        sort_keys=True,
        default=str,
    )
    for value in args.values():
        if isinstance(value, str) and value:
            text = text.replace(value, "<arg>")
    return {"answer_sha256": hashlib.sha256(text.encode()).hexdigest(), **_shape(result)}


async def _observe(
    caller: Caller, tool: str, boundary: Boundary, rejection: Rejection
) -> dict[str, object]:
    if boundary == "authentication":
        genuine = caller.token(rejection.grants)
        status = await caller.post(tool, rejection.args, forge(genuine))
        assert status == 401, f"forged-signature call to {tool} answered HTTP {status}, not 401"
        # The same request with the issued token must pass authentication, so the 401 above is
        # the signature's and not the request's.
        control = await caller.post(tool, rejection.args, genuine)
        assert control != 401, f"the issued token's call to {tool} was also refused with 401"
        return {"http_status": status, "token": "foreign-signature", "issued_token_status": control}
    result = await _attempt(caller, tool, rejection)
    if boundary == "validation":
        assert rejected_by_validation(caller.exposure, result), (
            f"{tool} was not rejected by argument validation: {_shape(result)}"
        )
        return {"rejected": _shape(result)}
    if rejection.filtered_by is not None and _filtered(result):
        raise ScenarioStop(
            Outcome.BLOCKED,
            f"{tool} answered an empty page instead of rejecting the {boundary} call; "
            f"{rejection.filtered_by} owns the decision",
        )
    category = result.error_category if isinstance(result, ToolResponse) else None
    assert category is not None, f"{tool} was not rejected: {_shape(result)}"
    assert category in rejection.categories, (
        f"{tool} rejected with {category}, not one of {sorted(rejection.categories)}"
    )
    observation: dict[str, object] = {"rejected": _shape(result)}
    if rejection.absent_twin is not None:
        twin = await _attempt(caller, tool, rejection, rejection.absent_twin)
        answered, absent = _answer(result, rejection.args), _answer(twin, rejection.absent_twin)
        assert answered == absent, f"{tool} answered {answered}, an absent owner {absent}"
        observation["absent_owner_twin"] = answered
    return observation


def _digest(state: object) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


async def prove_rejection(
    run: CellRun,
    caller: Caller,
    boundary: Boundary,
    rejection: Rejection,
    snapshot: Snapshot,
    *,
    setup: Setup | None = None,
) -> None:
    """Prove ``boundary`` rejects the call and the protected state is unchanged (ADR-0722).

    ``setup`` provides state the rejected call needs and removes it on exit; it yields argument
    overrides. ``unchanged-state`` brackets the call alone, and ``cleanup`` compares the snapshot
    taken before ``setup`` with the one taken after it, so the setup's own state must go.
    """
    operation = run.cell.operation
    outer = await snapshot() if setup is not None else None
    context: AbstractAsyncContextManager[Mapping[str, object]] = (
        setup() if setup is not None else nullcontext({})
    )
    async with context as overrides:
        effective = replace(rejection, args={**rejection.args, **overrides})
        before = await snapshot()
        try:
            observation = await _observe(caller, operation, boundary, effective)
        except ScenarioStop as stop:
            # The record's impediment is the generic missing-prerequisite; this artifact names
            # the owner and what the call answered.
            blocked = {"cell": run.cell.id, "boundary": boundary, "blocked": str(stop)}
            run.artifacts.append(
                run.writer.artifact({**blocked, "filtered_by": rejection.filtered_by})
            )
            raise
        run.prove(boundary, {"exposure": caller.exposure, **observation})
        after = await snapshot()
        assert after == before, f"protected state changed across the rejected {operation}"
        run.prove("unchanged-state", {"snapshot_sha256": _digest(before)})
    final = await snapshot() if setup is not None else after
    start = outer if setup is not None else before
    assert final == start, f"the setup of the rejected {operation} left state behind"
    owned = sorted(str(value) for value in overrides.values())
    run.prove("cleanup", {"owned": owned, "snapshot_sha256": _digest(start)})


Functional = Callable[[HttpCaller, Grants], Awaitable[dict[str, object]]]


async def prove_functional(
    run: CellRun, caller: HttpCaller, grants: Grants, body: Functional, snapshot: Snapshot
) -> None:
    """Prove ``effect`` with ``body``, then ``cleanup`` as an unchanged project snapshot.

    A body that writes state removes it and names it under ``owned``.
    """
    before = await snapshot()
    observed = await body(caller, grants)
    owned = observed.pop("owned", [])
    run.prove("effect", {"exposure": caller.exposure, **observed})
    after = await snapshot()
    assert after == before, f"{run.cell.operation} left durable state in the cell's project"
    run.prove("cleanup", {"owned": owned, "snapshot_sha256": _digest(before)})


async def project_state(db_url: str, project: str) -> dict[str, list[object]]:
    """Row count and row-text SHA-256 of ``project`` in every public table with a project column.

    Tables come from ``pg_catalog``, which lists every table whatever the DSN may read, so a
    table the DSN cannot read fails the snapshot instead of leaving it.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        # The documented DSN is the schema owner's; a read-only session keeps the snapshot a read.
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT c.relname FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "AND a.attname = 'project' AND NOT a.attisdropped ORDER BY c.relname"
        )
        tables = [str(row[0]) for row in await cursor.fetchall()]
        tables = [t for t in tables if t not in _AUDIT_TABLES]
        assert tables, "no table carries a project column; the snapshot would observe nothing"
        state: dict[str, list[object]] = {}
        for table in tables:
            query = sql.SQL(
                "SELECT count(*), encode(sha256(convert_to(coalesce("
                "string_agg(t::text, ',' ORDER BY t::text), ''), 'UTF8')), 'hex') "
                "FROM {} t WHERE t.project = %s"
            ).format(sql.Identifier(table))
            row = await (await conn.execute(query, (project,))).fetchone()
            assert row is not None
            state[table] = [row[0], row[1]]
    return state


def boundary_of(cell: Cell) -> Boundary:
    """The rejection boundary a rejection cell's scenario names."""
    boundary = cell.scenario_id.rsplit("/", 1)[1]
    assert boundary in get_args(Boundary), f"{cell.scenario_id} names no rejection boundary"
    return cast(Boundary, boundary)


def tool_cells(tools: Sequence[str]) -> list[Cell]:
    """The contract's cells of ``tools``."""
    return [cell for cell in build_contract().cells if cell.operation in tools]


def server_configuration(base_url: str, issuer: OidcIssuer) -> tuple[str, list[str]]:
    """The proven configuration and the recovery tools listed; read once per stack."""
    if base_url not in _CONFIGURATIONS:
        grants = Grants("cov-configuration", ("cov-configuration",), {}, ("platform_operator",))
        catalog = asyncio.run(operator_catalog(base_url, issuer, grants))
        listed = sorted(RECOVERY_TOOLS & set(catalog))
        _CONFIGURATIONS[base_url] = (configuration_of(catalog), listed)
    return _CONFIGURATIONS[base_url]


ToolScenario = Callable[[CellRun, str, OidcIssuer, str], Awaitable[None]]


def run_tool_cell(cell: Cell, scenario: ToolScenario) -> None:
    """Prove the configuration, skip another configuration's cell, else run and record it.

    A completed rejection cell records ``rejection``, a functional one ``success``. A local
    provider cell of another host architecture is skipped before the stack is read; a remote
    cell's host is the provider host, which its frame observes.
    """
    if cell.provider != "remote-libvirt" and cell.host_arch not in (None, platform.machine()):
        pytest.skip(f"{cell.id} runs on a {cell.host_arch} host")
    base_url = require_stack()
    try:
        issuer = require_issuer()
    except pytest.skip.Exception as exc:
        # Without the issuer the configuration cannot be read, so no cell could be placed.
        pytest.fail(f"cannot prove the stack's configuration: {exc}; set KDIVE_OIDC_ISSUER")
    configuration, listed = server_configuration(base_url, issuer)
    if configuration != cell.configuration:
        pytest.skip(f"stack runs {configuration}; {cell.id} needs {cell.configuration}")

    async def proven(run: CellRun, url: str, issuer: OidcIssuer, db_url: str) -> None:
        proof = {"cell": run.cell.id, "configuration": configuration, "recovery_tools": listed}
        run.artifacts.append(run.writer.artifact(proof))
        await scenario(run, url, issuer, db_url)

    run_cell(
        cell, proven, proves=Outcome.REJECTION if cell.kind == "rejection" else Outcome.SUCCESS
    )


def bindings(
    candidate: str,
    *,
    host_os: str,
    host_arch: str,
    matrix: str,
    cells: Iterable[Cell],
    staged: Callable[[str], Path | None] = staged_image,
    kernel: Mapping[str, str] | None = None,
    remote: RemoteHost | None = None,
    digest: Callable[[str, str, str], str] = volume_sha256,
    volumes: Callable[[str], str | None] = staged_volume,
) -> InputBindings:
    """The expected ``Context`` of every bound service, native local and remote tool cell.

    A native cell boots ``LANE_IMAGES[host_arch]``: its ``image_sha256`` is the staged bytes'
    digest, null when unstaged. With ``remote``, a remote cell of the provider host's
    architecture boots that architecture's remote representative there: its host is the provider
    host and its ``image_sha256`` the staged base volume's digest, null when unstaged. ``kernel``
    fills only the kernel fields a cell declares. A functional ``images.publish`` cell boots
    ``PUBLISHED_IMAGES[(host_arch, exposure)]``, the image it publishes, with a null
    ``image_sha256``.
    """
    bound = [c for c in cells if c.scenario_id.startswith("tool/") and c.node_id]
    contexts = {
        c.id: Context.model_validate(
            {"host_os": host_os, "host_arch": host_arch, "accelerator": "none"}
        )
        for c in bound
        if c.provider == "service"
    }
    native = [c for c in bound if c.provider == "local-libvirt" and c.guest_arch == host_arch]
    published = [c for c in native if c.operation == PUBLISH_TOOL and c.kind == "functional"]
    native = [c for c in native if c not in published]
    for cell in published:
        product = PUBLISHED_IMAGES.get((host_arch, cell.exposure))
        if product is not None:
            # The published image is the cell's output, so no input digest binds it.
            entry = load_rootfs_catalog()[product]
            contexts |= _lane_contexts([cell], host_os, host_arch, entry, None, kernel=kernel)
    name = LANE_IMAGES.get(host_arch)
    if native and name is not None:
        image = staged(name)
        sha = file_sha256(image) if image is not None else None
        entry = load_rootfs_catalog()[name]
        contexts |= _lane_contexts(native, host_os, host_arch, entry, sha, kernel=kernel)
    family = REMOTE_LANE_FAMILIES.get(remote.host_arch) if remote is not None else None
    if remote is not None and family is not None:
        hosted = [
            c for c in bound if c.provider == "remote-libvirt" and c.guest_arch == remote.host_arch
        ]
        base = REMOTE_REPRESENTATIVES[family]
        volume = volumes(base.name) if hosted else None
        sha = digest(remote.dest, remote.pool, volume) if volume else None
        contexts |= _lane_contexts(
            hosted, remote.host_os, remote.host_arch, base, sha, kernel=kernel
        )
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=contexts)


def _lane_contexts(
    cells: Iterable[Cell],
    host_os: str,
    host_arch: str,
    entry: GuestIdentity,
    digest: str | None,
    *,
    kernel: Mapping[str, str] | None,
) -> dict[str, Context]:
    return {
        cell.id: Context.model_validate(
            {
                "host_os": host_os,
                "host_arch": host_arch,
                "guest_os": f"{entry.distro}:{entry.version}",
                "guest_arch": entry.arch,
                "accelerator": cell.accelerator,
                "image_sha256": digest,
                **{k: v for k, v in (kernel or {}).items() if k in cell.inputs},
            }
        )
        for cell in cells
    }


def lane_image() -> tuple[str, RootfsCatalogEntry]:
    """The catalog image this host's provider cells boot; blocked on an unlisted architecture."""
    name = LANE_IMAGES.get(platform.machine())
    if name is None:
        raise ScenarioStop(Outcome.BLOCKED, f"no lane image for {platform.machine()}")
    return name, load_rootfs_catalog()[name]


class LaneFrame(Protocol):
    """A provider's System frame: provision, run ``body``, then prove owned cleanup."""

    def __call__(
        self,
        run: CellRun,
        base_url: str,
        issuer: OidcIssuer,
        db_url: str,
        *,
        project: str,
        body: CatalogBody,
        provision: Provision,
    ) -> Awaitable[None]: ...


@dataclass(frozen=True)
class Lane:
    """How one provider frames, profiles and observes a provider cell's System.

    ``profile`` takes the profile's unread ``kernel_source_ref``. ``xml`` and ``absent`` read the
    provider's own libvirt: the worker's for local-libvirt, the provider host's through the
    operator's observer for remote-libvirt.
    """

    provider: str
    entry: GuestIdentity
    profile: Callable[[str], dict[str, object]]
    xml: Callable[[str], str]
    absent: Callable[[str], bool]
    frame: LaneFrame


def _local_lane() -> Lane:
    name, entry = lane_image()
    return Lane(
        "local-libvirt",
        entry,
        partial(catalog_profile, entry, name),
        domain_xml,
        disk_absent,
        partial(on_catalog_system, image=name),
    )


def _remote_lane(arch: str) -> Lane:
    family = REMOTE_LANE_FAMILIES.get(arch)
    if family is None:
        raise ScenarioStop(Outcome.BLOCKED, f"no remote lane image for {arch}")
    image = REMOTE_REPRESENTATIVES[family]
    host = remote_host()
    volume = staged_base_volume(image)

    def profile(_unread_ref: str) -> dict[str, object]:
        return remote_profile(image.arch, volume)

    return Lane(
        "remote-libvirt",
        image,
        profile,
        partial(remote_xml, host.dest),
        partial(remote_volume_absent, host.dest),
        partial(on_remote_system, family=family),
    )


def lane_for(cell: Cell) -> Lane:
    """The lane of provider cell ``cell``: its provider host when remote, else this host."""
    if cell.provider == "remote-libvirt":
        return _remote_lane(str(cell.guest_arch))
    return _local_lane()


async def observe_guest(
    run: CellRun, op: LiveStackClient, system_id: str, scratch: Path, entry: GuestIdentity
) -> tuple[Endpoint, Path, dict[str, str]]:
    """Authorize a frame key, probe the guest as root and record its identity on ``run``."""
    scratch.mkdir(parents=True, exist_ok=True)
    endpoint, key = await asyncio.wait_for(
        authorize_ssh(op, system_id, scratch, "tool-cell"), timeout=900
    )
    probe = await asyncio.to_thread(ssh_probe, endpoint, key, IDENTITY_PROBE)
    assert probe.get("uid") == "0", f"ssh as root reported uid {probe.get('uid')!r}"
    assert os_matches(entry, probe), f"guest {probe.get('ID')} is not catalog {entry.distro}"
    run.observed |= {"guest_os": f"{entry.distro}:{entry.version}", "guest_arch": entry.arch}
    return endpoint, key, probe


@dataclass(frozen=True)
class Guest:
    """A ``ready`` lane System as a provider cell's body sees it.

    ``owned`` is the frame's list of provider-host paths the cleanup proves absent; a body that
    makes the System own more appends to it. ``observed`` is the cell's recorded context: a body
    that boots a kernel writes the kernel fields it observed there.
    """

    op: LiveStackClient
    project: str
    system_id: str
    lane: Lane
    endpoint: Endpoint
    key: Path
    probe: dict[str, str]
    owned: list[str]
    observed: dict[str, object]
    scratch: Path


LaneBody = Callable[[Guest], Awaitable[dict[str, object]]]


async def on_lane_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    body: LaneBody,
    provision: Provision = provision_catalog,
) -> None:
    """Provision the lane image in ``project``, observe its guest, prove ``effect``, then cleanup.

    ``body`` returns the ``effect`` observation; the frame adds the exposure.
    """
    lane = lane_for(run.cell)

    async def framed(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            frame = root / "frame"
            endpoint, key, probe = await observe_guest(run, op, system_id, frame, lane.entry)
            guest = Guest(
                op, project, system_id, lane, endpoint, key, probe, owned, run.observed, root
            )
            observed = await body(guest)
        run.prove("effect", {"exposure": run.cell.exposure, **observed})

    await lane.frame(
        run, base_url, issuer, db_url, project=project, body=framed, provision=provision
    )


@dataclass(frozen=True)
class LaneTarget:
    """A torn-down lane System and its released Allocation: what rejection cells aim at.

    ``observed`` is the target's host, guest, accelerator and image identity, which each
    rejection record carries; ``artifacts`` hold its own evidence and cleanup proof. ``profile``
    is the lane's provisioning profile, for the calls that name one.
    """

    project: str
    allocation_id: str
    system_id: str
    observed: dict[str, object]
    artifacts: tuple[str, ...]
    profile: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _Failed:
    """A lane target preparation that raised, and the artifacts its cleanup attempt left."""

    error: Exception
    artifacts: tuple[str, ...]


_TARGETS: dict[tuple[str, str], LaneTarget | _Failed] = {}


async def _allocation_of(db_url: str, system_id: str) -> str:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute("SELECT allocation_id FROM systems WHERE id = %s", (system_id,))
        row = await cursor.fetchone()
    assert row is not None, f"System {system_id} has no row"
    return str(row[0])


async def settled(db_url: str, project: str) -> None:
    """Wait until ``project``'s snapshot stops changing (release bookkeeping runs after it)."""
    for _ in range(_SETTLE_ATTEMPTS):
        before = await project_state(db_url, project)
        await asyncio.sleep(_SETTLE_S)
        if await project_state(db_url, project) == before:
            return
    raise AssertionError(f"project {project} kept changing after its System was released")


async def _provision_target(
    run: CellRun, target: CellRun, base_url: str, issuer: OidcIssuer, db_url: str
) -> LaneTarget:
    """Provision, observe and reclaim the target; ``target`` collects its own evidence."""
    lane = lane_for(run.cell)
    project = f"cov-{secrets.token_hex(4)}"
    seen: list[str] = []

    async def observe(op: LiveStackClient, system_id: str, _owned: list[str]) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            await observe_guest(target, op, system_id, Path(scratch), lane.entry)
        seen.append(system_id)

    await lane.frame(
        target,
        base_url,
        issuer,
        db_url,
        project=project,
        body=observe,
        provision=provision_catalog,
    )
    allocation = await _allocation_of(db_url, seen[0])
    await settled(db_url, project)
    cleanup = target.assertions["cleanup"]
    summary = run.writer.artifact(
        {"lane_target": {"system": "torn_down", "allocation": "released"}, "cleanup": cleanup}
    )
    return LaneTarget(
        project,
        allocation,
        seen[0],
        dict(target.observed),
        (*target.artifacts, cleanup, summary),
        lane.profile(f"{project}-unread"),
    )


async def lane_target(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> LaneTarget:
    """The stack's rejection target, provisioned, observed and reclaimed on first use.

    Every role check of the System tools runs before any state check, so a torn-down System and
    a released Allocation are valid targets that no background work changes. A failed or
    blocked preparation is remembered and replayed, with its cleanup-attempt artifacts, for every
    later cell rather than retried. Each provider of a stack has its own target.
    """
    key = (base_url, run.cell.provider)
    cached = _TARGETS.get(key)
    if cached is None:
        target = CellRun(run.cell, run.writer)
        try:
            cached = await _provision_target(run, target, base_url, issuer, db_url)
        except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
            # A cleanup the target already proved is evidence too, beside any cleanup attempt.
            cached = _Failed(exc, (*target.artifacts, *target.assertions.values()))
        _TARGETS[key] = cached
    if isinstance(cached, _Failed):
        run.artifacts.extend(cached.artifacts)
        if isinstance(cached.error, ScenarioStop):
            raise ScenarioStop(cached.error.outcome, str(cached.error))
        raise AssertionError(f"the lane target could not be prepared: {cached.error!r}")
    run.observed |= cached.observed
    run.artifacts.extend(cached.artifacts)
    return cached


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write tool-cell input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    write.add_argument(
        "--kernel-baseline",
        help=f"bind the verified ${FIXTURE_ROOT_ENV}/NAME kernel to cells declaring kernel inputs",
    )
    write.add_argument(
        "--remote",
        action="store_true",
        help=f"also bind the remote-libvirt cells of the provider host ${HOST_SSH_ENV} reaches",
    )
    args = parser.parse_args(argv)
    remote: RemoteHost | None = None
    if args.remote:
        try:
            remote = remote_host()
        except ScenarioStop as stop:
            print(f"cannot bind the remote cells: {stop}")
            return 2
        if remote.host_arch not in REMOTE_LANE_FAMILIES:
            print(f"cannot bind the remote cells: no remote lane image for {remote.host_arch}")
            return 2
    host_arch = platform.machine()
    kernel: dict[str, str] = {}
    if args.kernel_baseline:
        root = os.environ.get(FIXTURE_ROOT_ENV)
        if not root:
            parser.error(f"--kernel-baseline needs {FIXTURE_ROOT_ENV}")
        kernel = bound_kernel(Path(root), args.kernel_baseline, host_arch, load_fixture)
        if not kernel:
            print("no verified kernel fixture; kernel fields stay null")
    contract = build_contract()
    inputs = bindings(
        args.candidate,
        host_os=os_identity(Path("/etc/os-release").read_text(encoding="utf-8")),
        host_arch=host_arch,
        matrix=contract.matrix_sha256,
        cells=contract.cells,
        kernel=kernel,
        remote=remote,
    )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(inputs.cells)} tool-cell binding(s)")
    published = {
        c.id for c in contract.cells if c.operation == PUBLISH_TOOL and c.kind == "functional"
    }
    unstaged = sum(
        c.guest_arch is not None and c.image_sha256 is None
        for cell_id, c in inputs.cells.items()
        if cell_id not in published
    )
    if unstaged:
        print(
            f"{unstaged} provider binding(s) have no image digest: stage the lane image "
            f"({LANE_IMAGES.get(host_arch)} locally, the remote representative remotely) and "
            "declare it in KDIVE_SYSTEMS_TOML first"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
