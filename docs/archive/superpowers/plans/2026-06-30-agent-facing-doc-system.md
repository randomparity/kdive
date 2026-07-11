# Agent-facing workflow doc system — Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an agent-facing workflow doc system over MCP — an investigation index doc plus per-toolset purpose docs — with provider-static and role-based gating, so an agent can discover the typical session and each toolset's purpose without incremental probing.

**Architecture:** Extend the existing ADR-0151 doc-resource allowlist (`mcp/resources/registrar.py`) with two gating fields, add a `DocExposureMiddleware` that filters `resources/list` and `resources/read` by platform role, author an investigation index + four seed toolset docs as canonical `docs/` files snapshot-packaged by the existing generator, add a completeness drift-guard, point server `instructions` and `docs/README.md` at the served set, and fix the `build_boot_debug` prompt to name the `runs.build_install_boot` composite.

**Tech Stack:** Python 3.14, `uv`, `pytest`, FastMCP middleware (`on_list_resources`/`on_read_resource`), `ty`, `ruff`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-30-issue-940-agent-facing-doc-system-design.md`. ADR: `docs/adr/0284-agent-facing-workflow-docs.md`.
- Run guardrails before every commit: `just lint`, `just type` (whole tree), and the focused tests for the task. Run `just resources-docs-check` after any doc-resource change.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only (no relative `..`). Google-style docstrings on non-trivial public APIs.
- `ty` runs whole-tree (src + tests) with strict defaults; fix every diagnostic.
- Doc prose: no ADR numbers in agent-facing doc *bodies* served over MCP (ADR-0270 — the served toolset/index docs must not cite `ADR-NNNN`). Use plain factual prose; avoid "critical", "crucial", "essential", "significant", "comprehensive", "robust", "elegant"; use "Milestone" not "Sprint".
- Commit messages: Conventional Commits, imperative subject ≤72 chars, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Phase 1 only. Phases 2 (remaining investigation toolset docs) and 3 (operator workflow + operator toolset docs) are follow-up issues; this plan builds the full gating framework (including the operator role gate, tested with fixtures) but ships no `audience="operator"` doc.

## File Structure

**Create (source):**
- `src/kdive/mcp/middleware/doc_exposure.py` — `DocExposureMiddleware`: role-gates `resources/list` and `resources/read`.

**Create (canonical docs, snapshot-packaged):**
- `docs/guide/agent-index.md` — investigation index (workflow map + toolset catalog).
- `docs/guide/toolsets/runs.md`, `docs/guide/toolsets/artifacts.md`, `docs/guide/toolsets/debug.md`, `docs/guide/toolsets/systems.md` — per-toolset purpose docs.
- `src/kdive/mcp/resources/_content/agent-index.md`, `src/kdive/mcp/resources/_content/toolsets-runs.md`, `…-artifacts.md`, `…-debug.md`, `…-systems.md` — generated snapshots (written by `just resources-docs`; never hand-edited).

**Create (tests):**
- `tests/mcp/resources/test_doc_exposure.py` — middleware list+read gating.
- `tests/mcp/resources/test_toolset_doc_completeness.py` — drift guard.

**Modify:**
- `src/kdive/mcp/resources/registrar.py` — add `required_kind`, `audience`; thread `resolver`; provider-skip; export `audience_by_uri()`.
- `src/kdive/mcp/tool_registration.py:225-228` (`_register_doc_resources`) — pass `assembly.resolver`.
- `src/kdive/mcp/app.py:47-49` — add `DocExposureMiddleware`.
- `src/kdive/mcp/tool_index.py` (`build_instructions`) — add the index-doc pointer line.
- `src/kdive/mcp/prompts/registrar.py` (`build_boot_debug` spec) — name `runs.build_install_boot`.
- `docs/README.md` — list the MCP-served docs under the agent tier.
- `tests/mcp/resources/test_doc_resources.py` — provider-skip + audience-map coverage.
- `tests/mcp/prompts/test_lifecycle_prompts.py` — assert the composite is named.
- `tests/mcp/test_tool_index.py` — assert instructions name the index URI.

---

### Task 1: Extend `DocResource` with gating fields and an audience map

**Files:**
- Modify: `src/kdive/mcp/resources/registrar.py`
- Test: `tests/mcp/resources/test_doc_resources.py`

**Interfaces:**
- Produces: `DocResource` gains `required_kind: ResourceKind | None = None` and `audience: Literal["all", "operator"] = "all"`. New module function `audience_by_uri() -> dict[str, str]` mapping each entry's `uri` to its `audience`.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/resources/test_doc_resources.py (add)
from kdive.mcp.resources.registrar import DOC_RESOURCES, audience_by_uri


def test_doc_resources_default_to_all_audience_and_no_kind() -> None:
    for entry in DOC_RESOURCES:
        assert entry.audience in {"all", "operator"}
        # The three pre-existing docs are ungated.
        if entry.name in {"external-build-upload", "build-source-staging", "response-envelope"}:
            assert entry.audience == "all"
            assert entry.required_kind is None


def test_audience_by_uri_covers_every_entry() -> None:
    mapping = audience_by_uri()
    assert set(mapping) == {entry.uri for entry in DOC_RESOURCES}
    assert all(v in {"all", "operator"} for v in mapping.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/resources/test_doc_resources.py::test_audience_by_uri_covers_every_entry -q`
