"""Prove the core MCP tool cells over HTTP and record coverage evidence (#2811, ADR-0722).

``live_stack``-marked. One parameter per contract cell of the six tools #2811 owns: each
configuration (proven from the operator catalog; the other configuration's cells skip), each
exposure (operator direct, agent gateway) and each kind. A functional cell compares the tool's
result with a source the server did not produce: the token's own claims, the fixture catalog and
``systems.toml`` read by this process, or the operator-direct catalog. A rejection cell proves its
boundary and an unchanged project snapshot. ``docs/operating/runbooks/live-testing.md`` covers the
two lanes, bindings, assembly and qualification.
"""

from __future__ import annotations

import secrets
from functools import partial
from typing import cast

import pytest

from kdive.components.catalog import fixture_catalog_path_from_env, load_fixture_catalog
from kdive.domain.catalog.images import ImageVisibility
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.path import systems_toml_path
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.composition import _component_sources
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.services.systems.validation import validate_profile_for_provider
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import (
    Exposure,
    Functional,
    Grants,
    HttpCaller,
    Rejection,
    boundary_of,
    claims_of,
    matches,
    one,
    operator_catalog,
    project_state,
    prove_functional,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "fixtures.validate",
    "projects.list",
    "session.whoami",
    "systems.profile_examples",
    "tools.invoke",
    "tools.search",
)
_SCHEMA_TOOLS = ["session.whoami", "tools.invoke"]
_CONCEPT = "granted projects roles"
_VALID: dict[str, dict[str, object]] = {
    "tools.invoke": {"name": "projects.list", "arguments": {}},
    "tools.search": {"names": ["session.whoami"]},
}
_INVALID: dict[str, dict[str, object]] = {
    "tools.invoke": {"arguments": {}},
    "tools.search": {"limit": 0},
}


def _grants(project: str) -> Grants:
    """Distinctive claims: two projects, a role on one, and a platform role."""
    return Grants(
        subject=f"{project}-agent",
        projects=(project, f"{project}-b"),
        roles={project: "viewer"},
        platform_roles=("platform_auditor",),
    )


def _identity(claims: dict[str, object]) -> dict[str, object]:
    """What ``session.whoami`` must report for a token carrying ``claims``."""
    roles = cast(dict[str, str], claims.get("roles") or {})
    return {
        "principal": claims["sub"],
        "client_id": claims.get("azp") or claims.get("client_id"),
        "projects": sorted(set(cast(list[str], claims.get("projects") or []))),
        "roles": dict(sorted(roles.items())),
        "platform_roles": sorted(cast(list[str], claims.get("platform_roles") or [])),
    }


def _assert_projects(env: ToolResponse, claims: dict[str, object]) -> None:
    identity = _identity(claims)
    roles = cast(dict[str, str], identity["roles"])
    expected = [
        {"project": p, "role": roles.get(p, "")} for p in cast(list[str], identity["projects"])
    ]
    assert env.object_id == "projects", f"envelope {env.object_id} is not projects.list's"
    assert [dict(item.data) for item in env.items] == expected
    assert env.data.get("principal") == identity["principal"]
    assert env.data.get("platform_roles") == identity["platform_roles"]


