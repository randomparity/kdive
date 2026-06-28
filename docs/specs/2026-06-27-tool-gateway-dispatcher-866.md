# Tool gateway redo: `tools.invoke` dispatcher + build/install/boot composite (#866)

- Issue: #866
- ADR: [ADR-0268](../adr/0268-tool-gateway-dispatcher.md)
- Supersedes: the reverted 1a gateway (ADR-0267, PR #877 → reverted PR #881)
- Status: Draft

## Problem

Two costs compound on the MCP surface, and #866 frames only the first:

1. **Call ceremony.** The common "build this tree, boot it, tell me if it crashes" flow is a
   ~12-call chain with four identical `jobs.wait` polls
   (`allocations.request → systems.provision → jobs.wait → runs.create → runs.build →
   jobs.wait → runs.install → jobs.wait → runs.boot → jobs.wait → runs.get →
   artifacts.search_text`). Three of those polls — after `build`, `install`, `boot` — bracket a
   single bound Run and are pure round-tripping.
2. **Catalog size.** `build_app()` registers **83 tools across 18 namespaces** and `list_tools`
   returns the whole flat catalog (RBAC-scoped per ADR-0148, but still ~70+ for an operator). LLM
   tool-selection accuracy degrades with catalog size even at 128K context (LongFuncEval, cited in
   #506 / ADR-0148).

The two #866 proposals pull opposite ways on cost 2: a `wait:true` flag is catalog-neutral, a
composite *adds* surface. This spec resolves the tension by pairing the composite with a
**progressive-disclosure gateway** so the composite shrinks the default surface, and the full
capability stays reachable on demand.

### Why this is a redo

ADR-0267 (PR #877) shipped exactly this pairing but on the **1a model** — the agent calls a tool
learned from `tools.search` *directly by name*, without it appearing in `tools/list`. That is
unsupported on the target client and was reverted (PR #881):

- MCP spec (2025-06-18) makes `tools/list` the only path to a callable tool; an unknown name is
  `-32602`.
- Claude Code exposes only `tools/list` tools to the model; `tools/list_changed` is
  documented-but-reportedly-unwired (anthropics/claude-code#13646).

The ~76 demoted tools became **discoverable-but-uninvocable**, dead-ending cold start. The redo
switches to the **1b model**: a server-side dispatcher (`tools.invoke`). The client only ever calls
meta-tools that *are* listed; the inner tool is dispatched server-side, so `-32602` cannot occur.

## Goals

1. The common reproduce flow over a bound Run is **one tool call**, not three job-bearing calls plus
   three polls.
2. The default `list_tools` catalog is a small **core set** (~10 tools), not the full 83.
3. Every non-core tool stays **fully invocable** at native validation fidelity — no capability
   removed, only deferred behind discovery + dispatch that the client can actually drive.
4. The mechanism reuses the ADR-0148 `on_list_tools` seam, intersects (never widens) the RBAC
   filter, and **fails open** to the full catalog on any error.

## Non-goals

- **The 1a "call-by-name-off-list" model.** Reverted; unreachable on the target client. This spec
  is the 1b dispatcher.
- **A `tools/list_changed` injection model.** Spec-correct but unproven on the client; a separate
  empirical spike, not this change.
- **`wait:true` on granular `runs.build`/`install`/`boot`.** The composite removes ceremony for the
  happy path; inline-block on the granular recovery tools is an incremental follow-up.
- **Semantic/embedding search.** At 83 tools a deterministic lexical index is sufficient and
  testable.
- **A security control.** List filtering is an accuracy/UX optimisation; execution-time
  `require_role` / the destructive-op gate remain the only boundary (ADR-0148).
- **Resumable composite state.** Fire-and-forget happy path; recovery uses the granular tools.

## Design

### 1. `tools.invoke` — the dispatcher (the 1b pivot)

A PUBLIC tool, `tools.invoke(name: str, arguments: object | None)`, that re-enters the server's own
call path:

```python
@app.tool(name="tools.invoke", annotations=_docmeta.mutating())
async def tools_invoke(name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
    try:
        return await app.call_tool(name, arguments or {}, run_middleware=True)
    except NotFoundError:
        return _unknown_tool_response(name)   # configuration_error → "use tools.search"
```

- **Re-entrancy is the crux.** FastMCP 3.4.2's `call_tool(..., run_middleware=True)` runs the inner
  tool through the full middleware chain. So inner-tool **input validation** (native
  `ValidationError` on bad `arguments`), **`require_role` / destructive-op enforcement**,
  **`BindingErrorMiddleware` / `DenialAuditMiddleware` envelope mapping**, and the
  **`tool_invocation` telemetry row** all happen natively — nothing is re-implemented in the proxy.
- **RBAC.** `tools.invoke` is PUBLIC; the *inner* call enforces the inner tool's real role. An agent
  could already call any registered tool by name under ADR-0148's fail-open filter, so this adds no
  exposure.
- **Context propagation.** `call_tool` opens a fresh `fastmcp` `Context`, but kdive's
  `current_context()` is a contextvar set from the verified JWT, not from that object, so it
  survives the nested call on the same task. A test pins this (a viewer invoking an operator tool
  through the gateway is denied identically to a direct call).
- **Unknown name.** `NotFoundError` → `configuration_error` envelope naming `tools.search`, so a
  hallucinated name self-corrects instead of dead-ending.

### 2. `tools.search` — discovery

A PUBLIC tool returning, per match, the **full input schema + description + name** — sufficient to
construct a `tools.invoke` call, not a name hint. Reuses the schema-serialisation path that feeds
`list_tools`.

- **Two modes:** lexical `query` ranking, and `namespace="runs"` browse (all `runs.*`) as the
  enumeration safety net against ranking misses — under 1b an unfindable tool is unreachable, so
  browse guarantees a floor.
- **RBAC-filtered, all tiers.** Only tools the caller could invoke, spanning all tiers (search is
  the escape hatch out of the core set).
- **Ranking.** Deterministic lexical over `name + description + curated keywords`
  (`mcp/tool_index.py`, the `_TOOL_SCOPES` idiom); `limit`-capped; defaults to tokenised
  name+description when a tool has no keyword entry.
- **Search-miss telemetry.** Zero-result queries are logged structured (query + count) — the
  "agent reached for a capability it could not find" signal, the feedback loop for keyword curation.

### 3. `runs.build_install_boot` — the composite

OPERATOR tool orchestrating `build → install → boot → get` over an **already-created,
already-bound** Run, blocking each phase to terminal internally. Calls the service layer
(`_build_run` / `_install_run` / `_boot_run` / `_get_run`, reusing `_build_handlers`), not the MCP
tools, so there is no envelope re-entry for the composite's own steps.

- **Input:** `run_id` (created, bound, not-yet-built). `expected_boot_failure` passthrough unchanged
  — a matched expected crash is a success exactly as in `runs.boot`.
- **Scope.** Post-`create`/`bind`. Capacity, System selection, and reuse are explicit agent
  decisions; the three same-shaped job steps over one bound Run are the ceremony #866 names. A full
  `request→boot` mega-composite is rejected (conflates capacity into the reproduce step).
- **Progress.** Per-phase MCP progress notifications (phase name + underlying job state) so a
  multi-minute block is not blind.
- **Success contract.** Returns the terminal `runs.get` projection (boot outcome + artifacts
  pointer) in one response.
- **Failure contract.** Stops at the first phase not reaching `succeeded`; returns a terminal
  envelope with `data.failed_phase` (`build`|`install`|`boot`), that phase's `job_id` and error, and
  `run_id`. Recovery uses the granular tools (discovered via `tools.search`). No retry, no resume.

Rejected name `runs.reproduce` (implies crash-only; the tool equally validates a clean boot).

### 4. Core-set tier filter

`CORE_TOOLS: frozenset[str]` in `mcp/exposure.py`, chained into `ToolExposureMiddleware.on_list_tools`
after the RBAC filter:

```
visible = rbac_visible(ctx, names) ∩ CORE_TOOLS        # gateway on
```

| tool | why core |
|------|----------|
| `tools.search` | discovery entry point |
| `tools.invoke` | dispatch entry point |
| `session.whoami` | orient: caller project/roles |
| `runs.build_install_boot` | the happy-path composite |
| `runs.create` | mint the Run the composite runs |
| `runs.get` / `runs.list` | read terminal state / find Runs |
| `allocations.request` / `allocations.wait` | capacity entry + its poll |
| `systems.provision` | bring a System up to bind |

- **Fail-open.** On any tier-filter error, or `KDIVE_MCP_TOOL_GATEWAY=off`, `on_list_tools` returns
  the full RBAC-scoped catalog (ADR-0148 behaviour), never empty/broken.
- **Completeness guard.** A test asserts `CORE_TOOLS ⊆` the live registry, alongside the existing
  `CLASSIFIED_TOOLS | PUBLIC_TOOLS` guard.

### 5. Server `instructions` — table of contents

Add `instructions` to `FastMCP(name="kdive", …)` carrying (1) the gateway pattern — not every tool
is listed; `tools.search` by capability, then `tools.invoke(name, arguments)` — and (2) a namespace
TOC (the 18 namespaces with one-liners), restoring the ambient workflow map at ~18 lines instead of
83 schemas. A reviewed constant (`mcp/tool_index.py`) with a guard test asserting every live
namespace appears.

### 6. Meta-tool skip-set (telemetry correctness)

Because `run_middleware=True` re-runs the chain, the outer `tools.invoke` / `tools.search` **and**
the inner tool would each write a `tool_invocation` row. `UsageTrackingMiddleware._record` and
`TelemetryMiddleware` skip when `context.message.name ∈ {tools.invoke, tools.search}`, so exactly
one row per real call is written, keyed to the inner tool with correct project/outcome. `_call_project`
already reads `arguments["project"]`, which on the re-entered inner call is the real argument dict —
so project attribution is correct without further change.

## Surface delta

- **New tools:** `tools.search` (PUBLIC), `tools.invoke` (PUBLIC), `runs.build_install_boot`
  (OPERATOR) → +3 registered.
- **Default `list_tools`:** 83 → ~10 (core set, then RBAC-scoped).
- **Demoted (gateway-reachable, not removed):** ~76 tools incl. `runs.build`/`install`/`boot`.
- **Capability:** unchanged — every tool reachable via `tools.search` + `tools.invoke` at native
  validation fidelity.
- **Calls for the happy path over a bound Run:** 3 job calls + 3 polls → 1.

## Telemetry

One `tool_invocation` row per real call, written by the re-entered inner call; meta-tools skipped.
`tools.search` zero-result queries emit a structured log for keyword curation. A `tool_invocation`
schema column for search is out of scope (additive later).

## RBAC / migration

- `tools.search`, `tools.invoke`: PUBLIC — the inner call / returned schemas reflect the caller's
  real RBAC.
- `runs.build_install_boot`: OPERATOR — the max role of its constituent steps.
- No DB migration: `CORE_TOOLS`, curated keywords, and the TOC are code maps; search-miss is logged.

## Verification (gate — the revert lesson)

ADR-0267 merged without verifying the invocation path on the real client. This change is **not
default-on until**:

1. A unit/integration test proves `tools.invoke` re-entry preserves inner-tool validation, RBAC
   denial, and single-row telemetry attribution (viewer-through-gateway denied identically to
   direct).
2. A **cold-start end-to-end** run against the Claude Code client: starting from the ~10-tool core
   listing, discover the granular tools via `tools.search`, drive a full reproduce (mint → build →
   install → boot, including at least one demoted tool via `tools.invoke`) to terminal state, with
   the result recorded in the PR.

The gateway ships behind `KDIVE_MCP_TOOL_GATEWAY` so verification runs before the default flips.

## Risks

- **Annotation/consent collapse.** A per-tool-prompting client sees only `tools.invoke` and cannot
  distinguish a read from a destructive teardown at prompt time. Mitigation: the server-side
  destructive-op gate (ADR-0043) still fires on the inner call and remains the boundary;
  `tools.invoke` is annotated conservatively (`mutating`). Documented, not eliminated.
- **Discovery-index quality.** A bad keyword map silently strips capability. Mitigations: curated
  keywords, the `namespace` browse mode as a floor, search-miss telemetry, and the TOC.
- **Mis-sequencing.** Hiding the flat catalog hides implicit workflow order. Mitigations: the
  composite encodes the dominant sequence; the TOC + `instructions` describe the phases.
- **Opaque `arguments` at selection time** (the accepted 1b cost). Mitigation: `tools.search`
  returns full schemas; a malformed call fails with the inner tool's native `ValidationError`.

## Alternatives rejected

- **1a gateway (reverted ADR-0267).** Unreachable on Claude Code.
- **`tools/list_changed` injection.** Spec-correct but unproven on the client; needs a spike.
- **`run_middleware=False` + manual proxy.** Re-implements validation/RBAC/telemetry the stack does.
- **`wait:true` flag only (#866 option 1).** Solves ceremony, not catalog size.
- **Composite without demotion.** Grows the catalog.
- **`action`-enum verb collapse.** Trades catalog size for per-tool schema ambiguity.
