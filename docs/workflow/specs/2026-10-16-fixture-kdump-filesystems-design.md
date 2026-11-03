# Fixture kdump filesystem prerequisites

## Problem

Pinned fixture defaults omit filesystems advertised by the existing
CRASH_CAPTURE_RHEL_GUEST requirement. Fedora cannot build its kdump initramfs.
Issue #3219 requires corrected defaults, rebuilt identities for both pinned
baselines, and a Fedora guest reporting kexec_crash_loaded=1.

## Scope

The operator approved the exact #3219 design on 2026-10-16 (campaign design
SHA256 d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418).
Set SQUASHFS, SQUASHFS_ZSTD, EROFS_FS, EROFS_FS_ZIP,
EROFS_FS_ZIP_LZMA and OVERLAY_FS to y in fixtures/kernel/debug.config.
Both x86_64 and ppc64le consume this shared fragment. Keep only FW_CFG_SYSFS
in the existing x86 default append. No ownership transition is needed.
Reuse the existing post-olddefconfig requested-symbol validator and registry
clauses in tests; add no validator or duplicated requirement registry.
Custom fragments retain autonomous inputs. Verification remains an integrity
and selection check, so older retained manifests remain valid.
Full POWER matrix qualification belongs to #2818; general capture expansion
belongs to #2814. No ADR, migration or new interface is needed.

### Failure model

- Actors and deployments: operators building native x86_64 or ppc64le fixtures;
  existing CI exercises the builder with mocked external commands.
- Invariants and assets: requested symbols survive olddefconfig before compiling;
  exact retained bytes determine identity; custom inputs remain autonomous.
- Accepted failure classes: unavailable build tools fail through existing command
  errors; tampered or dropped inputs fail through existing integrity/config gates.
- Covered elsewhere: POWER matrix #2818; capture expansion #2814; expensive
  build/live resource permission remains an explicit operator gate.

## Success

Both architecture defaults satisfy every advertised RHEL capture clause.
Dropped requested filesystem symbols fail before compilation. Custom inputs
and retained manifest integrity semantics remain intact. Both pinned baselines
receive rebuilt verified identities, and the rebuilt Fedora guest arms kdump;
unit tests alone do not establish those final two criteria.

## Validation

- Default inputs and architecture/custom policy: focused-test:
  tests/scripts/test_kernel_fixtures.py::test_default_capture_input_preserves_arch_and_custom_policy.
- Dropped filesystem rejection: focused-test: the same parametrized family,
  simulating olddefconfig dropping each requested filesystem on both targets.
- Identity and old-manifest integrity: focused-test:
  tests/scripts/test_kernel_fixtures.py; add explicit changed-fragment identity
  and preserved older-default verification cases.
- Rebuilt baseline identities and Fedora arm: task-test-not-applicable:
  native kernel compilation and a real guest are required; execute the existing
  fixture builder/verify and capture carrier only under a separate approved
  host/resource/cleanup plan, retaining exact manifests and arm evidence.
- Run focused tests, just lint and whole-tree just type; one managed pre-push
  just ci and remote CI. Keep build/live proof distinct from those gates.
