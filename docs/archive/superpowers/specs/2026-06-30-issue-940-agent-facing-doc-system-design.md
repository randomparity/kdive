# Agent-facing workflow doc system (#940)

- Issue: [#940](https://github.com/randomparity/kdive/issues/940)
- ADR: [ADR-0284](../../adr/0284-agent-facing-workflow-docs.md)
- Date: 2026-06-30

## Problem

A black-box review found that common kernel-test workflows are slow to drive because
an agent must discover the tools for each step incrementally (`jobs.wait`, artifact
search, buildconfig operations, run lifecycle). The recipe-discovery layer is the gap,
not the tools themselves.

Today an MCP client has four agent-facing surfaces, and none of them gives a
workflow-shaped map of the tool surface:

- **Tool schemas** (`tools/list`): per-tool wrapper docstring + `Field` text. Low-level
  mechanics and parameter detail. The agent already holds these on the wire.
- **Doc resources** (`resources/list`, ADR-0151): three markdown docs only —
  `external-build-upload.md`, `build-source-staging.md`, `response-envelope.md`. All
  build- or envelope-flavored; ungated.
- **Prompts** (`prompts/list`, ADR-0202): three lifecycle step-lists
  (`start_investigation`, `build_boot_debug`, `triage_panic`). Reach only clients that
  list prompts; `build_boot_debug` omits the `runs.build_install_boot` composite it was
  built (ADR-0268) to favor.
- **Server instructions** (`initialize`): a namespace table of contents
  (`tool_index.py`). One line per namespace, no workflow ordering, no "when to use".

Good workflow narrative exists in the repo (`docs/guide/core-path.md`, the per-namespace
`docs/guide/reference/*.md`), but the per-namespace reference docs are auto-generated
parameter reference (the low-level layer), and the narrative is not reachable over MCP.

## Goals

- Give an agent a workflow-shaped entry point it can read over MCP: a typical session,
  ordered by stage, with one toolset doc per stage.
- For each agent-facing toolset, explain tool by tool *how each tool helps an
  investigation and when to reach for it* — purpose, not parameters.
- Leave low-level mechanics and data schema single-sourced in the tool docstrings.
- Gate provider-specific docs to deployments that registered that provider, and gate the
  operator/admin workflow to operator/admin callers.
- Surface the served set in `docs/README.md` so a human can see what an agent sees.

## Non-goals

- No new composite tools or worker jobs. This publishes discovery metadata over existing
  tools (the issue's "and/or discoverable recipe metadata" path).
- No change to tool docstrings as the source of truth for parameters and schema.
- No move of canonical docs out of `docs/`; the per-namespace reference generator and its
  output stay as-is.
- The three lifecycle prompts are kept, not replaced. They are a different MCP primitive
  for a different client capability.

## Design

### Layers and surfaces

```
SURFACE (what the client sees)          SOURCE
initialize.instructions  --points to--> investigation agent-index URI
resources/list + read  --[gated]--+
                           +-- agent-index.md           (investigation map + catalog)
                           +-- agent-index-operator.md  (operator entry; role-gated)
                           +-- guide/toolsets/<ns>.md   (investigation + admin/ops)
                           +-- existing 3 op/envelope docs
prompts/list (unchanged)   --- 3 ADR-0202 lifecycle prompts (cross-referenced from index)
tools/list  --docstrings-- low-level params/schema (unchanged source of truth)
```

### Index docs — `agent-index.md` and `agent-index-operator.md`

A doc resource is static markdown: its bytes are identical for every caller, so a single
index cannot show or hide an operator row per caller. The workflow is therefore split into
two index docs, each gated like the toolset docs it links:

- **`docs/guide/agent-index.md`** (`audience="all"`) — the investigation entry point,
  named in server `instructions`. It links only `audience="all"` toolset docs. It does not
  name any operator doc or operator URI.
- **`agent-index-operator.md`** (under `docs/guide/`, `audience="operator"`, role-gated,
  planned for the operator phase) — the
  operator/admin entry point. Listed and readable only for platform-operator callers, so
  it can safely name the operator toolset docs.

The investigation index has these sections:

1. **Typical session** — one ordered line per stage, each naming the toolset and the one
   tool to start with: orient → acquire capacity → define/provision system → build →
   install/boot → observe evidence → debug/introspect → triage → release.
2. **Toolset catalog** — a table: toolset, a one-line "what it's for in an investigation",
   and a link to the toolset's purpose-doc URI. Investigation toolsets only.
3. **Pointers** — to the three lifecycle prompts (for prompt-capable clients) and to
   `response-envelope.md`. For a platform-operator caller, a pointer to the operator index
   resolves; for others it is absent from the listing.

### Per-toolset purpose doc — `docs/guide/toolsets/<ns>.md`

Served as `resource://kdive/docs/guide/toolsets/<ns>.md`. Sections:

1. **Intro paragraph** — what the toolset does in an investigation and its place in the
   session flow, with prev/next stage links.
2. **Per-tool purpose lines** — each tool name plus one or two sentences on how it helps
   and when to reach for it. Example:
   > `artifacts.get` — fetch an artifact's bytes; use its optional `find`/`direction`
   > jump-cursor to locate a crash signature in a large console log without pulling the
   > whole file.
3. **Hand-off line** — "For exact parameters, types, and return schema, read the tool's
   own description." Keeps low-level detail single-sourced in docstrings.

The `runs` doc describes `runs.build_install_boot` as the preferred way to run the
single-host *server-build* lane when that lane is chosen — not as a default over the
external-upload lane, which stays the default build path (ADR-0234). The `build_boot_debug`
prompt is amended to stop omitting the composite (the original #940 fix).

### Coverage

Investigation subset (Phases 1–2): `investigations`, `allocations`, `resources`,
`systems`, `images`, `runs`, `buildconfig`, `build_envs`, `artifacts`, `jobs`, `control`,
`debug`, `introspect`, `vmcore`, `postmortem`.

Admin/ops workflow (Phase 3, role-gated): the operator index (`agent-index-operator.md`)
plus purpose docs for the operator/admin toolsets it references (for example `ops`,
`accounting`, `audit`, `inventory`, `build_hosts`, `shapes`).

### Gating

The `DocResource` model gains two optional fields:

- `required_kind: ResourceKind | None` — provider gate; default `None` means always.
- `audience: Literal["all", "operator"]` — role gate; default `"all"`.

**Provider-static (registration).** `resources/registrar.register(app, *, resolver)` is
threaded the resolver via `assembly.resolver`. It skips any entry whose `required_kind`
is not in `resolver.registered_kinds()`. A local-only deployment never registers a
remote-libvirt doc, so it can be neither listed nor read.

**Role (request-time).** A new `DocExposureMiddleware` mirrors `ToolExposureMiddleware` and
gates **both** the list and the read path, so an `audience="operator"` doc is neither
listed nor readable by a non-operator token (a list-only filter would leave the doc
readable by URI). The gate keys on the **platform-role axis** (`PlatformRole`,
`ctx.platform_roles`), not the project-scoped `Role.OPERATOR` (`projects_with_role`), since
the operator docs describe platform tools (`ops.*`, accounting admin, audit). The predicate
is "the caller holds **any** platform role" (`ctx.platform_roles` non-empty) — admitting
auditor, operator, and admin alike. A strict `require_platform_role(PLATFORM_OPERATOR)`
would be wrong here: `platform_admin` does not imply `platform_operator` (the implication
order is only `platform_admin ⊇ platform_auditor`), so it would hide the operator workflow
from a platform admin. A project-only token holds no platform role and is excluded.

- `on_list_resources`: drops `audience="operator"` resources for callers holding no platform
  role.
- `on_read_resource`: rejects a read of an `audience="operator"` resource from such a
  caller (an `AuthorizationError`, the same category the tools raise).
- **Fail-closed for the gated subset**: on auth error, list returns only `audience="all"`
  resources and a read of an operator doc is rejected, so an operator doc is never exposed
  to an unauthenticated caller. Investigation docs stay visible. The provider gate already
  ensures unregistered-provider docs are not registered at all, so they cannot be read
  either.

The middleware consults a URI→`audience` map derived from `DOC_RESOURCES`, so the audience
of a doc has one source.

### Server instructions and README

- `build_instructions()` gains one line pointing at the index doc URI, kept lean (the
  namespace TOC stays).
- `docs/README.md` gains a short list of the MCP-served docs under the agent tier, so a
  human can see the served set. The docs are not moved; only indexed.

### Generation and drift guard

The new docs are authored under canonical `docs/`, snapshot-packaged into
`mcp/resources/_content/` by `scripts/gen_doc_resources.py` (`just resources-docs`), and
drift-guarded by the existing `resources-docs-check` CI step — the same machinery as the
three current docs.

A new completeness test (mirroring `tests/mcp/test_tool_index.py`) asserts that for every
served toolset doc, each live tool in that namespace is named in the doc, so adding a tool
without documenting its purpose trips CI.

### Relationship to prompts

The three ADR-0202 prompts are kept. The `build_boot_debug` prompt is amended to signpost
`runs.build_install_boot`. The index doc cross-references the prompts. No deprecation.

## Phasing

- **Phase 1 — framework + seed.** Index doc; `DocResource` extended with `required_kind`
  and `audience`; provider-skip in the registrar; `DocExposureMiddleware`; completeness
  drift-guard; instructions + README wiring; seed toolset docs for `runs`, `artifacts`,
  `debug`, `systems`, including the `build_boot_debug` composite fix.
- **Phase 2 — remaining investigation toolset docs.**
- **Phase 3 — admin/ops workflow and operator toolset docs (role-gated).**

Each phase is a bisectable commit set; the framework lands and proves out before bulk doc
authoring.

## Testing

- Registrar provider-skip: a doc with `required_kind` not in `registered_kinds()` is not
  registered; one with a matching kind is.
- Middleware role filter (list): platform-operator token sees operator docs; a caller
  without the platform-operator role does not; an unauthenticated lister sees only
  `audience="all"` (fail-closed).
- Middleware role filter (read): a non-operator read of an `audience="operator"` URI is
  rejected; a platform-operator read succeeds; an unauthenticated read of an operator URI
  is rejected.
- Investigation index names no operator doc or operator URI (so a non-operator never learns
  an operator URI from a doc it can read).
- Completeness drift-guard: every live tool in a namespace that has a served doc is named in
  that doc (the guard checks the name is present, not the quality of the purpose prose).
- Snapshot drift: existing `resources-docs-check` covers content/snapshot sync.
- Instructions name the investigation index URI; `docs/README.md` lists the served set.
- `build_boot_debug` prompt names `runs.build_install_boot`.

## Acceptance criteria

- An agent can read one MCP doc resource that maps the typical session to toolsets and
  links a purpose doc per toolset.
- Each served investigation toolset doc names every tool in that namespace with a purpose
  line. (CI enforces that each tool is named; the prose itself is reviewed by a human.)
- Provider-specific docs are absent on deployments without that provider; operator docs are
  neither listed nor readable by callers without the platform-operator role.
- The served set is visible in `docs/README.md`.
- CI fails if a namespace with a served doc gains a tool the doc does not name, or if a doc
  snapshot drifts from canonical `docs/`.

## Considered & rejected

See [ADR-0284](../../adr/0284-agent-facing-workflow-docs.md) for the decision record and
rejected alternatives (new composite tools; serving the generated per-namespace reference;
folding the prompts away; ungated docs; embedding the full workflow in server
instructions).
