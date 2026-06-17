# ADR 0151 — Expose operator docs and cited ADRs as MCP resources

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

`build_app()` registers tools only; FastMCP's resource manager is never populated, so
`ListMcpResourcesTool` returns nothing (`mcp/app.py` `_PLANE_REGISTRARS`). Yet the tool
surface routinely *cites* operator documentation an agent cannot reach over MCP:

- `runs.create`'s `build_profile` schema says "See docs/operating/build-source-staging.md
  for staging the source" (`mcp/tools/lifecycle/runs/registrar.py`).
- `systems.profile_examples` and a remote-base-volume helper cite ADR-0080
  (`mcp/tools/lifecycle/systems/profile_examples.py`).

An agent told to satisfy an operator prerequisite has no self-serve way to read that
prerequisite — it has to guess (black-box-run defect #7, issue #515).

Two facts shape the mechanism:

- FastMCP 3.4.0 exposes `add_resource(...)` / `@app.resource(...)`; a registered
  `TextResource` carries a URI, mime type, and literal text body served by
  `read_resource`. This is the same registrar-seam shape as tools.
- **The runtime image ships only `src/`.** `Dockerfile` copies `src/kdive` into the
  runtime layer; `docs/` is not copied. Reading a doc from the repo-root `docs/` tree at
  request time would work in a source checkout and fail in every container deploy — the
  exact deploy where an agent most needs the doc. So the served content must live *inside
  the package* (`src/kdive/...`), the way `db/schema/*.sql` migrations already do.

## Decision

### 1. Register a fixed allowlist of doc resources, no caller-controlled path

A new plane registrar (`mcp/resources/registrar.py`) registers one `TextResource` per
entry in a hard-coded `DOC_RESOURCES` allowlist. Each entry binds a stable resource URI
to a packaged content file and a human title/description. There is **no** parameterized
template (`resource://docs/{name}`) and no path component taken from the request: a
template would invite path traversal and would advertise nothing in `ListMcpResources`.
The allowlist is closed; adding a doc is a code change reviewed like any other.

Initial allowlist (the docs the tool surface already cites):

| URI | Source doc |
|---|---|
| `resource://kdive/docs/operating/build-source-staging.md` | `docs/operating/build-source-staging.md` |
| `resource://kdive/adr/0080` | `docs/adr/0080-remote-provisioning-disk-image-profile.md` |

Both are mime type `text/markdown`.

### 2. Ship the content inside the package, generated from canonical `docs/`

`docs/` stays the canonical, human-edited home (the `docs-links` / `docs-paths` guards and
human readers depend on it). The served bytes are committed snapshots under
`src/kdive/mcp/resources/_content/`, produced by a generator
(`scripts/gen_doc_resources.py`) that copies each allowlisted source doc into the package.
A `just resources-docs-check` recipe (added to the CI `ci` recipe and to `ci.yml`)
regenerates into a temp dir and diffs, failing when a snapshot is stale — the same
generate-then-check drift guard the tool reference uses (`gen_tool_reference.py` /
`docs-check`). `just resources-docs` writes the snapshots.

This keeps a single editable source of truth, makes the served content reviewable in the
diff, and guarantees the content is present in the runtime image.

### 3. Resources are public, advisory metadata — not an access-control surface

The doc resources carry no secrets (they are operator-onboarding prose already public in
the repo). `ToolExposureMiddleware` filters tools only; it does not hook
`on_list_resources`, so resources are listed to every authenticated caller. That is
intended: the docs are public. Redaction does not apply (no secret, guest, console, or gdb
output flows through these static files). The auth boundary (`build_verifier`) still gates
the whole MCP surface, resources included.

## Consequences

- `ListMcpResourcesTool` returns the staging doc and ADR-0080; a doc cited in an
  error/schema string is reachable over MCP (the issue's acceptance criteria).
- A new drift guard (`resources-docs-check`) must pass in CI; editing an allowlisted doc
  without regenerating the snapshot fails the build, the same way a stale tool reference
  does. The guard is wired into `ci.yml` individually, not only the umbrella `ci` recipe,
  because hosted CI runs the sub-recipes directly.
- The served content is duplicated (canonical in `docs/`, snapshot in `src/`). The drift
  guard makes the duplication safe; the alternative (reading `docs/` at runtime) is broken
  in the container, which is disqualifying.
- Growing the resource set is additive: append to `DOC_RESOURCES`, regenerate, commit.

## Considered & rejected

- **Parameterized resource template `resource://docs/{path}`.** Rejected: a request-supplied
  path is a path-traversal vector, and a template advertises nothing in
  `ListMcpResources`, so it does not satisfy the acceptance criterion that the cited docs
  be *enumerable*.
- **Read from the repo-root `docs/` tree at request time.** Rejected: the runtime image
  ships only `src/`, so this returns nothing in the container — the deploy where the agent
  most needs the doc.
- **Copy `docs/` into the image in the `Dockerfile`.** Rejected: couples the feature to one
  deploy path (a `pip install` / non-Docker run still has no docs), and the content is not
  importable package data, so unit tests and `list_resources` would diverge from prod.
- **Embed the markdown as inline Python string literals.** Rejected: the prose then lives
  in two divergent editable forms with no drift guard; a snapshot file diffed against the
  canonical doc is reviewable and guarded.
