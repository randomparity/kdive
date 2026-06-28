# Derive Agent-Facing Provider Schemas From Composed Providers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent-facing MCP surface (allocation kind selector, systems provider-section union, `profile_examples`) present and accept only the providers the running deployment composed, derived from `ProviderResolver.registered_kinds()`.

**Architecture:** A first-class `PROVIDER_SECTIONS` registry is the single source. A structural schema-projection helper narrows the FastMCP-generated `$defs` at list-time (`on_list_tools` + `tools.search`); a call-time membership guard rejects non-composed kinds on the shared handler path. Both read the single live `registered_kinds()` set. The domain `ProvisioningProfile`/`ProviderSection` models stay static so `profile_digest` is byte-identical and stored Systems of a disabled kind keep parsing.

**Tech Stack:** Python 3.14, Pydantic v2, FastMCP, pytest, `uv`/`ruff`/`ty`.

**Spec:** `docs/specs/2026-06-28-derive-agent-schemas-from-composed-providers-879.md` · **ADR:** `docs/adr/0269-derive-agent-schemas-from-composed-providers.md`

## Global Constraints

- Python 3.14; absolute imports only (no `..` relative imports).
- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, 100-char lines.
- Google-style docstrings on non-trivial public APIs.
- Guardrails before every commit: `just lint`, `just type`, and the focused `pytest` for the task. Run `just test` (full suite) once before the final push — architecture/doc-generation tests live outside touched dirs.
- Zero warnings. Never weaken or un-gate existing tests.
- Conventional Commits; subject ≤72 chars; every commit ends with the trailer:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- No DB migration, no RBAC change, no change to storage/digest/render/teardown.
- `resources.list` `kind` filter is a **read** surface and stays permissive — never narrow it.

---

### Task 1: `PROVIDER_SECTIONS` registry + completeness guard

**Files:**
- Create: `src/kdive/profiles/provider_sections.py`
- Test: `tests/profiles/test_provider_sections.py`

**Interfaces:**
- Consumes: `ResourceKind` (`kdive.domain.catalog.resources`); `LibvirtProfile`, `FaultInjectProfile`, `RemoteLibvirtProfile` (`kdive.profiles.provisioning`).
- Produces:
  - `ProviderSectionSpec` (frozen dataclass: `kind: ResourceKind`, `alias: str`, `model: type[BaseModel]`, `label: str`).
  - `PROVIDER_SECTIONS: dict[ResourceKind, ProviderSectionSpec]`.
  - `aliases_for(kinds: frozenset[ResourceKind]) -> frozenset[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/profiles/test_provider_sections.py
from __future__ import annotations

from kdive.domain.catalog.resources import ResourceKind
from kdive.profiles.provider_sections import (
    PROVIDER_SECTIONS,
    aliases_for,
)
from kdive.profiles.provisioning import (
    FaultInjectProfile,
    LibvirtProfile,
    RemoteLibvirtProfile,
)


def test_registry_covers_every_resource_kind() -> None:
    assert set(PROVIDER_SECTIONS) == set(ResourceKind)


def test_alias_is_the_resource_kind_value() -> None:
    for kind, spec in PROVIDER_SECTIONS.items():
        assert spec.alias == kind.value


def test_section_models_match_provisioning() -> None:
    assert PROVIDER_SECTIONS[ResourceKind.LOCAL_LIBVIRT].model is LibvirtProfile
    assert PROVIDER_SECTIONS[ResourceKind.REMOTE_LIBVIRT].model is RemoteLibvirtProfile
    assert PROVIDER_SECTIONS[ResourceKind.FAULT_INJECT].model is FaultInjectProfile


def test_aliases_for_filters_to_the_live_set() -> None:
    one = frozenset({ResourceKind.LOCAL_LIBVIRT})
    assert aliases_for(one) == frozenset({"local-libvirt"})
    assert aliases_for(frozenset()) == frozenset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/profiles/test_provider_sections.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.profiles.provider_sections`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/profiles/provider_sections.py
"""The provider-section registry (ADR-0269): the single source mapping each
``ResourceKind`` to its provisioning-profile section model, alias, and label.

The agent-facing schema projection, the call-time guard, and ``profile_examples``
all iterate this registry, so a new provider is covered by one entry here plus its
``ResourceKind`` member, its section model + the static ``ProviderSection`` field, and a
composition opt-in — never by editing each agent surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from kdive.domain.catalog.resources import ResourceKind
from kdive.profiles.provisioning import (
    FaultInjectProfile,
    LibvirtProfile,
    RemoteLibvirtProfile,
)


@dataclass(frozen=True, slots=True)
class ProviderSectionSpec:
    """One provider's agent-facing provisioning metadata."""

    kind: ResourceKind
    alias: str
    model: type[BaseModel]
    label: str


PROVIDER_SECTIONS: dict[ResourceKind, ProviderSectionSpec] = {
    ResourceKind.LOCAL_LIBVIRT: ProviderSectionSpec(
        ResourceKind.LOCAL_LIBVIRT,
        ResourceKind.LOCAL_LIBVIRT.value,
        LibvirtProfile,
        "local-libvirt (direct-kernel)",
    ),
    ResourceKind.REMOTE_LIBVIRT: ProviderSectionSpec(
        ResourceKind.REMOTE_LIBVIRT,
        ResourceKind.REMOTE_LIBVIRT.value,
        RemoteLibvirtProfile,
        "remote-libvirt (disk-image)",
    ),
    ResourceKind.FAULT_INJECT: ProviderSectionSpec(
        ResourceKind.FAULT_INJECT,
        ResourceKind.FAULT_INJECT.value,
        FaultInjectProfile,
        "fault-inject (test/mock fixture)",
    ),
}


