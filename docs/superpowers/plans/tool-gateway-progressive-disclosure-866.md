# Implementation plan: tool gateway — progressive disclosure + composite (#866)

- Spec: [docs/specs/2026-06-27-tool-gateway-progressive-disclosure-866.md](../../specs/2026-06-27-tool-gateway-progressive-disclosure-866.md)
- ADR: [ADR-0267](../../adr/0267-tool-gateway-progressive-disclosure.md)
- Issue: #866

## Shape of the work

Five tasks, **strictly sequential on one branch** — they share `mcp/exposure.py`,
`mcp/tool_index.py`, and `mcp/app.py`, so they are tightly coupled and must not be dispatched to
parallel agents in the same working tree. Each task is TDD: failing test first, then minimal
implementation, then guardrails green before commit.

Ordering rationale: the two new tools (T2, T3) must be **registered before** the `CORE_TOOLS`
completeness guard (T4) runs, because that guard asserts `CORE_TOOLS ⊆ live registry` and
`CORE_TOOLS` names them. `tool_index.py` (T1) is shared by both `tools.search` (T2) and the
`instructions` TOC (T5), so it lands first.

Guardrail commands (run before every commit; CI gates these individually per repo memory):
`just lint`, `just type`, `just test`, plus the doc guardrails for doc-touching commits
(`just docs-links docs-paths docs-check adr-status-check`), and for T4 the config/env doc guards
(`just config-docs-check env-docs-check config-guard`).

---

## T1 — `mcp/tool_index.py`: keyword map + namespace TOC

**Where it fits:** shared substrate for `tools.search` (T2) ranking and the `instructions` TOC (T5).
Pure data + pure functions, no registration, no I/O — the cheapest first step.

