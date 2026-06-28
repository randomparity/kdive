# ADR-0268: tool gateway redo — `tools.invoke` dispatcher (1b) + build/install/boot composite (#866)

- Status: Proposed
- Date: 2026-06-27
- Supersedes: ADR-0267 (reverted in full, PR #881)

## Context

Two costs compound on the MCP surface and #866 frames only the first:

1. **Call ceremony.** The common reproduce flow is a ~12-call chain; three identical
   `jobs.wait` polls after `build`/`install`/`boot` bracket a single bound Run and are pure
   round-tripping.
2. **Catalog size.** `build_app()` registers **83 tools across 18 namespaces** and `list_tools`
   returns the flat catalog (RBAC-scoped per ADR-0148, still ~70+ for an operator). LLM
   tool-selection accuracy degrades with catalog size even at 128K context (LongFuncEval, cited
   in #506 / ADR-0148).

ADR-0267 (PR #877) paired a `runs.build_install_boot` composite with a progressive-disclosure
gateway, but rested on the **1a model**: the client invokes a tool learned out-of-band from
`tools.search` output, never appearing in `tools/list`. That assumption is **false on the target
client** and the PR was reverted in full (PR #881):

- The MCP spec (2025-06-18) sanctions only tools advertised via `tools/list`; an unknown tool name
  returns `-32602`. "Read a schema from another tool's output, then call it by name" is not a
  supported pattern.
- The Claude Code client exposes only `tools/list` tools to the model; its `tools/list_changed`
  handling is documented-but-reportedly-unwired (anthropics/claude-code#13646).

With the gateway defaulting on, the ~76 demoted tools became **discoverable-but-uninvocable**,
dead-ending the cold-start workflow. The 1a assumption was never empirically verified before merge.

This redo adopts the **1b model**: a server-side dispatcher tool. The client only ever calls
meta-tools that *are* in `tools/list`; the inner tool is dispatched **server-side**, so `-32602`
cannot occur and the discoverable-but-uninvocable dead-end is structurally impossible. ADR-0267's
objection to 1b — "collapses typed schemas into one opaque `args` and re-implements validation +
telemetry inside the proxy" — is **half-overturned**: FastMCP 3.4.2 exposes a public re-entrant
`app.call_tool(name, arguments, *, run_middleware=True)` (the flag exists precisely for calls from
inside the stack), so the inner tool runs through the full middleware chain and validation, RBAC,
error envelopes, and `tool_invocation` telemetry are **native, not re-implemented**. The residual
1b cost — typed schemas are not visible at *selection* time — is real and accepted; `tools.search`
returns full schemas so the agent can construct a valid call, and a malformed call fails with the
inner tool's native `ValidationError`.

## Decision

Pair a happy-path composite with a 1b dispatcher gateway so the composite shrinks the default
surface instead of growing it, and the full 83-tool capability stays reachable through a tool the
client can actually call.

### 1. `tools.invoke(name, arguments)` — the dispatcher

A PUBLIC tool that re-enters the server's own call path:

```python
return await app.call_tool(name, arguments or {}, run_middleware=True)
```

`run_middleware=True` runs the inner tool through the whole stack, so inner-tool input validation,
`require_role` / destructive-op enforcement, error-envelope mapping, and the `tool_invocation` row
all happen natively. `tools.invoke` itself is PUBLIC because the **inner** call enforces the inner
tool's real RBAC. `NotFoundError` (unknown/disabled inner name) maps to a `configuration_error`
envelope that points the caller at `tools.search`.

### 2. `tools.search(query, namespace?, limit?)` — discovery

A PUBLIC tool mapping a query to matching tools, returning per match the **full input schema +
description + name** — the same payload `list_tools` would return — so the result is sufficient to
build a `tools.invoke` call. Two modes: lexical `query` ranking, and `namespace` browse (all
`runs.*`) as the enumeration safety net. Results are **RBAC-filtered** (only invocable tools) but
span **all tiers** — search is the escape hatch out of the core set. Ranking is deterministic
lexical over `name + description + curated keywords` (`mcp/tool_index.py`, the `_TOOL_SCOPES`
idiom). Zero-result queries are logged structured for keyword curation.

### 3. `runs.build_install_boot` — the composite

An OPERATOR tool orchestrating `build → install → boot → get` over an **already-created,
already-bound** Run via the service layer (`_build_run` / `_install_run` / `_boot_run` / `_get_run`
— thin under the existing `runs.*` handlers, reusing the `_build_handlers` helper), each phase
blocking to terminal internally. It emits per-phase MCP progress notifications. On full success it
returns the terminal `runs.get` projection (boot outcome + artifacts pointer) in one response. On
the first non-`succeeded` phase it stops and returns `data.failed_phase`
(`build`|`install`|`boot`) with that phase's `job_id`, error, and `run_id`; recovery uses the
granular tools. It does not retry or resume. `expected_boot_failure` passes through unchanged.
Scope starts post-`create`/`bind` — capacity and System selection are explicit agent decisions, not
part of the reproduce step.

### 4. Core-set tier filter

`CORE_TOOLS: frozenset[str]` in `mcp/exposure.py`, intersected into
`ToolExposureMiddleware.on_list_tools` after the RBAC filter (`visible = rbac_visible ∩
CORE_TOOLS`). Core set: `tools.search`, `tools.invoke`, `session.whoami`,
`runs.build_install_boot`, `runs.create`, `runs.get`, `runs.list`, `allocations.request`,
`allocations.wait`, `systems.provision` — discovery + dispatch entry points, the composite, and
reads in nearly every flow (tunable later from `tool_invocation` data). The filter **fails open**
to the full RBAC-scoped catalog on any error; `KDIVE_MCP_TOOL_GATEWAY` (default `on`) disables the
tier intersection. A guard test pins `CORE_TOOLS ⊆` the live registry.

### 5. Server `instructions` table of contents

`FastMCP(name="kdive", …)` (currently no `instructions`) gains `instructions` carrying (a) the
gateway pattern — not every tool is listed; `tools.search` by capability, then `tools.invoke` — and
(b) a namespace table of contents (the 18 namespaces with one-liners), restoring the ambient
workflow map at ~18 lines instead of 83 schemas. A guard test asserts every live namespace appears.

### 6. Meta-tool skip-set in usage + telemetry middleware

Because the re-entered inner call re-runs the chain, the outer `tools.invoke` / `tools.search` call
**and** the inner tool would each record a `tool_invocation` row. `UsageTrackingMiddleware` and
`TelemetryMiddleware` skip recording when `context.message.name ∈ {tools.invoke, tools.search}`, so
exactly one row per real call is written — keyed to the inner tool with correct project/outcome.
This *improves* the ADR-0148 usage data: it measures real work, not dispatcher noise.

### 7. Empirical verification gate (revert lesson)

ADR-0267 merged the gateway without verifying the invocation path on the real client. This redo is
**not default-on until** a cold-start end-to-end reproduce flow (discover → invoke through the
gateway → terminal state) is run against the Claude Code client and recorded. The gateway ships
behind `KDIVE_MCP_TOOL_GATEWAY` so the verification can run before the default flips.

## Consequences

- Default `list_tools`: 83 → ~10 (then RBAC-scoped). Happy path over a bound Run: 3 job calls + 3
  polls → 1. Surface: +3 registered tools; ~76 tools demoted to gateway-reachable.
- **Capability unchanged** — every tool reachable via `tools.search` + `tools.invoke` at native
  validation fidelity. Unlike the reverted 1a gateway, demoted tools are *invocable*.
- **Telemetry** native and clean (skip-set keeps one row per real call).
- **Annotation/consent granularity collapses.** A client that prompts per tool sees only
  `tools.invoke`, so it cannot distinguish a read from a destructive teardown at prompt time. The
  server-side destructive-op gate (ADR-0043) still fires on the inner call and remains the real
  boundary; only the client-side prompt granularity degrades. `tools.invoke` is annotated
  conservatively (`mutating`).
- **Not a security control.** An agent could already call any registered tool under ADR-0148's
  fail-open advisory filter; this adds no new exposure. Execution-time `require_role` / the
  destructive-op gate remain the only boundary.
- No DB migration: `CORE_TOOLS`, curated keywords, and the TOC are code maps; search-miss is logged.

## Rejected alternatives

- **1a gateway (reverted ADR-0267).** Unreachable on Claude Code; dead-ends cold start.
- **`tools/list_changed` injection.** Spec-correct dynamic listing, but the client's handling is
  unproven (anthropics/claude-code#13646); needs an empirical spike before it can be trusted.
- **`run_middleware=False` + manual proxy.** Avoids the skip-set but re-implements validation,
  RBAC envelope mapping, and telemetry the stack already does.
- **`wait:true` flag only (#866 option 1).** Solves call ceremony, not catalog size.
- **Composite without demotion.** Grows the catalog.
- **`action`-enum verb collapse.** Cuts count but trades catalog size for per-tool schema ambiguity.
- **`runs.reproduce` name.** Implies crash-only; the composite equally validates a clean boot.
