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
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field, replace
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
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, run_cell

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
