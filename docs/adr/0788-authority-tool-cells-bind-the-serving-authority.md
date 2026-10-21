# 0788 — Authority tool cells bind the authority that served them

## Status

Proposed

## Context

Issue #3186 qualifies the 32 x86_64 functional cells of `runs.release_external_boot`,
`ops.force_release`, `ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict` on
the installed authority lanes. Two identity gaps stand in the way.

- **`ops.force_release` declares no `authority` role.** On 2026-10-07 the operator chose option A
  for #3112: only the two resolve tools need the authority
  ([break-glass design](../workflow/specs/2026-10-07-breakglass-tool-cells-design.md)). The
  #3185 proof (`held_teardown_release`) shows that the observed ordering — a refusal while the
  authority's teardown effect is held, then a release — exists only on an authority-owned System.
  Its outcome depends on the authority's revision. Issue #3186, whose scope the operator approved
  on 2026-10-16, requires that identity to be recorded and validated.
- **A remote cell records the wrong authority.** ADR-0715 reads the `authority` role from
  `/opt/kdive-provider-authority/revision` on the test host (`run_identity`,
  `tests/integration/live_stack/evidence.py`). A remote-libvirt authority cell is served by the
  authority on the provider host. The remote frame checks that revision against its proof config
  before it mutates anything (`require_installed_authority`), but the record never carries it. On
  the lane, the test host also runs the local authority, so a remote record names that one.
  `qualify` then judges an authority that took no part in the cell.

## Decision

We will make every functional authority tool cell record and require the revision of the authority
that served it.

- `ops.force_release` gains the `authority` role through a `role_overrides` entry
  (`server`, `worker`, `reconciler`, `authority`). `ops.force_teardown` and every rejection cell
  keep their roles.
- For a `remote-libvirt` cell that requires `authority`, `run_identity` also reads the remote
  authority's revision file. It reads it through the proof config's SSH seam, as the authority
  account, the same way `require_installed_authority` does. The cell records that revision as
  `authority`.
  - When the test host also has an authority installed, both revisions are read. If they differ,
    the one that is not the candidate is recorded, following the existing worker-slot rule. A stale
    authority on either host then fails `deployed-revision-mismatch`.
  - When the proof config is unset, the remote revision is unknown and is not recorded. The cell
    then fails `deployed-role-missing`.
  - If the config is set but the read fails or does not resolve, the identity read raises and the
    cell records nothing, as the ADR-0715 amendment does for an unreadable local authority.
- The `Role` vocabulary and the evidence record version stay as they are.

## Consequences

- The contract matrix digest changes, so earlier bindings and records stop qualifying. Earlier
  `force_release` records were all `blocked`, so no qualified result is lost.
- A remote authority cell now needs the remote proof config whenever its identity is read, even
  when the cell stops `blocked` before any mutation. Without it the cell cannot qualify.
- When the two hosts' revisions differ, the record keeps only one of them. It keeps the
  non-candidate one, which is enough to fail the cell. It cannot show both.
- This amends the 2026-10-07 option A decision for `ops.force_release` only.

## Considered & rejected

- **Leave `force_release` without `authority` and rely on the frame's revision gate.** judgment:
  the gate refuses a stale lane but leaves no record, so `qualify` cannot tell a gated run from an
  ungated one. Issue #3186 asks for a recorded identity.
- **Add a `remote-authority` role.** verified: `Role` is a closed `Literal` in
  `scripts/coverage_campaign/evidence.py:37` and is part of the version-1 record. A new role
  changes the record schema and every `Evidence` consumer, for a fact that the existing role name
  can carry.
- **Record the remote revision only when no local authority is installed.** judgment: the lane
  runs both. The local one is involved in neither the remote frame nor the remote tools, so
  recording it alone mislabels the cell.
- **Bind the remote revision from the proof config's `installed_revision`.** judgment: that is
  the operator's claim, not an observation. ADR-0715 records what is deployed, not what was
  configured.
