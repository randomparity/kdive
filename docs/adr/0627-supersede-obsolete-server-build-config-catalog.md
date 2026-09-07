# 0627 — Supersede obsolete server-build config catalog decision

## Status

Accepted (2026-09-06)

## Context

ADR-0096 authorized a seeded kdump config-fragment catalog, catalog-backed config resolution,
an implicit build-profile default, and an MCP read tool. All of those were inputs to the
server-build lane.

ADR-0316 removed that lane, its profile and MCP inputs, both config resolvers, the catalog, and
all server-side `.config` validation. ADR-0563 later removed `CONFIG_COMPONENT` declarations,
the last visible trace of ADR-0096's catalog-resolution decision. ADR-0096 remains Accepted even
though none of the runtime surface it authorized remains.

The surviving use of an uploaded Run `effective_config` is different. ADR-0318 owns its
feature-requirement registry and fail-open read behavior, including its explicitly scoped
refusal gates. ADR-0478 owns the RHEL-family advisory requirement set on that registry. Neither
record restores ADR-0096's server-build catalog or fragment path.

## Decision

Supersede ADR-0096. ADR-0316 removed every runtime surface it authorized, so ADR-0096 no longer
governs the live architecture. ADR-0318 and ADR-0478 remain the authorities for the surviving
uploaded-`effective_config` feature guidance and its scoped enforcement.

This is a documentation disposition only. It introduces no server-build lane, catalog, component
source declaration, schema change, or caller-supplied remote kernel path. #1432 remains closed.

## Consequences

- Readers no longer treat ADR-0096 as authorization for a catalog or server-side config merge.
- A future server-build or catalog capability requires a new decision and implementation; it
  cannot be inferred from this historical record.
- Existing uploaded-`effective_config` behavior is unchanged. Its limits and requirements remain
  in ADR-0318 and ADR-0478.

## Considered & rejected

- **Leave ADR-0096 Accepted.** **verified:** `rg -n "ADR-0096|_resolve_config_ref|CONFIG_COMPONENT"`
  over `docs/adr`, `src`, and `tests` on 2026-09-06 found ADR-0563's account that ADR-0316
  removed the resolvers and declarations; leaving the status unchanged would preserve a false
  live authority.
- **Restore the catalog or server-build lane.** **verified:** ADR-0316's Decision deletes both
  the lane and its config machinery. Restoring either is a new product and implementation change,
  outside this docs-only disposition.
- **Fold the remote caller-supplied KERNEL/VMLINUX question into this decision.** **verified:**
  ADR-0563 identifies reopening #1432 as a separate product call, and issue #1970 records that
  its declaration never had a runtime path. This ADR leaves #1432 closed and adds no such path.
