# Local external-boot storage ownership

## Problem

`external_boot.py` combines lifecycle orchestration with recovery records, target
projection persistence, and descriptor-relative filesystem operations. Session
mechanisms and the authority adapter import those storage concerns through the
orchestrator. This obscures the ownership established by ADR-0586 and ADR-0601.

## Scope

Move the existing recovery and projection models, both concrete stores, their
constants, and filesystem dependency closure to sibling `recovery_store.py`.
Move the recovery-point digest calculation there so records and storage do not
depend on the lifecycle coordinator. Migrate its internal callers directly.

`external_boot.py` retains lifecycle and guest publication orchestration. Direct
consumers, including session mechanisms, composition, the authority adapter and
tests, import moved symbols from their owner. Remove obsolete definitions and
imports; add no compatibility modules or forwarding helpers. Private tests that
patch filesystem operations must patch the module that now performs them.

This unit preserves public MCP/provider contracts, model fields and aliases,
canonical bytes, digests, filenames, permission checks, descriptor ownership,
locking, publication ordering, failure behavior and bounded reads. Archive
conversion remains the next separate cleanup task in the same run. No schema
change or new dependency is needed. Existing decisions remain unchanged.

### Failure model

- Deployment: the existing local-libvirt provider on Linux x86_64 and ppc64le.
- Wrong ownership: a reverse storage-to-orchestration import defeats the split.
- Regressions: dropped imports, changed patch targets or digest construction can
  break replay and cleanup; preserve source bodies and run boundary tests.
- Accepted: this unit does not simplify the existing publication state machine.

## Success

1. Recovery and projection storage load without importing `external_boot.py`.
2. Direct source/test consumers use the new storage owner, with no duplicated
   implementations or reverse import.
3. Existing record bytes, digests and filesystem safety behavior are unchanged.

## Validation

- `Mode: focused-test`: add a fresh-process storage-import test under
  `tests/providers/local_libvirt/lifecycle/boot/test_recovery_store.py`. Before
  implementation it fails because the module is absent; afterward it rejects a
  controlled reverse import. Run `just test-verbose` with that file.
- `Mode: focused-test`: existing recovery, projection, teardown, ownership,
  symlink and crash-replay tests protect criterion 3. Run `just test-verbose
  tests/providers/local_libvirt tests/jobs/handlers/external_boot/test_orphan_resolution.py`.
  Compare moved AST bodies and canonical-byte baselines; make no new behavior claim.
- Run `just lint`, whole-tree `just type`, and applicable documentation checks.
  Commit this independently before moving archive conversion.
