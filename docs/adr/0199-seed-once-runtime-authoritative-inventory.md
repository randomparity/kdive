# ADR 0199 — Seed-once, runtime-authoritative inventory via an override ledger

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-20
- **Deciders:** Platform / core-platform

## Context

[ADR-0021](0021-reconciler-loop-drift-repair.md) makes the reconciler a drift-repair loop;
[ADR-0112](0112-systems-inventory-config.md) makes `systems.toml` the source of truth for
config-owned inventory (`resources`, `build_hosts`, `image_catalog`), re-asserted every pass:
reconcile creates a declared identity, repairs a manually-deleted config row, and prunes (or
cordons, if live) an identity that left the file. The benefit is self-healing — a corrupted or
hand-deleted config row is repaired without operator action.

The cost is that **config-declared inventory is not runtime-mutable**. An operator cannot durably
remove a config-declared host at runtime (reconcile re-creates it from the still-present file
declaration) or modify one in place (`ops.set_host_capacity` is clobbered back to the file value
each pass). The only durable path is editing the file and re-applying the ConfigMap. Issue #429
requires runtime add/remove/modify of config-declared systems and build-hosts that survives
reconcile, plus an export of running state back to the file. See
[the design doc](../design/runtime-mutable-inventory.md) for the full milestone (M2.7).

The load-bearing constraint: we must let runtime mutations win over the file **without losing
drift repair** for identities the operator has not touched, and without an in-place flag (which a
deleted row cannot carry) or a re-label of `managed_by` (which would make export unable to re-emit
a still-declared identity).

## Decision

We will shift the inventory reconcile model from *drift-repair-from-file* to
*seed-once-then-DB-authoritative*, mediated by a per-identity **inventory-override ledger**
(`inventory_overrides`, keyed by `(source_kind, name)`, disposition `detached | removed`).
`systems.toml` seeds initial state and continues to repair drift for identities **with no ledger
entry**; a runtime mutation of a config-declared identity writes a ledger entry that makes the
mutation authoritative. The reconcile inventory pass consults the ledger under the existing
session-scoped `inventory-reconcile` lock:

- **no entry** → today's behavior unchanged (create / repair / prune-or-cordon).
- **`detached`** → ensure the row's identity exists but do not overwrite its runtime-owned fields
  from the file; never prune.
- **`removed`** → do not create; cordon a live row (never auto-drain) per ADR-0112 refuse-if-live,
  then delete the cordoned row once it becomes idle (a ledger-driven delete, since the file still
  declares the identity so the file-departure prune never fires).

A `detached` override is intent-only (it carries no field values), so it protects the live row's
runtime values in place; if that row is hand-deleted, reconcile GCs the entry and re-asserts the
file rather than resurrect stale values under a still-active override. A runtime-`removed`
config-declared identity is re-enabled through an explicit clear-override operation (sub-issue B)
that deletes the entry so the next no-entry pass re-asserts the file — the file still declares the
identity, so editing the file is not the re-add path.

A full-inventory export tool (`ops.export_systems_toml`, M2.7 sub-issue C) writes running state
back to `systems.toml`; once the operator commits the exported file and re-applies the ConfigMap,
the ledger entries agree with the file and reconcile GCs them (a `removed` entry whose identity is
no longer declared; a `detached` entry whose file values equal the live row). This ADR refines
ADR-0021 and ADR-0112; it does not supersede them — the file remains authoritative for every
identity without an override entry, and the refuse-if-live prune contract is preserved.

## Consequences

- **Easier:** operators add/remove/modify config-declared inventory at runtime, durably, without a
  cluster restart or a ConfigMap re-apply; the running state can be exported back to the file for a
  reproducible restart.
- **Harder / new obligations:**
  - A new `inventory_overrides` table and migration (additive, forward-only) and a reconcile-pass
    ledger lookup + GC step.
  - The reconcile inventory pass is no longer a pure function of (file, DB) — it also reads the
    ledger. Tests must cover all three ledger states, including the no-entry drift-repair regression
    that guards the ADR-0021 benefit.
  - Mutation tools that detach/remove a config-owned identity must take the per-identity lock and
    write the ledger in the same transaction as the row change, so a concurrent reconcile pass
    cannot interleave create/prune for that identity.
  - The export is a values snapshot, and a `remote_libvirt` block is a **skeleton**: the
    connection/debug fields the provider reads straight from the file (`gdb_addr`, `gdbstub_range`,
    the three TLS secret refs, `base_image`, `shapes`) are not persisted in `resources`, so the
    export emits them as operator-supplied placeholders with a header comment. They are required
    fields, so an unedited skeleton does not parse; a fresh start reproduces the live inventory only
    after the operator completes them. Images, build_hosts, and cost_classes round-trip with no
    operator step. The export's first task is a field-by-field persistence audit.
- **Unchanged:** discovery-owned (`local-libvirt`) rows stay hardware-probe-owned and outside the
  ledger; all other reconciler duties (orphan teardown, leaked-domain reap, zombie sweep,
  debug-session detach) are untouched.

## Alternatives considered

- **In-place `detached` flag on the row, no ledger.** A boolean handles modify but cannot durably
  express a removal while the file still declares the host: the deleted row has nowhere to carry the
  flag, so the next pass re-creates it from the file. A removal needs a record that outlives the
  row — i.e. the ledger.
- **Three-way last-applied snapshot (GitOps merge).** Diff desired (file) / last-applied / live. A
  host removed at runtime while still declared in the file reads as a *new* declaration → re-created;
  suppressing that requires a tombstone, which is the ledger's `removed` disposition under another
  name. More machinery for the same core record.
- **Re-label `managed_by` from `config` to `runtime` on mutation.** Flipping ownership discards the
  fact that the file still declares the identity, so export cannot faithfully re-emit it and a later
  file edit silently stops applying to it. Override intent is distinct from ownership and belongs in
  its own record.
- **Auto-write the ConfigMap on every runtime mutation.** Couples every mutation to a Kubernetes API
  write and its RBAC, and fails a mutation when the cluster API is unavailable. Export stays an
  explicit operator action; the writeback (sub-issue D) is opt-in.
