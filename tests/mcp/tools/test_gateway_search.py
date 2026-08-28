"""TDD tests for the ``tools.search`` gateway discovery tool (ADR-0268, #866).

Coverage:
* A keyword query returns the relevant tool.
* Namespace browse returns all tools in that plane.
* The result set is hard-capped and ``truncated`` is True when results were cut.
* RBAC filters: admin-only tools do not appear for a viewer-role caller.
* Matches are summaries by default; ``detail="full"`` restores the ``input_schema``
  and the complete description so ``tools.invoke`` can be called (ADR-0472).
* An exact tool-name query ranks that tool first, so a one-tool schema fetch is
  deterministic (ADR-0472).
* Namespace mode distinguishes an unauthorized plane from a nonexistent one (ADR-0472).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.mcp.schema.schema_advertising import registered_tools
from kdive.mcp.schema.tool_index import RETIRED_TOOL_NAMES
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def _operator_ctx() -> RequestContext:
    return RequestContext(
        principal="operator-user",
        agent_session="sess-op",
        projects=("proj-a",),
        roles={"proj-a": Role.OPERATOR},
    )


def _viewer_ctx() -> RequestContext:
    return RequestContext(
        principal="viewer-user",
        agent_session="sess-viewer",
        projects=("proj-a",),
        roles={"proj-a": Role.VIEWER},
    )


def _secret_registry() -> Any:
    from kdive.security.secrets.secret_registry import SecretRegistry

    return SecretRegistry()


def _call_result(result: Any) -> dict[str, Any]:
    """Extract the structured_content dict from an app.call_tool result."""
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict), f"expected structured_content dict, got {result!r}"
    return structured


def _match_names(content: dict[str, Any]) -> list[str]:
    return [m["name"] for m in content["data"]["matches"]]


@pytest.mark.parametrize("key", ["properties", "$defs", "definitions", "mapping"])
def test_schema_term_collection_never_exceeds_limit(key: str) -> None:
    from kdive.mcp.tools.gateway import _SCHEMA_TERM_LIMIT, _append_schema_terms

    schema = {key: {f"field_{index}": {"description": "value"} for index in range(250)}}
    terms: list[str] = []

    _append_schema_terms(schema, terms)

    assert len(terms) == _SCHEMA_TERM_LIMIT


# ---------------------------------------------------------------------------
# Test 1: keyword query surfaces the relevant tool
# ---------------------------------------------------------------------------


def test_query_ranks_relevant_tool_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query for 'boot a built kernel' returns runs.boot in the match list."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "boot a built kernel"})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = [m["name"] for m in content["data"]["matches"]]
    assert "runs.boot" in names, f"runs.boot not in {names}"


@pytest.mark.parametrize(
    "query",
    [
        "still running call again",
        "suggested next actions queued running",
    ],
)
def test_jobs_wait_discovered_from_followup_queries(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "jobs.wait" in _match_names(content)


# ---------------------------------------------------------------------------
# Test 2: namespace browse returns all tools in the plane
# ---------------------------------------------------------------------------


def test_namespace_browse_returns_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Namespace='debug' returns all debug.* tools visible to an operator."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"namespace": "debug", "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = {m["name"] for m in content["data"]["matches"]}
    assert {"debug.read_memory", "debug.set_breakpoint"} <= names, (
        f"expected debug.read_memory and debug.set_breakpoint in {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# Test 3: result payload is hard-capped; truncated reflects overflow
# ---------------------------------------------------------------------------


def test_payload_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """limit=3 on a namespace with >3 tools yields 3 matches and truncated=True."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"namespace": "debug", "limit": 3})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert len(content["data"]["matches"]) == 3
    assert content["data"]["truncated"] is True


# ---------------------------------------------------------------------------
# Retired tool names stay searchable vocabulary for their replacement (ADR-0456 §3)
# ---------------------------------------------------------------------------


def _every_scope_ctx() -> RequestContext:
    """A caller holding every project and platform role, so RBAC filters nothing out."""
    return RequestContext(
        principal="all-scopes-user",
        agent_session="sess-all",
        projects=("proj-a",),
        roles={"proj-a": Role.ADMIN},
        platform_roles=frozenset(PlatformRole),
    )