Expected: FAIL with `ImportError: cannot import name 'audience_by_uri'`.

- [ ] **Step 3: Write minimal implementation**

```python
# registrar.py — extend imports
from typing import Literal

from kdive.domain.catalog.resources import ResourceKind

# extend the dataclass (add fields after mime_type)
@dataclass(frozen=True, slots=True)
class DocResource:
    uri: str
    source: str
    content_file: str
    name: str
    title: str
    description: str
    mime_type: str = _MARKDOWN
    required_kind: ResourceKind | None = None
    audience: Literal["all", "operator"] = "all"


def audience_by_uri() -> dict[str, str]:
    """Return each allowlisted doc's URI mapped to its audience marker.

    The middleware consults this so a doc's audience has a single source.
    """
    return {entry.uri: entry.audience for entry in DOC_RESOURCES}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/resources/test_doc_resources.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/resources/registrar.py tests/mcp/resources/test_doc_resources.py
git commit -m "feat(940): add gating fields and audience map to DocResource"
```

---

### Task 2: Provider-gate doc registration on `registered_kinds()`

**Files:**
- Modify: `src/kdive/mcp/resources/registrar.py` (`register`)
- Modify: `src/kdive/mcp/tool_registration.py` (`_register_doc_resources`)
- Test: `tests/mcp/resources/test_doc_resources.py`

**Interfaces:**
- Consumes: `ProviderResolver.registered_kinds() -> frozenset[ResourceKind]` (from `kdive.providers.core.resolver`).
- Produces: `register(app, *, resolver)` — keyword-only `resolver`; entries whose `required_kind` is not in `resolver.registered_kinds()` are skipped. Return value stays "number registered".

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/resources/test_doc_resources.py (add)
from dataclasses import replace
from fastmcp import FastMCP
from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.resources import registrar


class _FakeResolver:
    def __init__(self, kinds: frozenset[ResourceKind]) -> None:
        self._kinds = kinds

    def registered_kinds(self) -> frozenset[ResourceKind]:
        return self._kinds


def test_register_skips_doc_whose_required_kind_is_absent(monkeypatch) -> None:
    gated = replace(
        DOC_RESOURCES[0],
        uri="resource://kdive/docs/test/remote-only.md",
        name="remote-only",
        required_kind=ResourceKind.REMOTE_LIBVIRT,
    )
    monkeypatch.setattr(registrar, "DOC_RESOURCES", (*DOC_RESOURCES, gated))
    app = FastMCP("probe")
    count = registrar.register(app, resolver=_FakeResolver(frozenset({ResourceKind.LOCAL_LIBVIRT})))
    assert count == len(DOC_RESOURCES)  # gated entry skipped


def test_register_includes_doc_when_required_kind_present(monkeypatch) -> None:
    gated = replace(
        DOC_RESOURCES[0],
        uri="resource://kdive/docs/test/remote-only.md",
        name="remote-only",
        required_kind=ResourceKind.REMOTE_LIBVIRT,
    )
    monkeypatch.setattr(registrar, "DOC_RESOURCES", (*DOC_RESOURCES, gated))
    app = FastMCP("probe")
    count = registrar.register(
        app, resolver=_FakeResolver(frozenset({ResourceKind.REMOTE_LIBVIRT}))
    )
    assert count == len(DOC_RESOURCES) + 1
