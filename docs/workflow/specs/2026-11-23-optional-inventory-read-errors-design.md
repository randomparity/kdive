# Optional inventory read errors

## Problem

`load_inventory_optional` uses `Path.exists`, which masks inaccessible paths on the
current Python runtime. A real private-directory reproduction gets `InventoryError`
from required loading but `None` from optional loading and reconciler fallback.

## Scope

One Cast unit restores the loader contract of
[ADR-0112](../../adr/0112-systems-inventory-config.md); no new ownership decision.
Delegate optional loading to `load_inventory`. Return `None` only when its
`InventoryError` has field `file` and a `FileNotFoundError` cause; otherwise re-raise.
Remove the existence precheck. Keep strict reading, TOML decoding and validation owned
by the existing loader. Keep reconciler hashing, cache, fallback and path selection;
clarify only its unreadable-file warning and docstrings. No compatibility aliases.

### Failure model

- Actors/deployments: default-file consumers and reconciler on supported developer and
  deployment hosts, including transient file absence and denied reads.
- Assets/invariants: missing defaults remain quiet; required absence stays an error;
  permission, directory, I/O, encoding, TOML and schema errors remain distinguishable
  `InventoryError`s; failed loading must not reach catalog reconciliation.
- Accepted: the existing two-read hash/parse cache is not an atomic filesystem snapshot.
  Root can bypass DAC permissions; real permission tests require an unprivileged UID,
  supplemented by injected filesystem-read failures that run under either UID.
- Covered elsewhere: inventory reconciliation transactionality, pruning, configuration
  path resolution and warning-once behavior retain their existing suites and owners.

## Success

Only an actual missing-file read produces optional `None`. Other failures retain the
original error and cause; required/optional valid parsing remains identical. Reconciler
fallback surfaces unreadable inventory and reports a load failure without reconciling it.

## Validation

- focused-test: extend `tests/inventory/test_loader.py` with actual inaccessible-parent,
  disappearance at read, directory/read-failure and invalid-input cases. The expected
  original RED is silent `None` for denied reads and a raised error after disappearance.
- focused-test: add a real-loader denial case to `tests/reconciler/test_inventory_pass.py`;
  original RED is no exception. Verify warning, cause and no reconciliation. Run these
  files plus `tests/reconciler/test_inventory_pass_cwd.py` and
  `tests/integration/test_reconcile_inventory.py` with Docker required.
- focused-test: rerun the original private chmod reproduction; restore the old precheck
  and a broad error-swallow fault separately, require targeted failures, then restore.
  Run lint, whole-tree types, doc links and hooks. No `Path.exists` stubs or loader mocks
  in the new regression cases; restore fixture permissions in `finally`.