@pytest.mark.parametrize(("retired", "replacement"), sorted(RETIRED_TOOL_NAMES.items()))
def test_retired_tool_name_query_finds_its_replacement(
    monkeypatch: pytest.MonkeyPatch, retired: str, replacement: str
) -> None:
    """Searching a retired tool name returns the tool that replaced it.

    Retired names are curated search vocabulary, not compatibility aliases: the old name is
    gone from the registry, so discovery is the only path back to the new entry point.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": retired, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert replacement in names, f"query {retired!r} did not surface {replacement!r}: {names}"
    assert retired not in names, f"{retired!r} is retired but still registered"


@pytest.mark.parametrize("query", ["ops.queue_pause", "ops.queue_resume", "resume"])
def test_queue_pause_resume_vocabulary_finds_the_state_setter(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Both retired queue names, and the resume intent word, rank ops.set_queue_paused.

    "resume" is the one direction the replacement's own name does not spell, so it can only
    reach the tool through the retired-name vocabulary (ADR-0459).
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "ops.set_queue_paused" in _match_names(content)


@pytest.mark.parametrize("query", ["postmortem.triage", "triage", "postmortem"])
def test_postmortem_triage_vocabulary_finds_postmortem_crash(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """The retired name and its intent words all rank postmortem.crash into the results."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "postmortem.crash" in _match_names(content)


@pytest.mark.parametrize("query", ["resources.cordon", "resources.uncordon", "cordon", "uncordon"])
def test_cordon_vocabulary_finds_resources_set_scheduling(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Both retired names and the bare `cordon`/`uncordon` intent words rank the setter.

    `uncordon` is the harder half: it appears nowhere in the live tool's name, description, or
    schema, so only the retired-name vocabulary can surface it (ADR-0460).
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "resources.set_scheduling" in _match_names(content)


@pytest.mark.parametrize(
    "query",
    [
        "artifacts.find",
        "search text in a log",
        "find string in console output",
        "grep a crash signature in a console log",
    ],
)
def test_artifact_text_search_vocabulary_finds_artifacts_get(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """The retired name and text-search intent phrases all rank artifacts.get (ADR-0462).

    The jump matcher is a `find` parameter on `artifacts.get`, not a tool, so an agent that
    describes the intent ("search text in a log") has no tool name to go on. Search is the only
    path from that intent to the capability.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert "artifacts.get" in names, f"query {query!r} did not surface artifacts.get: {names}"


@pytest.mark.parametrize("query", ["images.build", "build"])
def test_image_build_vocabulary_finds_images_publish(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """The retired name and the bare `build` intent word rank images.publish (ADR-0461).

    `images.build` was a duplicate of `images.publish`, so an agent that knows only the build
    half of the name must still reach the one surviving tool.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert "images.publish" in names
    assert "images.build" not in names


@pytest.mark.parametrize(
    "query",
    [
        "debug.step",
        "debug.next",
        "debug.step_instruction",
        "debug.finish",
        "step over",
        "step into",
        "finish frame",
        "stepi",
    ],
)
def test_stepping_vocabulary_finds_debug_advance(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """All four retired stepping names and their intent words rank debug.advance (ADR-0463).

    The surviving name spells neither "step" nor "finish", so those words reach the tool only
    through the retired-name vocabulary and the curated keyword set — the mode enum supplies
    `into`/`over`/`instruction`/`out` but not the verbs an agent is most likely to type.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert "debug.advance" in names, f"query {query!r} did not surface debug.advance: {names}"


@pytest.mark.parametrize(
    "query",
    [
        "fixtures.list",
        "list test fixture images",
        "baseline public images",
        "public_baseline",
    ],
)
def test_fixtures_list_vocabulary_finds_images_list(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """The retired name and the fixture/baseline intent phrases rank images.list (ADR-0465).

    `fixtures.list` became `images.list(scope="public_baseline")`, so an agent that knows only
    the old name — or that describes the intent in fixture vocabulary the surviving tool's own
    name never spells — must still reach the replacement.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert "images.list" in names, f"query {query!r} did not surface images.list: {names}"
    assert "fixtures.list" not in names


@pytest.mark.parametrize(
    "query",
    [
        "resources.register_remote_libvirt",
        "resources.register_local_libvirt",
        "resources.register_fault_inject",
        "register a remote libvirt host",
        "add a fault injection resource",
        "register a local libvirt host as capacity",
    ],
)
def test_resource_registration_vocabulary_finds_resources_register(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """The three retired names and their intent phrases all rank `resources.register` (ADR-0464).

    The provider words ("remote libvirt", "fault injection") used to *be* the tool names. After
    the consolidation they survive only as `kind` enum values and retired-name vocabulary, so an
    agent that describes the provider rather than the verb must still land on the one tool.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert "resources.register" in names, (
        f"query {query!r} did not surface resources.register: {names}"
    )


# ---------------------------------------------------------------------------
# Test 4: RBAC filter hides admin-only tools from a viewer
# ---------------------------------------------------------------------------


def test_results_rbac_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """control.force_crash (admin-only) does not appear in a viewer's search results."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "force crash"})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = {m["name"] for m in content["data"]["matches"]}
    assert "control.force_crash" not in names, (
        "control.force_crash must not be visible to a viewer-role caller"
    )


# ---------------------------------------------------------------------------
# Test 5: every match includes the full input schema
# ---------------------------------------------------------------------------


def test_match_includes_full_input_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """A detail='full' match for runs.get carries its full input_schema (including 'run_id')."""
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool(
            "tools.search", {"query": "get a run", "limit": 50, "detail": "full"}
        )

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    matches = content["data"]["matches"]
    runs_get = next((m for m in matches if m["name"] == "runs.get"), None)
    assert runs_get is not None, f"runs.get not found in matches: {[m['name'] for m in matches]}"
    assert "run_id" in str(runs_get["input_schema"]), (
        f"run_id not in input_schema: {runs_get['input_schema']}"
    )
    assert runs_get["annotations"]["readOnlyHint"] is True
    assert runs_get["maturity"] == "implemented"


def test_query_indexes_parameter_names_and_descriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "sha256 size_bytes"})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "artifacts.create_run_upload" in _match_names(content)


def test_legacy_static_artifact_tool_names_find_upload_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run(query: str) -> Any:
        return await app.call_tool("tools.search", {"query": query})

    for query in ("artifacts.expected_uploads", "artifacts.feature_config_requirements"):
        result = asyncio.run(_run(query))
        content = _call_result(result)
        assert content["status"] == "ok", f"expected ok, got {content}"
        assert "artifacts.create_run_upload" in _match_names(content)


def test_result_includes_mutating_safety_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "power reset"})

    result = asyncio.run(_run())
    content = _call_result(result)
    control_power = next(m for m in content["data"]["matches"] if m["name"] == "control.power")
    assert control_power["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
    }
    assert control_power["maturity"] == "implemented"


