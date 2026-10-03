"""Tool-cell frame of the live coverage carriers (#2811, ADR-0722).

A tool cell is one contract cell of a registered MCP tool: a configuration (``default`` or
``recovery``), an exposure (``direct`` or ``gateway``) and a kind (``functional`` or one rejection
boundary). :func:`run_tool_cell` proves the server's configuration from its operator catalog,
skips a cell of the other configuration without writing a record, and otherwise runs the cell
through :func:`~tests.integration.live_stack.scenario.run_cell`. :class:`HttpCaller` reaches a tool
in the cell's exposure; :func:`prove_functional` and :func:`prove_rejection` prove the cell's
assertions, with :func:`project_state` as the default protected-state snapshot.

``python -m tests.integration.live_stack.tool_cells bindings --candidate SHA --out FILE`` writes
the expected ``Context`` of every bound service tool cell.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import platform
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
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
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.scenario import CellRun, run_cell

Exposure = Literal["direct", "gateway"]
Boundary = Literal["authentication", "authorization", "project-isolation", "validation"]
Result = ToolResponse | list[ToolResponse] | LiveStackToolError
Snapshot = Callable[[], Awaitable[object]]

RECOVERY_TOOLS = frozenset({"ops.build_uses_list", "ops.recover_build_use"})
# Rows a rejected or successful call is expected to write (ADR-0722 §4).
_AUDIT_TABLES = frozenset({"audit_log", "platform_audit_log", "tool_invocation"})
_JWT_STANDARD = frozenset({"sub", "iss", "aud", "exp", "iat", "nbf", "jti"})
_CONFIGURATIONS: dict[str, tuple[str, list[str]]] = {}


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
    """One call a boundary must reject: its arguments, grants and accepted categories."""

    args: Mapping[str, object]
    grants: Grants
    categories: frozenset[str] = frozenset({ErrorCategory.AUTHORIZATION_DENIED.value})


def rejected_by_validation(exposure: str, result: Result) -> bool:
    """A ``configuration_error`` envelope, or a ``direct`` tool error naming validation."""
    if isinstance(result, LiveStackToolError):
        return exposure == "direct" and "validation error" in result.message.lower()
    return (
        isinstance(result, ToolResponse)
        and result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
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


async def _attempt(caller: Caller, tool: str, rejection: Rejection) -> Result:
    try:
        return await caller.call(tool, rejection.args, caller.token(rejection.grants))
    except LiveStackToolError as exc:
        return exc


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
            f"{tool} accepted invalid arguments: {_shape(result)}"
        )
    else:
        category = result.error_category if isinstance(result, ToolResponse) else None
        assert category is not None, f"{tool} was not rejected: {_shape(result)}"
        assert category in rejection.categories, (
            f"{tool} rejected with {category}, not one of {sorted(rejection.categories)}"
        )
    return {"rejected": _shape(result)}


def _digest(state: object) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


async def prove_rejection(
    run: CellRun, caller: Caller, boundary: Boundary, rejection: Rejection, snapshot: Snapshot
) -> None:
    """Prove ``boundary`` rejects the call and the protected state is unchanged (ADR-0722)."""
    before = await snapshot()
    observation = await _observe(caller, run.cell.operation, boundary, rejection)
    run.prove(boundary, {"exposure": caller.exposure, **observation})
    after = await snapshot()
    assert after == before, f"protected state changed across the rejected {run.cell.operation}"
    state = _digest(before)
    run.prove("unchanged-state", {"snapshot_sha256": state})
    run.prove("cleanup", {"owned": [], "snapshot_sha256": state})


Functional = Callable[[HttpCaller, Grants], Awaitable[dict[str, object]]]


async def prove_functional(
    run: CellRun, caller: HttpCaller, grants: Grants, body: Functional, snapshot: Snapshot
) -> None:
    """Prove ``effect`` with ``body``, then ``cleanup`` as an unchanged project snapshot."""
    before = await snapshot()
    run.prove("effect", {"exposure": caller.exposure, **await body(caller, grants)})
    after = await snapshot()
    assert after == before, f"{run.cell.operation} left durable state in the cell's project"
    run.prove("cleanup", {"owned": [], "snapshot_sha256": _digest(before)})


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

    A completed rejection cell records ``rejection``, a functional one ``success``.
    """
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
) -> InputBindings:
    """The expected ``Context`` of every bound service tool cell in ``cells``."""
    contexts = {
        cell.id: Context.model_validate(
            {"host_os": host_os, "host_arch": host_arch, "accelerator": "none"}
        )
        for cell in cells
        if cell.provider == "service" and cell.scenario_id.startswith("tool/") and cell.node_id
    }
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=contexts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write tool-cell input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    inputs = bindings(
        args.candidate,
        host_os=os_identity(Path("/etc/os-release").read_text(encoding="utf-8")),
        host_arch=platform.machine(),
        matrix=contract.matrix_sha256,
        cells=contract.cells,
    )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(inputs.cells)} tool-cell binding(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