```

Confirm `ResourceKind.REMOTE_LIBVIRT` / `LOCAL_LIBVIRT` member names first:
Run: `uv run python -c "from kdive.domain.catalog.resources import ResourceKind; print(list(ResourceKind))"` and adjust the test to the real member names if they differ.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/resources/test_doc_resources.py::test_register_skips_doc_whose_required_kind_is_absent -q`
Expected: FAIL — `register()` got an unexpected keyword `resolver` (current signature is `register(app)`).

- [ ] **Step 3: Write minimal implementation**

```python
# registrar.py — register()
from kdive.providers.core.resolver import ProviderResolver

def register(app: FastMCP, *, resolver: ProviderResolver) -> int:
    """Register every allowlisted doc whose provider gate is satisfied.

    Skips an entry whose ``required_kind`` is not in ``resolver.registered_kinds()``
    so a provider-specific doc is absent on a deployment that did not register that
    provider (it can be neither listed nor read).

    Raises:
        RuntimeError: If a registered entry's packaged snapshot is absent.
    """
    kinds = resolver.registered_kinds()
    registered = 0
    for entry in DOC_RESOURCES:
        if entry.required_kind is not None and entry.required_kind not in kinds:
            continue
        content_path = _CONTENT_DIR / entry.content_file
        if not content_path.is_file():
            raise RuntimeError(
                f"packaged doc-resource snapshot missing: {content_path} "
                f"(for {entry.uri}); run 'just resources-docs'"
            )
        text = content_path.read_text(encoding="utf-8")
        app.add_resource(
            TextResource(
                uri=AnyUrl(entry.uri),
                name=entry.name,
                title=entry.title,
                description=entry.description,
                mime_type=entry.mime_type,
                text=text,
            )
        )
        registered += 1
    return registered
```

```python
# tool_registration.py — _register_doc_resources
def _register_doc_resources(
    app: FastMCP, _pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    doc_resources.register(app, resolver=assembly.resolver)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/resources/test_doc_resources.py -q`
