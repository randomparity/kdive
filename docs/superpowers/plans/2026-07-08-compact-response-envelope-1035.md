# Compact Response Envelope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, server-configured compact response envelope that omits null/empty defaulted fields (recursively within `items`) to cut per-call tokens on token-heavy list tools (#1035).

**Architecture:** A new `KDIVE_COMPACT_RESPONSES` config flag (default `off`) gates a cross-cutting `on_call_tool` middleware. When on, the middleware re-validates each result's `structured_content` into a `ToolResponse` and re-dumps it with `model_dump(exclude_defaults=True)`, returning a fresh `ToolResult` (which auto-regenerates the `content` text block, so both wire copies shrink). Default output is byte-identical to today. Full design: `docs/specs/2026-07-08-compact-response-envelope-1035.md` and `docs/adr/0314-compact-response-envelope.md`.

**Tech Stack:** Python 3.14, `uv`, FastMCP 3.4.2, pydantic 2.x, pytest. Guardrails via `just` recipes.

## Global Constraints

- **Branch:** `feat/compact-response-envelope-1035` off `main`. Never commit on `main`.
- **Guardrails (run before every commit):** `just lint` (ruff check + format), `just type` (ty, whole tree). Run `just test` for the touched suites. After changing a config `Setting`, run `just config-docs` and stage the regenerated `docs/guide/reference/config.md`. After editing `docs/guide/response-envelope.md`, run `just resources-docs` and stage the regenerated packaged snapshot. The full PR gate is `just ci`.
- **Line length 100; ruff lint set `E,F,I,UP,B,SIM`; `ty` strict.** Absolute imports only.
- **No ADR citations in agent-facing text** (ADR-0270), but config `help=` is operator-facing — an ADR cite there is fine (matches existing settings).
- **Doc-style guard:** plain, factual prose; use "Milestone" not "Sprint"; avoid "critical/robust/comprehensive/elegant".
- **Commit trailer (every commit):** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Verified ground truth (do not re-litigate):** FastMCP 3.4.2 has no global tool serializer; `structured_content` is a hard-coded full dump (`fastmcp/tools/base.py:324`). First-added middleware is **outermost** — it processes the response **last** (verified empirically: IN `A,B,C` / OUT `C,B,A`). `ToolResult.__init__` signature is `(content, structured_content, meta, is_error)` — it accepts `meta=` and exposes `.meta` (verified: `ToolResult(structured_content={...}).meta` is a readable attribute). Constructing `ToolResult(structured_content={...})` with `content` omitted **regenerates a non-empty `content` text block** from the dict (verified: `.content` has one `TextContent` whose `.text` is the JSON of the structured content) — this is why compacting `structured_content` also shrinks the `content` copy. `tools.invoke` returns the inner `ToolResult` directly (`gateway.py:115`, `run_middleware=True`), so compaction is idempotent across the gateway double pass (verified end-to-end: a `tools.invoke(name="images.list")` response comes back with inner `items[]` rows compacted). Config test idiom: `monkeypatch.setenv(...)` is sufficient — the autouse `reset_config` fixture (`tests/conftest.py:32`) clears the snapshot and `config.get` lazy-loads.

---

## File Structure

- `src/kdive/config/core_settings.py` (modify) — add the `COMPACT_RESPONSES` `Setting` and append to `SETTINGS`.
- `src/kdive/mcp/verbosity.py` (create) — `compact_responses_enabled()`, the single-source flag reader.
- `src/kdive/mcp/middleware/compact.py` (create) — `CompactResponseMiddleware` + the `_compact_result` transform.
- `src/kdive/mcp/app.py` (modify) — register the middleware outermost and emit the one-time startup log.
- `docs/guide/response-envelope.md` (modify) — document the opt-in flag, its contract, and the absent==default consumer rule.
- `docs/guide/reference/config.md` (regenerated) — via `just config-docs`.
- Packaged doc snapshot (regenerated) — via `just resources-docs`.
- Tests: `tests/mcp/test_verbosity.py`, `tests/mcp/middleware/test_compact.py`, additions to `tests/mcp/core/test_app.py`.

---

## Task 1: Config flag + single-source reader

**Files:**
- Modify: `src/kdive/config/core_settings.py` (add `COMPACT_RESPONSES` near `MCP_TOOL_GATEWAY`; append to `SETTINGS`)
- Create: `src/kdive/mcp/verbosity.py`
- Create: `tests/mcp/test_verbosity.py`
- Regenerate: `docs/guide/reference/config.md` (via `just config-docs`)