async def _whoami(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    env = one(await caller.call("session.whoami", {}, token, discover=True))
    expected = _identity(claims_of(token))
    assert dict(env.data) == expected, f"whoami {env.data} != token claims {expected}"
    return {"fields_equal_claims": sorted(expected)}


async def _projects(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    env = one(await caller.call("projects.list", {}, token, discover=True))
    _assert_projects(env, claims_of(token))
    return {"items": len(env.items)}


async def _invoke(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    env = one(await caller.call("tools.invoke", _VALID["tools.invoke"], token, discover=True))
    _assert_projects(env, claims_of(token))
    return {"inner": "projects.list", "items": len(env.items)}


async def _search(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    named = one(await caller.call("tools.search", {"names": _SCHEMA_TOOLS}, token, discover=True))
    schemas = {str(m["name"]): m.get("input_schema") for m in matches(named)}
    direct = await operator_catalog(caller.base_url, caller.issuer, grants)
    assert list(schemas) == _SCHEMA_TOOLS, f"names mode returned {list(schemas)}"
    for name in _SCHEMA_TOOLS:
        assert schemas[name] == direct[name], f"{name} schema differs from direct exposure"
    # A test-side anchor: tools.invoke's own signature, independent of both catalogs.
    invoke = cast(dict[str, object], schemas["tools.invoke"])
    assert set(cast(dict[str, object], invoke["properties"])) == {"name", "arguments"}
    assert invoke.get("required") == ["name"], f"tools.invoke requires {invoke.get('required')}"
    concept = one(await caller.call("tools.search", {"query": _CONCEPT}, token))
    found = [str(m["name"]) for m in matches(concept)]
    assert "projects.list" in found, f"{_CONCEPT!r} found {found}"
    return {"named": _SCHEMA_TOOLS, "schemas_equal_direct": True, "concept_hits": found}


async def _fixtures(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    env = one(await caller.call("fixtures.validate", {}, caller.token(grants), discover=True))
    path = fixture_catalog_path_from_env()
    profiles = sorted(
        load_fixture_catalog(path).profiles, key=lambda p: (p.provider, p.name, p.arch)
    )
    expected = [{"provider": p.provider, "name": p.name, "arch": p.arch} for p in profiles]
    assert env.status == "valid", f"fixtures.validate reported {env.status}"
    assert env.data.get("path") == str(path), "server resolved another fixture catalog"
    assert env.data.get("profiles") == expected, "profiles differ from the fixture catalog"
    return {"profiles": len(expected)}


def _expected_local_rootfs() -> dict[str, object]:
    doc = load_inventory_optional(systems_toml_path())
    public = [
        image.name
        for image in (doc.image if doc is not None else [])
        if image.provider == "local-libvirt" and image.visibility == ImageVisibility.PUBLIC
    ]
    if public:
        return {"kind": "catalog", "provider": "local-libvirt", "name": public[0]}
    return {"kind": "local", "path": "/REPLACE_ME/rootfs.img"}


async def _examples(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    env = one(
        await caller.call("systems.profile_examples", {}, caller.token(grants), discover=True)
    )
    providers = []
    for item in env.items:
        provider = str(item.data.get("provider"))
        raw = cast(dict[str, object], item.data.get("profile"))
        profile = ProvisioningProfile.parse(raw)
        if provider == "local-libvirt":
            validate_profile_for_provider(
                profile, LocalLibvirtProfilePolicy(), _component_sources()
            )
            section = cast(dict[str, dict[str, object]], raw["provider"])["local-libvirt"]
            assert section["rootfs"] == _expected_local_rootfs(), "rootfs is not the inventory's"
        providers.append(provider)
    assert "local-libvirt" in providers, f"no local-libvirt example among {providers}"
    return {"providers": sorted(providers), "parsed": len(providers)}


_FUNCTIONAL: dict[str, Functional] = {
    "fixtures.validate": _fixtures,
    "projects.list": _projects,
    "session.whoami": _whoami,
    "systems.profile_examples": _examples,
    "tools.invoke": _invoke,
    "tools.search": _search,
}


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell = run.cell
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    project = f"cov-{secrets.token_hex(4)}"
    grants = _grants(project)
    snapshot = partial(project_state, db_url, project)
    if cell.kind == "functional":
        await prove_functional(run, caller, grants, _FUNCTIONAL[cell.operation], snapshot)
        return
    boundary = boundary_of(cell)
    args = _INVALID if boundary == "validation" else _VALID
    rejection = Rejection(args.get(cell.operation, {}), grants)
    await prove_rejection(run, caller, boundary, rejection, snapshot)


@pytest.mark.parametrize("cell", tool_cells(TOOLS), ids=lambda cell: cell.id)
def test_core_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a core tool and record it."""
    run_tool_cell(cell, _scenario)