def test_resolver_failure_keeps_search_usable_and_rbac_filtered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import kdive.mcp.tools.gateway as gateway_module
    from kdive.providers.core.resolver import ProviderResolver

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    def _raise_registered_kinds(self: ProviderResolver) -> frozenset:
        raise RuntimeError("injected resolver failure")

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())
    monkeypatch.setattr(ProviderResolver, "registered_kinds", _raise_registered_kinds)

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": "force crash"})

    with caplog.at_level(logging.WARNING, logger="kdive.mcp.tools.gateway"):
        result = asyncio.run(_run())

    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "registered_kinds() failed in tools.search" in caplog.text
    assert "control.force_crash" not in _match_names(content)


def test_projection_failure_falls_back_to_original_tool_schema(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _operator_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())
    original_project = gateway_module.project_listed_tool
    original_tools = {tool.name: tool for tool in registered_tools(app)}
    original_alloc_schema = original_tools["allocations.request"].parameters

    def _project_or_raise(tool: Any, kinds: Any) -> Any:
        if tool.name == "allocations.request":
            raise RuntimeError("injected projection failure")
        return original_project(tool, kinds)

    monkeypatch.setattr(gateway_module, "project_listed_tool", _project_or_raise)

    async def _run() -> Any:
        return await app.call_tool(
            "tools.search", {"namespace": "allocations", "limit": 50, "detail": "full"}
        )

    with caplog.at_level(logging.WARNING, logger="kdive.mcp.tools.gateway"):
        result = asyncio.run(_run())

    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "provider-schema projection failed for allocations.request" in caplog.text
    matches = content["data"]["matches"]
    assert len(matches) > 1
    allocations_request = next(m for m in matches if m["name"] == "allocations.request")
    assert allocations_request["input_schema"] == original_alloc_schema