**Interfaces:**
- Produces: `kdive.config.core_settings.COMPACT_RESPONSES: Setting[str]` (default `"off"`); `kdive.mcp.verbosity.compact_responses_enabled() -> bool`.

- [ ] **Step 1: Write the failing reader test**

Create `tests/mcp/test_verbosity.py`:

```python
"""The compact-responses flag reader (ADR-0314)."""

from __future__ import annotations

import pytest

from kdive.mcp.verbosity import compact_responses_enabled


@pytest.mark.parametrize("value", ["on", "1", "true", "TRUE", "  On  "])
def test_enabled_for_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", value)
    assert compact_responses_enabled() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "no", ""])
def test_disabled_for_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", value)
    assert compact_responses_enabled() is False


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_COMPACT_RESPONSES", raising=False)
    assert compact_responses_enabled() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_verbosity.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.mcp.verbosity` (and `COMPACT_RESPONSES` not yet defined).

- [ ] **Step 3: Add the setting**

In `src/kdive/config/core_settings.py`, add immediately after the `MCP_TOOL_GATEWAY = Setting(...)` block:

```python
COMPACT_RESPONSES = Setting(
    name="KDIVE_COMPACT_RESPONSES",
    parse=_str,
    default="off",
    group="mcp",
    processes=_SERVER,
    help=(
        "When on/1/true, the server omits null/empty defaulted fields from every tool "
        "response envelope (recursively within items) to cut per-call tokens (ADR-0314). "
        "Default off — the full ADR-0019 envelope. A failure envelope always keeps "
        "error_category and retryable; detail is kept when a reason exists."
    ),
)
```

Then append `COMPACT_RESPONSES,` to the `SETTINGS = [ ... ]` list (after `MCP_TOOL_GATEWAY,`).

- [ ] **Step 4: Create the reader**

Create `src/kdive/mcp/verbosity.py`:

```python
"""Response-verbosity flag: the opt-in compact-envelope switch (ADR-0314, #1035)."""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import COMPACT_RESPONSES


def compact_responses_enabled() -> bool:
    """Return True when KDIVE_COMPACT_RESPONSES is set to on/1/true (default off, ADR-0314).

    Single source of truth for the compact-envelope toggle: CompactResponseMiddleware reads it
    per call to decide whether to omit null/empty defaulted envelope fields, and build_app reads
    it once at assembly to emit the compaction-enabled startup log.
    """
    return (config.get(COMPACT_RESPONSES) or "").strip().lower() in {"on", "1", "true"}
```

- [ ] **Step 5: Run reader tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_verbosity.py -q`
Expected: PASS (11 cases).

- [ ] **Step 6: Regenerate the config reference and run guardrails**

Run: `just config-docs` (regenerates `docs/guide/reference/config.md` — it will add the `KDIVE_COMPACT_RESPONSES` row under the `mcp` group).
Run: `just lint type config-docs-check`
Expected: all pass; `config-docs-check` diff is clean.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/config/core_settings.py src/kdive/mcp/verbosity.py \
        tests/mcp/test_verbosity.py docs/guide/reference/config.md
git commit -m "feat(1035): add KDIVE_COMPACT_RESPONSES flag + reader

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: CompactResponseMiddleware

**Files:**
- Create: `src/kdive/mcp/middleware/compact.py`
- Create: `tests/mcp/middleware/test_compact.py`

**Interfaces:**
- Consumes: `kdive.mcp.verbosity.compact_responses_enabled` (Task 1); `kdive.mcp.responses.ToolResponse`; `fastmcp.tools.base.ToolResult`; `fastmcp.server.middleware.Middleware`.
- Produces: `kdive.mcp.middleware.compact.CompactResponseMiddleware` (a `Middleware` with `on_call_tool`); module-level `_compact_result(result) -> Any` transform.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/mcp/middleware/test_compact.py`. This mirrors the `_FakeContext` / `call_next` harness used by `tests/mcp/core/test_binding_error_middleware.py`.

