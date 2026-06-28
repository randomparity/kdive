# ADR-0268: tool gateway redo — `tools.invoke` dispatcher (1b) + build/install/boot composite (#866)

- Status: Accepted
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
returns full schemas so the agent can construct a valid call, and a malformed call returns the inner
tool's `configuration_error` envelope (the dispatcher catches `ValidationError`), the same shape a
direct call gives.

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
tool's real RBAC. The dispatcher catches `NotFoundError` (unknown/disabled inner name) and
`ValidationError` (bad `arguments`) and maps each to the same `configuration_error` envelope a
direct call gives — the former pointing the caller at `tools.search`. An inner `AuthorizationError`
is **not** caught: it propagates exactly as for a direct call (ADR-0148), and the §6 skip-set keeps
the outer chain from re-auditing it.

### 2. `tools.search(query, namespace?, limit?)` — discovery

A PUBLIC tool mapping a query to matching tools, returning per match the **full input schema +
description + name** — the same payload `list_tools` would return — so the result is sufficient to
build a `tools.invoke` call. Two modes: lexical `query` ranking, and `namespace` browse (all
`runs.*`) as the enumeration safety net. Results are **RBAC-filtered** (only invocable tools) but
span **all tiers** — search is the escape hatch out of the core set. Ranking is deterministic
lexical over `name + description + curated keywords` (`mcp/tool_index.py`, the `_TOOL_SCOPES`
idiom). Zero-result queries are logged structured for keyword curation.

### 3. `runs.build_install_boot` — the composite (one worker job, not a server block)

The composite must not block the server to terminal: the server is the thin async core that "never
blocks on a long provision" (`docs/design/top-level-design.md`, AGENTS.md) and `jobs.wait` is
deliberately bounded (`wait_job`, clamped to `MAX_WAIT_S`, ADR-0138). So `runs.build_install_boot`
is an OPERATOR tool that **enqueues one composite worker job** over an already-created, already-bound
Run and returns that single job handle immediately. It adds a new `JobKind` (`build_install_boot`)
whose handler calls the existing per-phase job **executors** sequentially — `build_handler` →
`install_handler` → `boot_handler` (`src/kdive/jobs/handlers/runs/`, sharing one `RunHandlerPorts`),
the same functions bound for the `BUILD`/`INSTALL`/`BOOT` kinds — **not** the MCP admission/enqueue
path (the composite is already in the worker; it does the work, it does not enqueue three sub-jobs).
Each executor commits its own `run_steps` row (the existing 30-minute handler model). The agent polls that **one** job with `jobs.wait`,
replacing three job handles and three waits with one. Intra-run phase progress is the `run_steps`
ledger each executor writes (read via `runs.get`; the `Job` row has no phase field) — no MCP
progress-token notification, which would re-introduce a client-capability dependency. On `succeeded` the job's terminal result carries the `runs.get`
projection; on the first non-`succeeded` phase the job ends terminal-failed with `data.failed_phase`
(`build`|`install`|`boot`), that phase's error, and `run_id` (recovery via the granular tools, no
retry/resume). A client disconnect is not a recovery hole — the durable job continues and the agent
re-attaches via `jobs.wait`/`jobs.get`; `jobs.cancel` cancels it. `expected_boot_failure` passes
through unchanged. Scope starts post-`create`/`bind` — capacity and System selection are explicit
agent decisions.

### 4. Core-set tier filter

`CORE_TOOLS: frozenset[str]` in `mcp/exposure.py`, intersected into
`ToolExposureMiddleware.on_list_tools` after the RBAC filter (`visible = rbac_visible ∩
CORE_TOOLS`). Core set: `tools.search`, `tools.invoke`, `session.whoami`,
`runs.build_install_boot`, `runs.create`, `runs.get`, `runs.list`, `allocations.request`,
`allocations.wait`, `systems.provision` — discovery + dispatch entry points, the composite, and
reads in nearly every flow (tunable later from `tool_invocation` data). The filter **fails open**
to the full RBAC-scoped catalog on any error. `KDIVE_MCP_TOOL_GATEWAY` ships **off by default** (the
ADR-0148 status-quo full catalog); the tier intersection activates only when set on, and the default
flips to on in a separate follow-up once the cold-start E2E gate is recorded — the prior attempt was
reverted for defaulting on before that path was proven. This launch core set is reproduce-flow
centric; debug/introspect-heavy flows reach every tool via the gateway and pay per-call overhead, an
accepted launch trade-off revisited from usage data. A guard test pins `CORE_TOOLS ⊆` the live
registry.

