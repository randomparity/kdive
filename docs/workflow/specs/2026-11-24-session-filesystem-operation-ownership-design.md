# Keep filesystem operations with the boot session

## Problem

The local session factory stores and forwards six filesystem callbacks that production
composition never configures. Its concrete session already owns descriptor-relative
artifact access and atomic download publication. Constructor copies make those platform
operations look like separately configured mechanisms. Existing tests use the callbacks
to prove real ownership, ordering and failure contracts; removing that proof would be wrong.

## Scope and design

Use the existing module helpers `_open_relative`, `_unlink_relative`, `_replace_relative`
and `_temporary_artifact_name`, and standard `os.close`/`os.fsync`, directly at the concrete
session's current operation sites. Remove their six corresponding constructor parameters,
stored fields and forwarding arguments from the factory and concrete session. The complete
source/test census identifies one concrete-session caller, one production factory assembly
using these defaults, and six existing test functions supplying these hooks.

Keep `_download_artifact` as the existing focused publication owner. Its sequence remains:
validate names, open an exclusive no-follow temporary, download by owned descriptor,
fsync, close, then replace relative to the retained artifact directory. Its failure path
still preserves the primary exception, attempts descriptor/temp cleanup and attaches the
same secondary notes. Projection publication, upload and explicit artifact operations keep
their current validations and descriptor ownership; only primitive lookup changes.

Move existing tests' injection to the actual module helper or filesystem operation under
short monkeypatch contexts. Restore shared `os` patches before session/fixture teardown;
close-failure doubles release real descriptors before reporting their simulated failure.
Add real-file default-factory publication and operation-boundary failure observations.
Test instrumentation after factory creation is a private verification technique, not a
runtime mechanism-reconfiguration promise.

Keep higher-level artifact-root, overlay, guest, readiness, observation, cleanup and timing
mechanisms unchanged. Composition remains byte-identical. No new helper/type/framework,
public MCP/provider-port contract, dependency, persistence or deployment is introduced.
[ADR-0587](../../adr/0587-local-external-boot-uses-an-operation-session.md) already assigns
artifact transfers and resource lifetime to this session; no accepted decision changes.

## Failure model

- Actors/deployments: trusted internal factory assembly on Linux x86_64 and ppc64le.
  Only x86_64 local filesystem and provider-double proof is available here.
- Assets: retained descriptors, final artifact bytes, private relative names, exclusive
  temporary publication, ordered durability operations and primary failure identity.
- Accepted classes: existing filesystem/guest operational failures and cleanup notes keep
  current handling. This does not add atomic protection against privileged concurrent
  directory rewriting or alter close-syscall interruption policy.
- Covered elsewhere: session ownership/lease, inactive-domain fences, projection metadata,
  recovery cleanup and guest/hardware behavior retain current owners and tests.

## Success

1. The concrete session owns platform filesystem operations directly; no stale constructor
   hooks, stored copies, forwarding arguments or compatibility aliases remain.
2. Every existing publication/transfer assertion survives migration to operation-boundary
   instrumentation, including exclusive/no-follow access, exact descriptors, fsync/close/
   replace order, partial-download cleanup and primary/secondary errors.
3. Real-file default construction publishes the intended bytes and preserves existing final
   bytes on open, sync, close or replace failure; owned transfer descriptors close once.
   Invalid temporary names refuse before creation; failed unlink preserves the primary.
4. Unrelated source logic, configured mechanisms, factory acquisition cleanup, composition,
   caller behavior and all applied migration bytes remain unchanged.

## Validation

Use existing `test_session.py`. New operation-boundary observations installed after default
factory construction must expose the old captured-hook path: open/sync/close/replace faults
are missed, invalid temporary-name instrumentation is ignored, and an injected unlink
failure loses its secondary note. Confirm those original-source failures alongside a real
successful-publication control before changing source. Preserve all existing assertions.
After migration run session, mechanism, external-boot, composition and system-authority
callers, lint, whole-tree types, docs checks and hooks. Controlled omission of synchronization,
incorrect close/replace ordering and lost cleanup notes must fail; restore exact bytes and
rerun the affected controls. Compare the whole source AST after only the explicit ownership
mapping, protected caller hashes and 198 applied SQL files. Freeze implementation hashes for
fresh independent code and static security reviews. Final campaign CI and whole-branch
reviews remain separate requirements.
