# Bind idempotency keys to the call's arguments (#3735)

Decision record: [ADR-0193](../../adr/0193-uniform-mutation-idempotency.md), amended by this
change; extends the #3713 and #3734 amendments and reuses `object_scoped_kind`.

## Problem

A key reused on the same target with different arguments replays the first call's stored
envelope. The second call is neither run nor rejected.

## Scope

In: the object-scoped keyed tools (`systems.plug_device`, `systems.reprovision`, the five
`control.*` tools), the ADR-0193 amendment, tests. Out: any schema change or stored fingerprint,
any change to the replay lookup, key retention, which tools are keyed, and the teardown early
returns that answer before the key is recorded.

## Contract

- `object_scoped_kind(kind, object_id, arguments)` appends a 16-hex SHA-256 digest of the
  sorted-key compact JSON of `arguments`; `None` leaves the kind as before.
- Same key, same target, changed argument: `conflict` / `idempotency_key_in_use`, no work.
  Identical arguments replay. The per-tool argument lists are in the ADR amendment.
- Canonical form is the effective request: defaults applied, clamped values after clamping,
  unset optionals present as `null`, so omitted and explicit default are one request.
- `systems.unplug_device` and `systems.teardown` have no argument besides their targets and
  keep their kind unchanged.
- A row without a digest is never matched: the retry fails closed until retention.

## Failure model

- A site that records under a digested kind but looks up under another would never replay; the
  identical-argument retry tests cover record, lookup and conflict re-resolve per tool.
- A refusal that precedes the record (reprovision in flight, device op in flight) answers before
  the key is consulted; a test pins that it runs nothing.
- A hash collision of 64 bits would let a changed request replay; only the same principal can
  construct it on its own key, so it is accepted.

## Tests

Per keyed tool: identical retry replays, changed argument conflicts and enqueues nothing.
Omitted-versus-default for `control.capture_traffic` through the registered tool and for a
reprovision profile's defaulted fields. A row stored without a digest conflicts. Removing the
digest turns the changed-argument tests red.
