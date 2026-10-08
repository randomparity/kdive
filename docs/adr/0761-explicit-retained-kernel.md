# 0761 — Explicit retained kernel for the Fedora regression image

## Status

Accepted (2026-10-08)

Supersedes ADR-0760’s customization-only field for this unmerged interface.

## Context

The Fedora43 virt-builder image built at `258221bb` still contains kernels
`6.17.1-300.fc43.x86_64` and `6.18.5-200.fc43.x86_64`. Its required unhinted image-smoke
cell failed during provision in 17.50 seconds. ADR-0310 correctly refuses that ambiguity.
The approved customization hint fixes only the temporary build boot, not the finished image.

## Decision

Replace the unmerged `customization_kernel` catalog field with optional
`retained_kernel`, retaining its exact filename/version selector and the existing Fedora43
value `6.18.5-200.fc43.x86_64`. An omitted field changes nothing. A present field is supported
only for Fedora x86_64 images and governs both customization boot and final kernel retention.
Reject the retired field explicitly so stale catalogs cannot silently lose their selection.
No CLI, provisioning profile, carrier, runtime selector or provenance-schema change.

After normal customization packages finish, verify the running kernel equals the selection.
Query installed RPM metadata and erase only other-release packages named `kernel`, `kernel-core`,
`kernel-modules-core`, `kernel-modules`, or `kernel-modules-extra`, in one explicit transaction.
Use RPM's normal dependency checks and scriptlets; no dependency override or automatic removal
of unrelated packages. Retain every selected-release package and packages outside that name set.
Failure stops customization. Before publication, inspect the actual final boot inventory strictly:
one non-rescue candidate must equal the selected kernel and have its matching initramfs.
No latest-version inference, manual boot-file deletion or weakening of advisory discovery.

## Consequences

The opted-in disposable build artifact loses alternate kernel packages and their package-owned
boot artifacts. The source template is unchanged. An unexpected dependency, retained extra kernel,
missing selected kernel/initramfs or probe failure stops publication; it does not add removal rules.
Normal RPM erasure may run package scriptlets; actual build and unchanged unhinted smoke are required.
The current failed image remains failed evidence. Native POWER remains with #2818.

## Considered & rejected

- **Keep customization-only selection.** verified: the required cell at `258221bb` failed with
  the two listed candidates; the actual selector reproduces that error from retained inventory.
- **Add a provisioning hint or change the carrier.** judgment: moves the image obligation to
  consumers and cannot prove the unchanged required unhinted cell.
- **Delete or hide alternate boot files.** judgment: bypasses package ownership and scriptlets.
- **Erase only the old kernel-core package.** verified: RPM `--erase --test` against a read-only
  copy of the built Fedora43 RPM database exits1 naming three exact old-version dependents;
  erasing the four old runtime packages together exits0. No real erasure was performed.
- **DNF automatic old-kernel cleanup or newest selection.** judgment: introduces version-order
  policy instead of retaining the explicitly selected kernel and a bounded package set.
- **Remove the catalog row.** judgment: violates the approved regression-reference exclusion.
