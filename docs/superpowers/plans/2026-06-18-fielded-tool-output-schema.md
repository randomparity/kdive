# Fielded Tool Output Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for each task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ADR-0113's flat `{"type": "object"}` advertised MCP output schema with a fielded, non-recursive `ToolResponse` envelope schema, and publish the response-envelope guide as an MCP doc resource.

**Architecture:** One module-level constant (`ENVELOPE_OUTPUT_SCHEMA`) in `src/kdive/mcp/app.py` carries a flat object schema with a `properties` entry per envelope field and no `$ref`/`$defs`. The existing `build_app` sweep (renamed `_advertise_envelope_output_schema`) writes a shallow copy onto every live tool. The runtime `ToolResponse` model and `structured_content` wire payload are unchanged. AC#4 corrects `docs/guide/response-envelope.md` and registers it via the ADR-0151 doc-resource allowlist.

**Tech Stack:** Python 3.14, FastMCP 3.4.0, pydantic, `uv`, `just`, pytest.

## Global Constraints

- ADR: **0170**. Spec: `docs/specs/2026-06-18-fielded-tool-output-schema.md`.
- No change to `src/kdive/mcp/responses.py` (`ToolResponse`), `structured_content`, or `validate_json_value`.
- No migration, no new state, no auth change.
- `just ci` green before every commit. Recipes hard-gated individually in CI: `lint`, `type`, `lint-shell`, `lint-workflows`, `check-mermaid`, `docs-links`, `docs-paths`, `adr-status-check`, `docs-check`, `config-docs-check`, `config-guard`, `env-docs-check`, `resources-docs-check`, `chart-version-check`, `test`.
- Limits: ≤100 lines/function, cyclomatic ≤8, absolute imports only, Google-style docstring on the public helper, 100-char lines, plain factual prose (no "critical"/"robust"/"comprehensive"/"elegant").
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Fielded `ENVELOPE_OUTPUT_SCHEMA` + renamed sweep + probe-level tests

