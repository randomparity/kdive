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

1. **Measurable — ceremony.** The common reproduce flow over a bound Run drops from 3 dispatch calls
   + 3 separate `jobs.wait` polls (three job handles) to 1 dispatch + polls of one job handle. This
   is countable and falsifiable.
2. **Measurable — catalog size.** The default `list_tools` catalog (gateway on) is a small **core
   set** (~10 tools), not the full 83. Countable.
3. Every non-core tool stays **fully invocable** at native validation fidelity — no capability
   removed, only deferred behind discovery + dispatch that the client can actually drive.
4. The mechanism reuses the ADR-0148 `on_list_tools` seam, intersects (never widens) the RBAC
   filter, and **fails open** to the full catalog on any error.

**Not a goal of this change: a proven selection-accuracy improvement.** The catalog-size premise
(smaller catalog → better LLM tool selection) is the *motivation*, but this change ships only the
mechanism and the two countable wins above. Whether selection accuracy actually improves — net of
the added search→invoke hops — is a **hypothesis to be measured after merge** from `tool_invocation`
data (wrong-tool / not-found rates, calls-per-task) with the gateway off vs on, not a claim this
change asserts or gates on. Shrinking the catalog is necessary for that experiment; it is not itself
evidence the experiment will succeed.

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
@app.tool(name="tools.invoke", annotations=_docmeta.destructive())
async def tools_invoke(name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
    try:
        return await app.call_tool(name, arguments or {}, run_middleware=True)
    except NotFoundError:
        return _unknown_tool_response(name)        # configuration_error → "use tools.search"
    except ValidationError as exc:
        return _bad_arguments_response(name, exc)  # configuration_error, inner field errors
```

The dispatcher's static annotation cannot mirror the inner tool, which may be a read or a teardown.
It is annotated `destructive()` so a consent-prompting client errs toward prompting (the safe
direction). Crucially, `tools.invoke` is **not** added to `_docmeta.DESTRUCTIVE_TOOLS`: the kdive
destructive-op gate (ADR-0043/0047) keys off that set, and it must fire on the **inner** re-entered
call (so a destructive inner tool is gated and a read is not), not blanket-gate every gateway call.
The `destructive()` annotation is the client-facing MCP hint; `DESTRUCTIVE_TOOLS` membership is the
server-side gate — they are deliberately decoupled here.

`call_tool` validates `arguments` against the inner tool's schema and raises `ValidationError`;
the dispatcher catches it and returns the same `configuration_error` envelope shape a direct call
produces (a pinned test asserts the two are equivalent), so a malformed gateway call is not an
opaque transport error. An inner `AuthorizationError` is **not** caught — it propagates exactly as
for a direct call (ADR-0148), reaching the client as a denial; the skip-set in §6 keeps the outer
chain from re-auditing it.

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
- **Bounded payload.** Each call returns at most `limit` matches (default small, hard-capped), so
  `tools.search` never re-emits the whole demoted catalog in one response. Namespace browse is
  capped the same way; a namespace larger than the cap (`debug.*` is 12) paginates. This is what
  keeps the catalog-reduction win from being undone by one broad search — the cost is paid per
  capability actually needed, not for the whole plane.
- **Ranking.** Deterministic lexical over `name + description + curated keywords`
  (`mcp/tool_index.py`, the `_TOOL_SCOPES` idiom); ranked by match strength, `limit`-capped;
  defaults to tokenised name+description when a tool has no keyword entry.
- **Search-miss telemetry.** Zero-result queries are logged structured (query + count) — the
  "agent reached for a capability it could not find" signal, the feedback loop for keyword curation.

### 3. `runs.build_install_boot` — the composite (a single worker job)

The composite must **not** block the server to terminal. The server is the thin async core that
"never blocks on a long provision" (`docs/design/top-level-design.md:81`, AGENTS.md:60), and
`jobs.wait` is deliberately bounded (`wait_job`, `src/kdive/mcp/tools/jobs.py:154-188`: clamped to
`MAX_WAIT_S`, holds no pool connection while sleeping, returns the non-terminal state as an
"ask again" signal — the ADR-0138 retry contract). A composite that internally waited
build→install→boot to terminal would hold one request for minutes, violating both the principle and
that contract.

So `runs.build_install_boot` is an OPERATOR tool that **enqueues one composite worker job** over an
**already-created, already-bound** Run and returns that single job handle immediately. It adds a new
`JobKind` (`build_install_boot`) whose handler calls the existing **per-phase job executors**
sequentially — `build_handler` → `install_handler` → `boot_handler` in
`src/kdive/jobs/handlers/runs/{build,install,boot}.py`, the same functions
`runs.handlers.register_handlers` binds for the `BUILD`/`INSTALL`/`BOOT` job kinds, sharing one
`RunHandlerPorts`. It must reuse those **executors**, not the MCP admission/enqueue path
(`_build_handlers` in the tool registrar enqueues a job; the composite is already inside the worker
and must do the work, not enqueue three more sub-jobs). Each executor commits its own `run_steps`
row as it completes — the existing long-running model ("a handler runs 30+ minutes and commits its
own steps", `src/kdive/jobs/worker.py`). The agent polls that **one** job with `jobs.wait` —
replacing three separate job handles and three waits with one job to learn and poll. This is the
ceremony reduction #866 asks for, expressed in the existing async spine rather than against it.

- **Input:** `run_id` only (a created, bound, not-yet-built Run). `expected_boot_failure` is already
  persisted on the Run at `runs.create` (registrar field, not a boot-time argument), so the composite
  reads it from the Run — a matched expected crash is a success exactly as in `runs.boot`. No extra
  parameter.
- **Scope.** Post-`create`/`bind`. Capacity, System selection, and reuse are explicit agent
  decisions; the three same-shaped job steps over one bound Run are the ceremony #866 names. A full
  `request→boot` mega-composite is rejected (conflates capacity into the reproduce step).
- **Progress.** `jobs.wait` tracks the **single** composite job to terminal (the call-count win).
  Intra-run phase progress is the `run_steps` ledger each executor already writes
  (`build`/`install`/`boot` rows), read via `runs.get` — the standard async-spine pattern. The `Job`
  row itself has no phase field, so phase is read from `runs.get`, not `jobs.get`; no new
  phase-surfacing field and no MCP progress-token notification are required (the latter would
  re-introduce a client-capability dependency of the kind that sank 1a).
- **Success contract.** The job reaches `succeeded`; its terminal result carries the `runs.get`
  projection (boot outcome + artifacts pointer), so the agent needs no extra `runs.get`.
- **Failure contract.** The worker stops at the first phase not reaching `succeeded` and the job
  ends in a terminal failed state carrying `data.failed_phase` (`build`|`install`|`boot`), that
  phase's error, and `run_id`. Recovery uses the granular tools (discovered via `tools.search`). No
  retry, no resume.
- **Cancellation / disconnect.** It is an ordinary durable job: `jobs.cancel` cancels it, and a
  client disconnect mid-run is not a recovery hole — the job continues server-side and the agent
  re-attaches with `jobs.wait` / `jobs.get` on the returned handle. There is no minutes-long open
  request to drop.

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
- **Default OFF at merge.** `KDIVE_MCP_TOOL_GATEWAY` ships **off** (full RBAC-scoped catalog,
  ADR-0148 status quo). The tier filter activates only when the flag is set on. The default flips to
  on in a **separate follow-up** once the cold-start E2E gate (see Verification) is recorded — the
  prior attempt was reverted precisely for defaulting on before that path was proven. The ADR states
  the same default.
- **Completeness guard.** A test asserts `CORE_TOOLS ⊆` the live registry, alongside the existing
  `CLASSIFIED_TOOLS | PUBLIC_TOOLS` guard.
- **Core set is reproduce-flow-centric (acknowledged).** This launch set optimises the
  build→boot→inspect loop; debug/introspect-heavy sessions (`debug.*` is 12 tools, none core) reach
  every tool through `tools.search` + `tools.invoke`, trading per-call overhead for catalog
  reduction. That is a deliberate launch choice, not an oversight: the gateway is off by default, the
  core set is a code constant tunable from `tool_invocation` data, and a `debug.*` composite or an
  expanded core set is a follow-up once usage data shows the real per-class cost. The composite gives
  the reproduce loop a one-call path; no equivalent relief ships for debug flows in this change.

### 5. Server `instructions` — table of contents

Add `instructions` to `FastMCP(name="kdive", …)` carrying (1) the gateway pattern — not every tool
is listed; `tools.search` by capability, then `tools.invoke(name, arguments)` — and (2) a namespace
TOC (the 18 namespaces with one-liners), restoring the ambient workflow map at ~18 lines instead of
83 schemas. A reviewed constant (`mcp/tool_index.py`) with a guard test asserting every live
namespace appears.

### 6. Meta-tool skip-set (recording correctness across the re-entered chain)

`run_middleware=True` re-runs the **whole** middleware chain for the inner call, nested inside the
outer `tools.invoke` call's chain. Every middleware that records or audits **per call** therefore
fires twice — once for the inner tool (correct) and once for the outer meta-tool (noise/corruption).
The fix applies to all of them, not just telemetry:

- `UsageTrackingMiddleware` — would write a second `tool_invocation` row for `tools.invoke`.
- `TelemetryMiddleware` — would emit a duplicate span.
- `DenialAuditMiddleware` — this is the subtle one. An inner `AuthorizationError` propagates (it is
  not enveloped, per ADR-0148), so it unwinds through the **outer** chain too; without a skip the
  outer `DenialAuditMiddleware` writes a **second, misattributed** `platform_audit_log` denial row
  keyed to `tools.invoke`. That corrupts the audit trail, not just metrics.

So every per-call recording/auditing middleware skips when
`context.message.name ∈ {tools.invoke, tools.search}` — the inner call is the sole recorder. One
row per real call, keyed to the inner tool with correct project/outcome (`_call_project` already
reads `arguments["project"]`, which on the re-entered inner call is the real argument dict). A test
asserts a gateway-denied call writes exactly one denial audit row, attributed to the inner tool.
`BindingErrorMiddleware` and `ToolExposureMiddleware` record nothing per call, so they need no
skip.

## Surface delta

- **New tools:** `tools.search` (PUBLIC), `tools.invoke` (PUBLIC), `runs.build_install_boot`
  (OPERATOR) → +3 registered.
- **`list_tools` when gateway on:** 83 → ~10 (core set, then RBAC-scoped). At merge the gateway is
  off, so the default listing is unchanged (full RBAC-scoped catalog) until the follow-up flip.
- **Demoted (gateway-reachable, not removed):** ~76 tools incl. `runs.build`/`install`/`boot`.
- **Capability:** unchanged — every tool reachable via `tools.search` + `tools.invoke` at native
  validation fidelity.
- **Calls for the happy path over a bound Run:** 3 dispatch calls + 3 separate `jobs.wait` polls
  (three job handles to track) → 1 dispatch + bounded re-polls of **one** job handle.

## Telemetry

One `tool_invocation` row per real call, written by the re-entered inner call; meta-tools skipped.
`tools.search` zero-result queries emit a structured log for keyword curation. A `tool_invocation`
schema column for search is out of scope (additive later).

## RBAC / migration

- `tools.search`, `tools.invoke`: PUBLIC — the inner call / returned schemas reflect the caller's
  real RBAC.
- `runs.build_install_boot`: OPERATOR — the max role of its constituent steps.
- **One migration (0051), for the composite job kind only.** The composite is a new `JobKind`
  (`build_install_boot`), and `jobs.kind` is CHECK-constrained (`jobs_kind_check`, with a SQL↔enum
  tie tested in `test_migrate.py`). So migration `0051` widens that CHECK to admit the new kind,
  exactly as `0040` did for `diagnostics_worker_check` (additive, forward-only per ADR-0015;
  drop-and-recreate to keep the constraint name stable), and the `JobKind` enum gains the matching
  member. The gateway pieces — `CORE_TOOLS`, curated keywords, the TOC — are code maps and need no
  migration; search-miss is logged, not persisted.

## Verification (gate — the revert lesson)

ADR-0267 merged with the gateway defaulting **on** without verifying the invocation path on the real
client, and that is what dead-ended cold start and forced the revert. So this change ships with
`KDIVE_MCP_TOOL_GATEWAY` **off by default** (status-quo full RBAC-scoped catalog). Merging it is
safe regardless of client behaviour because nothing is hidden until an operator opts in. The default
flips to on only in a **separate follow-up PR**, gated on:

1. A unit/integration test that `tools.invoke` re-entry preserves inner-tool validation
   (`ValidationError` → same `configuration_error` envelope as a direct call), RBAC denial
   (viewer-through-gateway denied identically to direct), and **single-row** recording — exactly one
   `tool_invocation` row and exactly one denial-audit row per call, attributed to the inner tool.
2. A worker-level test that the `build_install_boot` job runs the three phases over a bound Run,
   commits each `run_steps` row, and on a failing phase ends terminal with `data.failed_phase` set.
3. A **cold-start end-to-end** run against the Claude Code client *with the gateway on*: starting
   from the ~10-tool core listing, discover granular tools via `tools.search`, drive a full reproduce
   (mint → `runs.build_install_boot` → poll one job to terminal, plus at least one demoted tool via
   `tools.invoke`) to terminal state, recorded in the follow-up PR.

The composite, `tools.search`, and `tools.invoke` themselves are always listed (core set), so they
are exercised even with the gateway off — only the *demotion* of the long tail waits on the flip.

## Risks

- **Annotation/consent collapse.** A per-tool-prompting client sees only `tools.invoke` and cannot
  distinguish a read from a destructive teardown at prompt time. Mitigation: `tools.invoke` is
  annotated `destructive()` so a prompting client errs toward prompting, and the server-side
  destructive-op gate (ADR-0043) still fires on the re-entered inner call and remains the real
  boundary. Documented, not eliminated.
- **Discovery-index quality.** A bad keyword map silently strips capability. Mitigations: curated
  keywords, the `namespace` browse mode as a floor, search-miss telemetry, and the TOC.
- **Mis-sequencing.** Hiding the flat catalog hides implicit workflow order. Mitigations: the
  composite encodes the dominant sequence; the TOC + `instructions` describe the phases.
- **Opaque `arguments` at selection time** (the accepted 1b cost). Mitigation: `tools.search`
  returns full schemas; a malformed call returns the inner tool's `configuration_error` envelope
  (the dispatcher catches `ValidationError`), the same shape a direct call gives.
- **Per-class cost shift.** The reproduce-centric core set adds per-call overhead to debug/introspect
  flows (see §4). Mitigation: gateway off by default; core set is a tunable code constant; a debug
  composite / expanded core set is a data-driven follow-up.

## Alternatives rejected

- **1a gateway (reverted ADR-0267).** Unreachable on Claude Code.
- **`tools/list_changed` injection.** Spec-correct but unproven on the client; needs a spike.
- **`run_middleware=False` + manual proxy.** Re-implements validation/RBAC/telemetry the stack does.
- **`wait:true` flag only (#866 option 1).** Solves ceremony, not catalog size.
- **Composite without demotion.** Grows the catalog.
- **`action`-enum verb collapse.** Trades catalog size for per-tool schema ambiguity.