### 5. Server `instructions` table of contents

`FastMCP(name="kdive", …)` (currently no `instructions`) gains `instructions` carrying (a) the
gateway pattern — not every tool is listed; `tools.search` by capability, then `tools.invoke` — and
(b) a namespace table of contents (the 18 namespaces with one-liners), restoring the ambient
workflow map at ~18 lines instead of 83 schemas. A guard test asserts every live namespace appears.

### 6. Meta-tool skip-set across the re-entered recording/audit chain

`run_middleware=True` re-runs the **whole** chain for the inner call, nested in the outer
`tools.invoke` chain, so every middleware that records or audits per call fires twice. The skip
covers all of them, not just telemetry: `UsageTrackingMiddleware` (a duplicate `tool_invocation`
row), `TelemetryMiddleware` (a duplicate span), and — the subtle one — `DenialAuditMiddleware`: an
inner `AuthorizationError` propagates un-enveloped (ADR-0148) through the outer chain, where without
a skip the outer `DenialAuditMiddleware` would write a **second, misattributed** `platform_audit_log`
denial row keyed to `tools.invoke`, corrupting the audit trail. So every per-call recording/auditing
middleware skips when `context.message.name ∈ {tools.invoke, tools.search}` — the inner call is the
sole recorder. `BindingErrorMiddleware` / `ToolExposureMiddleware` record nothing per call and need
no skip. This *improves* the ADR-0148 usage data: it measures real work, not dispatcher noise.

### 7. Empirical verification gate (revert lesson)

ADR-0267 merged with the gateway defaulting on without verifying the invocation path on the real
client. This redo ships `KDIVE_MCP_TOOL_GATEWAY` **off by default**, so the merge is safe regardless
of client behaviour. The default flips to on only in a separate follow-up gated on: a re-entry test
(inner validation/RBAC denial + single-row recording), a worker test of the composite job, and a
cold-start end-to-end reproduce flow (discover → invoke through the gateway → poll one job to
terminal) run against the Claude Code client and recorded in that follow-up PR.

## Consequences

- Default `list_tools` (gateway on): 83 → ~10 (then RBAC-scoped). Happy path over a bound Run: 3
  dispatch calls + 3 separate `jobs.wait` polls → 1 dispatch + bounded re-polls of one job handle.
  Surface: +3 registered tools; ~76 tools demoted to gateway-reachable.
- **Capability unchanged** — every tool reachable via `tools.search` + `tools.invoke` at native
  validation fidelity. Unlike the reverted 1a gateway, demoted tools are *invocable*.
- **Telemetry/audit** native and clean (skip-set keeps one row per real call across the whole chain).
- **Annotation/consent granularity collapses.** A client that prompts per tool sees only
  `tools.invoke`, so it cannot distinguish a read from a destructive teardown at prompt time.
  `tools.invoke` is annotated `destructive()` (the prompting client errs toward prompting) and is
  therefore listed in `DESTRUCTIVE_TOOLS` (the reviewed `destructiveHint` set). That membership is
  hint-only: the destructive-op gate keys off `DESTRUCTIVE_JOB_KINDS` / `assert_destructive_allowed`,
  not `DESTRUCTIVE_TOOLS`, so it still fires on the re-entered inner call (a destructive inner tool is
  gated, a read is not). The annotation is the client hint; the job-kind gate is the server-side
  boundary, and only the client-side prompt granularity degrades.
- **Not a security control.** An agent could already call any registered tool under ADR-0148's
  fail-open advisory filter; this adds no new exposure. Execution-time `require_role` / the
  destructive-op gate remain the only boundary.
- **One migration (0051).** The composite's new `JobKind` requires widening the `jobs_kind_check`
  constraint (additive, forward-only per ADR-0015, like 0040 did for `diagnostics_worker_check`).
  The gateway pieces — `CORE_TOOLS`, curated keywords, the TOC — are code maps; search-miss is
  logged, not persisted.
- **Per-class cost shift.** The reproduce-centric core set adds per-call overhead to
  debug/introspect flows; mitigated by gateway-off-by-default and a tunable core set.
- **Accuracy is a post-merge hypothesis, not a shipped claim.** The change delivers the two countable
  wins (fewer calls, smaller catalog); whether selection accuracy improves net of the search→invoke
  hops is measured afterward from `tool_invocation` data, not asserted here.

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