Expected: PASS. Also `uv run python -m pytest tests/mcp/core/test_app.py -q` to confirm app assembly still builds.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/resources/registrar.py src/kdive/mcp/tool_registration.py tests/mcp/resources/test_doc_resources.py
git commit -m "feat(940): provider-gate doc resources on registered_kinds"
```

---

### Task 3: `DocExposureMiddleware` — role-gate list and read

**Files:**
- Create: `src/kdive/mcp/middleware/doc_exposure.py`
- Modify: `src/kdive/mcp/app.py`
- Test: `tests/mcp/resources/test_doc_exposure.py`

**Interfaces:**
- Consumes: `audience_by_uri()` (Task 1); `request_context()` from `kdive.mcp.middleware.shared`; `RequestContext.platform_roles` (a set of `PlatformRole`); `AuthError`/`AuthorizationError` from `kdive.security.authz.errors`.
- Produces: `DocExposureMiddleware(Middleware)` with `on_list_resources` and `on_read_resource`. `_caller_has_platform_role(ctx) -> bool` returns `bool(ctx.platform_roles)`.

- [ ] **Step 0: Confirm hook payload shapes**

Run: `uv run python -c "import inspect; from fastmcp.server.middleware import Middleware; print(inspect.getsource(Middleware.on_read_resource)); print(inspect.getsource(Middleware.on_list_resources))"`
Note how the resource URI is reached on the read context (e.g. `context.message.uri`) and what `on_list_resources` returns (a sequence of resource objects each exposing `.uri`). Use the real attribute names below.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/resources/test_doc_exposure.py
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kdive.mcp.middleware import doc_exposure
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import AuthorizationError

_OPERATOR_URI = "resource://kdive/docs/guide/agent-index-operator.md"
_ALL_URI = "resource://kdive/docs/guide/agent-index.md"


def _resources():
    return [SimpleNamespace(uri=_ALL_URI), SimpleNamespace(uri=_OPERATOR_URI)]


def _audience_map():
    return {_ALL_URI: "all", _OPERATOR_URI: "operator"}


class _Ctx:
    def __init__(self, platform_roles) -> None:
        self.platform_roles = frozenset(platform_roles)


def _mw(monkeypatch, ctx_or_exc):
    mw = doc_exposure.DocExposureMiddleware()
    monkeypatch.setattr(doc_exposure, "audience_by_uri", _audience_map)

    def _ctx():
        if isinstance(ctx_or_exc, Exception):
            raise ctx_or_exc
        return ctx_or_exc

    monkeypatch.setattr(doc_exposure, "request_context", _ctx)
    return mw


def test_list_hides_operator_doc_from_project_only_token(monkeypatch) -> None:
    mw = _mw(monkeypatch, _Ctx(platform_roles=set()))

    async def _call_next(_c):
        return _resources()

    out = asyncio.run(mw.on_list_resources(SimpleNamespace(), _call_next))
    assert {r.uri for r in out} == {_ALL_URI}


def test_list_shows_operator_doc_to_platform_principal(monkeypatch) -> None:
    mw = _mw(monkeypatch, _Ctx(platform_roles={"platform_auditor"}))

    async def _call_next(_c):
        return _resources()

    out = asyncio.run(mw.on_list_resources(SimpleNamespace(), _call_next))
    assert {r.uri for r in out} == {_ALL_URI, _OPERATOR_URI}


def test_list_fails_closed_on_auth_error(monkeypatch) -> None:
    mw = _mw(monkeypatch, AuthError("no token"))

    async def _call_next(_c):
        return _resources()

    out = asyncio.run(mw.on_list_resources(SimpleNamespace(), _call_next))
    assert {r.uri for r in out} == {_ALL_URI}


def test_read_rejects_operator_doc_for_project_only_token(monkeypatch) -> None:
    mw = _mw(monkeypatch, _Ctx(platform_roles=set()))
    msg = SimpleNamespace(uri=_OPERATOR_URI)

    async def _call_next(_c):
        return "should-not-reach"

    with pytest.raises(AuthorizationError):
        asyncio.run(mw.on_read_resource(SimpleNamespace(message=msg), _call_next))


def test_read_allows_all_audience_doc_for_anyone(monkeypatch) -> None:
    mw = _mw(monkeypatch, AuthError("no token"))
    msg = SimpleNamespace(uri=_ALL_URI)

    async def _call_next(_c):
        return "ok"

    assert asyncio.run(mw.on_read_resource(SimpleNamespace(message=msg), _call_next)) == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/resources/test_doc_exposure.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.mcp.middleware.doc_exposure`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/mcp/middleware/doc_exposure.py
"""Per-connection doc-resource exposure middleware (#940).

Role-gates the doc-resource surface so an ``audience="operator"`` doc is neither
listed nor readable by a caller that holds no platform role. The audience of each
doc has a single source (``audience_by_uri``); the predicate keys on the platform-role
axis (``ctx.platform_roles`` non-empty), not the project-scoped ``Role.OPERATOR``.
Both paths are fail-closed for the gated subset: an auth error hides operator docs
from the listing and rejects an operator-doc read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.server.middleware import Middleware

from kdive.mcp.resources.registrar import audience_by_uri
from kdive.mcp.middleware.shared import request_context
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import AuthorizationError

_log = logging.getLogger(__name__)


def _caller_has_platform_role(ctx: Any) -> bool:
    """Return True when the caller holds any platform role."""
    return bool(getattr(ctx, "platform_roles", frozenset()))


class DocExposureMiddleware(Middleware):
    """Filter the doc-resource list and read by the caller's platform role."""

    async def on_list_resources(
        self, context: Any, call_next: Callable[[Any], Any]
    ) -> Sequence[Any]:
        resources = await call_next(context)
        audience = audience_by_uri()
        try:
            elevated = _caller_has_platform_role(request_context())
        except AuthError:
            elevated = False
        except Exception:
            _log.warning("doc-exposure list filter failed; hiding operator docs", exc_info=True)
            elevated = False
        if elevated:
            return resources
        return [r for r in resources if audience.get(str(r.uri), "all") != "operator"]

    async def on_read_resource(
        self, context: Any, call_next: Callable[[Any], Any]
    ) -> Any:
        uri = str(context.message.uri)
        if audience_by_uri().get(uri, "all") == "operator":
            try:
                elevated = _caller_has_platform_role(request_context())
            except Exception:
                elevated = False
            if not elevated:
                raise AuthorizationError(f"{uri} requires a platform role")
        return await call_next(context)
