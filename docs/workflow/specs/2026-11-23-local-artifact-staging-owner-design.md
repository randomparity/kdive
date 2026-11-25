# Separate local artifact staging from boot recovery

## Problem and outcome

The local boot recovery module also owns exact-version object streaming, immutable artifact
publication, local validation and interrupted-payload cleanup. Provider composition constructs
its materializer both for local recovery and for the narrower shared artifact-stager port.
Keeping that independent work inside the lifecycle module couples those two entry points to
recovery orchestration. Give staging one private sibling owner without changing its behavior.

## Scope and ownership

Create `src/kdive/providers/local_libvirt/lifecycle/boot/_artifact_staging.py`. Move these existing
nodes with executable AST unchanged: `_ExactVersionDescriptorStore`, `_LOCAL_WHOLE_DISK_ROOT`,
`_whole_disk_root_uuid`, `RealLocalExternalBootMaterializer`, the helper block from
`_stream_exact_version` through `_installed_module_manifest`, and `_cleanup_uncommitted_payloads`.
Move their required imports; keep the existing class name, constructor, methods and annotations.
The module imports existing session, recovery-store, kernel-bundle, validation, domain and store
APIs. It never imports the lifecycle orchestration module.

Keep `LocalExternalBootMaterializer`, `_require_whole_disk_root`, `_entry_mode`,
`_prepared_module_manifest`, all lifecycle state machines and capacity accounting in
`external_boot.py`. Import `_open_descriptor_digest` there for the existing debuginfo reader.
Remove the old concrete implementation and unused imports; do not add forwarding aliases.
The existing `staged_write.py` uses replacement semantics for unrelated lifecycle files and is
not an owner for immutable descriptor-relative staging.

Migrate the concrete import in provider composition; both `build_local_external_boot_authority`
and `build_external_boot_artifact_stager` retain their arguments, bodies and return contracts.
Migrate test class/helper imports and monkeypatch lookup owners in materializer, session-mechanism
and external-boot tests. The cleanup test is a verified additional caller of the same moved helper.
Use the existing kernel-bundle owner for replacement-archive fixture construction and import
`ObjectStore` directly from its existing owner. Retain every old behavioral assertion.

### Failure model and threat model

- Actors: provider composition, local recovery operations, remote consumers of the shared stager,
  object-store streams and interrupted retrying processes. Assets: exact artifact bytes, trusted
  descriptor-relative paths, bounded space, private modes, immutable projections, ownership and
  publication order, recovery receipts and cleanup safety.
- Trust crossings: object-store/archive bytes enter validated private files; owned directory
  descriptors select publication targets; committed projection records distinguish durable data
  from disposable interrupted payloads. Preserve every check, byte bound, digest, exact object
  version, no-follow flag, private mode, cleanup allowlist, refusal, fsync and error note.
- The relocation must not create an import cycle or make staging load the lifecycle orchestrator.
  The operation keeps its existing digest implementation through one explicit import. Local-only
  debuginfo staging remains separate from the shared `materialize_artifacts` entry point.
- Accepted: the concrete internal Python import owner and class `__module__` change. These are
  internal surfaces under [ADR-0041](../../adr/0041-versioning-release-process.md); in-tree callers
  migrate together. Public ports, method signatures, domain values and persisted bytes stay.
- No host/VM/libvirt/SSH operation, operator-service change, new dependency, schema, public policy
  or compatibility layer. Existing recovery policy under [ADR-0586](../../adr/0586-local-external-boot-recovery-uses-an-owned-host-directory.md)
  and cleanup policy under [ADR-0601](../../adr/0601-reconstruct-and-finish-cleanup-tombstones.md)
  remain unchanged, so no new ADR is needed. Other queued assembly/journal work stays separate.

## Success and validation

1. One private module owns the exact moved bodies; the old module has no concrete-class/helper
   re-export except the intentionally shared digest helper. The new owner has no lifecycle import.
2. Both existing composition entry points construct that class with the same store and retain
   the authority/session wiring. A focused structural and builder test fails before relocation.
3. Existing exact-version streams, retry equality, validation-before-publication, interrupted
   cleanup and refusal, local-only debuginfo, direct-root, capacity and preparation tests pass.
4. AST comparisons prove moved bodies and retained lifecycle/builder bodies unchanged; normalize
   only explicit test import-owner changes. Compare session/recovery/archive/domain/port/authority
   modules and all SQL bytes. Use deliberate ownership and behavioral faults and restore bytes.
5. Build and install an actual wheel, verify bytes and module origins, cold import both orders,
   and run focused real filesystem/archive tests against installed code. Run lint, whole-tree
   types, documentation checks and commit hooks, then fresh bounded code/security reviews.

## Global Constraints

Python 3.14, managed with `uv`. Declared targets are Linux x86_64 and ppc64le; the host proof runs
on Linux x86_64 and makes no live hardware claim. Use existing dependencies only. Preserve public
contracts and persisted bytes. Keep all changes and conventional per-task commits local.
