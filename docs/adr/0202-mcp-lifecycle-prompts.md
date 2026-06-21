# ADR 0202 — MCP prompts surface for canonical lifecycle workflows

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** kdive maintainers

## Context

The uniform `ToolResponse` envelope (ADR-0019) carries `suggested_next_actions`, which is
reactive — it offers the next hop from where the agent already is. There is no proactive,
server-authored description of the canonical multi-step journeys (orient and acquire
capacity; build → boot → debug; crash → vmcore → postmortem), so an agent new to kdive
must reconstruct each workflow from individual tool docs. MCP's `prompts` surface is the
protocol-native channel for that guidance, and `build_app()` registers no prompts today
(`mcp/app.py` `_PLANE_REGISTRARS` registers tools and doc resources only). See issue #624
(part of #618) and the design spec `../design/2026-06-21-mcp-lifecycle-prompts.md`.

Two facts shape the mechanism:

- **fastmcp-slim 3.4.2** exposes `add_prompt(...)`; a `FunctionPrompt` whose function
  returns a `str` renders to a single user-role text message (verified). This is the same
  registrar-seam shape the doc resources use (ADR-0151).
- **Tool maturity** (ADR-0175) is `implemented | partial | planned`, carried per tool in
  its registration `meta` and fixed for the process lifetime. The current surface has no
  `planned` tools, but nearly the whole live lifecycle is `partial`
  (`runs.build/install/boot`, `systems.provision`, `control.*`, `debug.*_session`,
  `introspect.*`, `vmcore.*`, `postmortem.*`). The two most valuable journeys are
  therefore dominated by `partial` tools.

## Decision

We will register a fixed, code-defined set of three canonical lifecycle prompts
(`start_investigation`, `build_boot_debug`, `triage_panic`) through a new plane registrar
(`mcp/prompts/registrar.py`), mirroring the doc-resources registrar.

1. **Thin pointers.** Each prompt is an ordered list of real tool names with a one-line
   purpose per step, rendered as static markdown — not a reimplementation of any tool. No
   prompt arguments in this first cut.
2. **Respect maturity by disclosure, not omission.** A referenced tool must exist in the
   live registry and must not be `planned`; either violation raises at registration (fail
   fast). A `partial` step is *disclosed* — the rendered body tags it with its maturity
   and `reason` — rather than removed, because removing `partial` steps would empty
   `build_boot_debug` and `triage_panic`.
3. **Read maturity from the live registry at registration time.** The registrar adapter in
   `build_app` builds a `tool name → maturity` map from the registered `Tool` metas and
   passes it to the pure `register(app, *, tool_maturity=...)`. Maturity is
   registration-time-immutable, so a snapshot taken when the app is built is runtime truth;
   a promotion automatically drops the `partial` tag on the next build. A drift-guard test
   pins prompt content and tags to the registry.
4. **No behavioral coupling.** Prompts are advisory; no tool handler reads a prompt. A
   client that ignores prompts sees an unchanged tool surface. Prompts carry no secrets and
   need no RBAC gate (same posture as doc resources, ADR-0151).

## Consequences

- `ListMcpPrompts` / `GetPrompt` return the three journeys; an agent gets server-authored
  orientation without scraping per-tool docs.
- The prompt registrar must run after every tool registrar (it reads tool metas); it is
  appended last in `_PLANE_REGISTRARS`, and the fail-fast on an unknown referenced tool
  catches an accidental reordering.
- `build_app` now reaches into the FastMCP component store (`app.local_provider._components`)
  for tool metas. A shared `_registered_tools` helper concentrates that private-accessor use
  (already present in `_advertise_envelope_output_schema`, ADR-0170) in one place.
- Adding or editing a journey is a reviewed code change to `CANONICAL_PROMPTS`; the drift
  test fails if a referenced tool's maturity changes without the rendered tag following.

## Alternatives considered

- **Omit `partial` steps / register only fully-`implemented` prompts.** Empties the two
  lifecycle journeys the issue most wants, since the live lifecycle is almost entirely
  `partial` today.
- **Per-request dynamic render reading the registry on each `GetPrompt`.** Unnecessary:
  maturity is registration-time-immutable, so a registration snapshot is already runtime
  truth; per-request render re-touches FastMCP internals for no behavioral gain.
- **Hardcode the `[partial]` tags in prompt text.** Rots silently on promotion; reading the
  live registry keeps the text honest by construction.
- **A `goal` free-text argument on `start_investigation`.** Adds input surface and an error
  path for no current need; deferred.
- **A generated, drift-checked prompt-reference doc (like the tool reference).** The unit
  drift-guard already pins content to the registry; a second generated artifact is
  maintenance surface the issue does not call for. Deferred.