```

Adjust `context.message.uri` / `r.uri` to the real shapes found in Step 0.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/mcp/resources/test_doc_exposure.py -q`
Expected: PASS.

- [ ] **Step 5: Wire into the app**

```python
# app.py — after the existing add_middleware calls (near line 49)
from kdive.mcp.middleware.doc_exposure import DocExposureMiddleware
...
    app.add_middleware(DocExposureMiddleware())
```

- [ ] **Step 6: Add an end-to-end denial-shape check through the built app**

The unit tests assert the middleware raises; this confirms a raised `AuthorizationError` from
`on_read_resource` surfaces as a clean MCP error (not an opaque 500) through the real app. Add
to `tests/mcp/resources/test_doc_exposure.py`. Register a temporary `audience="operator"`
fixture doc (via `monkeypatch` on `registrar.DOC_RESOURCES`, mirroring Task 2's fixture, with a
real packaged snapshot path that exists — reuse an existing `_content` file), build the app with
the no-DB pattern (`AsyncConnectionPool("postgresql://unused", open=False)`, `_verifier()` from
`tests/mcp/test_tool_index.py`), and assert that reading the operator URI with no platform-role
token produces an error result whose category/shape matches a denial rather than an internal
error. First confirm how FastMCP renders a middleware-raised exception:

Run: `uv run python -c "import inspect, fastmcp.server.middleware as m; print(inspect.getsource(m))" | head -120`
and pick the assertion (error result vs. raised `McpError`) that matches the installed version.
If FastMCP wraps the raise into an error *result*, assert on that result; if it re-raises,
assert `pytest.raises`. Keep the denial observable either way.

- [ ] **Step 7: Guardrails + commit**

```bash
just lint && just type && uv run python -m pytest tests/mcp/resources/ tests/mcp/core/test_app.py -q
git add src/kdive/mcp/middleware/doc_exposure.py src/kdive/mcp/app.py tests/mcp/resources/test_doc_exposure.py
git commit -m "feat(940): role-gate doc resources via DocExposureMiddleware"
```

---

### Task 4: Name the composite in the `build_boot_debug` prompt

**Files:**
- Modify: `src/kdive/mcp/prompts/registrar.py` (the `build_boot_debug` `PromptSpec.summary`)
- Test: `tests/mcp/prompts/test_lifecycle_prompts.py`

**Interfaces:**
- Consumes: existing `CANONICAL_PROMPTS`. No signature change.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/prompts/test_lifecycle_prompts.py (add)
def test_build_boot_debug_names_the_composite() -> None:
    spec = next(s for s in CANONICAL_PROMPTS if s.name == "build_boot_debug")
    assert "runs.build_install_boot" in spec.summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/prompts/test_lifecycle_prompts.py::test_build_boot_debug_names_the_composite -q`
Expected: FAIL — `runs.build_install_boot` not in the summary.

- [ ] **Step 3: Write minimal implementation**

Edit the `build_boot_debug` `summary` string (currently ends "... A warm-tree server build (runs.build, after runs.create) is the secondary single-host path."). Replace that sentence with:

```python
            "The single-host server-build lane (runs.build, after runs.create) is the "
            "secondary single-host path; prefer runs.build_install_boot to run that lane "
            "as one pollable job when you choose it. "
