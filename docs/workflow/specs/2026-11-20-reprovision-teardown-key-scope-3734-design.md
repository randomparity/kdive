# Scope reprovision and teardown idempotency keys to the System (#3734)

Decision record: [ADR-0193](../../adr/0193-uniform-mutation-idempotency.md), amended by this
change; extends the #3713 amendment and reuses its `object_scoped_kind` helper.

## Problem

`systems.reprovision` and `systems.teardown` record and look up idempotency rows under the bare
tool name. `idempotency_keys` is unique on `(principal, key)`, so a key reused on a second System
finds the first System's stored envelope and replays it; the second System is never mutated.

## Scope

In: the two tools in `systems/admin.py`, the ADR-0193 amendment, tests. Out: runs,
investigations, allocation and provisioning scopes; same-target argument-mismatch detection
(#3735).

## Contract

- Every replay lookup, conflict re-resolve and record in the two tools passes
  `object_scoped_kind(<tool kind>, system_id)`. Reprovision has three such sites; teardown has
  the ordinary, authority-owned preactivation, settled-routing and break-glass record sites, the
  keyed replay, the `UniqueViolation` re-resolve and the `_key_names_job` lookup, which now takes
  the System id.
- Same key, second System: `conflict` / `idempotency_key_in_use`, no job enqueued. Same key,
  same System: stored envelope replays.
- A row stored under the bare tool name is never matched: the retry fails closed, nothing runs.

## Failure model

- A site left on the bare kind would record under one kind and look up under another; the
  cross-System and bare-row tests exercise record, lookup and conflict re-resolve for both tools.
- No migration: `kind` is an opaque discriminator.

## Tests

Parametrized over both tools: cross-System key reuse conflicts and enqueues nothing while a
same-System retry replays; a bare-kind row conflicts and enqueues nothing. Removing the scoping
turns all four red. The existing keyed-retry test counts rows under the scoped kind.
