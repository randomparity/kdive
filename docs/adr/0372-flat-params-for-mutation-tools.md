# ADR 0372 — Mutation tools take flat top-level params

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** kdive maintainers

## Context

The MCP tool surface is served to a *black-box* agent host: with deferred tools, the agent
sees only a tool's name and one-line summary until it fetches the full schema, then makes its
first call. So the *shape* of a tool's arguments — flat top-level params vs. a single nested
`request` object — is a contract the agent must be able to predict.

Across the mutation surface that shape was **inconsistent** (#1239). Some mutation tools took
their arguments flat at top level (`systems.provision`, `investigations.close/link/unlink`,
`runs.bind/cancel`, `artifacts.create_run_upload`, …), while others nested every argument under
`request: Annotated[_XxxPayload, Field(...)]` (`investigations.open`, `runs.create`,
`runs.complete_build`, `images.build/publish/upload`, `resources.register_*`,
`allocations.request`, `accounting.set_quota`, `shapes.set`). Nothing in the name or one-line
summary revealed which convention a given tool used, so the first call to a wrapped tool tended
to fail validation (the agent sent the fields flat; the schema wanted them under `request`).
The inconsistency even appeared *within a single file* — in the systems registrar,
`systems.provision` was flat while a sibling tool wrapped under `request`.

Read/query tools are a separate case: several (`*.list`, `artifacts.get/find`,
`accounting.estimate`, the audit queries) group filter/pagination fields under an *optional*
`request` object, which is a deliberate, self-consistent convention for read filters and is not
what a mutating first-call has to get right. This ADR is scoped to **mutation** tools
(`annotations=_docmeta.mutating()` / `_docmeta.destructive()`).

## Decision

**Every mutation tool takes its arguments as flat, top-level `Annotated[T, Field(...)]`
parameters. No mutation tool nests its arguments under a `request` (or any other single-object)
wrapper.**

This is now the project convention; new mutation tools follow it, and reviewers reject a
mutation tool that introduces a request wrapper.

Concretely, for this change:

- Each wrapped mutation `@app.tool` wrapper had its `request: Annotated[_XxxPayload, ...]`
  parameter replaced by the payload's fields as top-level `Annotated[T, Field(description=...)]`
  parameters, preserving every field's type, default, and `Field` description verbatim (the
  `Field` text is the agent-facing contract — ADR-0270 / #880).
- The internal service/handler request records (`InvestigationOpenRequest`, `_RunCreateRequest`,
  `AllocationRequestPayload`, `QuotaSetRequest`, the image build/publish/upload request records,
  the resource-registration records, …) are **kept as internal DTOs**. Each wrapper now
  reconstructs its DTO from the flat params before calling the handler, so handler and service
  code are unchanged.
- The now-dead *public* MCP payload models that existed only to be the wrapper's single argument
  (`_InvestigationOpenPayload`, `_RunsCreatePayload`, `_RunsCompleteBuildPayload`, and the image /
  resource / accounting / shape public wrappers) were **removed**, not deprecated — no dual
  convention and no back-compat shim (per the "replace, don't deprecate" standard).

This is a **breaking** change to the affected tools' call signatures/schemas; there is no schema
migration (the change is to the MCP tool surface, not to any persisted schema or database).

## Consequences

- One predictable convention: an agent can call any mutation tool with flat top-level arguments
  without first fetching the schema to discover a wrapper. The undiscoverable first-call failure
  is gone.
- Callers and tests that invoked these tools with a nested `request={...}` (or constructed a
  public `_XxxPayload`) were updated to pass the fields flat. The generated agent-facing tool
  reference (`docs/guide/reference/*.md`) was regenerated from the live registry.
- Read/query tools keep their optional `request` filter wrapper; this ADR does not touch them.
  The convention line to enforce is specifically "mutation tools are flat," which is the surface
  where a wrong first call has a side effect worth avoiding.

## Alternatives considered

- **Document the wrapper instead of removing it** (issue Option 1: append "(args nested under
  `request`)" to each wrapped tool's one-line summary). Low-effort and non-breaking, but it
  preserves two conventions forever and leans on the agent reading prose to avoid a validation
  error. Rejected in favor of a single convention.
- **Wrap *all* mutations under `request`** (standardize the other direction). Also uniform, but
  it makes the common case (a mutation with two or three scalar args) verbose and nests every
  field a level deeper in the schema for no benefit. Rejected: flat is the simpler default and
  already the majority convention.
- **Flatten reads too.** Out of scope; the read-filter wrapper is self-consistent and a wrong
  first call on a read has no side effect. Left unchanged to keep this change bounded.