**Build:**
- New module `src/kdive/mcp/tool_index.py` with, following the `mcp/exposure.py` `_TOOL_SCOPES`
  central-reviewed-map idiom:
  - `TOOL_KEYWORDS: dict[str, frozenset[str]]` — curated synonyms per tool name (e.g. `runs.boot →
    {"boot", "power on", "start vm", "kernel"}`). A tool absent from the map defaults to keywords
    tokenised from its `name + description` at lookup time (do not pre-expand).
  - `NAMESPACE_TOC: dict[str, str]` — the 18 namespaces → one-line summary, plus a short gateway-
    pattern preamble string `GATEWAY_INSTRUCTIONS` ("not every tool appears in `list_tools`; use
    `tools.search` … then call it directly by name").
  - `rank_tools(query: str, candidates: Iterable[tuple[str, str]], *, limit: int) -> list[str]` —
    deterministic lexical scorer over `(name, description)` + `TOOL_KEYWORDS`, ties broken
    lexicographically by tool name. Pure function; no RBAC, no schema work (T2 composes it).

**Files:** `src/kdive/mcp/tool_index.py`, `tests/mcp/test_tool_index.py`.

**Acceptance:**
- `rank_tools` is deterministic: same query+candidates → identical order across runs; ties
  broken by name; respects `limit`.
- Empty/whitespace query → `rank_tools` returns `[]` (caller T2 turns that into the TOC-pointer
  rejection; the scorer itself does not raise).
- A tool with no `TOOL_KEYWORDS` entry still ranks on tokenised name+description.

**Rollback:** delete the module + test; nothing imports it yet.

---

## T2 — `tools.search` discovery tool (PUBLIC)

**Where it fits:** the gateway "ceiling" — lets an agent load any demoted tool's full schema and
then call it directly (the 1a model).

**Build:**
- New tool module under the meta/identity plane (mirror `session.whoami`'s registrar placement;
  confirm its home in `mcp/tools/identity/`). Register `name="tools.search"`, read-only annotation.
- **Registry-enumeration prerequisite (verified, fastmcp 3.4.2).** The handler closes over the
  `app` the registrar receives. Enumerate the **raw** registry with `await app.list_tools()` →
  `list[fastmcp.tools.Tool]`; each `Tool.to_mcp_tool()` yields the MCP `{name, description,
  inputSchema}` — the same serialization `list_tools` emits. **Verify** at implementation time that
  `app.list_tools()` returns the unfiltered registry and is **not** itself wrapped by
  `ToolExposureMiddleware.on_list_tools` (the connection tier filter); if it is, enumerate via the
  tool manager directly so search spans **all tiers**. Tools' `name`/`description` for ranking come
  from the same objects.
- Handler: `query: str`, `limit: int = 8` (Field-bounded `1..=20`). Steps:
  1. Enumerate the raw registry (above) → `(name, description)` candidates, then keep only
     RBAC-visible names via `mcp.exposure.visible_tool_names(ctx, names)` — **all tiers**, not
     core-filtered (search is the escape hatch out of the core set; do **not** apply `CORE_TOOLS`).
  2. Reject empty/whitespace `query` with a `configuration_error` whose `data` points at the
     namespace TOC (reason `empty_query`).
  3. `rank_tools(query, candidates, limit=limit)` (T1), then serialise each hit via
     `Tool.to_mcp_tool()` (full input schema + description + name) so the result is sufficient to
     construct a call. Return them in `ToolResponse.data`.
  4. Telemetry: a zero-result query emits a structured log (query + count). (The
     searched-but-never-invoked counter is observability wiring carried as T2-followup below, not a
     blocker for the tool itself.)
- Classify `tools.search` as **PUBLIC** in `mcp/exposure.py` `PUBLIC_TOOLS` (it returns only
  RBAC-permitted schemas). Update nothing in `_TOOL_SCOPES`.

**Files:** new `src/kdive/mcp/tools/identity/search.py` (+ registrar wiring), `mcp/exposure.py`
(PUBLIC_TOOLS), `mcp/app.py` registrar list if a new plane registrar is needed,
`tests/mcp/...test_search.py`.

**Acceptance:**
- A viewer-only caller searching "boot a kernel" gets `runs.boot` with a constructible input schema;
  a tool the caller's RBAC forbids never appears.
- `limit` defaults to 8, rejects >20 at the schema boundary, empty query → `configuration_error`
  `reason=empty_query`.
- The returned schema for a demoted tool equals what `list_tools` would emit for it (parity test).
- The completeness guard (`CLASSIFIED_TOOLS | PUBLIC_TOOLS == live registry`) stays green.

**Rollback:** unregister the tool, drop from `PUBLIC_TOOLS`, delete module+test.

---

## T3 — `runs.build_install_boot` composite (CONTRIBUTOR)

**Where it fits:** the gateway "floor" — collapses build→install→boot→get into one call.

**Build:**
- New handler in the runs lifecycle plane. `run_id: str`, `timeout: float | None` (Field-bounded;
  server cap constant). Orchestration:
  1. For each phase in `(build, install, boot)`: enqueue at the **service layer** (build via the
     `server_build` enqueue path / `ProviderRuntime`, install via `install_run`, boot via
     `boot_run` — `mcp/tools/lifecycle/runs/steps.py`), passing a **deterministic per-phase
     `idempotency_key`** = `f"bib:{run_id}:{phase}"`. Then poll that job to terminal with a
     **re-poll loop** around `mcp.tools.jobs.wait_job` (jobs.py:150) — **not a single call**:
     `wait_job` clamps `timeout_s` to `MAX_WAIT_S` (=300s, jobs.py:53) and on a non-terminal
     deadline returns the job's *current* `queued`/`running` envelope (with `jobs.wait` in
     `suggested_next_actions`), by design, because one long idle stream gets severed by an
     intermediary proxy (its docstring). A kernel build routinely exceeds 300s, so the composite
     loops: call `wait_job(job_id, per_iter_s)` with `per_iter_s ≤ MAX_WAIT_S`, inspect the returned
     envelope's job state — terminal → advance to the next phase; still `queued`/`running` →
     re-issue — accumulating elapsed against the caller `timeout` budget. Inject `sleep`/clock for
     deterministic tests. Emit an MCP progress notification per phase transition.
  2. On the first phase whose job is not `succeeded`: return a terminal envelope with
     `data.failed_phase`, that phase `job_id`, the job error, and `run_id`. Stop.
  3. On total `timeout` budget exhaustion: return the in-flight phase's **running envelope** —
     mirroring `wait_job`'s own non-terminal return, **not** a new `timeout`/`deadline` error
     category (none exists) — carrying `failed_phase=null`, the in-flight phase `job_id`, `run_id`,
     and `runs.get`/`jobs.list` in `suggested_next_actions` as the reattach path; jobs keep running.
  4. On all-succeeded: return `get_run(...)` projection (same shape as `runs.get`).
- Reuse the existing precondition errors (not-bound, not-built) by letting the underlying step
  functions return them — the composite surfaces the first step's failure envelope verbatim with
  `failed_phase` added.
- Classify as **CONTRIBUTOR** in `_TOOL_SCOPES`.

**Files:** `src/kdive/mcp/tools/lifecycle/runs/` (new `build_install_boot.py` + registrar wiring),
`mcp/exposure.py` (`_TOOL_SCOPES`), `tests/mcp/.../test_build_install_boot.py`.