```

The replacement keeps the **contiguous** substring "secondary single-host" because the
existing `test_build_boot_debug_leads_with_external_upload_loop` asserts
`"secondary single-host" in lowered` (tests/mcp/prompts/test_lifecycle_prompts.py:131). It
also keeps "external upload is the default" earlier in the summary (do not edit that
sentence). After editing, run that existing test too and confirm it still passes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/prompts/test_lifecycle_prompts.py -q`
Expected: PASS (both the new test and `test_build_boot_debug_leads_with_external_upload_loop`).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/prompts/registrar.py tests/mcp/prompts/test_lifecycle_prompts.py
git commit -m "fix(940): name runs.build_install_boot in build_boot_debug prompt"
```

---

### Task 5: Completeness drift-guard for served toolset docs

**Files:**
- Create: `tests/mcp/resources/test_toolset_doc_completeness.py`

**Interfaces:**
- Consumes: the live registered tool names from a built app. There is no shared helper today; build the app inline with the **same pattern** `tests/mcp/test_tool_index.py:_built_app()` uses (`AsyncConnectionPool("postgresql://unused", open=False)` + `build_app(...)` + `app.list_tools()`), which does not touch the database. The guard targets only `DOC_RESOURCES` entries whose `source` matches `toolsets/<ns>.md`.

- [ ] **Step 1: Write the guard (it enforces exact-set equality for every registered toolset doc; with none registered yet, the loop body does not run and it passes)**

```python
# tests/mcp/resources/test_toolset_doc_completeness.py
"""Every served toolset doc must name exactly the live tools in its namespace (#940)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.resources.registrar import DOC_RESOURCES
from kdive.security.secrets.secret_registry import SecretRegistry

# Build the app with the no-DB pattern from tests/mcp/test_tool_index.py. If that file
# already exposes a reusable verifier/app builder, import and reuse it instead of copying.
from tests.mcp.test_tool_index import _verifier  # reuse the existing JWT verifier helper

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLSET_RE = re.compile(r"toolsets/(?P<ns>[a-z_]+)\.md$")


def _live_tool_names() -> set[str]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app: FastMCP = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _run() -> set[str]:
        return {t.name for t in await app.list_tools()}

    return asyncio.run(_run())


def _served_toolset_docs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for entry in DOC_RESOURCES:
        m = _TOOLSET_RE.search(entry.source)
        if m:
            out.append((m.group("ns"), _REPO_ROOT / entry.source))
    return out


def test_each_served_toolset_doc_names_exactly_its_namespace_tools() -> None:
    live = _live_tool_names()  # set[str] of "ns.tool"
    for namespace, path in _served_toolset_docs():
        body = path.read_text(encoding="utf-8")
        named = set(re.findall(rf"\b{namespace}\.[a-z_]+", body))
        expected = {t for t in live if t.startswith(f"{namespace}.")}
        missing = expected - named
        stale = named - expected
        assert not missing, f"{path.name} omits live tools: {sorted(missing)}"
        assert not stale, f"{path.name} names non-live tools: {sorted(stale)}"
```

`_verifier` is the private helper already in `test_tool_index.py`; if importing a private
name across test modules is undesirable in this repo, copy the three-line verifier inline
instead. Confirm `tests/mcp/__init__.py` exists (it does, the suite already cross-imports);
if not, build the verifier inline rather than importing.

- [ ] **Step 2: Run guard to verify it passes (loop body does not execute — no toolset docs registered yet)**

Run: `uv run python -m pytest tests/mcp/resources/test_toolset_doc_completeness.py -q`
Expected: PASS. The app builds (no DB access from `list_tools`), `_served_toolset_docs()` is empty, so the assertion loop is a no-op.

- [ ] **Step 3: Commit**

```bash
just lint && just type
git add tests/mcp/resources/test_toolset_doc_completeness.py
git commit -m "test(940): add toolset-doc completeness drift guard"
```

---

### Task 6: Author + register the investigation index and four seed toolset docs

This task repeats the same cycle per doc. Do the index first, then each toolset doc. Each toolset doc must name **every** live tool in its namespace (the Task 5 guard enforces exact-set equality). The namespaces and their live tools:

- `runs`: bind, boot, build, build_install_boot, cancel, complete_build, create, get, install, list, profile_examples, validate_profile
- `artifacts`: create_run_upload, create_system_upload, expected_uploads, fetch_raw, get, list
- `debug`: backtrace, clear_breakpoint, clear_watchpoint, continue, disassemble, end_session, get_session, interrupt, list_breakpoints, list_modules, list_sessions, list_watchpoints, load_module_symbols, read_frame, read_memory, read_registers, resolve_symbol, set_breakpoint, set_watchpoint, start_session
- `systems`: authorize_ssh_key, define, get, list, profile_examples, provision, provision_defined, reprovision, ssh_info, teardown

Before authoring, reconfirm each namespace's live set:
Run: `uv run python -m pytest tests/mcp/resources/test_toolset_doc_completeness.py -q` after registering each doc; the failure message lists any missing/stale tool so the doc can be corrected against the live registry (the source of truth, not this list).

- [ ] **Step 1: Author the investigation index** — `docs/guide/agent-index.md`

Skeleton (fill the prose; do not cite ADR numbers in the body):

```markdown
# Driving a kdive investigation

A typical session moves through these stages. Each names the toolset to use and the
first tool to call.

1. Orient — `investigations.open` to group related runs.
2. Acquire capacity — `allocations.request`, then `allocations.wait`.
3. Define / provision a system — `systems.define`, then `systems.provision`.
4. Build — upload a prebuilt kernel (`runs.create` with the external lane) or build on a
   host; see the runs toolset.
5. Install / boot — `runs.install`, `runs.boot` (or one pollable job, see runs).
6. Observe evidence — `runs.get`, `artifacts.list`, `artifacts.get`.
7. Debug / introspect — `debug.start_session`, `introspect.run`.
8. Triage a crash — `vmcore.fetch`, `postmortem.triage`.
9. Release — `allocations.release`.

## Toolsets

| Toolset | What it is for | Guide |
|---|---|---|
| runs | Build / install / boot lifecycle of a kernel test run | resource://kdive/docs/guide/toolsets/runs.md |
| artifacts | Fetch run evidence (logs, console, vmlinux) and upload builds | resource://kdive/docs/guide/toolsets/artifacts.md |
| debug | Live GDB kernel debugging — breakpoints, memory, stacks | resource://kdive/docs/guide/toolsets/debug.md |
| systems | Provision, reprovision, and SSH into the target system | resource://kdive/docs/guide/toolsets/systems.md |

For the response shape every tool returns, read
resource://kdive/docs/guide/response-envelope.md. Clients that list MCP prompts also have
the `start_investigation`, `build_boot_debug`, and `triage_panic` prompts.
```

The index must name **no** operator doc or operator URI.

- [ ] **Step 2: Author each toolset doc** — `docs/guide/toolsets/<ns>.md`

Per-doc structure (worked example for `runs`; mirror for `artifacts`, `debug`, `systems`):

```markdown
# runs toolset

A run is one build → install → boot lifecycle of a kernel on a defined system. Reach
for these after you have an allocated, defined system (see the systems toolset) and
before debugging (see the debug toolset).

- `runs.create` — open a run bound to an investigation and a build profile; the external
  upload lane is the default build path.
- `runs.build_install_boot` — run the single-host server-build lane as one pollable job;
  prefer it over calling build, install, and boot separately when you build on a host.
- `runs.build` — enqueue a warm-tree server build for a run (the step the composite folds in).
- `runs.complete_build` — finalize an externally uploaded build.
- `runs.install` — install the built kernel and modules onto the system.
- `runs.boot` — boot the system into the built kernel.
- `runs.bind` — bind a run to a system.
- `runs.cancel` — cancel an in-flight run.
- `runs.get` — read a run's status, build provenance, and console access.
- `runs.list` — list runs with filters and pagination.
- `runs.validate_profile` — check a build profile without creating a run.
- `runs.profile_examples` — fetch ready-made build-profile templates.

For exact parameters, types, and return schema, read each tool's own description.
```

Every live tool in the namespace must appear as a `` `ns.tool` `` token. Keep prose plain; no ADR numbers.

- [ ] **Step 3: Register the five docs** in `DOC_RESOURCES` (`registrar.py`), each `audience="all"`, `required_kind=None`:

```python
    DocResource(
        uri="resource://kdive/docs/guide/agent-index.md",
        source="docs/guide/agent-index.md",
        content_file="agent-index.md",
        name="agent-index",
        title="Driving a kdive investigation",
        description="The typical investigation session mapped to toolsets, with a per-toolset guide link.",
    ),
    DocResource(
        uri="resource://kdive/docs/guide/toolsets/runs.md",
        source="docs/guide/toolsets/runs.md",
        content_file="toolsets-runs.md",
        name="toolset-runs",
        title="runs toolset",
        description="How each runs.* tool helps an investigation (build / install / boot lifecycle).",
    ),
    # …artifacts, debug, systems — same shape, content_file="toolsets-<ns>.md", name="toolset-<ns>".
```

- [ ] **Step 4: Generate snapshots**

Run: `just resources-docs`
Then: `git status --porcelain src/kdive/mcp/resources/_content/` — expect the five new snapshot files.

- [ ] **Step 5: Run the guard + resources tests**

Run: `uv run python -m pytest tests/mcp/resources/ -q`
Expected: PASS — completeness guard now enforces the four namespaces. If it reports missing/stale tools, fix the doc and re-run `just resources-docs`.
Run: `just resources-docs-check` → expect "snapshots in sync".

- [ ] **Step 6: Guardrails + commit (one commit per doc is fine; group is acceptable here since the guard ties them together)**

```bash
just lint && just type && uv run python -m pytest tests/mcp/resources/ -q
git add docs/guide/agent-index.md docs/guide/toolsets/ src/kdive/mcp/resources/registrar.py src/kdive/mcp/resources/_content/
git commit -m "feat(940): publish investigation index and seed toolset docs"
```

---

### Task 7: Point server instructions and `docs/README.md` at the served set

**Files:**
- Modify: `src/kdive/mcp/tool_index.py` (`build_instructions`)
- Modify: `docs/README.md`
- Test: `tests/mcp/test_tool_index.py`

**Interfaces:**
- Consumes: nothing new. `build_instructions()` return string gains a line referencing the index URI.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/test_tool_index.py (add)
from kdive.mcp.tool_index import build_instructions


def test_instructions_point_at_the_agent_index() -> None:
    text = build_instructions()
    assert "resource://kdive/docs/guide/agent-index.md" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_tool_index.py::test_instructions_point_at_the_agent_index -q`
Expected: FAIL.

- [ ] **Step 3: Implement** — append one line to the `build_instructions()` return, before or after the namespace TOC block:

```python
    index_line = (
        "For a workflow-shaped map of the typical session and a per-toolset guide, read "
        "the doc resource resource://kdive/docs/guide/agent-index.md.\n"
    )
```
Include `index_line` in the returned string (keep the existing gateway paragraph and TOC).

- [ ] **Step 4: Update `docs/README.md`** — under "Use KDIVE — agents and users", add a row group:

```markdown
| [Agent workflow index](guide/agent-index.md) | Served over MCP; maps the session to toolsets |
| [Toolset guides](guide/toolsets/) | Served over MCP; per-tool purpose for each toolset |
```

- [ ] **Step 5: Run tests + doc guards**

Run: `uv run python -m pytest tests/mcp/test_tool_index.py -q && just docs-links && just docs-paths`
Expected: PASS / links + paths resolve.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tool_index.py docs/README.md tests/mcp/test_tool_index.py
git commit -m "feat(940): point instructions and README at the agent doc index"
```

---

### Task 8: Full-suite verification

- [ ] **Step 1: Run the full local gate**

Run: `just ci`
Expected: all green (lint, type, doc guards incl. `resources-docs-check`, tests). Fix any failure before proceeding. In particular confirm `test_doc_resources.py`, `test_doc_exposure.py`, `test_toolset_doc_completeness.py`, `test_lifecycle_prompts.py`, and `test_tool_index.py` pass together.

- [ ] **Step 2: No commit** unless `just ci` surfaced a fix.

---

## Follow-up (out of scope for this PR)

- **Phase 2:** author the remaining investigation toolset docs (`investigations`, `allocations`, `resources`, `images`, `buildconfig`, `build_envs`, `jobs`, `control`, `introspect`, `vmcore`, `postmortem`) — same pattern; the completeness guard enforces each.
- **Phase 3:** the operator workflow — `agent-index-operator.md` and operator toolset docs with `audience="operator"`; the role gate built in Task 3 already covers them.

Open a tracking issue for each before closing #940, or note them in the PR body.

## Self-Review

- **Spec coverage:** index doc (Task 6), per-toolset purpose docs (Task 6), docstrings-as-SoT (doc hand-off line, Task 6), provider gate (Task 2), role gate list+read on platform-role axis (Task 3), instructions pointer + README (Task 7), drift guard (Task 5), composite signpost in prompt (Task 4) and runs doc (Task 6). Operator workflow is explicitly deferred (Phase 3) with the gate built and fixture-tested now.
- **Placeholder scan:** none — every code/test step shows real content; doc steps give full skeletons plus the live-tool list and the guard as the objective gate.
- **Type consistency:** `register(app, *, resolver)`, `audience_by_uri()`, `DocExposureMiddleware`, `_caller_has_platform_role` are used with the same names/signatures across Tasks 1–3 and 6.