def aliases_for(kinds: frozenset[ResourceKind]) -> frozenset[str]:
    """Return the profile-section aliases for the live ``kinds`` (ADR-0269)."""
    return frozenset(PROVIDER_SECTIONS[k].alias for k in kinds if k in PROVIDER_SECTIONS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/profiles/test_provider_sections.py -q` → Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run pytest tests/profiles/test_provider_sections.py -q
git add src/kdive/profiles/provider_sections.py tests/profiles/test_provider_sections.py
git commit -m "feat(profiles): add PROVIDER_SECTIONS registry (ADR-0269)"
```

---

### Task 2: schema-projection helper + call-time guard

**Files:**
- Create: `src/kdive/mcp/provider_schema.py`
- Test: `tests/mcp/test_provider_schema.py`

**Interfaces:**
- Consumes: `PROVIDER_SECTIONS`, `aliases_for` (Task 1); `ResourceKind`; `CategorizedError`, `ErrorCategory` (`kdive.domain.errors`).
- Produces:
  - `project_tool_schema(parameters: dict, kinds: frozenset[ResourceKind]) -> dict` — a narrowed deep-copy.
  - `assert_kind_composed(kind: ResourceKind, kinds: frozenset[ResourceKind]) -> None` — raises `CategorizedError(CONFIGURATION_ERROR)` when not composed.

> **Discovery first:** the `$def` key names and `ProviderSection` property keys come from Pydantic. Step 1 pins them with a real schema dump so the filter targets the right keys.

- [ ] **Step 1: Write the failing test (pins schema shape + behavior)**

```python
# tests/mcp/test_provider_schema.py
from __future__ import annotations

import pytest

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.provider_schema import assert_kind_composed, project_tool_schema
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.profiles.provisioning import ProvisioningProfile

LOCAL = ResourceKind.LOCAL_LIBVIRT
REMOTE = ResourceKind.REMOTE_LIBVIRT
FAULT = ResourceKind.FAULT_INJECT


# The two surfaces narrow via DIFFERENT $defs (verified against the real schemas):
#   - allocations.request: `$defs.ResourceKind` is a named enum (the kind selector).
#   - systems.define/provision: `$defs.ProviderSection.properties` keyed by alias; the profile
#     schema has NO `ResourceKind` $def. So enum tests source from the allocation payload and
#     section tests source from the profile.
def _allocation_schema() -> dict:
    return AllocationRequestPayload.model_json_schema()


def _profile_schema() -> dict:
    return ProvisioningProfile.model_json_schema()


def test_resource_kind_enum_narrows_to_live_set() -> None:
    schema = _allocation_schema()
    assert set(schema["$defs"]["ResourceKind"]["enum"]) == {
        "local-libvirt",
        "fault-inject",
        "remote-libvirt",
    }
    projected = project_tool_schema(schema, frozenset({LOCAL}))
    assert projected["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]


def test_profile_schema_has_no_resource_kind_def() -> None:
    # Pins the asymmetry: the section union is alias-keyed, not a ResourceKind enum.
    assert "ResourceKind" not in _profile_schema()["$defs"]


def test_provider_section_properties_narrow_to_live_aliases() -> None:
    schema = _profile_schema()
    props = schema["$defs"]["ProviderSection"]["properties"]
    assert {"local-libvirt", "remote-libvirt", "fault-inject"} <= set(props)
    projected = project_tool_schema(schema, frozenset({LOCAL, REMOTE}))
    kept = set(projected["$defs"]["ProviderSection"]["properties"])
    assert "fault-inject" not in kept
    assert {"local-libvirt", "remote-libvirt"} <= kept


def test_projection_does_not_mutate_the_input() -> None:
    schema = _allocation_schema()
    before = schema["$defs"]["ResourceKind"]["enum"][:]
    project_tool_schema(schema, frozenset({LOCAL}))
    assert schema["$defs"]["ResourceKind"]["enum"] == before


def test_empty_set_narrows_each_surface() -> None:
    alloc = project_tool_schema(_allocation_schema(), frozenset())
    assert alloc["$defs"]["ResourceKind"]["enum"] == []
    profile = project_tool_schema(_profile_schema(), frozenset())
    assert profile["$defs"]["ProviderSection"]["properties"] == {}


def test_schema_without_defs_is_returned_unchanged() -> None:
    assert project_tool_schema({"type": "object"}, frozenset({LOCAL})) == {"type": "object"}


def test_assert_kind_composed_accepts_composed() -> None:
    assert_kind_composed(LOCAL, frozenset({LOCAL, REMOTE}))  # no raise


def test_assert_kind_composed_rejects_non_composed() -> None:
    with pytest.raises(CategorizedError) as exc:
        assert_kind_composed(FAULT, frozenset({LOCAL}))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["kind"] == "fault-inject"
    assert exc.value.details["registered"] == ["local-libvirt"]


def test_assert_kind_composed_empty_set_message() -> None:
    with pytest.raises(CategorizedError) as exc:
        assert_kind_composed(LOCAL, frozenset())
    assert "no providers configured" in str(exc.value)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcp/test_provider_schema.py -q`
Expected: FAIL (`ModuleNotFoundError: kdive.mcp.provider_schema`). If instead the two schema-shape asserts (`ResourceKind`/`ProviderSection` `$def` keys) fail, the generated names differ — **read the failure, update the `$def`/property key constants in Step 3 to the real names, and re-run.**

- [ ] **Step 3: Write minimal implementation**

```python
# src/kdive/mcp/provider_schema.py
"""Agent-facing provider-schema narrowing + call-time guard (ADR-0269).

Both helpers read the single live ``registered_kinds()`` set, so the published schema
and the accept/reject decision cannot disagree about membership. The projection is a
structural narrowing of the FastMCP-generated schema: the domain models stay static, so
the section sub-models are already present in ``$defs`` and the projection only drops
members.
"""

from __future__ import annotations

import copy

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provider_sections import aliases_for

_RESOURCE_KIND_DEF = "ResourceKind"
_PROVIDER_SECTION_DEF = "ProviderSection"


def project_tool_schema(parameters: dict, kinds: frozenset[ResourceKind]) -> dict:
    """Return a deep-copy of ``parameters`` narrowed to the composed ``kinds``.

    Filters the ``ResourceKind`` enum to the live kind values and the ``ProviderSection``
    object's properties to the live aliases. A schema with no ``$defs`` (or missing either
    definition) is returned structurally unchanged.
    """
    projected = copy.deepcopy(parameters)
    defs = projected.get("$defs")
    if not isinstance(defs, dict):
        return projected
    live_values = [k.value for k in ResourceKind if k in kinds]
    kind_def = defs.get(_RESOURCE_KIND_DEF)
    if isinstance(kind_def, dict) and isinstance(kind_def.get("enum"), list):
        kind_def["enum"] = [v for v in kind_def["enum"] if v in live_values]
    section_def = defs.get(_PROVIDER_SECTION_DEF)
    if isinstance(section_def, dict) and isinstance(section_def.get("properties"), dict):
        live = aliases_for(kinds)
        section_def["properties"] = {
            alias: schema
            for alias, schema in section_def["properties"].items()
            if alias in live
        }
    return projected


def assert_kind_composed(kind: ResourceKind, kinds: frozenset[ResourceKind]) -> None:
    """Raise ``configuration_error`` when ``kind`` is not in the composed ``kinds``."""
    if kind in kinds:
        return
    registered = sorted(k.value for k in kinds)
    message = (
        "no providers configured"
        if not kinds
        else f"resource kind {kind.value!r} is not configured in this deployment"
    )
    raise CategorizedError(
        message,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"kind": kind.value, "registered": registered},
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/mcp/test_provider_schema.py -q` → Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run pytest tests/mcp/test_provider_schema.py -q
git add src/kdive/mcp/provider_schema.py tests/mcp/test_provider_schema.py
git commit -m "feat(mcp): add provider schema projection + call-time guard (ADR-0269)"
```

---

### Task 3: `profile_examples` iterates the composed set

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py:96-130` (signature + `_configured_providers`)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (`_register_systems_profile_examples` + `register`)
- Test: `tests/mcp/lifecycle/test_systems_profile_examples.py`

**Interfaces:**
- Consumes: `PROVIDER_SECTIONS` (Task 1); `ResourceKind`; `ProviderResolver.registered_kinds()`.
- Produces: `build_profile_examples(doc: InventoryDoc | None, kinds: frozenset[ResourceKind]) -> ToolResponse`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/mcp/lifecycle/test_systems_profile_examples.py
from __future__ import annotations

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.tools.lifecycle.systems.profile_examples import build_profile_examples

LOCAL = ResourceKind.LOCAL_LIBVIRT
REMOTE = ResourceKind.REMOTE_LIBVIRT
FAULT = ResourceKind.FAULT_INJECT


def _providers(resp) -> set[str]:
    return {item.structured_content["data"]["provider"] for item in resp.items}


def test_examples_cover_exactly_the_composed_kinds() -> None:
    resp = build_profile_examples(None, frozenset({LOCAL}))
    assert _providers(resp) == {"local-libvirt"}


def test_fault_inject_absent_unless_composed() -> None:
    without = build_profile_examples(None, frozenset({LOCAL, REMOTE}))
    assert "fault-inject" not in _providers(without)
    with_fault = build_profile_examples(None, frozenset({LOCAL, FAULT}))
    assert "fault-inject" in _providers(with_fault)


def test_empty_composed_set_yields_no_examples() -> None:
    resp = build_profile_examples(None, frozenset())
    assert resp.items == []
```

> Adjust `.structured_content["data"]` / `.items` access to match the existing helpers in this test file if they differ; reuse whatever accessor the file already uses for item data.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcp/lifecycle/test_systems_profile_examples.py -q`
Expected: FAIL — `build_profile_examples()` takes one positional arg (TypeError) or asserts mismatch.

- [ ] **Step 3: Implement — drive the provider set from `kinds`**

Replace `build_profile_examples` and `_configured_providers` in `profile_examples.py`:

```python
def build_profile_examples(
    doc: InventoryDoc | None, kinds: frozenset[ResourceKind]
) -> ToolResponse:
    """Build the example-profiles collection for the deployment's composed providers.

    Args:
        doc: The parsed ``systems.toml`` inventory, or ``None`` when no file is present.
        kinds: The providers composed in this deployment (``resolver.registered_kinds()``);
            one example is emitted per composed kind, ordered by ``ResourceKind``.
    """
    providers = [
        PROVIDER_SECTIONS[kind].alias for kind in ResourceKind if kind in kinds
    ]
    items = [_example_item(provider, doc) for provider in providers]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )
```

Delete `_configured_providers` (now dead). Add imports at the top of the module:

```python
from kdive.domain.catalog.resources import ResourceKind
from kdive.profiles.provider_sections import PROVIDER_SECTIONS
```

In `registrar.py`, thread the resolver into the examples registrar:

```python
def _register_systems_profile_examples(app: FastMCP, resolver: ProviderResolver) -> None:
    @app.tool(name="systems.profile_examples", ...)  # keep the existing decorator args
    async def systems_profile_examples() -> ToolResponse:
        return build_profile_examples(
            load_inventory_for_examples(), resolver.registered_kinds()
        )
```

And update the `register(...)` call site:

```python
    _register_systems_profile_examples(app, resolver)
```

- [ ] **Step 4: Run to verify it passes (+ existing cases)**

Run: `uv run pytest tests/mcp/lifecycle/test_systems_profile_examples.py -q`
Expected: PASS. Update any pre-existing test that called `build_profile_examples(doc)` with one arg to pass `frozenset(ResourceKind)` (the old "all three by default" contract) so behavior is preserved where the test intends all providers.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run pytest tests/mcp/lifecycle/test_systems_profile_examples.py -q
git add src/kdive/mcp/tools/lifecycle/systems/profile_examples.py \
        src/kdive/mcp/tools/lifecycle/systems/registrar.py \
        tests/mcp/lifecycle/test_systems_profile_examples.py
git commit -m "feat(mcp): drive profile_examples from composed providers (ADR-0269)"
```

---

### Task 4: list-time schema projection in `on_list_tools`

**Files:**
- Modify: `src/kdive/mcp/middleware/exposure.py` (inject resolver, project affected tools)
- Modify: `src/kdive/mcp/app.py:26-61` (build resolver before middleware; pass it in)
- Create: `tests/mcp/middleware/test_exposure_projection.py`
- Modify (existing call sites — see Step 3b): `tests/mcp/middleware/test_exposure.py` (6 sites: lines 47, 64, 81, 102, 116, 134), `tests/mcp/core/test_tool_exposure_middleware.py` (4 sites: lines 66, 75, 86, 99)

**Interfaces:**
- Consumes: `project_tool_schema` (Task 2); `ProviderResolver`.
- Produces: `NARROWED_TOOLS: frozenset[str]` and `ToolExposureMiddleware(resolver)` projecting the affected tools' `inputSchema`.

> **Prerequisite — the constructor change is breaking.** `ToolExposureMiddleware()` is currently
> constructed with no args at 11 sites (1 in `app.py`, 6 in `tests/mcp/middleware/test_exposure.py`,
> 4 in `tests/mcp/core/test_tool_exposure_middleware.py`). Adding a required `resolver` param breaks
> every one. Step 3 fixes `app.py`; **Step 3b** fixes all 10 test sites in the same task so the suite
> stays green.

> FastMCP `Tool` is a Pydantic model with a mutable `parameters` field; return `tool.model_copy(update={"parameters": projected})` so the shared registry object is left intact (precedent: `schema_advertising.py` mutates `tool.output_schema`).

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/middleware/test_exposure_projection.py
from __future__ import annotations

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.middleware.exposure import NARROWED_TOOLS, project_listed_tool
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.profiles.provisioning import ProvisioningProfile


class _FakeTool:
    def __init__(self, name: str, parameters: dict) -> None:
        self.name = name
        self.parameters = parameters

    def model_copy(self, *, update: dict) -> "_FakeTool":
        return _FakeTool(self.name, update["parameters"])


def test_narrowed_tools_membership() -> None:
    assert "systems.define" in NARROWED_TOOLS
    assert "allocations.request" in NARROWED_TOOLS
    assert "resources.list" not in NARROWED_TOOLS


def test_allocation_tool_kind_enum_is_projected() -> None:
    # allocations.request narrows via the $defs.ResourceKind enum.
    tool = _FakeTool("allocations.request", AllocationRequestPayload.model_json_schema())
    out = project_listed_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))
    assert out.parameters["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]


def test_systems_tool_section_props_are_projected() -> None:
    # systems.define narrows via $defs.ProviderSection.properties (no ResourceKind enum here).
    tool = _FakeTool("systems.define", ProvisioningProfile.model_json_schema())
    out = project_listed_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))
    kept = set(out.parameters["$defs"]["ProviderSection"]["properties"])
    assert kept == {"local-libvirt"}


def test_unaffected_tool_is_returned_unchanged() -> None:
    tool = _FakeTool("resources.list", ProvisioningProfile.model_json_schema())
    out = project_listed_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))
    assert out is tool
```

> **Live-app integration assertion (add to this test module).** The unit tests above use bare
> `Model.model_json_schema()` as a proxy; this one pins the proxy against the *real* FastMCP-published
> tool `.parameters`. FastMCP generates the schema from the handler signature and **hoists nested
> model `$defs` to the top level** — verified: the real `allocations.request` parameters carry
> `$defs.ResourceKind` and `systems.define` carries `$defs.ProviderSection`, exactly the keys the
> helper targets. The test builds the app and projects the registry's published schema:

```python
# same file — builds the app like tests/mcp/core/test_app.py does (pool + secret_registry args)
from kdive.mcp.schema_advertising import registered_tools


def _tool(app, name: str):
    return next(t for t in registered_tools(app) if t.name == name)


def test_real_published_schema_narrows_for_local_only(app) -> None:
    # `app` is built like tests/mcp/core/test_app.py. The DEFAULT composition is already
    # local-only: no remote-libvirt config is present and KDIVE_FAULT_INJECT is unset, so
    # registered_kinds() == {local-libvirt}. `build_app` has no enable_* flag — the resolver
    # opt-ins live on ProviderComposition.build_provider_resolver. Pass the app's own resolver
    # set rather than hard-coding, to track whatever the build composed.
    kinds = frozenset({ResourceKind.LOCAL_LIBVIRT})
    alloc = project_listed_tool(_tool(app, "allocations.request"), kinds)
    assert alloc.parameters["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]
    define = project_listed_tool(_tool(app, "systems.define"), kinds)
    assert set(define.parameters["$defs"]["ProviderSection"]["properties"]) == {"local-libvirt"}
```

> Construct `app` inline in the test (see `tests/mcp/core/test_app.py` for the `build_app(pool, ...,
> secret_registry=...)` construction pattern); do not add a new conftest fixture. To force a specific
> composed set explicitly, pass a `ProviderComposition` and build the resolver with
> `build_provider_resolver(enable_remote_libvirt=False, enable_fault_inject=False)` — but the default
> build is already local-only, so the assertions above hold without any flag.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcp/middleware/test_exposure_projection.py -q`
Expected: FAIL (`ImportError: NARROWED_TOOLS`).

- [ ] **Step 3: Implement the projection in the middleware**

Add to `exposure.py`:

```python
from kdive.mcp.provider_schema import project_tool_schema
from kdive.providers.core.resolver import ProviderResolver

NARROWED_TOOLS: frozenset[str] = frozenset(
    {"allocations.request", "systems.define", "systems.provision"}
)


def project_listed_tool(tool: Tool, kinds: frozenset[ResourceKind]) -> Tool:
    """Return ``tool`` with its inputSchema narrowed to ``kinds`` (or unchanged)."""
    if tool.name not in NARROWED_TOOLS:
        return tool
    projected = project_tool_schema(tool.parameters, kinds)
    return tool.model_copy(update={"parameters": projected})
```

Give the middleware the resolver and apply the projection (fail-open with a warning):

```python
class ToolExposureMiddleware(Middleware):
    def __init__(self, resolver: ProviderResolver) -> None:
        self._resolver = resolver

    async def on_list_tools(self, context: Any, call_next: Callable[[Any], Any]) -> Sequence[Tool]:
        tools: Sequence[Tool] = await call_next(context)
        try:
            ctx = request_context()
            visible = visible_tool_names(ctx, (tool.name for tool in tools))
            if _gateway_enabled():
                visible &= CORE_TOOLS
        except AuthError:
            _log.debug("no verified token in on_list_tools; advertising the full catalog")
            return tools
        except Exception:
            _log.warning("tool-exposure filter failed; advertising the full catalog", exc_info=True)
            return tools
        kinds = self._resolver.registered_kinds()
        result: list[Tool] = []
        for tool in tools:
            if tool.name not in visible:
                continue
            try:
                result.append(project_listed_tool(tool, kinds))
            except Exception:
                _PROJECTION_FAILURES.add(1)
                _log.warning(
                    "provider-schema projection failed for %s; advertising full schema",
                    tool.name,
                    exc_info=True,
                )
                result.append(tool)
        return result
```

Add the fail-open counter near the top of `exposure.py`:

```python
from opentelemetry import metrics

_PROJECTION_FAILURES = metrics.get_meter("kdive.mcp").create_counter(
    "kdive_mcp_provider_schema_projection_failures",
    description="provider-schema projection fell open to the full schema (ADR-0269)",
)
```

Wire the resolver in `app.py` — build it **before** the middleware and reuse the same instance for the `AppAssembly`:

```python
    composition = provider_composition or ProviderComposition(secret_registry=secret_registry)
    resolver = composition.build_provider_resolver()
    app.add_middleware(TelemetryMiddleware(...))   # unchanged
    app.add_middleware(UsageTrackingMiddleware(pool))
    app.add_middleware(ToolExposureMiddleware(resolver))   # was: ToolExposureMiddleware()
    app.add_middleware(DenialAuditMiddleware(pool))
    app.add_middleware(BindingErrorMiddleware())
    assembly = AppAssembly(
        resolver=resolver,                                 # was: composition.build_provider_resolver()
        secret_registry=composition.secret_registry,
        ...
    )
```

> Move the `composition = ...` and `resolver = ...` lines above the `add_middleware` block. Keep `FastMCP(...)` construction first. Build the resolver **once**.

- [ ] **Step 3b: Update the existing `ToolExposureMiddleware()` call sites**

The constructor now requires a `resolver`. Update all 10 existing test constructions to pass an
empty resolver — `ProviderResolver({})` has `registered_kinds() == frozenset()` (valid, ADR-0131),
and these tests assert on filtered tool **names**, not schemas, so the projection (which fail-opens)
does not affect their assertions:

```python
# add the import to each of the two test files
from kdive.providers.core.resolver import ProviderResolver

# replace every  ToolExposureMiddleware()  with:
ToolExposureMiddleware(ProviderResolver({}))
```

Sites: `tests/mcp/middleware/test_exposure.py` lines 47, 64, 81, 102, 116, 134;
`tests/mcp/core/test_tool_exposure_middleware.py` lines 66, 75, 86, 99. (If any of these tests
*does* assert on a tool's schema, pass a resolver whose `registered_kinds()` returns the full set
instead, so the schema is unchanged — but a name-only filter test takes the empty resolver.)

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/mcp/middleware/test_exposure_projection.py \
              tests/mcp/middleware/test_exposure.py \
              tests/mcp/core/test_tool_exposure_middleware.py \
              tests/mcp/core/test_app.py -q
```
Expected: PASS (new projection tests green; the 10 updated call sites green; the app still builds).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run pytest tests/mcp/middleware/ tests/mcp/core/test_tool_exposure_middleware.py tests/mcp/core/test_app.py -q
git add src/kdive/mcp/middleware/exposure.py src/kdive/mcp/app.py \
        tests/mcp/middleware/test_exposure_projection.py \
        tests/mcp/middleware/test_exposure.py \
        tests/mcp/core/test_tool_exposure_middleware.py
git commit -m "feat(mcp): narrow listed tool schemas to composed providers (ADR-0269)"
```

---

### Task 5: `tools.search` applies the same projection

**Files:**
- Modify: `src/kdive/mcp/tools/gateway.py:60-69` (`_describe` → `describe_tool`) + `gateway.register` signature (gains `resolver`) + the `tools.search` registrar
- Modify: `src/kdive/mcp/tool_registration.py:90-93` (`_register_gateway_tools` — un-underscore `_assembly`, pass `assembly.resolver` to `gateway.register`)
- Modify: `tests/mcp/tools/test_gateway_invoke.py:137` (existing `gateway.register(app)` call site — breaks on the signature change)
- Test: `tests/mcp/test_gateway_projection.py`

**Interfaces:**
- Consumes: `project_listed_tool` (Task 4), `project_tool_schema` (Task 2); the `ProviderResolver` reaching the gateway registrar.

> **Gateway registrar threading.** `gateway.register(app)` (gateway.py:72) gains `resolver:
> ProviderResolver`. It is dispatched by `_register_gateway_tools(app, _pool, _assembly)`
> (tool_registration.py:90) which currently ignores `_assembly` — change it to use `assembly` and
> call `gateway.register(app, resolver=assembly.resolver)`. The closure inside `tools.search`
> captures that `resolver`. **Two existing `gateway.register(app)` call sites break** and must be
> updated: tool_registration.py:93 (pass the resolver as above) and `tests/mcp/tools/
> test_gateway_invoke.py:137` (pass `ProviderResolver({})`, or a full-set resolver if that test
> asserts on schema content).

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/test_gateway_projection.py
from __future__ import annotations

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.tools.gateway import describe_tool  # renamed/exported _describe
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.profiles.provisioning import ProvisioningProfile


class _AllocTool:
    name = "allocations.request"  # narrows via $defs.ResourceKind enum
    description = "request an allocation"
    parameters = AllocationRequestPayload.model_json_schema()


class _SystemsTool:
    name = "systems.define"  # narrows via $defs.ProviderSection.properties (no ResourceKind def)
    description = "define a system"
    parameters = ProvisioningProfile.model_json_schema()


def test_describe_narrows_allocation_kind_enum() -> None:
    described = describe_tool(_AllocTool(), frozenset({ResourceKind.LOCAL_LIBVIRT}))
    assert described["input_schema"]["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]


def test_describe_narrows_systems_section_props() -> None:
    described = describe_tool(_SystemsTool(), frozenset({ResourceKind.LOCAL_LIBVIRT}))
    props = set(described["input_schema"]["$defs"]["ProviderSection"]["properties"])
    assert props == {"local-libvirt"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcp/test_gateway_projection.py -q` → FAIL (`ImportError: describe_tool`).

- [ ] **Step 3: Implement — project per match in the search result**

Rename `_describe(tool)` to `describe_tool(tool, kinds)` and narrow:

```python
def describe_tool(tool: Tool, kinds: frozenset[ResourceKind]) -> dict[str, JsonValue]:
    """Serialise a Tool into the ``{name, description, input_schema}`` match shape,
    narrowing the input schema to the composed ``kinds`` (ADR-0269)."""
    return cast(
        "dict[str, JsonValue]",
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": project_listed_tool(tool, kinds).parameters,
        },
    )
```

Capture `resolver` in the `tools.search` closure and pass `resolver.registered_kinds()` to `describe_tool` for each match:

```python
        kinds = resolver.registered_kinds()
        data={
            "matches": cast("JsonValue", [describe_tool(t, kinds) for t in matches]),
            "truncated": len(ranked) > limit,
        },
```

Import `project_listed_tool` from `kdive.mcp.middleware.exposure` and `ResourceKind`.

- [ ] **Step 4: Run to verify it passes (incl. the updated call site)**

```bash
uv run pytest tests/mcp/test_gateway_projection.py tests/mcp/tools/test_gateway_invoke.py -q
```
Expected: PASS (the new projection tests, and `test_gateway_invoke.py` with its line-137 call site updated to the new `gateway.register` signature).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run pytest tests/mcp/test_gateway_projection.py tests/mcp/tools/test_gateway_invoke.py -q
git add src/kdive/mcp/tools/gateway.py src/kdive/mcp/tool_registration.py \
        tests/mcp/test_gateway_projection.py tests/mcp/tools/test_gateway_invoke.py
git commit -m "feat(mcp): narrow tools.search schemas to composed providers (ADR-0269)"
```

---

### Task 6: call-time guard on allocation + systems handlers

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/allocations/request.py` (add the `_guard_resource_kind` helper only — **no signature change** to `request_allocation`/`_request_allocation`, which have ~26 existing callers)
- Modify: `src/kdive/mcp/tools/lifecycle/allocations/registrar.py:56-87` (thread `resolver` in; call the guard in the closure)
- Modify: `src/kdive/mcp/tool_registration.py` (pass `assembly.resolver` to the allocations registrar)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (guard in define/provision/reprovision closures)
- Test: `tests/mcp/lifecycle/test_call_time_kind_guard.py`

**Interfaces:**
- Consumes: `assert_kind_composed` (Task 2); `ResourceByKind` (`kdive.mcp.tool_payloads`); `ProviderResolver`.

- [ ] **Step 1: Write the failing test (direct + gateway parity)**

The red test targets a **not-yet-existing** production guard function `_guard_resource_kind` in
`request.py`, so it drives the change (no DB/pool needed — it tests the guard the handler calls):

```python
# tests/mcp/lifecycle/test_call_time_kind_guard.py
from __future__ import annotations

import pytest

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tool_payloads import AllocationRequestPayload, ResourceByKind, ResourceByPool
from kdive.mcp.tools.lifecycle.allocations.request import _guard_resource_kind


class _StubResolver:
    def __init__(self, kinds: frozenset[ResourceKind]) -> None:
        self._kinds = kinds

    def registered_kinds(self) -> frozenset[ResourceKind]:
        return self._kinds


def test_guard_rejects_non_composed_kind() -> None:
    payload = AllocationRequestPayload(
        shape="small", resource=ResourceByKind(kind=ResourceKind.FAULT_INJECT)
    )
    with pytest.raises(CategorizedError) as exc:
        _guard_resource_kind(payload, _StubResolver(frozenset({ResourceKind.LOCAL_LIBVIRT})))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_guard_accepts_composed_kind() -> None:
    payload = AllocationRequestPayload(
        shape="small", resource=ResourceByKind(kind=ResourceKind.LOCAL_LIBVIRT)
    )
    _guard_resource_kind(payload, _StubResolver(frozenset({ResourceKind.LOCAL_LIBVIRT})))


def test_guard_ignores_non_kind_selectors() -> None:
    # A pool/id selector names no kind, so the guard is a no-op even with NO providers composed
    # (resolution fails closed downstream for an absent resource).
    payload = AllocationRequestPayload(shape="small", resource=ResourceByPool(pool="p"))
    _guard_resource_kind(payload, _StubResolver(frozenset()))  # no raise
```

> Also add an end-to-end test that exercises the guard where it actually lives — the **registered
> tool**, not `request_allocation` directly (which is intentionally unchanged and does not guard).
> Invoke `allocations.request` through the app (`app.call_tool("allocations.request", {...})`, the
> pattern in the existing `tests/mcp/lifecycle/test_allocations_tools.py`) with a non-composed
> `ResourceByKind` and assert a `configuration_error` envelope; if the repo has a `tools.invoke`
> harness, drive the same call through it to prove the guard is on the shared dispatch path, not
> schema-only.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/mcp/lifecycle/test_call_time_kind_guard.py -q`
Expected: FAIL with `ImportError: cannot import name '_guard_resource_kind'` (the production guard does not exist yet). Implement Step 3, then re-run → PASS.

- [ ] **Step 3: Implement the guards**

Define the named guard in `request.py` (so the red test's import resolves and it is unit-testable),
but **do not change `request_allocation`'s signature** — it has ~26 existing callers
(`tests/integration/test_m1_allocation_accounting.py`, `tests/mcp/lifecycle/test_allocations_tools.py`,
`test_allocations_pcie.py`) that a new required param would break. Instead, the guard runs in the
allocations **registrar closure**, which already captures `resolver`. The closure is the registered
tool function `app.call_tool` invokes, so it is on the shared handler path for both a direct call
and the ADR-0268 `tools.invoke` dispatcher:

```python
# src/kdive/mcp/tools/lifecycle/allocations/request.py  (new helper, no signature change elsewhere)
from kdive.mcp.provider_schema import assert_kind_composed
from kdive.mcp.tool_payloads import ResourceByKind
from kdive.providers.core.resolver import ProviderResolver


def _guard_resource_kind(
    request: AllocationRequestPayload, resolver: ProviderResolver
) -> None:
    """Reject a kind-selected resource whose kind is not composed (ADR-0269).

    A pool/id selector names no kind, so the guard is a no-op there — resolution fails
    closed downstream for an absent resource.
    """
    if isinstance(request.resource, ResourceByKind):
        assert_kind_composed(request.resource.kind, resolver.registered_kinds())
```

Thread `resolver` into the allocations registrar and call the guard in the closure, before
`_request_allocation(...)`:

```python
# src/kdive/mcp/tools/lifecycle/allocations/registrar.py
def _register_allocations_request(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    ...
    @app.tool(name="allocations.request", ...)  # keep existing decorator args
    async def allocations_request(project, request, idempotency_key=None) -> ToolResponse:
        _guard_resource_kind(request, resolver)   # ADR-0269 — on the shared handler path
        return await _request_allocation(
            pool, current_context(), project=project, request=request,
            idempotency_key=idempotency_key, admission_metrics=admission_metrics,
        )
```

`request_allocation` / `_request_allocation` are **unchanged**, so their existing callers keep
compiling. Thread the resolver down the plane: `allocations_tools.register(app, pool)` →
`register(app, pool, *, resolver: ProviderResolver)` → `_register_allocations_request(app, pool,
resolver)`. The allocations plane is currently dispatched as
`_pool_only_plane_registrar(allocations_tools.register)` (tool_registration.py:255), which passes
only `(app, pool)` and **cannot reach `assembly.resolver`**. Replace that tuple entry with a custom
assembly-aware registrar mirroring `_register_systems_tools` (tool_registration.py:123):

```python
# src/kdive/mcp/tool_registration.py
def _register_allocations_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    allocations_tools.register(app, pool, resolver=assembly.resolver)

# in PLANE_REGISTRARS, replace `_pool_only_plane_registrar(allocations_tools.register)` with:
    _register_allocations_tools,
```

In `systems/registrar.py`, in each profile-accepting closure (`systems.define`, `systems.provision`, `systems.reprovision`), guard the parsed profile's kind before proceeding:

```python
        assert_kind_composed(profile.provider.kind, resolver.registered_kinds())
```

(`profile.provider.kind` is the existing `ProviderSection.kind` property, provisioning.py:191-200.) `systems.provision_defined` is exempt — it provisions an already-defined System and accepts no new provider section.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/mcp/lifecycle/test_call_time_kind_guard.py tests/mcp/lifecycle/ -q` → PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run pytest tests/mcp/lifecycle/ -q
git add src/kdive/mcp/tools/lifecycle/allocations/registrar.py \
        src/kdive/mcp/tools/lifecycle/allocations/request.py \
        src/kdive/mcp/tools/lifecycle/systems/registrar.py \
        src/kdive/mcp/tool_registration.py \
        tests/mcp/lifecycle/test_call_time_kind_guard.py
git commit -m "feat(mcp): reject non-composed kinds at the call boundary (ADR-0269)"
```

---

### Task 7: digest-stability + read-surface regression guards

**Files:**
- Test: `tests/profiles/test_digest_stability.py`
- Test: extend `tests/mcp/lifecycle/` for the `resources.list` permissive assertion

**Interfaces:** Consumes Task 1-6 outputs; no production code changes (proves invariants).

- [ ] **Step 1: Write the regression tests**

```python
# tests/profiles/test_digest_stability.py
from __future__ import annotations

from kdive.profiles.provisioning import ProvisioningProfile, profile_digest

_REMOTE_PROFILE = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "disk-image",
    "provider": {"remote-libvirt": {"base_image_volume": "vol-1"}},
}

# Pin the digest of a stored remote-libvirt profile. If this value changes, the boundary
# projection has leaked into the domain model and broken reprovision dedup (ADR-0038).
_EXPECTED_DIGEST = ""  # fill from the first green run, then assert it stays constant


def test_remote_profile_parses_and_digest_is_stable() -> None:
    parsed = ProvisioningProfile.parse(_REMOTE_PROFILE)
    digest = profile_digest(parsed)
    assert digest == _EXPECTED_DIGEST
```

- [ ] **Step 2: Capture the digest, then lock it**

Run: `uv run pytest tests/profiles/test_digest_stability.py -q` → it fails showing the actual digest. Paste that hex into `_EXPECTED_DIGEST`, re-run → PASS. This pins that the domain model (and therefore the digest) is untouched by this change.

- [ ] **Step 3: Add the read-surface permissive assertion**

Add a test asserting the `resources.list` tool's published `inputSchema` still enumerates every `ResourceKind` after projection (it is **not** in `NARROWED_TOOLS`). Use the existing app-building test fixture; assert `"resources.list"` schema's `ResourceKind` enum has all three values even when only local-libvirt is composed.

- [ ] **Step 4: Run to verify**

Run: `uv run pytest tests/profiles/test_digest_stability.py tests/mcp/ -q` → PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type && uv run pytest tests/profiles/test_digest_stability.py -q
git add tests/profiles/test_digest_stability.py tests/mcp/
git commit -m "test(mcp): pin digest stability + resources.list permissive (ADR-0269)"
```

---

## Final verification (before push)

- [ ] **Signature-change call-site sweep.** Every task that changes a function/constructor signature
  must `rg` its call sites across `src/` and `tests/` and update them in the same task — the suite
  goes red otherwise. The known breaking changes and their existing call sites:
  - `ToolExposureMiddleware()` → `(resolver)`: `app.py` + 6 in `tests/mcp/middleware/test_exposure.py` + 4 in `tests/mcp/core/test_tool_exposure_middleware.py` (Task 4 Step 3b).
  - `gateway.register(app)` → `(app, resolver=...)`: `tool_registration.py:93` + `tests/mcp/tools/test_gateway_invoke.py:137` (Task 5).
  - allocations `register(app, pool)` → `(app, pool, *, resolver)`: dispatched via `_pool_only_plane_registrar` → switch to a custom registrar (Task 6). No test call sites.
  - `build_profile_examples(doc)` → `(doc, kinds)`: registrar + 4 sites in `tests/mcp/lifecycle/test_systems_profile_examples.py` (Task 3 Step 4).
  - `request_allocation`/`_request_allocation`: **unchanged** by design (Task 6 guards in the closure), so their ~26 callers are untouched.
- [ ] Run the **full** suite: `just lint && just type && just test`.
- [ ] Regenerate the agent-facing tool reference and confirm it is unchanged (the registry tool schema is the deployment-agnostic full schema; the projection happens only per-connection at list-time): `just docs && git diff --exit-code docs/guide/reference/`. If it changed, investigate — the static models should be untouched.
- [ ] `just adr-status-check && just docs-links && just check-mermaid`.

## Self-Review (plan author)

- **Spec coverage:** Goal 1 → Tasks 3/4/5 (the three narrowed surfaces). Goal 2 → Task 6 (call-time guard, direct + gateway). Goal 3 → Task 1 registry + the forward-looking factory-level test (Task 2/3 prove registry-driven). Goal 4 (list/call-time, hot-add seam) → Tasks 4/5 read `registered_kinds()` per call. §3 domain-permissive/digest → Task 7. §5 fail-open + counter → Task 4. §7 empty set → Tasks 2/3/6. §8 fault-inject derived → Task 3.
- **Type consistency:** `project_tool_schema(parameters, kinds)`, `assert_kind_composed(kind, kinds)`, `project_listed_tool(tool, kinds)`, `describe_tool(tool, kinds)`, `build_profile_examples(doc, kinds)`, `aliases_for(kinds)` — names used consistently across tasks.
- **Open verification points flagged inline:** exact `$def`/property key names (Task 2 Step 2), the gateway/allocations resolver-threading call sites (Tasks 5/6), and the existing test accessors (Task 3) are confirmed against the live code during implementation, not assumed.
