# Fault-inject capability keys are validated where a row is bound and reconciled (#3747)

Decision record: [ADR-0074](../../adr/0074-fault-inject-engine-port-wiring.md) (unchanged: its
text stays true). Follow-up to [#3716](2026-11-20-fault-inject-capability-wiring-3716-design.md).

## Problem

A fault-inject row with a malformed `seed`, `fault_rate`, `max_latency_s` or `hotplug_behaviour`
fails every binding for its Systems, teardown included. Nothing checks the keys when the row is
reconciled, and `_engine_from` returns "no engine" for a falsy value before the parsers run, so
`0`, `false`, `""`, `[]` in the two maps, and a bad `seed` on a row with no rates, are ignored.

## Scope

In: `validate_capabilities` in `providers/fault_inject/capabilities.py`; binding calls it first;
the reconcile merge calls it for `fault-inject` rows; tests.
Out (approved): teardown tolerating a malformed row (still fails closed); a database CHECK on
`resources.capabilities`; other providers' keys.

Ownership: the fault-inject package owns what a valid key is; the reconcile merge only asks.
No port, resolver or schema change.

## Contract

- `validate_capabilities(capabilities)` raises `CategorizedError(CONFIGURATION_ERROR)` naming the
  first malformed key and its value. It runs each key through the parser the runtime already uses
  (`FaultEngine.from_capabilities`, `HotplugBehaviour.from_capabilities`) in isolation, so there
  is one definition of valid. Absent keys, `null` maps and empty maps are valid.
- Binding: the capabilities hook validates before building anything. Because every key is
  checked, falsy malformed values and a bad `seed` fail like any other. The wrappers still install
  only when `fault_rate` or `max_latency_s` is non-empty, so a valid row behaves as before.
- Reconcile: for a `fault-inject` row the merged capabilities (existing row plus declared) are
  validated. A malformed row is reported in `diff.warned` with the key and value and is left as
  stored (no update or adoption write); other rows in the pass are unaffected. A new row is built
  from declared keys only and needs no check.

## Failure model

- Malformed row at reconcile: warned each pass until an operator edits it; no write, no raise, so
  one bad row does not abort the pass.
- Malformed row at binding: unchanged, fails closed for every binding including teardown.
- The skip covers adoption as well as the field overwrite, and a detached row is warned too: a
  row that needs a repair write waits until its keys are fixed, and nothing is written meanwhile.
- A `seed` of `null` or a non-integer on a row with no rates used to bind and now fails closed;
  discovery and `systems.toml` only ever write an integer.
- Direct SQL edit after reconcile: caught at binding, as before.

## Tests

Parametrized malformed and well-formed values against the validator and the registry-built
binding (`tests/providers/fault_inject/test_capability_validation.py`); reconcile against a
migrated database for a malformed row (warned, unchanged) and a well-formed edited row (updated)
(`tests/inventory/test_fault_inject_reconcile.py`). Both fail with the validator calls removed.