```python
"""CompactResponseMiddleware: opt-in null/empty envelope trimming (ADR-0314, #1035)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp.tools.base import ToolResult

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware.compact import CompactResponseMiddleware
from kdive.mcp.responses import ToolResponse


class _FakeContext:
    def __init__(self, name: str = "demo.list") -> None:
        self.message = type("_M", (), {"name": name})()


def _drive(result: Any, *, enabled: bool, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the middleware over a call_next that yields `result`, with the flag on/off."""
    if enabled:
        monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")
    else:
        monkeypatch.delenv("KDIVE_COMPACT_RESPONSES", raising=False)
    mw = CompactResponseMiddleware()

    async def _call_next(_ctx: Any) -> Any:
        return result

    return asyncio.run(mw.on_call_tool(_FakeContext(), _call_next))


def _full_collection() -> ToolResult:
    rows = [ToolResponse.success("img-0", "registered", data={"name": "n0"})]
    env = ToolResponse.collection("images", "ok", rows, suggested_next_actions=["images.list"])
    return ToolResult(structured_content=env.model_dump(mode="json"))


def test_disabled_passes_result_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _full_collection()
    out = _drive(result, enabled=False, monkeypatch=monkeypatch)
    assert out is result  # identical object, no rebuild


def test_enabled_compacts_collection_and_items(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(_full_collection(), enabled=True, monkeypatch=monkeypatch)
    sc = out.structured_content
    # top-level defaulted empties/nulls gone; object_id/status/non-empty data kept
    assert set(sc) == {"object_id", "status", "suggested_next_actions", "data", "items"}
    assert "error_category" not in sc and "refs" not in sc and "detail" not in sc
    row = sc["items"][0]
    assert set(row) == {"object_id", "status", "data"}  # per-item empties/nulls gone


def test_enabled_compacts_content_text_block(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(_full_collection(), enabled=True, monkeypatch=monkeypatch)
    assert out.content, "content block must be regenerated"
    parsed = json.loads(out.content[0].text)
    assert "error_category" not in parsed
    assert "error_category" not in parsed["items"][0]


def test_enabled_preserves_direct_failure_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    env = ToolResponse.failure("obj", ErrorCategory.NOT_FOUND)  # suppressed constant detail
    out = _drive(ToolResult(structured_content=env.model_dump(mode="json")),
                 enabled=True, monkeypatch=monkeypatch)
    sc = out.structured_content
    assert sc["error_category"] == ErrorCategory.NOT_FOUND.value
    assert sc["retryable"] is False
    assert sc["detail"]  # non-null suppressed constant kept


def test_enabled_drops_null_detail_on_from_job_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A worker-plane FAILED envelope: failure status, category set, detail=None by design.
    env = ToolResponse(object_id="job-1", status="failed",
                       error_category=ErrorCategory.BUILD_FAILURE.value)
    out = _drive(ToolResult(structured_content=env.model_dump(mode="json")),
                 enabled=True, monkeypatch=monkeypatch)
    sc = out.structured_content
    assert sc["error_category"] == ErrorCategory.BUILD_FAILURE.value
    assert sc["retryable"] is False
    assert "detail" not in sc  # null detail correctly omitted


def test_enabled_passes_superset_dict_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dict with a key the envelope does not define must NOT be stripped.
    result = ToolResult(structured_content={"object_id": "x", "status": "ok", "extra": 1})
    out = _drive(result, enabled=True, monkeypatch=monkeypatch)
    assert out is result  # untouched — extra key survives


def test_enabled_passes_non_dict_structured_content_through(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ToolResult(content=[])  # no structured_content
    out = _drive(result, enabled=True, monkeypatch=monkeypatch)
    assert out is result


def test_enabled_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    once = _drive(_full_collection(), enabled=True, monkeypatch=monkeypatch)
    twice = _drive(once, enabled=True, monkeypatch=monkeypatch)
    assert twice.structured_content == once.structured_content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/middleware/test_compact.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.mcp.middleware.compact`.

- [ ] **Step 3: Implement the middleware**

Create `src/kdive/mcp/middleware/compact.py`:

