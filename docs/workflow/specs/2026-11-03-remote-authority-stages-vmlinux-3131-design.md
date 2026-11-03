# Remote authority stages the uploaded vmlinux in the module tree (#3131)

## Problem

The plan can carry the Run's uploaded DWARF `vmlinux` (`debuginfo`, #3129), and ADR-0723 item 5
requires every install path to stage it for drgn-live. The remote-libvirt authority ignores the
member: its only guest write is the offline module appliance (ADR-0585), whose single mutable
destination is `lib/modules/<release>/`. Operator decision (2026-11-03, "Route 2: module tree"):
stage the file as a member of the module source tree. It lands at `/lib/modules/<release>/vmlinux`,
a path drgn 0.2.0 searches by default (read from its shipped `_drgn` binary), and inherits the
appliance's capture, crash-safe replace and restore with no appliance change and no image rebuild.

## Scope

Completion criteria: (1) a plan with a staged vmlinux installs it at `lib/modules/<release>/vmlinux`
through the existing module operation; (2) the source-manifest binding is defined for plans with
and without the member, and stored plans, recovery records and materializations keep their
identities; (3) the prior state of that path is captured and restored by the existing rules;
(4) the choice is in ADR-0723/0724 and the operating docs.

### Design

**Plan.** `DebuginfoSource` gains optional `staged_module_source_manifest`: the
`module-source-manifest-v1` digest of the module tree with the `vmlinux` added as a regular file
named `vmlinux` (mode `0644`). It is omitted from the canonical bytes when absent, so plans stored
before this change keep their identity. Two read-only plan properties: `module_tree_debuginfo`
(the member when it carries the manifest, else `None`) and `module_tree_manifest` (that manifest,
else `module_obligation.source_manifest`). A plan whose member lacks the manifest behaves as
today: the remote authority ignores the member.

**Evidence.** `external-boot-evidence-v1` `debuginfo` gains the same optional key. Build
completion computes it while scanning the bundle: the module manifest document list plus one
`vmlinux` entry, re-sorted and hashed with the existing algorithm. It is left out when the module
tree already has a `vmlinux` entry. `services/external_boot/plan.py` copies it into the member.

**Stager.** `DebuginfoModuleTreeStager` (new, `remote_libvirt/debuginfo_module_tree.py`) wraps the
authority host's `ExternalBootArtifactStager`. After the inner call, when `module_tree_debuginfo`
is set, it streams the exact object version to `<dir>/debuginfo`, verifying SHA-256 and size
(an existing `<dir>/debuginfo` is verified instead of refetched, so a local stager that later
writes the same name coexists). It returns evidence with `module_source_manifest` replaced by
`module_tree_manifest`, and an installed-tree digest of
`sha256(b"kdive-remote-installed-with-debuginfo-v1\0" + base + sha + size)`. That digest is an
identity compared only with itself (state identities in the recovery record); the appliance's
observed manifest is never compared to it.

**Binding.** `derive_operation`, the worker check in `remote_module_authority_preparation.py`
and the host evidence checks use `plan.module_tree_manifest`, so `operation.source_manifest`,
the source volume manifest (`prepare_attempt_volumes`) and the appliance's own check all name
the combined tree. `VolumeRequest` gains `debuginfo: Path | None`; `build_from_archive` hard-links
it into the extracted tree as `vmlinux` (mode `0644`), refusing an existing `vmlinux`, and counts
it toward entry and byte bounds (200,000 entries, 8 GiB). Restore rebuilds the same source
volume from the same plan. `materialization.source_module_manifest` and `installed_module_tree`
carry the new values; no recovery, teardown or cleanup record gains a field.

**Cost.** Materialize and each prepare or restore stream the `vmlinux` once more (at most 1.5 GiB)
and hold it in the work directory; the reservation already counts it (ADR-0724 item 4).

### Failure model

- Actors and deployments: remote-libvirt authority host staging for a worker; operator upgrading
  hosts; no other.
- Invariants: the guest is written only by the appliance; a replaced `lib/modules/<release>/` is
  restorable; plan identity of stored plans never changes; the bytes installed equal the
  uploaded version (SHA-256 plus tree-manifest readback).
- Accepted: a module tree plus `vmlinux` over 8 GiB fails the source build (BUILD_FAILURE; remedy:
  smaller modules, or no `vmlinux` upload); a bundle that already has `lib/modules/<release>/vmlinux`
  gets no staged manifest and the member is ignored (rare; the runtime probe reports the gap); a
  Run built before this change is not staged (ADR-0724 consequences); an old authority host
  rejects a plan with the new key (upgrade order as ADR-0724).
- Covered elsewhere: guest free space #3125; local staging #3130; legacy remote install #3124;
  live x86_64 proof #3135; appliance protocol is unchanged (ADR-0585).

## Success

All numbered criteria above hold for the named remote-libvirt authority path.

## Validation

- Plan member round trip, omission, properties, golden identity: `focused-test`
  `tests/providers/ports/test_external_boot.py`.
- Evidence key equals the manifest of the real tree plus file; collision omits it:
  `focused-test` `tests/providers/local_libvirt/test_validate_external_artifacts.py`.
- Plan construction copies the key: `focused-test` `tests/services/external_boot/`.
- Stager fetch, verify, reuse, mismatch, passthrough: `focused-test`
  `tests/providers/remote_libvirt/test_debuginfo_module_tree.py`.
- Volume build links the file and its readback manifest equals the evidence manifest; collision
  and bounds refused: `focused-test` `tests/providers/remote_libvirt/lifecycle/`.
- Operation derivation and worker check use `module_tree_manifest`: `focused-test`
  `tests/providers/remote_libvirt/test_remote_module_authority_host.py`.
- Docs and ADR amendments: `task-test-not-applicable`; prose with no executable consumer, covered
  by `just records` and the doc guards.
- Live remote-lab proof: open item unless a lab grant exists.
