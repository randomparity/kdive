"""``runs.validate_profile`` — no-insert build-profile validation (#839, ADR-0259).

The handler (`validate_build_profile`) runs the same `BuildProfile.parse` +
`check_source_kind_compatibility` checks `runs.create` runs, over the *raw* profile document,
and returns the typed envelope without inserting a Run or touching capacity. The tests cover:

1. **Parse + external lane** — DB-free: every parse-failure and the external lane return before
   any DB access, so these run without a Docker daemon.
2. **Server lane** — DB-backed: warm-tree/git against the seeded ``worker-local`` (local) host,
   an inserted ``ssh`` host (remote-incompat), and an unregistered host (compat skipped).
3. **Registrar boundary** — the tool is exposed ``read_only`` and is auth-only.
4. **Parity** — the compat verdict equals ``_compat_block_response``'s, and the structural
   accept/reject set equals the ``runs.create`` boundary union's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter, ValidationError

from kdive.domain.errors import CategorizedError
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.runs import registrar as runs_registrar
from kdive.mcp.tools.lifecycle.runs.validate_profile import validate_build_profile
from kdive.profiles.build import (
    BuildProfile,
    ExternalBuildProfile,
    ServerBuildProfile,
)
from kdive.profiles.types import BuildProfileInput
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role

# --- helpers ---------------------------------------------------------------


def _closed_pool() -> AsyncConnectionPool:
    """A never-opened pool: the parse/external paths return before any connection."""
    return AsyncConnectionPool("postgresql://unused", open=False)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx() -> RequestContext:
    return RequestContext(
        principal="validate-user",
        agent_session="validate-session",
        projects=("proj",),
        roles={"proj": Role.VIEWER},
        platform_roles=frozenset(),
    )


def _data(resp: ToolResponse) -> dict[str, Any]:
    return cast(dict[str, Any], resp.data)


def _validate_dbfree(profile: BuildProfileInput) -> ToolResponse:
    """Drive the handler on a path that never opens a DB connection."""

    async def _run() -> ToolResponse:
        return await validate_build_profile(_closed_pool(), profile)

    return asyncio.run(_run())


async def _insert_ssh_host(pool: AsyncConnectionPool, name: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
            "workspace_root, max_concurrent) VALUES (%s, %s, 'ssh', '10.0.0.1', "
            "'cred-ref', '/build', 2)",
            (uuid4(), name),
        )


# --- DB-free: external lane + parse failures -------------------------------


def test_external_profile_is_valid() -> None:
    resp = _validate_dbfree({"schema_version": 1, "source": "external"})
    assert resp.status == "valid"
    assert _data(resp)["source"] == "external"
    assert "build_host" not in _data(resp)
    assert resp.suggested_next_actions == ["runs.create"]


def test_external_profile_normalized_echo_round_trips() -> None:
    resp = _validate_dbfree({"schema_version": 1, "source": "external"})
    parsed = BuildProfile.parse(cast(dict[str, Any], _data(resp)["profile"]))
    assert isinstance(parsed, ExternalBuildProfile)


def test_unknown_source_is_configuration_error() -> None:
    resp = _validate_dbfree({"schema_version": 1, "source": "bogus"})
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.suggested_next_actions == ["runs.profile_examples"]


def test_extra_field_is_configuration_error() -> None:
    resp = _validate_dbfree({"schema_version": 1, "source": "external", "surprise": "x"})
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"


def test_external_with_server_field_is_configuration_error() -> None:
    resp = _validate_dbfree(
        {"schema_version": 1, "source": "external", "kernel_source_ref": "warm"}
    )
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"


def test_bare_url_source_rejected_without_leaking_value() -> None:
    bare_url = "https://example.com/private-repo.git"
    resp = _validate_dbfree(
        {"schema_version": 1, "source": "server", "kernel_source_ref": bare_url}
    )
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    # Redaction (ADR-0029/0242): the submitted URL never appears in the envelope.
    assert bare_url not in repr(_data(resp))
    assert (resp.detail is None) or (bare_url not in resp.detail)


def test_empty_string_source_is_configuration_error() -> None:
    resp = _validate_dbfree({"schema_version": 1, "source": "server", "kernel_source_ref": ""})
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"


def test_wrong_type_schema_version_is_configuration_error() -> None:
    resp = _validate_dbfree({"schema_version": 2, "source": "server", "kernel_source_ref": "warm"})
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"


# --- DB-backed: server lane against an unregistered host --------------------


def test_unregistered_build_host_is_valid_with_compat_skipped(migrated_url: str) -> None:
    # An explicitly named host that has no build_hosts row: the server lane still queries the DB
    # to learn it is absent, then allows it (compat skipped) — matching _compat_block_response.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            return await validate_build_profile(
                pool,
                {
                    "schema_version": 1,
                    "source": "server",
                    "kernel_source_ref": "warm",
                    "build_host": "no-such-host-xyz",
                },
            )

    resp = asyncio.run(_run())
    assert resp.status == "valid"
    data = _data(resp)
    assert data["build_host"] == "no-such-host-xyz"
    assert data["build_host_registered"] is False
    assert data["host_kind"] is None
    assert data["source_kind"] == "warm-tree"


# --- DB-backed: server lane against registered hosts ------------------------


def test_server_warm_tree_against_local_host_is_valid(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            return await validate_build_profile(
                pool, {"schema_version": 1, "source": "server", "kernel_source_ref": "warm"}
            )

    resp = asyncio.run(_run())
    assert resp.status == "valid"
    data = _data(resp)
    assert data["source"] == "server"
    assert data["build_host"] == "worker-local"
    assert data["build_host_registered"] is True
    assert data["host_kind"] == "local"
    assert data["source_kind"] == "warm-tree"
    parsed = BuildProfile.parse(cast(dict[str, Any], data["profile"]))
    assert isinstance(parsed, ServerBuildProfile)


def test_server_git_against_local_host_is_valid(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            return await validate_build_profile(
                pool,
                {
                    "schema_version": 1,
                    "source": "server",
                    "kernel_source_ref": {"git": {"remote": "git://x/y", "ref": "main"}},
                },
            )

    resp = asyncio.run(_run())
    assert resp.status == "valid"
    assert _data(resp)["source_kind"] == "git"


def test_omitted_source_defaults_to_server(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            return await validate_build_profile(
                pool, {"schema_version": 1, "kernel_source_ref": "warm"}
            )

    resp = asyncio.run(_run())
    assert resp.status == "valid"
    assert cast(dict[str, Any], _data(resp)["profile"])["source"] == "server"


def test_server_warm_tree_against_ssh_host_is_incompatible(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _insert_ssh_host(pool, "validate-ssh")
            return await validate_build_profile(
                pool,
                {
                    "schema_version": 1,
                    "source": "server",
                    "kernel_source_ref": "warm",
                    "build_host": "validate-ssh",
                },
            )

    resp = asyncio.run(_run())
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    data = _data(resp)
    assert data["build_host"] == "validate-ssh"
    assert data["host_kind"] == "ssh"
    assert resp.suggested_next_actions == ["runs.profile_examples"]


def test_server_git_against_ssh_host_is_valid(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            await _insert_ssh_host(pool, "validate-ssh-git")
            return await validate_build_profile(
                pool,
                {
                    "schema_version": 1,
                    "source": "server",
                    "kernel_source_ref": {"git": {"remote": "git://x/y", "ref": "main"}},
                    "build_host": "validate-ssh-git",
                },
            )

    resp = asyncio.run(_run())
    assert resp.status == "valid"
    assert _data(resp)["host_kind"] == "ssh"
    assert _data(resp)["source_kind"] == "git"


# --- registrar boundary + auth-only ----------------------------------------


def _read_only_hint(tool: object) -> bool | None:
    annotations = getattr(tool, "annotations", None)
    value = getattr(annotations, "readOnlyHint", None)
    return value if isinstance(value, bool) else None


def test_validate_profile_registered_read_only_and_auth_only(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def fake_current_context() -> RequestContext:
        seen.append(True)
        return _ctx()

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            monkeypatch.setattr(runs_registrar, "current_context", fake_current_context)
            app = FastMCP("runs-validate-profile-test")
            runs_registrar.register(app, pool, resolver=cast(ProviderResolver, object()))
            tools = {tool.name: tool for tool in await app.list_tools()}
            assert "runs.validate_profile" in tools
            assert _read_only_hint(tools["runs.validate_profile"]) is True
            fn = cast(Any, tools["runs.validate_profile"]).fn
            return cast(ToolResponse, await fn({"schema_version": 1, "source": "external"}))

    resp = asyncio.run(_run())
    assert isinstance(resp, ToolResponse)
    assert resp.status == "valid"
    assert seen == [True]


# --- parity invariants -----------------------------------------------------

# Server documents that all parse cleanly; the only axis under test is compat.
_COMPAT_MATRIX: list[tuple[str, dict[str, Any]]] = [
    ("worker-local", {"schema_version": 1, "source": "server", "kernel_source_ref": "warm"}),
    (
        "worker-local",
        {
            "schema_version": 1,
            "source": "server",
            "kernel_source_ref": {"git": {"remote": "git://x/y", "ref": "main"}},
        },
    ),
    (
        "parity-ssh",
        {
            "schema_version": 1,
            "source": "server",
            "kernel_source_ref": "warm",
            "build_host": "parity-ssh",
        },
    ),
    (
        "parity-ssh",
        {
            "schema_version": 1,
            "source": "server",
            "kernel_source_ref": {"git": {"remote": "git://x/y", "ref": "main"}},
            "build_host": "parity-ssh",
        },
    ),
    (
        "absent",
        {
            "schema_version": 1,
            "source": "server",
            "kernel_source_ref": "warm",
            "build_host": "absent-host-parity",
        },
    ),
]


def test_compat_verdict_matches_create_time_compat_block(migrated_url: str) -> None:
    # Import here so a refactor of the admission internal surfaces as a clear import error.
    from kdive.services.runs.admission import _compat_block_response

    async def _run() -> list[tuple[bool, bool]]:
        results: list[tuple[bool, bool]] = []
        async with _pool(migrated_url) as pool:
            await _insert_ssh_host(pool, "parity-ssh")
            for _label, doc in _COMPAT_MATRIX:
                resp = await validate_build_profile(pool, doc)
                validate_ok = resp.status == "valid"
                parsed = BuildProfile.parse(doc)
                async with pool.connection() as conn:
                    block = await _compat_block_response(conn, parsed, "parity-object")
                create_ok = block is None
                results.append((validate_ok, create_ok))
        return results

    for validate_ok, create_ok in asyncio.run(_run()):
        assert validate_ok == create_ok


# Structural matrix: documents whose *shape* is the axis under test.
_STRUCTURAL_MATRIX: list[dict[str, Any]] = [
    {"schema_version": 1, "source": "external"},
    {"schema_version": 1, "source": "server", "kernel_source_ref": "warm"},
    {"schema_version": 1, "kernel_source_ref": "warm"},
    {
        "schema_version": 1,
        "source": "server",
        "kernel_source_ref": {"git": {"remote": "git://x/y", "ref": "main"}},
    },
    {"schema_version": 1, "source": "external", "kernel_source_ref": "warm"},
    {"schema_version": 1, "source": "server", "kernel_source_ref": ""},
    {"schema_version": 2, "source": "server", "kernel_source_ref": "warm"},
    {"schema_version": 1, "source": "server", "surprise": "x"},
    {"schema_version": 1, "source": "server", "kernel_source_ref": "https://x/y.git"},
]

_UNION_ADAPTER: TypeAdapter[ExternalBuildProfile | ServerBuildProfile] = TypeAdapter(
    ExternalBuildProfile | ServerBuildProfile
)


def _accepts_via_parse(doc: dict[str, Any]) -> bool:
    try:
        BuildProfile.parse(doc)
    except CategorizedError:
        return False
    return True


def _accepts_via_union(doc: dict[str, Any]) -> bool:
    try:
        _UNION_ADAPTER.validate_python(doc)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("doc", _STRUCTURAL_MATRIX)
def test_structural_accept_reject_matches_create_boundary_union(doc: dict[str, Any]) -> None:
    # BuildProfile.parse (validate_profile) and the ExternalBuildProfile | ServerBuildProfile
    # union (the runs.create signature) must agree on accept/reject for any document, so the two
    # surfaces cannot diverge into a "validate said valid, create rejected it" surprise.
    assert _accepts_via_parse(doc) == _accepts_via_union(doc)