@pytest.mark.parametrize(
    "query",
    [
        "vmcore.list",
        "captured vmcore for this run",
        "which core did this run capture",
    ],
)
def test_vmcore_list_vocabulary_finds_runs_get(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    """The retired name and the run-scoped core-lookup intent rank runs.get (ADR-0466).

    `vmcore.list` was the only Run-scoped vmcore listing; its replacement is `runs.get`, whose
    `refs.vmcore` carries the redacted core's artifact id. An agent holding only a `run_id` — no
    capture job id — must still reach the core, so the old name and the intent vocabulary both
    have to land there rather than on the System-scoped `artifacts.list`.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert "runs.get" in names, f"query {query!r} did not surface runs.get: {names}"
    assert "vmcore.list" not in names


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("accounting.usage_project", "accounting.usage"),
        ("accounting.usage_investigation", "accounting.usage"),
        ("usage for my projects", "accounting.usage"),
        ("how much has this investigation spent", "accounting.usage"),
        ("accounting.report_granted_set", "accounting.report"),
        ("accounting.report_all_projects", "accounting.report"),
        ("cost report across all projects", "accounting.report"),
        ("spend rollup for my granted projects", "accounting.report"),
        ("reports.generate_granted_set", "reports.generate"),
        ("reports.generate_all_projects", "reports.generate"),
        ("generate a billing report", "reports.generate"),
        ("download a csv spend export", "reports.generate"),
    ],
)
def test_accounting_scope_vocabulary_finds_the_merged_tool(
    monkeypatch: pytest.MonkeyPatch, query: str, expected: str
) -> None:
    """The six retired scope names and their intent phrases rank the merged tool (ADR-0467).

    The target kind (`project` / `investigation`) and the reporting scope (`granted-set` /
    `all-projects`) used to *be* the tool names. After the consolidation they survive only as
    discriminator enum values and retired-name vocabulary, so an agent that knows only an old
    name — or that describes the money intent in words no surviving tool name spells — must
    still land on the one tool.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert expected in names, f"query {query!r} did not surface {expected!r}: {names}"
    assert not any(name.endswith(("_granted_set", "_all_projects")) for name in names)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("jobs.get", "jobs.wait"),
        ("get job status", "jobs.wait"),
        ("check if my job finished", "jobs.wait"),
        ("look up a job by id", "jobs.wait"),
        ("fetch the job result", "jobs.wait"),
        ("allocations.get", "allocations.wait"),
        ("look up an allocation by id", "allocations.wait"),
        ("get allocation status", "allocations.wait"),
    ],
)
def test_point_read_vocabulary_finds_the_wait_tool(
    monkeypatch: pytest.MonkeyPatch, query: str, expected: str
) -> None:
    """The retired getter names and their intent phrases rank the wait tool (ADR-0468).

    `jobs.wait` / `allocations.wait` spell neither `get` nor `status`, so an agent phrasing
    the point read the way the removed tools were named reaches the survivor only through the
    keyword vocabulary those tools left behind. With the gateway on by default (ADR-0456) that
    is the difference between a reachable and an unreachable capability.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert expected in names, f"query {query!r} did not surface {expected!r}: {names}"
    assert "jobs.get" not in names
    assert "allocations.get" not in names


@pytest.mark.parametrize(
    "query",
    [
        "systems.define",
        "systems.provision_defined",
        "define a system",
        "define the target system to build on",
        "create a system without provisioning it yet",
        "set up a target machine",
        "provision a system I already defined",
    ],
)
def test_define_vocabulary_finds_the_one_create_lane(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """The retired define-lane names and their intent phrases rank `systems.provision` (ADR-0457).

    An agent that wants a target machine says "define a system", not "systems.provision" — the
    staged lane's own docs and the core-path guide taught that verb for months. `systems.provision`
    spells neither `define` nor `target`, so those phrasings reach the surviving create lane only
    through the vocabulary the retired tools left behind. With the gateway on by default
    (ADR-0456), a miss here is an unreachable capability, not merely a worse ranking.
    """
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _every_scope_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query, "limit": 50})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    names = _match_names(content)
    assert "systems.provision" in names, f"query {query!r} did not surface it: {names}"
    assert "systems.define" not in names
    assert "systems.provision_defined" not in names


# ---------------------------------------------------------------------------
# Summary-first matches; full detail is opt-in (ADR-0472, #1597)
# ---------------------------------------------------------------------------


def _search(app: Any, args: dict[str, Any]) -> dict[str, Any]:
    async def _run() -> Any:
        return await app.call_tool("tools.search", args)

    content = _call_result(asyncio.run(_run()))
    assert content["status"] == "ok", f"expected ok, got {content}"
    return content


def _build(monkeypatch: pytest.MonkeyPatch, ctx_factory: Any) -> Any:
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", ctx_factory)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    return build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())


def test_default_match_omits_schema_and_full_description(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every default match carries name/summary/annotations/maturity and nothing heavier."""
    app = _build(monkeypatch, _operator_ctx)

    content = _search(app, {"query": "boot a built kernel"})

    matches = content["data"]["matches"]
    assert matches, "expected at least one match"
    for match in matches:
        assert set(match) == {"name", "summary", "annotations", "maturity"}, (
            f"unexpected default match keys for {match.get('name')}: {sorted(match)}"
        )


