"""The three external-boot recovery contracts, as registered MCP tools (#2117).

The services themselves are covered by ``tests/services/external_boot/test_recovery_requests.py``.
This module covers what only the registration adds: the schema an agent reads (maturity, hints,
parameter descriptions, the wrapper docstring) and that a call through the registered wrapper
reaches the service and comes back as the same failure envelope.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import pytest
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.schema_advertising import registered_tools
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.lifecycle.runs import registrar as runs_registrar
from kdive.mcp.tools.lifecycle.systems import registrar as systems_registrar
from kdive.mcp.tools.ops.security import breakglass
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role, RoleDenied
from kdive.security.secrets.secret_registry import SecretRegistry
from scripts.generate.gen_tool_reference import _maturity_detail
from tests.mcp._seed import seed_run_on_system
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver
from tests.mcp.tool_registry_support import build_registered_tools
from tests.services.external_boot.conftest import seed_activation

_STATE = ExternalBootActivationState
_RELEASE = "runs.release_external_boot"
_RESOLVE = "systems.resolve_external_boot_conflict"
_ORPHAN = "ops.resolve_recovery_orphan"
_CONTRACTS = (_RELEASE, _RESOLVE, _ORPHAN)
_UNAVAILABLE = "recovery_executor_unavailable"
_RESOLUTION = "restore-recorded-source"
_DIGEST = "sha256:" + "b" * 64
# The first sentence has to say what the tool does today. These are the words that say it.
_MISSING_EXECUTOR_WORDS = ("missing", "unavailable", "not installed")

TOOLS = {tool.name: tool for tool in build_registered_tools()}


# --- the registered schema ------------------------------------------------------------------


def test_the_three_contracts_are_registered() -> None:
    assert set(_CONTRACTS) <= TOOLS.keys()


@pytest.mark.parametrize("name", _CONTRACTS)
def test_each_contract_is_a_mutation_tool_with_flat_parameters(name: str) -> None:
    tool = TOOLS[name]
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert "request" not in tool.parameters.get("properties", {})


def _destructive_hint(name: str) -> bool:
    annotations = TOOLS[name].annotations
    return bool(annotations is not None and annotations.destructiveHint)


def test_only_the_orphan_repair_is_destructive() -> None:
    """The orphan repair deletes or adopts quarantined objects; the other two do not."""
    assert {name for name in _CONTRACTS if _destructive_hint(name)} == {_ORPHAN}
    assert _ORPHAN in _docmeta.DESTRUCTIVE_TOOLS


@pytest.mark.parametrize("name", _CONTRACTS)
def test_each_contract_declares_a_partial_maturity_the_generator_accepts(name: str) -> None:
    """`just docs-check` fails on a malformed detail, so the generator's own check runs here."""
    meta = TOOLS[name].meta or {}
    assert meta.get("maturity") == "partial"
    detail = _maturity_detail(name, "partial", dict(meta))
    assert detail is not None
    assert detail.reason == "degraded_stub"
    assert _UNAVAILABLE in detail.detail
    assert "#2118" in detail.promotion


@pytest.mark.parametrize("name", _CONTRACTS)
def test_every_parameter_description_is_one_line(name: str) -> None:
    """The reference generator rejects a newline inside a parameter description."""
    properties = TOOLS[name].parameters.get("properties", {})
    assert properties
    for parameter, schema in properties.items():
        description = schema.get("description")
        assert isinstance(description, str) and description.strip(), f"{name}:{parameter}"
        assert "\n" not in description, f"{name}:{parameter}"


@pytest.mark.parametrize("name", _CONTRACTS)
def test_each_docstring_opens_on_what_the_tool_does_today(name: str) -> None:
    """An agent that reads only the first sentence must not believe the operation happened."""
    description = TOOLS[name].description or ""
    opening = description.split(".")[0].lower()
    assert "validate" in opening, opening
    assert "report" in opening, opening
    assert any(word in opening for word in _MISSING_EXECUTOR_WORDS), opening


@pytest.mark.parametrize("name", _CONTRACTS)
def test_each_docstring_discloses_the_refusal_and_names_the_promotion(name: str) -> None:
    description = TOOLS[name].description or ""
    assert _UNAVAILABLE in description
    assert "configuration_error" in description
    assert "#2118" in description


@pytest.mark.parametrize(
    ("name", "role"),
    [(_RELEASE, "contributor"), (_RESOLVE, "admin"), (_ORPHAN, "platform_admin")],
)
def test_each_docstring_states_the_required_role(name: str, role: str) -> None:
    assert role in (TOOLS[name].description or "")


@pytest.mark.parametrize("name", _CONTRACTS)
def test_each_docstring_names_the_recovery_action_it_cannot_perform(name: str) -> None:
    """A System stuck in a recovery state is recovered by teardown, and observed by runs.get."""
    description = TOOLS[name].description or ""
    assert "systems.teardown" in description
    assert "runs.get" in description
    assert "recovery_conflict" in description
    assert "recovery_failed" in description


def test_the_release_names_the_only_admissible_activation_state() -> None:
    assert "active" in (TOOLS[_RELEASE].description or "")


def test_the_conflict_resolution_names_the_only_admissible_activation_state() -> None:
    assert "recovery_conflict" in (TOOLS[_RESOLVE].description or "")


def test_the_resolution_operation_field_names_its_single_accepted_value() -> None:
    description = TOOLS[_RESOLVE].parameters["properties"]["operation"]["description"]
    assert _RESOLUTION in description


