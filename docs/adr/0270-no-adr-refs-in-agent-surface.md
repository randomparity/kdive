# ADR-0270: keep internal ADR citations out of the agent-facing MCP surface (#880)

- Status: Accepted
- Date: 2026-06-28

## Context

ADRs (`docs/adr/`) are internal design records — they carry tradeoffs, rejected
alternatives, and decision provenance meant for maintainers, not for the agents that
call the production MCP server. But `ADR-NNNN` citations have spread across the
agent-facing surface:

- FastMCP renders each tool function's **docstring** as the tool `description` and each
  Pydantic `Field(description=...)` / model docstring as the **schema** `description` the
  client sees. So an `ADR-0024` citation written as if it were an internal note is in fact
  rendered to the agent. Introspecting the live registry (`build_app` → `list_tools`) finds
  ADR refs in tool descriptions, input-schema field descriptions, and output-schema
  descriptions.
- The single shared ToolResponse output-schema description (`schema_advertising.py`,
  swept onto every tool) cites `ADR-0019`, so the ref appears once per registered tool —
  the bulk of the leak by count.
- One ADR is served wholesale as an MCP resource: `resource://kdive/adr/0080`
  (`resources/registrar.py`), letting an agent read 16 KB of internal design rationale
  directly.

Internal numbering also couples the public contract to an internal document scheme: an ADR
renumber or supersession would silently change agent-visible strings.

`build_app` already exposes the whole surface for introspection — `tests/mcp/core/test_tool_docs.py`
builds the app with a null pool and walks `list_tools()` for its documentation guards — so
the leak is mechanically detectable.

## Decision

**1. Scrub `ADR-NNNN` from every agent-rendered string.** Remove the inline `(ADR-NNNN …)`
citations from tool descriptions, schema field/model descriptions (including the shared
envelope output-schema description and the domain state-enum class docstrings that Pydantic
renders into `state` filter schemas), and the server `instructions`. Where the citation
carried maintainer-useful provenance, move it to a non-exposed location — the module
docstring or an adjacent `#` comment — never a string the client renders. Module docstrings
are not part of the rendered surface (FastMCP uses function docstrings and field
descriptions), so a module-docstring citation is a safe home for provenance.

**2. Stop serving ADRs as MCP resources.** Remove the `adr-0080` `DocResource` from
`DOC_RESOURCES` and delete its packaged snapshot. The remaining served resources are
curated **operator documentation** (`external-build-upload`, `build-source-staging`,
`response-envelope`), not ADRs; their listing **descriptions** are agent-facing schema
strings and are scrubbed of ADR refs too. No `resource://kdive/adr/*` is served.

**3. Add a CI-gated guard.** A new pytest guard (`tests/mcp/core/test_no_adr_leak.py`)
builds the app and fails if `ADR-\d+` appears in any agent-rendered string. It walks **every
string leaf** of the surface (via a recursive `_strings` helper, not a key allowlist):
server `instructions`; each tool's `description` and every string in its `inputSchema` and
`outputSchema` (so a ref hiding in `examples`/`const`/an enum value is caught, not only
`description`/`title`); each registered resource's `name`/`title`/`description`; and each
prompt's `description` and argument descriptions. A vacuity canary asserts the matcher flags
a known-bad string and that `_strings` descends nested structures, so a broken walk cannot
pass silently. The guard runs in `just test`, which CI gates individually.

The guard governs the **rendered contract metadata** — the strings the MCP client displays
as the tool/resource API — not the bodies of the deliberately-published operator reference
documents (see rejected alternatives).

No schema, migration, RBAC, persistence, or config change. The committed tool reference
(`just docs`) and doc-resource snapshots (`just resources-docs`) are regenerated.

## Consequences

- The agent sees tool/field/resource descriptions describing **what the tool does and how
  to call it**, with no internal document numbers. Provenance for maintainers survives in
  module docstrings / code comments and in the ADRs themselves.
- The public contract no longer moves when an ADR is renumbered or superseded.
- `resource://kdive/adr/0080` disappears from `ListMcpResources`. Nothing in the tool
  surface linked to it (the only references were in the registrar entry itself); the
  rationale remains available to maintainers as
  `docs/adr/0080-remote-provisioning-disk-image-profile.md` in the repo.
- The guard pins the property going forward: any new tool/field/resource description that
  cites an ADR fails CI with the exact offending path. Provenance must go in a comment.
- The guard checks rendered metadata, not served document bodies. The three operator docs
  still cite ADRs in their **content** (they are generated from the canonical `docs/` tree
  and serve operators). This is the curated-published-subset escape hatch the issue allows;
  forking the snapshots from canonical to strip refs was rejected.

## Considered & rejected

- **Also scan served resource content bodies.** Would force the packaged snapshots to
  diverge from the canonical `docs/` tree they are generated from (the drift guard asserts
  byte-equality), turning a metadata-hygiene change into an edit of operator-facing
  documentation that legitimately cites ADRs for human operators. The issue scopes the
  resource concern to *serving an ADR as a resource* (removed) and to *descriptions*, not
  to operator-doc bodies. Rejected; the boundary is documented above.
- **A standalone `scripts/check_no_adr_leak.py` wired into `just ci` only.** CI invokes
  recipes individually, so a guard reachable only through the umbrella `ci` target would not
  gate PRs. A pytest guard rides the already-gated `just test`, reuses the existing
  null-pool app-build harness, and lives beside the other registry guards. Rejected the
  separate script.
- **Keep ADR refs but strip them at render time** (a FastMCP description post-processor).
  Hidden machinery that rewrites every description, easy to bypass by a new render path, and
  it still leaves the internal ref in the source as the source of truth for the public
  string. Rejected; scrub at the source.
- **A `# noqa`-style per-string allowlist for "useful" ADR refs.** Reintroduces internal
  numbering into the contract for the cases the issue most wants gone. Rejected; provenance
  belongs in comments, not the rendered string.
- **Move all provenance to a separate machine-readable ADR-map file.** More machinery than
  the leak warrants; module docstrings and adjacent comments already carry provenance by
  repo convention (AGENTS.md). Rejected as premature.
