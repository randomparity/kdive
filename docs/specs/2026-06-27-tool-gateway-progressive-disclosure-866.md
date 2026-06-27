# Tool gateway: progressive disclosure + build/install/boot composite (#866)

- Issue: #866
- ADR: [ADR-0267](../adr/0267-tool-gateway-progressive-disclosure.md)
- Status: Draft

## Problem

Two costs compound on the MCP surface, and #866 frames only the first:

1. **Call ceremony.** The common "build this tree, boot it, tell me if it crashes" flow is a
   ~12-call chain with four identical `jobs.wait` polls
   (`allocations.request → systems.provision → jobs.wait → runs.create → runs.build →
   jobs.wait → runs.install → jobs.wait → runs.boot → jobs.wait → runs.get →
   artifacts.search_text`). Three of those four polls — after `build`, `install`, `boot` —
   bracket a single bound Run and are pure round-tripping.
2. **Catalog size.** `build_app()` registers **83 tools across 18 namespaces** and
   `list_tools` returns the whole flat catalog (RBAC-scoped per ADR-0148, but still ~70+ for an
   operator). LLM tool-selection accuracy degrades with catalog size even at 128K context (the
   LongFuncEval result cited in #506 / ADR-0148). A large flat catalog makes the model likelier
   to mis-pick or mis-sequence.

The two #866 proposals pull in opposite directions on cost 2: a `wait:true` flag is catalog-
neutral, while a composite tool *adds* surface. This spec resolves the tension by pairing the
composite with a **progressive-disclosure gateway** so the composite shrinks the default surface
instead of growing it, and the full 83-tool capability stays reachable on demand.

This builds directly on machinery ADR-0148 already shipped: the `on_list_tools` filtering seam
(`mcp/middleware/exposure.py`), the central reviewed classification map (`mcp/exposure.py`
`_TOOL_SCOPES`), and the `tool_invocation` usage table. ADR-0148 explicitly anticipated this:
"a future workflow/phase filter chains inside the same `on_list_tools` seam and intersects
(never widens) this result."

## Goals

1. The common reproduce flow over a bound Run is **one tool call**, not three job-bearing calls
   plus three polls.
2. The default `list_tools` catalog is a small **core set** (~9 tools), not the full 83.
3. Every non-core tool stays **fully reachable** at native schema fidelity — no capability is
   removed, only deferred behind discovery.
4. The mechanism reuses the ADR-0148 `on_list_tools` seam and intersects (never widens) the RBAC
   filter, and **fails open** to the full catalog on any error.

## Non-goals

- **A `tools/list_changed` injection model.** Searched tools are called *directly by name*, never
  spliced into `list_tools`; the listing stays static (no client cache churn). This is the 1a
  client model (search-then-call-directly), chosen because kdive's primary client is the
  Claude family, which calls tools learned out-of-band. A dispatcher/proxy tool (1b) that wraps
  every call was rejected — it collapses 83 typed schemas into one opaque `args` and forces
  re-implementing validation and per-tool telemetry inside the proxy.
- **`wait:true` on the granular `runs.build`/`install`/`boot`.** The composite removes the
  ceremony for the happy path; adding inline-block to the granular tools (used only on the
  recovery escape-hatch) is an incremental follow-up, not part of this change.
- **Semantic/embedding search.** At 83 tools a deterministic lexical index is sufficient and
  testable; embeddings are rejected (non-determinism, latency, infra).
- **A security control.** Like ADR-0148, list filtering is an accuracy/UX optimisation. An agent
  could already call any registered tool by name under ADR-0148's fail-open advisory filter; this
  change introduces **no new exposure**. Execution-time `require_role` / the destructive-op gate
  remain the only boundary.
- **Resumable composite state.** The composite is a fire-and-forget happy path; recovery uses the
  granular tools. Making it resumable would duplicate the Run state machine.

## Design

### 1. `runs.build_install_boot` — the composite (raises the floor)

A single OPERATOR tool that orchestrates `build → install → boot → get` over an
**already-created, already-bound** Run, blocking each phase to terminal internally. It calls the
service layer (`_build_run` / `_install_run` / `_boot_run` / `_get_run` — confirmed thin under the
existing `runs.*` handlers), not the MCP tools, so there is no envelope re-entry.

- **Input:** `run_id` (a created, bound, not-yet-built Run). Optional `expected_boot_failure`
  passthrough is unchanged — a matched expected crash is a success exactly as in `runs.boot`.
- **Scope (boundary).** Deliberately starts post-`create`/post-`bind`. `allocations.request`,
  `systems.provision`, `runs.create`, `runs.bind` involve capacity, system selection, and reuse
  decisions an agent should make explicitly; the three job-bearing same-shaped steps over one
  bound Run are the ceremony #866 names. A full `request→boot` mega-composite was rejected for
  conflating capacity decisions into the reproduce step.
- **Progress.** Emits MCP progress notifications per phase (phase name + underlying job state) so a
  multi-minute block is not blind.
- **Success contract.** Returns the terminal `runs.get` projection (same shape) — boot outcome
  plus the artifacts pointer — in one response.
- **Failure contract.** Stops at the first phase that does not reach `succeeded`, and returns a
  terminal envelope carrying `data.failed_phase` (`build` | `install` | `boot`), that phase's
  `job_id` and error, and `run_id`. The agent then drops into the granular tools (which it
  discovers via `tools.search`) to inspect or retry. The composite does not retry or resume.

Rejected name `runs.reproduce` (implies crash-only; the tool equally validates a clean boot).

### 2. `tools.search` — the discovery tool (handles the ceiling)

A PUBLIC tool that maps a natural-language/keyword `query` to the matching tools and returns, per
match, the **full input schema + description + name** — the exact payload `list_tools` would have
returned for that tool. This is the crux of the 1a model: the search result must be *sufficient to
construct a valid call*, not a name hint. It reuses the same schema-serialisation path that feeds
`list_tools`, keyed by query.

- **RBAC-filtered, not tier-filtered.** Results include only tools the caller could invoke under
  its grants (reusing `mcp/exposure.py`), but span *all* tiers — search is the escape hatch out of
  the core set, so a core-demoted tool must be findable.
- **Ranking.** Deterministic lexical match over `name + description + curated keywords`, ranked by
  match strength, `limit`-capped. Curated keywords live in a central reviewed map
  (`mcp/tool_index.py`, the `_TOOL_SCOPES` idiom), defaulting to tokenised name+description when a
  tool has no entry.
- **Search-miss telemetry.** A zero-result query is logged structured (query + result count) — the
  new "agent reached for a capability it could not find," analogous to the denied-tool signal
  ADR-0148 prized. This is the feedback loop for curating keywords. A `tool_invocation` schema
  column is out of scope (additive later).

### 3. Core-set tier filter (shrinks the default surface)

Add `CORE_TOOLS: frozenset[str]` to `mcp/exposure.py` and chain a tier intersection into the
existing `ToolExposureMiddleware.on_list_tools` after the RBAC filter:

```
visible = rbac_visible(ctx, names) ∩ CORE_TOOLS      # gateway on
```

Proposed core set (discovery entry points + the composite + reads in nearly every flow; tunable
later from `tool_invocation` data):

| tool | why core |
|------|----------|
| `tools.search` | discovery entry point |
| `session.whoami` | orient: caller project/roles |
| `runs.build_install_boot` | the happy-path composite |
| `runs.create` | mint the Run the composite runs |
| `runs.get` / `runs.list` | read terminal state / find Runs |
| `allocations.request` / `allocations.wait` | capacity entry + its poll |
| `systems.provision` | bring a System up to bind |

Everything else (`runs.build`/`install`/`boot`, all of `debug.*`, `accounting.*`, `investigations.*`,
etc.) is registered, RBAC-scoped, and **searchable** — demoted from the default listing, not
removed.

- **Fail-open.** On any tier-filter error, or when the gateway is disabled, `on_list_tools` returns
  the full RBAC-scoped catalog (ADR-0148 behaviour), never an empty/broken listing.
- **Escape valve.** A config switch `KDIVE_MCP_TOOL_GATEWAY` (default `on`) disables the tier
  intersection and restores the full RBAC-scoped catalog, for a client that cannot call a tool it
  did not receive from `list_tools` (the 1a compatibility bet's release valve).
- **Completeness guard.** A test asserts `CORE_TOOLS ⊆` the live registry, alongside the existing
  `CLASSIFIED_TOOLS | PUBLIC_TOOLS` guard, so a renamed/removed core tool fails loudly.

### 4. Server `instructions` — the table of contents (prevents "forgot to search")

`FastMCP(name="kdive", …)` currently sets no `instructions` (`mcp/app.py:33`). Add `instructions`
carrying:

1. **The gateway pattern**, stated plainly: not every tool appears in `list_tools`; use
   `tools.search` by capability to load any tool's schema, then call it directly by name.
2. **A namespace table of contents** — the 18 namespaces with one-liners
   (`debug.* — live kernel debugging`, `accounting.* — budgets/quotas/usage`, …). This restores the
   ambient workflow map that a flat catalog gave for free, at ~18 cheap lines instead of 83 schemas,
   so the agent knows a capability *exists* and is worth searching for.

The TOC is a reviewed constant (`mcp/tool_index.py`) with a guard test asserting every live
namespace appears.

## Surface delta

- **New tools:** `tools.search` (PUBLIC), `runs.build_install_boot` (OPERATOR) → +2 registered.
- **Default `list_tools`:** 83 → ~9 (core set, then RBAC-scoped).
- **Demoted (searchable, not removed):** ~76 tools incl. `runs.build`/`install`/`boot`.
- **Capability:** unchanged — every tool reachable via `tools.search` at native schema fidelity.
- **Calls for the happy path over a bound Run:** 3 job calls + 3 polls → 1.

## Telemetry

`UsageTrackingMiddleware` already records one `tool_invocation` row per real call; under 1a the
searched-then-called tool runs for real, so attribution is unchanged (no proxy to re-attribute).
`tools.search` is itself recorded; zero-result searches additionally emit a structured log for
keyword curation.

## RBAC / migration

- `tools.search`: PUBLIC — it returns only schemas the caller's RBAC already permits.
- `runs.build_install_boot`: OPERATOR — the max role of its constituent steps.
- No DB migration: `CORE_TOOLS`, curated keywords, and the TOC are code maps; search-miss is logged.

## Risks

- **Client compatibility (the 1a bet).** A client that only lets its model call tools from
  `list_tools` would not reach demoted tools. Mitigations: the explicit `instructions` pattern, the
  `KDIVE_MCP_TOOL_GATEWAY=off` escape valve, and fail-open on filter error.
- **Discovery-index quality.** A bad keyword map silently strips capability (agent searches, finds
  nothing, gives up). Mitigations: curated keywords, search-miss telemetry as the correction loop,
  and the TOC so the agent knows what to search for.
- **Mis-sequencing.** Hiding the flat catalog hides the implicit workflow order. Mitigations: the
  composite encodes the dominant sequence; the TOC + `instructions` describe the phases.

## Alternatives rejected

- **`wait:true` flag only (#866 option 1).** Solves call ceremony, not catalog size — the larger
  cost the user reframed around.
- **Composite without demotion.** Adds surface; makes catalog size worse.
- **Dispatcher/proxy meta-tool (1b).** Universal client compatibility, but collapses typed schemas
  into one opaque `args` and re-implements validation + telemetry inside the proxy.
- **`action`-enum verb collapse** (`runs.run(action=build|install|boot)`). Cuts tool count
  mechanically but trades catalog size for per-tool schema ambiguity.
- **Injecting searched tools into `list_tools`.** Causes `tools/list_changed` churn and client cache
  invalidation; the direct-call model avoids it.
