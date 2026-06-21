# Design — MCP prompts surface for canonical lifecycle workflows

- **Issue:** #624 (part of #618; source AX_REVIEW A5)
- **ADR:** [ADR-0202](../adr/0202-mcp-lifecycle-prompts.md)
- **Status:** Draft
- **Date:** 2026-06-21

## Problem

`suggested_next_actions` (ADR-0019) is reactive: each tool response offers the next
one or two hops from where the agent already is. There is no proactive, server-authored
description of the canonical multi-step journeys, so an agent new to kdive has to
reconstruct the workflow from individual tool docs. The MCP `prompts` surface is the
protocol-native channel for that guidance, and `build_app()` registers none today
(`ListMcpPrompts` returns nothing).

## Goal

Register three canonical lifecycle prompts that point an agent at the real tool
sequence for each journey:

- `start_investigation` — orient and acquire capacity.
- `build_boot_debug` — build a kernel, boot it, attach a live debug session.
- `triage_panic` — turn a crash into a captured vmcore and a postmortem.

Each prompt is a *thin pointer* into the real tools — an ordered list of tool names with
a one-line purpose per step — not a reimplementation of any tool's behavior.

## Constraints (from the issue acceptance criteria)

1. The three canonical prompts are registered and resolve to real tool sequences.
2. Prompts respect maturity: no steering into unavailable tools.
3. Degrades gracefully: a client that ignores prompts loses nothing — no tool behavior
   depends on a prompt being read.

## What "respect maturity" means here

Tool maturity is `implemented | partial | planned` (ADR-0175), carried in each tool's
registration `meta`. The current surface has **no** `planned` tools, **but nearly the
entire live lifecycle is `partial`**: `runs.build/install/boot`, `systems.provision`,
`control.power/force_crash`, `debug.start_session/end_session`, `introspect.*`,
`vmcore.*`, and `postmortem.*` are all `partial` today (provider/live-path reasons).

The two most valuable journeys (`build_boot_debug`, `triage_panic`) are therefore
*dominated* by `partial` tools. So "respect maturity" cannot mean "omit `partial` steps"
— that would empty those two prompts and defeat the issue. It means the opposite of
*silent* steering:

- A referenced tool must exist in the live registry, and must **not** be `planned`
  (an advertised-but-unavailable target). Referencing an unknown or `planned` tool is a
  registration-time error, not a runtime surprise.
- A `partial` step is **disclosed**: the rendered prompt tags it with its maturity and
  its one-line maturity `reason` so the agent knows the step may not fully work yet and
  why. The agent is informed, never blindly routed.

Maturity is read from the **live registered tools** at registration time (it is fixed
for the process lifetime — tools never change maturity at runtime), so the prompt text
cannot drift from the tools it points at; a promotion of `runs.build` to `implemented`
automatically drops its `partial` tag the next time the app is built. A drift-guard test
pins each prompt's referenced tools and rendered maturity tags to the registry.

## Design

### 1. A prompts plane registrar (mirrors the doc-resources registrar, ADR-0151)

A new module `mcp/prompts/registrar.py` exposes `register(app, *, tool_maturity)` and a
code-defined, closed `CANONICAL_PROMPTS` tuple. Each entry is a `PromptSpec`:

```
PromptSpec(
    name: str,            # e.g. "build_boot_debug"
    title: str,
    description: str,     # one line, shown in ListMcpPrompts
    summary: str,         # one line of orientation rendered at the top of the body
    steps: tuple[Step, ...],
)
Step(tool: str, purpose: str)   # tool is a real registered tool name
```

There are **no prompt arguments** in this first cut: each prompt renders static
orientation text. This keeps the surface a thin pointer and makes graceful degradation
trivial (a no-argument prompt has no failure mode). A free-text `goal` argument for
`start_investigation` is a deliberate non-goal here (see Alternatives).

`register` is pure with respect to FastMCP internals: it takes an explicit
`tool_maturity: Mapping[str, str]` (tool name → maturity), so it is unit-tested by
passing a fabricated map. For each step it:

- raises `RuntimeError` if `step.tool` is absent from `tool_maturity` (a typo or a
  removed tool — fail fast, mirroring the doc-resources "missing snapshot" guard);
- raises `RuntimeError` if the referenced tool is `planned` (steering into an
  unavailable tool);
- renders the body, tagging any `partial` step.

It registers one `FunctionPrompt` per spec via `app.add_prompt(...)` whose render
function returns the pre-rendered body string (FastMCP wraps a returned `str` as a
single user-role text message — verified against fastmcp-slim 3.4.2). Returns the count.

### 2. Wiring (mcp/app.py)

`build_app` already reaches into `app.local_provider._components` once, in
`_advertise_envelope_output_schema`, to sweep registered `Tool` instances. Extract a
small `_registered_tools(app)` helper used by both that sweep and a new
`_register_lifecycle_prompts(app, pool, assembly)` adapter. The adapter builds
`tool_maturity` from the live `Tool` metas (`tool.meta["maturity"]`, defaulting to
`implemented` when a tool carries no maturity key) and calls
`prompts_registrar.register(app, tool_maturity=...)`.

