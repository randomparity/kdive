# 0807 — `systems.get` shows an external boot's conflict evidence to admins

## Status

Accepted (2026-10-26)

## Context

[ADR-0583](0583-external-run-boot-uses-prepared-recovery-points.md) parks an external-boot
activation in `recovery_conflict`, and `systems.resolve_external_boot_conflict` resolves it from
there. The tool requires `observed_identity`, the composite state of the conflict evidence that
[ADR-0805](0805-authority-observed-conflict-is-recorded-from-its-journal-head.md) records.
Nothing under `src/kdive/mcp` read that evidence (#3331), so an operator could not learn the
required input without database access. The tool's own parameter help already pointed at
`systems.get`.

`systems.get` requires the viewer role. The evidence's observed objects are opaque provider
references that name storage the provider holds, and resolution needs admin.

## Decision

1. **`systems.get` returns `data.external_boot_conflict`** while the System's restricting
   activation has a current recovery attempt in `conflict`. It holds `activation_id`,
   `attempt_id`, `observation_id`, `composite_state`, `observed_at`, `objects` and
   `objects_truncated`. `objects` lists at most 64 references in their recorded order.
2. **Only a caller holding admin on the System's project receives the block.** The check is
   non-raising, so every other role reads the same response as before. A caller outside the
   project still gets the not-found answer. `suggested_next_actions` names
   `systems.resolve_external_boot_conflict` only when the block is present.
3. **The read follows the System, never a caller-supplied identifier.** It takes the System's
   restricting activation and that activation's current attempt. A conflict that is no longer the
   current attempt is not reported.
4. **No new tool, migration or role.** The block is an additive field of an existing read.

## Consequences

- An admin resolves a conflict with two calls: `systems.get`, then
  `systems.resolve_external_boot_conflict` with `composite_state` as `observed_identity`. The
  worker still rechecks the identity against a fresh observation before acting.
- `systems.get` is now a viewer tool whose payload depends on the caller's role. The conditional
  is confined to this one block, which is the reason for recording it here.
- A stored attempt row that fails validation fails the admin's read as a server fault, as other
  stored-row rebuilds do. Lower roles never reach that read.
- The tool catalog, exposure matrix and per-tool obligation cells are unchanged. The generated
  CLI help for `observed_identity` and the toolset guide describe where the value comes from.
- ADR-0805's note that no MCP read exposes the composite is now out of date.

## Considered & rejected

- **A new admin-only read tool.** judgment: it adds a catalog entry, an exposure row, a CLI verb
  and a set of obligation cells for one field, and the existing parameter help already names
  `systems.get`.
- **Show the block to every viewer.** judgment: the object references name provider storage and
  a viewer cannot act on them.
- **Report it on `runs.get`.** judgment: the conflict belongs to the System, and the operator may
  not know which Run produced it.