def test_the_observed_identity_field_discloses_that_only_its_shape_is_checked() -> None:
    """The compare-and-set that consumes it lands with the executor, so no digest is compared."""
    description = TOOLS[_RESOLVE].parameters["properties"]["observed_identity"]["description"]
    assert "shape" in description.lower()
    assert "systems.get" in description


def test_the_orphan_disposition_field_names_both_accepted_values() -> None:
    description = TOOLS[_ORPHAN].parameters["properties"]["disposition"]["description"]
    assert "delete" in description
    assert "adopt" in description


# --- the registered call path ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Harness:
    """The three registrars on one app, bound to a migrated database."""

    tools: dict[str, FunctionTool]
    pool: AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class _Seeded:
    system_id: str
    run_id: str


def _ctx(
    role: Role = Role.ADMIN,
    *,
    platform: frozenset[PlatformRole] = frozenset(),
) -> RequestContext:
    return RequestContext(
        principal="user-1",
        agent_session="s",
        projects=("proj",),
        roles={"proj": role},
        platform_roles=platform,
    )


@asynccontextmanager
async def _harness(url: str) -> AsyncIterator[_Harness]:
    async with runs_support.pool(url) as conn_pool:
        app = FastMCP("external-boot-contracts-test")
        resolver = provider_resolver()
        runs_registrar.register(app, conn_pool, resolver=resolver, secret_registry=SecretRegistry())
        systems_registrar.register(app, conn_pool, resolver=resolver)
        breakglass.register(app, conn_pool)
        # `registered_tools` is typed as the base `Tool`; the runtime objects are the
        # `FunctionTool`s whose `.fn` this module calls.
        tools = {tool.name: cast("FunctionTool", tool) for tool in registered_tools(app)}
        yield _Harness(tools=tools, pool=conn_pool)


def _drive[T](
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    ctx: RequestContext,
    body: Callable[[_Harness], Awaitable[T]],
) -> T:
    for module in (runs_registrar, systems_registrar, breakglass):
        monkeypatch.setattr(module, "current_context", lambda bound=ctx: bound)

    async def _run() -> T:
        async with _harness(url) as harness:
            return await body(harness)

    return asyncio.run(_run())


async def _seed(pool: AsyncConnectionPool, *, state: ExternalBootActivationState | None) -> _Seeded:
    system_id = await runs_support.seed_system(pool)
    run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id="b")
    if state is not None:
        async with pool.connection() as conn:
            await seed_activation(conn, state=state, system_id=UUID(system_id), run_id=UUID(run_id))
    return _Seeded(system_id=system_id, run_id=run_id)


def _assert_unavailable(response: ToolResponse) -> None:
    dumped = response.model_dump()
    assert response.error_category == "configuration_error", dumped
    assert response.data["reason"] == _UNAVAILABLE, dumped


def test_the_release_tool_reports_the_executor_is_unavailable(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _body(harness: _Harness) -> ToolResponse:
        seeded = await _seed(harness.pool, state=_STATE.ACTIVE)
        return await harness.tools[_RELEASE].fn(run_id=seeded.run_id)

    _assert_unavailable(_drive(migrated_url, monkeypatch, _ctx(), _body))


def test_the_conflict_resolution_tool_reports_the_executor_is_unavailable(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _body(harness: _Harness) -> ToolResponse:
        seeded = await _seed(harness.pool, state=_STATE.RECOVERY_CONFLICT)
        return await harness.tools[_RESOLVE].fn(
            system_id=seeded.system_id,
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
        )

    _assert_unavailable(_drive(migrated_url, monkeypatch, _ctx(), _body))


def test_the_orphan_repair_tool_reports_the_executor_is_unavailable(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _body(harness: _Harness) -> ToolResponse:
        seeded = await _seed(harness.pool, state=None)
        return await harness.tools[_ORPHAN].fn(
            system_id=seeded.system_id,
            object_identities=["objects/orphan-1"],
            disposition="delete",
        )

    ctx = _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))
    _assert_unavailable(_drive(migrated_url, monkeypatch, ctx, _body))


def test_the_release_tool_denies_a_viewer(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`require_role` raises through the wrapper; the server middleware renders the envelope."""

    async def _body(harness: _Harness) -> None:
        seeded = await _seed(harness.pool, state=_STATE.ACTIVE)
        with pytest.raises(RoleDenied):
            await harness.tools[_RELEASE].fn(run_id=seeded.run_id)

    _drive(migrated_url, monkeypatch, _ctx(Role.VIEWER), _body)


def test_the_conflict_resolution_tool_denies_a_contributor(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _body(harness: _Harness) -> None:
        seeded = await _seed(harness.pool, state=_STATE.RECOVERY_CONFLICT)
        with pytest.raises(RoleDenied):
            await harness.tools[_RESOLVE].fn(
                system_id=seeded.system_id,
                operation=_RESOLUTION,
                observed_identity=_DIGEST,
            )

    _drive(migrated_url, monkeypatch, _ctx(Role.CONTRIBUTOR), _body)


def test_the_orphan_repair_tool_denies_a_caller_without_the_platform_role(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform-admin tools return the denial envelope rather than raising."""

    async def _body(harness: _Harness) -> ToolResponse:
        seeded = await _seed(harness.pool, state=None)
        return await harness.tools[_ORPHAN].fn(
            system_id=seeded.system_id,
            object_identities=["objects/orphan-1"],
            disposition="delete",
        )

    response = _drive(migrated_url, monkeypatch, _ctx(), _body)
    assert response.error_category == "authorization_denied", response.model_dump()
    assert response.data["missing_roles"] == [PlatformRole.PLATFORM_ADMIN.value]
