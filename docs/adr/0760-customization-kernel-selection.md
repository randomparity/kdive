# 0760 — Explicit catalog kernel selection for customization boot

## Status

Accepted (2026-10-07). Superseded by [ADR-0761](0761-explicit-retained-kernel.md).

## Context

Issue #3065's required live build crossed its repaired cloud-init upload and reached
ADR-0345's customization boot. The Fedora43 virt-builder template already contains two
non-rescue kernels. The builder passes no hint to ADR-0310's exact selector and therefore
fails closed. The operator approved the catalog-only interface and exact Fedora43 value.

## Decision

Extend ADR-0251's build catalog with optional `customization_kernel`, a nonempty string when
present and `None` when omitted. Forward it only to the existing customization boot extractor.
Reuse ADR-0310's full-filename or bare-version match, rescue exclusion, matching initrd and
stale-hint refusal. Never guess a newest version or fall back after an explicit mismatch.
No CLI/profile/RootfsBuildSpec or provenance-schema extension is proposed.

The Fedora43 virt-builder row requests `6.18.5-200.fc43.x86_64`. Read-only inspection of the
retained source template's grubenv saved entry and matching BLS stanza names that kernel and
initrd. This is a reviewed catalog constant, not a new production bootloader parser.
The original offline directory fix remains unchanged. No other catalog row receives a hint.

## Consequences

Template drift removing the selected kernel stops the build and requires an explicit catalog
update and fresh proof. Omitted hints retain fail-closed behavior. The field governs the
transient customization boot only; final kernel-count/config discovery remains unchanged.
A multi-kernel final image still requires the existing provisioning hint for direct boot.
Successful selection does not replace real build, ready and cleanup evidence.

## Considered & rejected

- **Add a build-fs CLI option.** judgment: wider public input surface for a catalog-owned template
  choice; the existing catalog entry is already available at the customization call site.
- **Infer the newest kernel.** verified: the retained no-hint selector rejects both candidates,
  and ADR-0310 deliberately requires explicit choice; guessing would weaken that contract.
- **Interpret GRUB/BLS defaults at runtime.** judgment: a bootloader policy/parser is unnecessary
  when one explicit constant can reuse the existing selector.
- **Prune kernels or remove the row.** judgment: changes image content or removes the regression
  reference instead of supplying explicit selection; catalog removal is expressly excluded.
