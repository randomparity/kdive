# ADR-0267: tool gateway — progressive disclosure + build/install/boot composite (#866)

- Status: Proposed
- Date: 2026-06-27

## Context

`build_app()` registers **83 tools across 18 namespaces** and `list_tools` returns the flat
catalog, RBAC-scoped per ADR-0148 but still ~70+ for an operator. LLM tool-selection accuracy
degrades with catalog size even at 128K context (LongFuncEval, cited in #506/ADR-0148). Separately,
the common reproduce flow is a ~12-call chain whose three `jobs.wait` polls after `build`,
`install`, and `boot` bracket a single bound Run (#866).

#866 proposes either a `wait:true` flag (catalog-neutral) or a composite tool (catalog-*growing*).
The two proposals pull opposite ways on the catalog cost. ADR-0148 already shipped the seam to
resolve this: the `on_list_tools` middleware (`mcp/middleware/exposure.py`), the central reviewed
`_TOOL_SCOPES` map (`mcp/exposure.py`), and the `tool_invocation` usage table — and stated that "a
future workflow/phase filter chains inside the same `on_list_tools` seam and intersects (never
widens) this result." The MCP client is the Claude family, which calls tools learned out-of-band
(the 1a model), not only tools returned by `list_tools`.

## Decision

Pair a happy-path composite with a progressive-disclosure gateway so the composite shrinks the
default surface instead of growing it, keeping the full 83-tool capability reachable on demand.

### 1. `runs.build_install_boot` composite

A CONTRIBUTOR tool orchestrating `build → install → boot → get` over an already-created,
already-bound Run. The step service functions (`server_build` enqueue path, `install_run`,
`boot_run` in `runs/steps.py`) **enqueue a job and return** — they do not block — so the composite
supplies the blocking: per phase it enqueues at the service layer (no MCP envelope re-entry) and
polls that job to terminal with the same primitive `jobs.wait` uses, then enqueues the next phase,
and reads the final Run with `get_run`. Each phase is enqueued with a deterministic per-phase
`idempotency_key` (`run_id` + phase) so a retried blocking call re-attaches to in-flight jobs
instead of double-enqueuing. It emits per-phase MCP progress notifications. On full success it
returns the terminal `runs.get` projection (boot outcome + artifacts pointer) in one response. On
the first non-`succeeded` phase it stops and returns `data.failed_phase` (`build`|`install`|`boot`)
with that phase's `job_id`, error, and `run_id`; recovery uses the granular tools. On caller-
supplied `timeout` expiry or a dropped connection it returns the in-flight phase + `job_id` and the
underlying jobs keep running — the agent reattaches via `runs.get`/`jobs.list`. It does not retry or
resume. `expected_boot_failure` is a create-time Run property (not a composite input); the boot
phase honors the Run's stored value unchanged. Scope deliberately starts post-`create`/`bind` —
capacity and System-selection are explicit agent decisions, not part of the reproduce step.

### 2. `tools.search` discovery tool (1a model)

A PUBLIC tool mapping a `query` to matching tools, returning per match the **full input schema +
description + name** — the exact `list_tools` payload, keyed by query, so the result is sufficient
to construct a call. The agent then calls the tool **directly by name**; searched tools are never
spliced into `list_tools` (the listing stays static, no `tools/list_changed` churn). Results are
RBAC-filtered (only invocable tools) but span all tiers — search is the escape hatch out of the
core set. Ranking is deterministic lexical over `name + description + curated keywords`
(`mcp/tool_index.py`, the `_TOOL_SCOPES` idiom); zero-result queries are logged for keyword
curation.

### 3. Core-set tier filter

`CORE_TOOLS: frozenset[str]` in `mcp/exposure.py`, intersected into
`ToolExposureMiddleware.on_list_tools` after the RBAC filter (`visible = rbac_visible ∩
CORE_TOOLS`). Core set: `tools.search`, `session.whoami`, `runs.build_install_boot`, `runs.create`,
`runs.get`, `runs.list`, `allocations.request`, `allocations.wait`, `systems.provision` — discovery
entry points, the composite, and reads in nearly every flow (tunable later from `tool_invocation`
data). The filter **fails open** to the full RBAC-scoped catalog on any error, and a
`KDIVE_MCP_TOOL_GATEWAY` config (default `on`) disables it for clients that cannot call an
unlisted tool. A guard test pins `CORE_TOOLS ⊆` the live registry.

### 4. Server `instructions` table of contents

`FastMCP(name="kdive", …)` (currently no `instructions`, `mcp/app.py:33`) gains `instructions`
carrying (a) the gateway pattern — not every tool is listed; `tools.search` by capability then call
directly — and (b) a namespace table of contents (the 18 namespaces with one-liners). The TOC
restores the ambient workflow map a flat catalog gave for free, at ~18 lines instead of 83 schemas,
so the agent knows a capability exists and is worth searching for. A guard test asserts every live
namespace appears.

## Consequences

- Default `list_tools`: 83 → ~9 (then RBAC-scoped). Happy path over a bound Run: 3 job calls + 3
  polls → 1. Surface: +2 registered tools (`tools.search` PUBLIC, `runs.build_install_boot`
  CONTRIBUTOR); ~76 tools demoted to searchable-only. No capability removed — every tool reachable
  via `tools.search` at native schema fidelity.
- Verification: surface deltas are asserted by guard/integration tests (core-set listing, demoted-
  tool search-then-call, idempotent re-call enqueues one build); the unfalsifiable accuracy goal is
  carried by two `tool_invocation` signals — composite success/failure-phase distribution and a
  searched-but-never-invoked counter that makes an incompatible client detectable, not silent.
- `UsageTrackingMiddleware` attribution is unchanged: under 1a the searched-then-called tool runs
  for real, so each call still writes its own `tool_invocation` row (no proxy to re-attribute).
- **Not a security control.** An agent could already call any registered tool by name under
  ADR-0148's fail-open advisory filter; this adds no new exposure. Execution-time `require_role` /
  the destructive-op gate remain the only boundary.
- No DB migration: `CORE_TOOLS`, curated keywords, and the TOC are code maps; search-miss is logged.
- **Risk — the 1a client bet.** A client that only calls `list_tools` tools cannot reach demoted
  tools; mitigated by the `instructions` pattern, the `KDIVE_MCP_TOOL_GATEWAY=off` valve, and
  fail-open. **Risk — index quality**: a poor keyword map silently strips capability; mitigated by
  curated keywords, search-miss telemetry, and the TOC.

## Rejected alternatives

- **`wait:true` flag only (#866 option 1):** solves call ceremony, not catalog size (the reframed
  cost).
- **Composite without demotion:** grows the catalog.
- **Dispatcher/proxy meta-tool (1b):** universal client compat, but collapses 83 typed schemas into
  one opaque `args` and re-implements validation + telemetry inside the proxy.
- **`action`-enum verb collapse:** cuts count but trades catalog size for per-tool schema ambiguity.
- **Injecting searched tools into `list_tools`:** `tools/list_changed` churn and cache
  invalidation; the direct-call model avoids it.
- **`runs.reproduce` name:** implies crash-only; the composite equally validates a clean boot.