```python
"""Opt-in compact response envelope middleware (ADR-0314, #1035).

When KDIVE_COMPACT_RESPONSES is on, rebuild each tool result's envelope with the null/empty
defaulted fields omitted (recursively within ``items``), cutting per-call tokens. Registered
outermost in ``build_app`` so it observes the final ``ToolResult`` — including the failure
envelopes DenialAudit/BindingError synthesize. Default off: the result passes through unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from pydantic import ValidationError

from kdive.mcp.responses import ToolResponse
from kdive.mcp.verbosity import compact_responses_enabled

# The exact set of top-level envelope keys. A dict carrying any other key is not a ToolResponse
# dump and is passed through untouched — pydantic's default extra="ignore" would otherwise let a
# superset validate and silently drop its extra keys.
_ENVELOPE_FIELDS = frozenset(ToolResponse.model_fields)


class CompactResponseMiddleware(Middleware):
    """Omit null/empty defaulted envelope fields when KDIVE_COMPACT_RESPONSES is on (ADR-0314)."""

    async def on_call_tool(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
        """Compact the tool result's envelope when the flag is on; otherwise pass it through."""
        result = await call_next(context)
        if not compact_responses_enabled():
            return result
        return _compact_result(result)


def _compact_result(result: Any) -> Any:
    """Return `result` with its envelope compacted, or unchanged when it is not an envelope.

    Compacts only a ``ToolResult`` whose ``structured_content`` is a dict of envelope keys and
    validates as a ``ToolResponse``; ``model_dump(exclude_defaults=True)`` recurses into ``items``
    and keeps every non-default failure field. Rebuilding a ``ToolResult`` from only the compact
    ``structured_content`` regenerates a matching ``content`` text block, so both wire copies
    shrink. Anything else (a superset/non-envelope dict, a ``ValidationError``, non-dict content)
    is returned untouched — fail safe, never corrupt a response.
    """
    if not isinstance(result, ToolResult):
        return result
    sc = result.structured_content
    if not isinstance(sc, dict) or not set(sc) <= _ENVELOPE_FIELDS:
        return result
    try:
        envelope = ToolResponse.model_validate(sc)
    except ValidationError:
        return result
    compact = envelope.model_dump(mode="json", exclude_defaults=True)
    return ToolResult(structured_content=compact, meta=result.meta)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/middleware/test_compact.py -q`
Expected: PASS (8 cases).

- [ ] **Step 5: Run guardrails**

Run: `just lint type`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/middleware/compact.py tests/mcp/middleware/test_compact.py
git commit -m "feat(1035): add CompactResponseMiddleware envelope trimmer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Wire the middleware into build_app (outermost) + startup log + integration coverage

**Files:**
- Modify: `src/kdive/mcp/app.py`
- Modify: `tests/mcp/core/test_app.py`

**Interfaces:**
- Consumes: `CompactResponseMiddleware` (Task 2); `compact_responses_enabled` (Task 1).

- [ ] **Step 1: Write the failing wiring + integration tests**

Add to `tests/mcp/core/test_app.py` (module already imports `build_app`, `AsyncConnectionPool`, `SecretRegistry`, and has a `_verifier()` helper):

```python
def test_compact_middleware_is_registered_outermost() -> None:
    # CompactResponseMiddleware must be outer of DenialAudit + BindingError so it observes their
    # synthesized failure envelopes; first-added is outermost (ADR-0314).
    from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware
    from kdive.mcp.middleware.compact import CompactResponseMiddleware
    from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    order = [type(m).__name__ for m in app.middleware]
    assert order.index(CompactResponseMiddleware.__name__) < order.index(
        DenialAuditMiddleware.__name__
    )
    assert order.index(CompactResponseMiddleware.__name__) < order.index(
        BindingErrorMiddleware.__name__
    )


def test_build_app_logs_when_compact_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    with caplog.at_level("INFO", logger="kdive.mcp.app"):
        build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    assert sum("compact_responses enabled" in r.getMessage() for r in caplog.records) == 1


def test_build_app_silent_when_compact_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("KDIVE_COMPACT_RESPONSES", raising=False)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    with caplog.at_level("INFO", logger="kdive.mcp.app"):
        build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    assert not any("compact_responses enabled" in r.getMessage() for r in caplog.records)
```

`import pytest` is already present at `tests/mcp/core/test_app.py:9` and the new tests need no additional imports (they use the `caplog` fixture and `pytest.MonkeyPatch`/`pytest.LogCaptureFixture` hints). Do **not** add `import logging` to the test file — it is unused there and ruff would flag F401. The `logging` import belongs only in `src/kdive/mcp/app.py` (Step 3).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/core/test_app.py -k "compact" -q`
Expected: FAIL — `CompactResponseMiddleware` not in `app.middleware`; no log line.

- [ ] **Step 3: Wire it in**

In `src/kdive/mcp/app.py`:

Add imports near the other middleware imports:

```python
import logging

from kdive.mcp.middleware.compact import CompactResponseMiddleware
from kdive.mcp.verbosity import compact_responses_enabled
```

Add a module logger after the imports (top level, before `build_app`):

```python
_log = logging.getLogger(__name__)
```

In `build_app`, register `CompactResponseMiddleware` **unconditionally** as the **first** middleware — before `TelemetryMiddleware` — and emit the startup log only when the flag is on. Insert this immediately before the existing first `app.add_middleware(TelemetryMiddleware(...))` block:

```python
    app.add_middleware(CompactResponseMiddleware())  # first == outermost (ADR-0314)
    if compact_responses_enabled():
        _log.info("compact_responses enabled")
    app.add_middleware(
        TelemetryMiddleware(
            tracer=trace.get_tracer("kdive.mcp"), meter=metrics.get_meter("kdive.mcp")
        )
    )