**Acceptance (integration, injected `sleep`/fake queue):**
- Happy path: three jobs enqueued in order, each polled to `succeeded`, returns the terminal
  `runs.get` projection.
- Forced install failure: returns `data.failed_phase=install` with the job error; **no boot job
  enqueued**.
- Re-call after a phase reached terminal: re-enqueue with the same key returns the stored result
  (single-shot) — assert exactly one `runs.build` job exists for the `run_id`.
- Timeout: with a never-terminal build job and a short `timeout`, returns the non-terminal in-flight
  envelope; the build job is not cancelled.
- RBAC: a viewer is denied at execution (`require_role` CONTRIBUTOR via the step functions).

**Rollback:** unregister, drop from `_TOOL_SCOPES`, delete module+test.

---

## T4 — `CORE_TOOLS` tier filter + `KDIVE_MCP_TOOL_GATEWAY` switch

**Where it fits:** the actual catalog-shrink. Runs **after** T2/T3 so the new tools exist for the
guard.

**Build:**
- `mcp/exposure.py`: add `CORE_TOOLS: frozenset[str]` (the spec's nine names) and a
  `core_visible_tool_names(ctx, names)` that returns `visible_tool_names(ctx, names) & CORE_TOOLS`.
- `mcp/middleware/exposure.py`: in `on_list_tools`, after the RBAC filter, intersect with
  `CORE_TOOLS` **iff** the gateway is enabled. Read the switch from config
  (`KDIVE_MCP_TOOL_GATEWAY`, default on) via `config/core_settings.py` (mirror an existing
  `KDIVE_*` setting). Keep the existing fail-open `except` arms: any error → full RBAC catalog.
- `config/core_settings.py` + `config/external_env.py`: declare the setting so the env-doc guard
  passes.
- Completeness guard in `tests/mcp/core/test_app.py`: assert `CORE_TOOLS <= live registry`.

**Files:** `mcp/exposure.py`, `mcp/middleware/exposure.py`, `config/core_settings.py`,
`config/external_env.py`, `tests/mcp/core/test_app.py`, env/config docs.

**Acceptance:**
- Gateway **on** (default): a contributor connection's `list_tools` returns exactly
  `CORE_TOOLS ∩ RBAC` (≤ 9); `runs.build` is absent.
- Gateway **off** (`KDIVE_MCP_TOOL_GATEWAY=off`): `list_tools` returns the full RBAC-scoped catalog
  (ADR-0148 behaviour) — regression test.
- Filter-error path still falls open to the full catalog (existing fail-open test extended).
- `CORE_TOOLS ⊆ registry` guard green; `env-docs-check`/`config-guard` green.

**Rollback:** remove `CORE_TOOLS` intersection + setting; `on_list_tools` reverts to ADR-0148.

---

## T5 — Server `instructions` TOC

**Where it fits:** prevents "agent never searches" by advertising the namespace map.

**Build:**
- `mcp/app.py:33`: pass `instructions=` to `FastMCP(...)`, composed from
  `tool_index.GATEWAY_INSTRUCTIONS` + a rendered `NAMESPACE_TOC` (T1).
- Guard test: every live namespace (derived from the registry) appears in `NAMESPACE_TOC`, so a new
  namespace must be triaged into the TOC.

**Files:** `mcp/app.py`, `mcp/tool_index.py` (render helper), `tests/mcp/core/test_app.py`.

**Acceptance:** built app's `instructions` is non-empty, contains the gateway-pattern preamble and
every namespace; namespace-completeness guard green.

**Rollback:** drop the `instructions=` kwarg.

---

## T6 — Docs regeneration + full-suite gate

**Where it fits:** final step before push (workflow step 7 runs the **full** suite).

**Build:**
- Regenerate any generated tool-reference / config docs touched by the two new tools and the new
  setting (`just config-docs`, tool-catalog generators if present). Review diffs.
- Run the **full** `just lint type test` plus all doc/config guards; fix fallout (architecture and
  doc-generation tests live outside the dirs edited).

**Acceptance:** full local suite green; generated docs match.

---

## Cross-cutting notes

- **No DB migration.** All new state is code maps + a config setting + structured logs.
- **Secrets/redaction:** `tools.search` echoes tool descriptions (build-time constants) and the
  caller's `query`; the query is request input echoed like `investigations.title` — not run through
  the secret redactor, consistent with ADR-0264. Do not log raw queries at a level that would
  persist secrets beyond the existing structured-log policy.
- **Telemetry follow-up (not blocking):** the searched-but-never-invoked counter (spec
  §Verification) is session-correlation observability; implement as a follow-up once the tools land,
  tracked in the spec, not gating this PR.