def test_summary_is_the_first_paragraph_on_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """``summary`` is the description's first paragraph, not a byte-truncated prefix.

    ``tools.invoke`` has a five-paragraph description whose first paragraph is one short
    sentence, so a summary equal to that sentence can only come from paragraph splitting.
    """
    app = _build(monkeypatch, _operator_ctx)

    summary_match = next(
        m
        for m in _search(app, {"query": "tools.invoke"})["data"]["matches"]
        if m["name"] == "tools.invoke"
    )
    full_match = next(
        m
        for m in _search(app, {"query": "tools.invoke", "detail": "full"})["data"]["matches"]
        if m["name"] == "tools.invoke"
    )

    description = full_match["description"]
    assert summary_match["summary"] == "Call any registered tool by name (gateway dispatch)."
    assert "\n" not in summary_match["summary"]
    assert description.startswith(summary_match["summary"])
    assert len(description) > 5 * len(summary_match["summary"]), (
        "the fixture tool no longer has a long multi-paragraph description"
    )


def test_summary_match_still_carries_safety_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """annotations + maturity ride a summary match, so a found tool is always classifiable."""
    app = _build(monkeypatch, _operator_ctx)

    content = _search(app, {"query": "power reset"})

    control_power = next(m for m in content["data"]["matches"] if m["name"] == "control.power")
    assert control_power["annotations"] == {"readOnlyHint": False, "destructiveHint": False}
    assert control_power["maturity"] == "implemented"


def test_detail_full_adds_schema_and_complete_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detail='full' is additive: the summary keys survive and two more appear."""
    app = _build(monkeypatch, _operator_ctx)

    content = _search(app, {"query": "boot a built kernel", "detail": "full"})

    matches = content["data"]["matches"]
    assert matches, "expected at least one match"
    for match in matches:
        assert set(match) == {
            "name",
            "summary",
            "annotations",
            "maturity",
            "description",
            "input_schema",
        }, f"unexpected full match keys for {match.get('name')}: {sorted(match)}"


def test_default_query_is_far_cheaper_than_full_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default envelope for the issue's own query is a fraction of the full one.

    This is the defect #1597 reports, so the assertion is on serialized bytes rather than
    on the presence of a key: a future change that re-inlines schemas under another name
    has to fail here.
    """
    app = _build(monkeypatch, _operator_ctx)

    query = {"query": "boot a built kernel", "limit": 10}
    default_bytes = len(json.dumps(_search(app, query)))
    full_bytes = len(json.dumps(_search(app, {**query, "detail": "full"})))

    assert full_bytes > 20_000, f"fixture drift: full envelope is only {full_bytes} bytes"
    assert default_bytes * 4 < full_bytes, (
        f"default envelope {default_bytes}B is not materially cheaper than full {full_bytes}B"
    )


