# 0813 — BYO inventory skeleton and HMC partition identity

## Status

Accepted (2026-11-04)

## Context

Issue #1817 introduces inventory and a refusing runtime before operational planes.
Proposed [ADR-0540](0540-adopt-only-provisioning.md) compares each OOB endpoint for
uniqueness, which would prevent declaring two different LPARs on one HMC. The
operator approved per-LPAR uniqueness and a non-operational skeleton on 2026-11-04.

## Decision

For this skeleton, SSH targets must be unique across BYO declarations. Redfish/IPMI
endpoints must be unique across those two driver kinds. HMC identity is the tuple
(endpoint, managed_system, lpar_name); distinct LPARs may share an HMC. Comparisons
use declared strings, so cross-endpoint aliases remain the operator's responsibility.
This narrows only the inventory identity rule of the Proposed ADR-0540; it does
not accept or implement that record's future adoption/boot/profile decisions.

Inventory persists the validated refs-only BYO declaration in Resource capabilities
for lossless export/writeback. Shared public capability views retain their existing
allowlist. Reconciliation owns declared facts and preserves unrelated runtime facts.
A default-false fadump declaration is an operator claim, never a firmware probe.

The opt-in runtime advertises no operational capabilities and refuses required port
calls. The existing ProviderSupport descriptor gains supports_build/supports_boot,
both default False. Existing local-libvirt, remote-libvirt and fault-inject
compositions explicitly retain True; the catalog derives these tokens from the
descriptor so an unwired/refusing runtime advertises neither. This changes no
provider dispatch seam. Profile examples report BYO unavailable until #1819. Migration
0190 carries the enum CHECK, neutral coefficient and the two schema prerequisites
already assigned to #1817 by the milestone design; later issues own their writers.

## Consequences

Several LPARs can share an HMC without permitting duplicate declared partitions.
This does not prove physical isolation across different aliases. Export preserves
credential reference names, never resolves material, and cannot omit BYO resources.
The skeleton is discoverable representation, not an operational hardware provider.

## Considered & rejected

- **Require every HMC endpoint to be unique.** judgment: this excludes the ordinary
  multi-LPAR inventory the approved scope requires.
- **Infer POWER fadump or emit a local-libvirt example.** judgment: both would claim
  an operational capability this skeleton does not implement.
- **Re-read the original file during export.** judgment: retaining validated declared
  facts with the Resource keeps export deterministic and avoids dropping detached rows.