```

Rationale: first-added is outermost, so it processes the response last — after Telemetry/Usage read the full envelope and after DenialAudit/BindingError synthesize their envelopes. **Register unconditionally** — the middleware's `on_call_tool` no-ops per call via `compact_responses_enabled()` when off (one negligible frame), so the ordering test can build the app with the flag off and still find the middleware, and a runtime flag flip takes effect without rebuilding the app. Only the `_log.info` is gated, satisfying the "silent when off" test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/core/test_app.py -k "compact" -q`
Expected: PASS (3 cases).

- [ ] **Step 5: Add gateway + synthesized-envelope integration tests**

Append to `tests/mcp/middleware/test_compact.py` an integration section that drives a real FastMCP app + `Client` (mirror the `Client(app)` usage in `tests/mcp/core/test_binding_error_middleware.py`). This is **verified to work** end-to-end (the gateway's `tools.invoke` re-enters the middleware chain with `run_middleware=True`, so the inner call compacts and the outer pass compacts again — idempotent; an unknown-tool call yields a handler-synthesized failure envelope that the outermost middleware compacts). Put the group imports at the top of the file with the other imports (not inline) to avoid ruff E402.

Add these imports to the file header (`json` is already imported from Task 2 Step 1):

```python
from fastmcp import Client, FastMCP

from kdive.mcp.tools.gateway import register as register_gateway
from kdive.providers.core.resolver import ProviderResolver
```

Then the integration tests:

```python
def _gateway_app() -> FastMCP:
    """A minimal app: Compact outermost, the real tools.invoke gateway, and one list tool."""
    app = FastMCP(name="probe")
    app.add_middleware(CompactResponseMiddleware())  # first == outermost
    register_gateway(app, resolver=ProviderResolver({}))

    @app.tool(name="images.list")
    async def images_list() -> ToolResponse:
        rows = [ToolResponse.success("img-0", "registered", data={"name": "n0"})]
        return ToolResponse.collection("images", "ok", rows)

    return app


def test_integration_gateway_routed_response_compacted(monkeypatch: pytest.MonkeyPatch) -> None:
    # tools.invoke re-enters the chain (run_middleware=True): inner + outer compaction pass.
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")

    async def _run() -> dict[str, Any]:
        async with Client(_gateway_app()) as client:
            res = await client.call_tool("tools.invoke", {"name": "images.list", "arguments": {}})
            return res.structured_content

    sc = asyncio.run(_run())
    assert "error_category" not in sc and "refs" not in sc
    row = sc["items"][0]
    assert set(row) == {"object_id", "status", "data"}  # inner row compacted too
    assert row["data"] == {"name": "n0"}


def test_integration_synthesized_failure_envelope_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unknown tool makes tools.invoke synthesize a full configuration_error envelope; the
    # outermost middleware must compact it (proving it wraps handler/downstream-synthesized results).
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")

    async def _run() -> dict[str, Any]:
        async with Client(_gateway_app()) as client:
            res = await client.call_tool("tools.invoke", {"name": "nope.missing", "arguments": {}})
            return res.structured_content

    sc = asyncio.run(_run())
    assert sc["error_category"] == "configuration_error"
    assert sc["retryable"] is False and sc["detail"]  # failure fields kept
    assert "refs" not in sc and "items" not in sc  # empty defaults compacted away
```

Note: the `register_gateway`/`ProviderResolver({})` combo logs a benign "no registered runtimes" warning — harmless for this test.

- [ ] **Step 6: Run the integration tests + guardrails**

Run: `uv run python -m pytest tests/mcp/middleware/test_compact.py tests/mcp/core/test_app.py -q`
Run: `just lint type`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/mcp/app.py tests/mcp/core/test_app.py tests/mcp/middleware/test_compact.py
git commit -m "feat(1035): register CompactResponseMiddleware outermost + startup log

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Document the compact mode in the response-envelope guide

**Files:**
- Modify: `docs/guide/response-envelope.md`
- Regenerate: packaged doc snapshot (via `just resources-docs`)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add a "Compact responses" section**

Append to `docs/guide/response-envelope.md` (after the fields/invariant sections) a section documenting:

```markdown
## Compact responses (opt-in)

When an operator sets `KDIVE_COMPACT_RESPONSES=on` (default `off`), the server omits
null/empty *defaulted* envelope fields from every tool response, recursively within `items`,
to cut per-call tokens on token-heavy list tools. The default is unchanged and byte-identical.

Under compaction:

- A field at its default is **omitted**: `error_category`/`retryable`/`detail` when null,
  and `suggested_next_actions`/`refs`/`items`/`data` when empty. `object_id` and `status`
  are always present.
- A failure envelope always keeps `error_category` and `retryable`. `detail` is kept only
  when non-null (a `not_found`/`authorization_denied` suppressed constant, or a reason the
  tool set); a worker-plane job-handle failure whose `detail` is null omits it.
- **Absent means default.** An omitted field is semantically identical to its documented
  default (empty list/dict, or null). A consumer must not read key-absence as a distinct
  "unknown" signal. This applies to first-party clients too — the `response.get("items", [])`
  idiom is compaction-safe, and a populated collection's `items` is never dropped.

The advertised output schema types every omittable field as optional/nullable, so compact
responses stay schema-valid.
```

- [ ] **Step 2: Verify no banned words**

Run: `rg -ni '\b(critical|crucial|essential|comprehensive|robust|elegant|sprint)\b' docs/guide/response-envelope.md`
Expected: no matches in the added section.

- [ ] **Step 3: Regenerate the packaged doc snapshot**

Run: `just resources-docs` (refreshes the packaged snapshot of `docs/guide/response-envelope.md`, ADR-0151).
Run: `just resources-docs-check docs-links`
Expected: pass; snapshot in sync.

- [ ] **Step 4: Commit**

`just resources-docs` regenerates the packaged snapshot at
`src/kdive/mcp/resources/_content/response-envelope.md` (the mirror of the source doc,
ADR-0151). Stage both by explicit path — inspect `git status` first, never `git add -A`:

```bash
git add docs/guide/response-envelope.md src/kdive/mcp/resources/_content/response-envelope.md
git commit -m "docs(1035): document opt-in compact response envelope

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Full-gate verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full PR gate**

Run: `just ci`
Expected: green — lint, type, lock-check, shell/workflow/mermaid lints, `docs-links`, `docs-paths`, `adr-status-check`, `docs-check`, `config-docs-check`, `config-guard`, `env-docs-check`, `resources-docs-check`, `chart-version-check`, and `test` all pass.

- [ ] **Step 2: If any generated-doc check is stale**

Re-run the matching regen recipe (`just config-docs` / `just resources-docs` / `just docs`), stage the explicit regenerated path, amend or add a `docs(1035)` commit, and re-run `just ci`.

---

## Self-Review

- **Spec coverage:** flag (Task 1) ✓; single-source reader (Task 1) ✓; middleware transform with exact-shape guard + exclude_defaults + meta forwarding (Task 2) ✓; outermost registration + startup log (Task 3) ✓; every acceptance criterion has a test — OFF byte-identical passthrough (T2 `test_disabled_...`), ON top-level+items omission (T2 `test_enabled_compacts_collection_and_items`), content-block compaction (T2 `test_enabled_compacts_content_text_block`), non-null-detail failure kept (T2 `test_enabled_preserves_direct_failure_fields`), from_job null-detail dropped (T2 `test_enabled_drops_null_detail_on_from_job_failure`), superset passthrough (T2 `test_enabled_passes_superset_dict_through_unchanged`), non-envelope passthrough (T2 `test_enabled_passes_non_dict_...`), idempotent (T2 `test_enabled_is_idempotent`), gateway-routed double pass (T3 `test_integration_gateway_routed_response_compacted`), synthesized failure envelope compacted (T3 `test_integration_synthesized_failure_envelope_compacted`), outermost ordering (T3 `test_compact_middleware_is_registered_outermost`), startup log on/off (T3 `test_build_app_logs_when_compact_enabled` / `_silent_when_compact_disabled`), docs + absent==default (Task 4), `just config-docs`/`just resources-docs`/`just ci` (Tasks 1/4/5) ✓.
- **Placeholder scan:** none — every code/test step shows full content.
- **Type consistency:** `compact_responses_enabled()`, `CompactResponseMiddleware`, `_compact_result`, `COMPACT_RESPONSES` names are used identically across tasks.

## Rollback

Pure additive opt-in. Rollback is `KDIVE_COMPACT_RESPONSES=off` (the default) or reverting the branch. No migration, no persisted state, no schema change.
