# Spec — Expose operator docs and cited ADRs as MCP resources

- **Issue:** #515
- **ADR:** [0151](../adr/0151-mcp-doc-resources.md)
- **Date:** 2026-06-17

## Problem

kdive registers zero MCP resources, so `ListMcpResourcesTool` returns nothing. The tool
surface cites operator docs (`docs/operating/build-source-staging.md`) and ADRs (ADR-0080)
in schema/error strings, but those are unreachable over MCP. An agent told to satisfy a
prerequisite must guess.

## Goal

Register the operator-facing docs the tool surface already cites as MCP resources, so a
doc named in an error/schema string is reachable over MCP.

## Acceptance criteria (from the issue, made falsifiable)

1. `app.list_resources()` (the server side of `ListMcpResourcesTool`) returns **at least**
   a resource for `docs/operating/build-source-staging.md` and one for ADR-0080.
2. Reading each registered resource by URI returns text equal to `read_text` of the
   corresponding canonical `docs/` file, compared via the same UTF-8 `read_text` on both
   sides so the pre-commit end-of-file fixer cannot split the snapshot from its source.
   The advertised URI round-trips through `list_resources` unchanged (FastMCP 3.4.0 accepts
   the `resource://kdive/...` scheme verbatim — verified).
3. The served content is present in the runtime container image (which ships only `src/`).
4. No request-supplied path selects a file: the resource set is a closed, code-defined
   allowlist. Reading an unregistered URI is a normal FastMCP not-found, never a filesystem
   read of an arbitrary path.

## Design (per ADR-0151)

### Allowlist

`DOC_RESOURCES` in `src/kdive/mcp/resources/registrar.py`, each entry:

- `uri`: a stable `resource://kdive/...` URI.
- `source`: path of the canonical doc relative to repo root (used only by the generator
  and the drift test, never at request time).
- `content_file`: filename of the packaged snapshot under
  `src/kdive/mcp/resources/_content/`.
- `title`, `description`: human metadata for the listing.
- `mime_type`: `text/markdown`.

Initial entries:

| URI | source |
|---|---|
| `resource://kdive/docs/operating/build-source-staging.md` | `docs/operating/build-source-staging.md` |
| `resource://kdive/adr/0080` | `docs/adr/0080-remote-provisioning-disk-image-profile.md` |

### Registration

A plane registrar `register(app)` reads each entry's packaged snapshot via
`importlib.resources` (`Path(__file__).parent / "_content" / content_file`, mirroring the
`db/schema` loader) and calls `app.add_resource(TextResource(uri=..., name=..., title=...,
description=..., mime_type="text/markdown", text=...))`. It is wired into `_PLANE_REGISTRARS`
via a pool-less adapter (resources need neither pool nor assembly). The registrar fails
loudly if a snapshot file is missing — a packaging regression must not silently register an
empty resource.

### Packaged snapshots + drift guard

- `scripts/gen_doc_resources.py`: writes each `source` doc's `read_text` (UTF-8) into
  `src/kdive/mcp/resources/_content/<content_file>` via the same `write_text`, so the
  snapshot is identical under the repo's text-normalizing hooks. `--check` mode regenerates
  into a temp dir and diffs against the committed snapshots, exiting non-zero on drift.
- `justfile`: `resources-docs` (write) and `resources-docs-check` (verify); the latter is
  added to the `ci` recipe **and** to `.github/workflows/ci.yml` as an individual step
  (hosted CI runs sub-recipes directly).

## Tests

- `test_app.py`: `build_app(...)` registers the two doc resources; `list_resources()`
  includes both URIs **verbatim** (no scheme/host normalization); reading each returns text
  equal to `read_text` of the canonical `docs/` file.
- A drift unit test asserts each packaged snapshot equals its canonical source doc (so a
  doc edit without regeneration fails locally, not only in the CI shell recipe).
- Registrar edge: a missing snapshot file raises at registration (packaging regression).
- A guard asserting the two cited strings still point at allowlisted docs would couple the
  test to prose; instead the drift test pins the snapshot↔source equality, and the
  acceptance test pins reachability by URI.

## Out of scope

- RBAC/exposure filtering of resources (they are public; ADR-0151 §3).
- A parameterized resource template or any caller-supplied path (rejected in ADR-0151).
- Auto-discovering every ADR cited anywhere in `src/`. The allowlist starts with the two
  the issue names; growth is additive and reviewed.