**Files:**
- Modify: `src/kdive/mcp/app.py:393-424` (constant + helper), `:477` (call site).
- Test: `tests/mcp/core/test_output_schema.py` (rewrite the suite's assertions).
- Test (behavior change, not just a rename): `tests/mcp/core/test_binding_error_middleware.py` — rename the helper references at `:257,269,290,301` **and** convert the two end-to-end tests' `result.data` subscript reads to `result.structured_content[...]`, because once the fielded schema is swept on, `result.data` is a pydantic model (not subscriptable), while `structured_content` stays the byte-stable dict.

**Interfaces:**
- Produces: `ENVELOPE_OUTPUT_SCHEMA: dict[str, Any]` (fielded), `_advertise_envelope_output_schema(app: FastMCP) -> int` (renamed from `_advertise_flat_output_schema`, same body shape + zero-count guard).

- [ ] **Step 1: Write the failing tests.** In `tests/mcp/core/test_output_schema.py`, replace the import `from kdive.mcp.app import ENVELOPE_OUTPUT_SCHEMA, _advertise_flat_output_schema` with `_advertise_envelope_output_schema`, and add/replace these tests (delete `test_detail_field_is_not_an_advertised_output_property`, which asserted `"properties" not in schema` — now false by design):

```python
import json

from kdive.mcp.app import ENVELOPE_OUTPUT_SCHEMA, _advertise_envelope_output_schema


def test_schema_advertises_every_envelope_field() -> None:
    # AC#1 + AC#3 drift guard: the advertised properties are exactly the model fields.
    assert ENVELOPE_OUTPUT_SCHEMA["type"] == "object"
    assert set(ENVELOPE_OUTPUT_SCHEMA["properties"]) == set(ToolResponse.model_fields)


def test_schema_is_ref_free() -> None:
    # AC#2: no recursion — the constant carries no $ref/$defs.
    serialized = json.dumps(ENVELOPE_OUTPUT_SCHEMA)
    assert "$ref" not in serialized
    assert "$defs" not in serialized


def test_sweep_advertises_fielded_schema() -> None:
    app = _probe_app()
    swept = _advertise_envelope_output_schema(app)
    assert swept == 2

    async def _run() -> list[dict[str, object] | None]:
        async with Client(app) as client:
            return [t.outputSchema for t in await client.list_tools()]

    for schema in asyncio.run(_run()):
        assert schema is not None
        assert set(schema["properties"]) == set(ToolResponse.model_fields)
```

Update the existing behavior tests that call the helper. First rename `_advertise_flat_output_schema` → `_advertise_envelope_output_schema` in `test_failure_detail_round_trips_through_client`, `test_sweep_restores_data_and_logs_no_parse_error`, `test_sweep_raises_on_empty_tool_surface` (the unswept `test_unswept_recursive_schema_fails_to_parse` does not call the helper but does call `_call_and_capture`, see below).

`_call_and_capture` gains a third return value (`structured_content`). **Every caller must be updated to the 3-tuple** — there are three: `test_failure_detail_round_trips_through_client` (was line 105), `test_sweep_restores_data_and_logs_no_parse_error` (was line 114), and `test_unswept_recursive_schema_fails_to_parse` (was line 127, unswept path). Change the helper and all three unpack sites together:

```python
def _call_and_capture(
    app: FastMCP, tool: str
) -> tuple[object | None, list[str], dict[str, Any] | None]:
    """Call ``tool``; return (``.data``, structured-content parse-error messages, ``.structured_content``)."""
    logger = logging.getLogger("fastmcp")
    handler = _ErrorCollector()
    logger.addHandler(handler)
    try:

        async def _call() -> tuple[object | None, dict[str, Any] | None]:
            async with Client(app) as client:
                result = await client.call_tool(tool, {})
                return result.data, result.structured_content

        data, structured = asyncio.run(_call())
    finally:
        logger.removeHandler(handler)
    errors = [r.getMessage() for r in handler.records if "structured content" in r.getMessage()]
    return data, errors, structured
```

```python
def test_sweep_restores_data_and_logs_no_parse_error() -> None:
    app = _probe_app()
    _advertise_envelope_output_schema(app)
    data, errors, structured = _call_and_capture(app, "scalar.one")
    assert data is not None  # parse succeeded (model instance), not nulled
    assert structured is not None and structured["object_id"] == "obj-1"  # structured_content restored
    assert errors == []  # no parse-error log


def test_collection_round_trips_through_client() -> None:
    # AC#2: a non-empty `items` envelope parses; structured_content keeps the nested list.
    app = _probe_app()
    _advertise_envelope_output_schema(app)
    data, errors, structured = _call_and_capture(app, "list.coll")
    assert data is not None
    assert structured is not None and isinstance(structured["items"], list) and structured["items"]
    assert errors == []
```

Update `test_failure_detail_round_trips_through_client` to unpack the 3-tuple and read `structured["detail"]` (not `data["detail"]`). Update `test_unswept_recursive_schema_fails_to_parse` to unpack the 3-tuple (`data, errors, _structured = _call_and_capture(...)`); its assertions (`data is None`, `errors` non-empty) are unchanged because the unswept path still nulls `.data`.

- [ ] **Step 2: Run the tests, verify they fail.**

Run: `uv run python -m pytest tests/mcp/core/test_output_schema.py -q`
Expected: ImportError / AssertionError (helper not renamed yet, constant still flat).

- [ ] **Step 3: Implement.** In `src/kdive/mcp/app.py`, replace the flat constant and rename the helper:

```python
# A fielded, non-recursive output schema advertised for every tool (ADR-0170, revisiting
# ADR-0113). Documents every top-level `ToolResponse` envelope field. The two recursive
# fields collapse to generic shapes — `data` to a bare object and `items` to an array of
# bare objects — so the schema carries no self-`$ref` and the FastMCP 3.4.0 client builds a
# validator (the recursion ADR-0113 worked around). No field is `required` and
# `additionalProperties` is left permissive so the client never rejects a real payload; a new
# envelope field is caught by the drift-guard test, not silently. Typed `dict[str, Any]` to
# match FastMCP's `Tool.output_schema`.
ENVELOPE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The uniform kdive ToolResponse envelope (ADR-0019). `data` and `items` are "
        "intentionally open; see resource://kdive/docs/guide/response-envelope.md."
    ),
    "properties": {
        "object_id": {"type": "string"},
        "status": {"type": "string"},
        "suggested_next_actions": {"type": "array", "items": {"type": "string"}},
        "refs": {"type": "object", "additionalProperties": {"type": "string"}},
        "error_category": {"type": ["string", "null"]},
        "retryable": {"type": ["boolean", "null"]},
        "detail": {"type": ["string", "null"]},
        "data": {"type": "object"},
        "items": {"type": "array", "items": {"type": "object"}},
    },
}


def _advertise_envelope_output_schema(app: FastMCP) -> int:
    """Override every registered tool's advertised ``outputSchema`` with the envelope schema.

    Mutates the *live* registered ``Tool`` instances (the ``Tool``-typed values in the local
    provider's component store); ``app.list_tools()`` returns copies whose mutation would not
    change what the server advertises. Raises if no tools are found: ``build_app`` always
    registers a non-empty surface, so a zero count means the FastMCP registry accessor changed
    under us and the app must not silently fall back to the recursive auto-schema (ADR-0170).

    Returns the number of tools swept.
    """
    swept = 0
    for component in app.local_provider._components.values():
        if isinstance(component, Tool):
            component.output_schema = dict(ENVELOPE_OUTPUT_SCHEMA)
            swept += 1
    if swept == 0:
        raise RuntimeError(
            "no tools found to advertise an envelope outputSchema for; the FastMCP registry "
            "accessor (app.local_provider._components) may have changed (ADR-0170)"
        )
    return swept
```

Update the call site at `app.py:477` (`_advertise_flat_output_schema(app)` → `_advertise_envelope_output_schema(app)`) and the comment above it if it names the old helper.

In `tests/mcp/core/test_binding_error_middleware.py`, rename the import and call sites (`:257,269,290,301`) **and** convert each test's `result.data` subscript reads to `result.structured_content`, because `result.data` is now a non-subscriptable model. Concretely, in `test_end_to_end_malformed_profile_returns_envelope_not_toolerror` the inner `_run` must `return result.structured_content` and the assertions read `data["status"]`/`data["error_category"]`/`data["detail"]` off that dict (line ~277-283); apply the same `result.data` → `result.structured_content` change to the second test (`test_end_to_end_runs_create_typed_build_profile...`, line ~303+). The `data is not None` and the `["status"]`/`["error_category"]`/`["detail"]`/`["source"]` assertions then pass unchanged against the dict.

- [ ] **Step 4: Run the tests, verify they pass.**

Run: `uv run python -m pytest tests/mcp/core/test_output_schema.py tests/mcp/core/test_binding_error_middleware.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type, then commit.**

```bash
just lint && just type
git add src/kdive/mcp/app.py tests/mcp/core/test_output_schema.py tests/mcp/core/test_binding_error_middleware.py
git commit  # feat(mcp): advertise a fielded non-recursive ToolResponse output schema
```

---

### Task 2: Update the real `build_app` boundary test

**Files:**
- Modify/Test: `tests/mcp/core/test_tool_wrapper_boundary.py:549-565`.

**Interfaces:**
- Consumes: `ENVELOPE_OUTPUT_SCHEMA` and `ToolResponse.model_fields` from Task 1.

- [ ] **Step 1: Rewrite the assertion.** Replace `test_real_build_app_tools_advertise_flat_output_schema`'s body assertion `assert all(schema == {"type": "object"} for schema in schemas)` with a fielded check, and rename the test:

```python
def test_real_build_app_tools_advertise_envelope_output_schema() -> None:
    """Every build_app tool advertises the fielded envelope schema (#565, ADR-0170).

    Exercises build_app's real registry sweep: a renamed registry accessor makes build_app
    raise via the zero-count guard, and a non-fielded schema fails this assertion.
    """
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _schemas() -> list[dict[str, Any] | None]:
        async with Client(app) as client:
            return [t.outputSchema for t in await client.list_tools()]

    schemas = asyncio.run(_schemas())
    assert schemas, "build_app registered no tools"
    expected = set(ToolResponse.model_fields)
    for schema in schemas:
        assert schema is not None
        assert set(schema["properties"]) == expected
```

Add the `ToolResponse` import to the test module if absent.

- [ ] **Step 2: Run, verify it passes.**

Run: `uv run python -m pytest tests/mcp/core/test_tool_wrapper_boundary.py::test_real_build_app_tools_advertise_envelope_output_schema -q`
Expected: PASS.

- [ ] **Step 3: Lint + type, then commit.**

```bash
just lint && just type
git add tests/mcp/core/test_tool_wrapper_boundary.py
git commit  # test(mcp): pin the real build_app surface to the fielded output schema
```

---

### Task 3: AC#4 — correct the envelope guide and register it as an MCP doc resource

**Files:**
- Modify: `docs/guide/response-envelope.md`.
- Modify: `src/kdive/mcp/resources/registrar.py` (`DOC_RESOURCES`).
- Create: `src/kdive/mcp/resources/_content/response-envelope.md` (generated, do not hand-edit).
- Test: `tests/mcp/resources/test_doc_resources.py` is parameterized over `DOC_RESOURCES` (no count edit needed); verify it still passes.

**Interfaces:**
- Consumes: the ADR-0151 `DocResource` dataclass + `register` (unchanged).

- [ ] **Step 1: Correct `docs/guide/response-envelope.md`.** Fix the `data` row type (`dict[str, str]` → `dict[str, JsonValue]`, "plane-specific JSON values"), add rows for `retryable` (`bool | None`, derived from `error_category` per ADR-0118), `detail` (`str | None`, human-readable failure reason per ADR-0123), and `items` (`list[ToolResponse]`, one nested envelope per collection element). Add a section **"Reading an open payload"** explaining that `tools/list` advertises `data` as a generic object and `items` as an array of generic objects on purpose (the per-tool payload shape is open): `data` holds plane-specific scalars, each `items` entry is itself a full `ToolResponse`, and `refs` are object-store keys fetched via `artifacts.get` — never inline bytes. Plain factual prose.

- [ ] **Step 2: Add the `DOC_RESOURCES` entry.** Append to the tuple in `src/kdive/mcp/resources/registrar.py`:

```python
    DocResource(
        uri="resource://kdive/docs/guide/response-envelope.md",
        source="docs/guide/response-envelope.md",
        content_file="response-envelope.md",
        name="response-envelope",
        title="The kdive ToolResponse envelope",
        description=(
            "How to read any kdive tool result: the uniform ToolResponse envelope fields and "
            "how to interpret the intentionally-open data, items, and refs. Referenced by the "
            "advertised tool outputSchema (ADR-0170)."
        ),
    ),
```

- [ ] **Step 3: Generate the snapshot.**

Run: `just resources-docs`
Expected: writes `src/kdive/mcp/resources/_content/response-envelope.md`.

- [ ] **Step 4: Verify resource tests + drift check pass.**

Run: `uv run python -m pytest tests/mcp/resources/test_doc_resources.py tests/scripts/test_gen_doc_resources.py -q && just resources-docs-check && just docs-links docs-paths docs-check`
Expected: PASS / no drift.

- [ ] **Step 5: Commit.**

```bash
git add docs/guide/response-envelope.md src/kdive/mcp/resources/registrar.py src/kdive/mcp/resources/_content/response-envelope.md
git commit  # docs(mcp): publish the response-envelope guide as an MCP doc resource
```

---

### Task 4: Full-suite guardrail pass

- [ ] **Step 1:** Run the full gate: `just ci`. Expected: all recipes green (DB tests skip cleanly if Docker is absent; note any skip in the PR body).
- [ ] **Step 2:** If any boundary/arch/doc test outside the touched dirs fails, fix and fold into the relevant commit before pushing.

## Self-Review

- **Spec coverage:** AC#1 → Tasks 1+2; AC#2 (no recursion / parse / `$ref`-free / `structured_content`) → Task 1 tests; AC#3 drift guard → Task 1; AC#4 doc + resource → Task 3. Zero-count guard retained → Task 1. All covered.
- **Placeholder scan:** none — every code block is concrete.
- **Type consistency:** helper named `_advertise_envelope_output_schema` everywhere (app.py, test_output_schema.py, test_binding_error_middleware.py); constant `ENVELOPE_OUTPUT_SCHEMA` reused by both boundary test and probe test; `DocResource` fields match the ADR-0151 dataclass.