@pytest.mark.parametrize("name", ["runs.get", "jobs.wait", "runs.list"])
def test_exact_tool_name_query_ranks_that_tool_first(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """query=<exact name> + limit=1 fetches that tool's schema, not a same-score neighbour.

    All three names lose the plain (score, name) tiebreak: every hit scores 1, so without the
    exact-name rule ``limit=1`` returns ``artifacts.create_run_upload`` for ``runs.get``,
    ``control.capture_traffic`` for ``jobs.wait``, and ``runs.create`` for ``runs.list``.
    """
    app = _build(monkeypatch, _every_scope_ctx)

    content = _search(app, {"query": name, "detail": "full", "limit": 1})

    matches = content["data"]["matches"]
    assert [m["name"] for m in matches] == [name], (
        f"limit=1 fetch for {name!r} returned {[m['name'] for m in matches]}"
    )
    assert matches[0]["input_schema"]["type"] == "object"


def test_exact_name_first_does_not_drop_the_other_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ranking the exact name first reorders the hit list; it does not filter it."""
    app = _build(monkeypatch, _every_scope_ctx)

    content = _search(app, {"query": "runs.get", "limit": 50})

    names = _match_names(content)
    assert names[0] == "runs.get"
    assert len(names) > 1, f"expected sibling hits alongside the exact match: {names}"
    assert "artifacts.create_run_upload" in names, (
        "the tool that outranks runs.get without the exact-name rule must still be returned"
    )


# ---------------------------------------------------------------------------
# Namespace status: unauthorized is distinguishable from unknown (ADR-0472, #1597)
# ---------------------------------------------------------------------------


def test_authorized_namespace_reports_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build(monkeypatch, _operator_ctx)

    content = _search(app, {"namespace": "debug", "limit": 50})

    assert content["data"]["namespace_status"] == "ok"
    assert "namespace_required_grants" not in content["data"]


def test_unauthorized_namespace_names_the_grant_without_naming_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live-but-entirely-filtered plane says so and names the grants, never the tools."""
    app = _build(monkeypatch, _viewer_ctx)

    content = _search(app, {"namespace": "ops", "limit": 50})

    data = content["data"]
    assert data["matches"] == []
    assert data["namespace_status"] == "unauthorized"
    assert data["namespace_required_grants"] == [
        "platform_admin",
        "platform_auditor",
        "platform_operator",
    ]
    serialized = json.dumps(data)
    for leaked in ("ops.diagnostics", "ops.force_teardown", "ops.tool_trail"):
        assert leaked not in serialized, f"{leaked} leaked into an unauthorized-namespace response"


def test_unknown_namespace_is_not_reported_as_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build(monkeypatch, _viewer_ctx)

    content = _search(app, {"namespace": "no_such_plane", "limit": 50})

    assert content["data"]["matches"] == []
    assert content["data"]["namespace_status"] == "unknown"
    assert "namespace_required_grants" not in content["data"]


def test_query_mode_carries_no_namespace_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """The namespace keys describe a namespace argument, so query mode must not carry them."""
    app = _build(monkeypatch, _operator_ctx)

    data = _search(app, {"query": "boot a built kernel"})["data"]

    assert "namespace_status" not in data
    assert "namespace_required_grants" not in data


@pytest.mark.parametrize(
    ("namespace", "status"),
    [("ops", "unauthorized"), ("no_such_plane", "unknown")],
)
def test_namespace_miss_is_logged_for_curation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, namespace: str, status: str
) -> None:
    """A namespace miss reaches the log the query miss already reached (#1597)."""
    app = _build(monkeypatch, _viewer_ctx)

    with caplog.at_level(logging.INFO, logger="kdive.mcp.tools.gateway"):
        _search(app, {"namespace": namespace, "limit": 50})

    record = next(
        (r for r in caplog.records if r.getMessage() == "tool_search_namespace_miss"), None
    )
    assert record is not None, f"no namespace-miss log for {namespace!r}: {caplog.text}"
    assert record.__dict__["namespace"] == namespace
    assert record.__dict__["namespace_status"] == status


def test_authorized_namespace_logs_no_miss(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = _build(monkeypatch, _operator_ctx)

    with caplog.at_level(logging.INFO, logger="kdive.mcp.tools.gateway"):
        _search(app, {"namespace": "debug", "limit": 50})

    assert "tool_search_namespace_miss" not in caplog.text
