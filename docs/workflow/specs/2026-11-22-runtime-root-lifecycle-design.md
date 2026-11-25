# Configured runtime root lifecycle

## Problem

`runtime_paths` reads console and rootfs configuration at import. Later loaded
snapshots and consumers with captured default arguments keep those first values,
so import order determines paths instead of the active configuration lifecycle.

## Scope

Replace import-time root bindings with `rootfs_dir()` and per-call console-root
resolution. Use the getter for overlay/baseline paths and discovery disk capacity.
Replace captured constructor/invocation defaults in local device volumes,
provisioning allowed roots and rootfs reclamation with `None` resolved at the
existing construction or invocation boundary. Retain the storage getter re-export
used by its direct consumers; migrate obsolete constant imports and test snippets.
Keep explicit roots, the existing allowed-roots fallback, path validation and the
fixed uploads directory outside allowed roots. Existing device-volume roots and
provisioning allowlists retain construction values; do not freeze existing
provisioning path callbacks, which remain dynamic. Add no reload mechanism,
configuration setting, provider algorithm or filesystem permission policy.

### Failure model

- Prevent: frozen import-time configuration and captured default arguments.
- Detect: import-before-load, successive load/reset snapshots, stale new-object
  defaults, changed explicit-root behavior or broken absolute-path validation.
- Recover: revert the local default-binding change; persisted data is unchanged.
- Accepted: this unit does not make configuration reload atomic across ongoing
  operations or freeze every behavior of already-created provider objects.

## Success

1. Path helpers and newly bound defaults follow the active loaded configuration.
2. Explicit roots and captured instance fields preserve their existing semantics.
3. Root validation and uploads isolation remain unchanged.

## Validation

- `Mode: focused-test`: extend `tests/providers/test_runtime_paths.py` with import
  before load, two loaded snapshots and reset/environment reload for overlay and
  console paths; existing invalid-path subprocess checks remain.
- `Mode: focused-test`: existing provisioning, hotplug and discovery test files
  cover baseline/default roots, captured roots/allowlists, explicit roots and the
  disk-usage target across snapshots. Existing rootfs-reclaim tests cover current
  invocation defaults and explicit-root preservation.
- Demonstrate snapshot regressions fail before implementation, then run these five
  files with `just test-verbose`, `just lint`, whole-tree `just type`, documentation
  checks and configured hooks before the local conventional commit.