The adapter is appended to `_PLANE_REGISTRARS` **after every tool registrar** (after
`_register_doc_resources`), so all tool metas exist when it reads them. The fail-fast
`RuntimeError` on an unknown referenced tool also catches an accidental reordering that
would run prompts before a tool plane.

### 3. Rendered body shape

Each rendered prompt body is plain markdown:

```
<summary line>

Canonical tool sequence:
1. <tool> — <purpose>
2. <tool> — <purpose>  [partial: <reason>]
...

Notes:
- Poll long-running steps with jobs.wait / jobs.get.
- Read any tool's full contract before calling it; see
  resource://kdive/docs/guide/response-envelope.md for how to read results.
- [partial] steps are not yet proven end-to-end; check the tool's maturity_detail.
```

The `[partial: <reason>]` tag is appended only to steps whose live maturity is
`partial`. The reason string is the maturity `reason` enum value when present, else
`partial`.

### Journey content (the three specs)

`start_investigation` (orientation; every step `implemented` today):

1. `investigations.open` — open an investigation to group related runs.
2. `resources.list` — see which resources you can allocate.
3. `allocations.request` — request capacity on a resource.
4. `allocations.wait` — wait until the allocation is granted.
5. `systems.define` — define the target system to build/boot on.

`build_boot_debug` (build → boot → live debug):

1. `runs.create` — create a run against the system/build target.
2. `runs.build` — build the kernel.
3. `runs.complete_build` — record the build outputs.
4. `runs.install` — install the built kernel onto the system.
5. `runs.boot` — boot the system into the built kernel.
6. `debug.start_session` — attach a live debug session.
7. `introspect.run` — inspect kernel state in the live session.
8. `debug.end_session` — detach when done.

`triage_panic` (crash → vmcore → postmortem):

1. `control.force_crash` — induce a crash (or react to an observed panic).
2. `vmcore.fetch` — capture the vmcore from the crashed system.
3. `vmcore.list` — confirm the captured vmcore reference.
4. `postmortem.triage` — run the first-pass crash triage.
5. `introspect.from_vmcore` — inspect kernel state from the captured vmcore.

(The exact tool names are pinned to the live registry by the drift test; the only steps
that are `implemented` today are the `start_investigation` set plus `runs.create` /
`runs.complete_build`. Every other step renders a `[partial]` tag until promoted.)

### 4. Graceful degradation / no behavioral coupling

Prompts are advisory text. No tool reads a prompt; nothing in a tool handler depends on
a prompt being fetched. The `ToolExposureMiddleware` filters only `on_list_tools`, so
prompts are unaffected by tool-exposure policy, and prompts carry no secrets and need no
RBAC gate (same posture as the doc resources). A client that never calls
`ListMcpPrompts` / `GetPrompt` sees an unchanged tool surface.

## Testing

- **Registration + resolution:** build the app; assert the three prompt names are listed
  and each `GetPrompt` renders a non-empty user message whose body names every step's
  tool.
- **Maturity disclosure:** assert each `partial` step in the rendered body carries a
  `[partial...]` tag and each `implemented` step does not; cross-check the tags against
  the live registry (drift guard) so a promotion/demotion that isn't reflected fails.
- **Unknown tool fails fast:** `register` with a `PromptSpec` referencing a missing tool
  raises `RuntimeError`.
- **`planned` tool fails fast:** `register` with a `tool_maturity` marking a referenced
  tool `planned` raises `RuntimeError`.
- **No behavioral coupling:** a build with the prompts registrar removed exposes the same
  tool set (prompts add no tools and remove none).

## Alternatives considered

- **Omit `partial` steps / only register fully-`implemented` prompts.** Rejected: it
  empties `build_boot_debug` and `triage_panic`, the two journeys the issue most wants,
  because the live lifecycle is almost entirely `partial` today.
- **Per-request dynamic render reading the registry on every `GetPrompt`.** Rejected as
  unnecessary: maturity is registration-time-immutable, so a render snapshot taken when
  the app is built is already runtime truth; per-request render would re-touch the
  FastMCP internals on every call for no behavioral gain.
- **Hardcode the maturity tags in the prompt text.** Rejected: it rots silently when a
  tool is promoted. Reading the live registry keeps the prompt honest by construction.
- **A `goal` / free-text argument on `start_investigation`.** Deferred: adds input
  surface and an error path for no clear current need; the static orientation already
  satisfies the acceptance criteria. Add it later if an agent need appears.
- **A generated, drift-checked prompt-reference doc (like the tool reference).**
  Deferred: the unit drift-guard test already pins prompt content to the registry; a
  second generated artifact is maintenance surface the issue does not call for.
