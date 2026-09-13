# 0652 — Repair mutation obligations stranded by failed Systems

## Status

Accepted (2026-09-13)

## Context

`failed` is terminal, so `repair_orphaned_systems` does not enqueue teardown for it. An open
remote-module mutation obligation on such a System therefore remains in `retained_owners` and
permanently pins its source and scratch volumes. The authority teardown transition also refuses to
move a failed System to `torn_down` while that obligation is open, so the existing torn-down-only
repair lane cannot reach it.

ADR-0634 already provides a bounded reconciler lane that selects an open mutation obligation on a
terminal System, defers while its teardown can still be running, rechecks under the System advisory
lock, and calls ADR-0629's fixed `terminal_escape` SECURITY DEFINER write. That write is
idempotent and is available to the reconciler role. The only runtime opener is the external-boot
authority function, which requires an activation in `preparing` with cleanup incomplete. A
restricting activation is therefore the durable recovery owner, not `failed` itself. Activation
creation holds the same System advisory lock as the repair, and a completed activation can no
longer authorize the opener.

## Decision

Extend ADR-0634's existing repair lane to select both `torn_down` and `failed` Systems only when
they have no restricting external-boot activation. Keep its 100-candidate bound, stable ordering,
teardown in-flight/settle-window predicate, advisory-lock recheck, per-candidate failure isolation,
and `worker_discharge_system_mutation_obligations` call. The locked recheck repeats the state,
teardown, and activation predicates. Since activation creation serializes on that lock and the
opener requires the created live activation, no recovery owner can appear between recheck and
commit. The lane remains the sole repair owner and continues to return obligation-row counts.

## Consequences

- A failed System with an open mutation obligation is discharged only after its external-boot
  recovery owner is durably absent; its module volumes can then leave the retained-owner set.
- Ordinary teardown remains unchanged: ADR-0650 prevents new ordinary `torn_down` leaks and
  ADR-0634 continues to repair historical ones.
- No System transition, migration, privilege grant, provider call, or agent-facing contract is
  introduced. The existing discharge audit evidence remains the durable explanation.

## Considered & rejected

- **Treat every failed System as terminal-escape eligible.** verified: the live activation is the
  mutation recovery owner, so state alone cannot authorize a discharge while it restricts the
  System.
- **Create a second failed-System repair lane.** judgment: the existing lane already owns the
  identical bounded, locked, role-fenced discharge operation; another lane would duplicate it.
- **Drop failed rows from `retained_owners`.** verified: that query is the durable obligation
  record, so filtering its read would release storage while leaving the obligation open.
- **Transition failed Systems to `torn_down` before repair.** verified: the authority teardown
  guard rejects that transition while the mutation obligation remains open.
