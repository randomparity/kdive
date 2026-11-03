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
`vmlinux` entry, re-sorted and hashed with the existing algorithm. It is left out, and the member
is then ignored, when the module tree already has a `vmlinux` entry or when the combined tree
would exceed the 200,000-entry or 8 GiB module bounds. `services/external_boot/plan.py` copies
it into the member.

**Stager.** `DebuginfoModuleTreeStager(inner, object_store, fetch)` (new,
`remote_libvirt/debuginfo_module_tree.py`) wraps the authority host's `ExternalBootArtifactStager`.
When `module_tree_debuginfo` is set it returns evidence with `module_source_manifest` replaced by
`module_tree_manifest`, and an installed-tree digest
`"sha256:" + hex(sha256(b"kdive-remote-installed-with-debuginfo-v1\0" + base + b"\0" + sha +
b"\0" + decimal size))` computed from plan fields. With `fetch=True` (the module host: prepare
and restore, before the System is stopped) it first streams the exact object version to
`<dir>/debuginfo`, verifying SHA-256 and size; an existing `<dir>/debuginfo` is verified instead
of refetched, so a local stager that later writes the same name coexists. The materializer's
instance uses `fetch=False`: it reads nothing it does not publish. The digest is an identity
compared only with itself in the recovery record; the appliance's observed manifest is never
compared to it.

**Binding.** Every reader of `module_obligation.source_manifest` on the remote path uses
`plan.module_tree_manifest`: `derive_operation` and the execute and restore evidence checks in
`remote_module_authority_host.py`, the worker check in `remote_module_authority_preparation.py`,
and `RemoteModuleVolumePreparationRequestV1._operation_matches_authority`
(`external_boot_authority.py`, a validator outside the reserved finalize region). Task 4 ends with
`rg "module_obligation.source_manifest" src` and justifies each remaining hit. So
`operation.source_manifest`, the source volume manifest (`prepare_attempt_volumes`) and the
appliance's own check all name the combined tree. `VolumeRequest` gains `debuginfo: Path | None`;
`build_from_archive` hard-links it into the extracted tree as `vmlinux` (mode `0644`), refusing an
existing `vmlinux`, and counts it toward entry and byte bounds (200,000 entries, 8 GiB). Restore
rebuilds the same source volume from the same plan. `materialization.source_module_manifest` and
`installed_module_tree` carry the new values; no recovery, teardown or cleanup record gains a
field.

**Cost.** Each prepare or restore streams the `vmlinux` (at most 1.5 GiB) into
`remote_module_work_dir`, then the ext4 source image holds a second copy, so the work-directory
peak grows by about twice the file size (3 GiB); the operating doc states this figure. The
materialization reservation already counts the size (ADR-0724 item 4).

### Failure model

- Actors and deployments: remote-libvirt authority host staging for a worker; operator upgrading
  hosts; no other.
- Invariants: the guest is written only by the appliance; a replaced `lib/modules/<release>/` is
  restorable; plan identity of stored plans never changes; the bytes installed equal the
  uploaded version (SHA-256 plus tree-manifest readback).
- Accepted: the prepare-time bound check is a backstop (BUILD_FAILURE) for a stored manifest the
  tree no longer fits; a vmlinux object version gone at prepare or restore fails like a missing
  bundle version (same category, before the System is stopped), and it must be retained as long
  as the bundle; downgrading below this change while a staged plan is stored is unsupported; a bundle that already has `lib/modules/<release>/vmlinux`
  gets no staged manifest and the member is ignored (rare; the runtime probe reports the gap); a
  Run built before this change is not staged (ADR-0724 consequences); an old authority host
  rejects a plan with the new key (upgrade order as ADR-0724).
- Covered elsewhere: guest free space #3125; local staging #3130; legacy remote install #3124;
  live x86_64 proof #3135; appliance protocol is unchanged (ADR-0585).

## Success

Criteria 1 to 4 above hold on the remote-libvirt authority path, each mapped to a Validation
bullet below. Criterion 5, the live remote-lab proof, is an open item without a lab grant.

## Validation

- Plan member round trip, omission, properties, golden pre-change identity of a member without
  the key: `focused-test` `tests/providers/ports/test_external_boot.py`.
- Evidence key equals `_source_tree_manifest` of a real directory (extracted modules plus the
  file); collision and bound overflow omit it: `focused-test`
  `tests/providers/local_libvirt/test_validate_external_artifacts.py`.
- Plan construction copies the key: `focused-test` `tests/services/external_boot/`.
- Stager fetch, verify, reuse, mismatch, missing version (categorized, before any stop),
  `fetch=False`, passthrough: `focused-test`
  `tests/providers/remote_libvirt/test_debuginfo_module_tree.py`.
- Volume build links the file and its readback manifest equals the evidence manifest; collision
  and bounds refused: `focused-test` `tests/providers/remote_libvirt/lifecycle/`.
- Operation derivation, worker check, request validator, execute and restore wiring (fake stager
  and writer; `VolumeRequest.debuginfo` set only for a staged plan), composition wraps the
  stager: `focused-test` `tests/providers/remote_libvirt/test_remote_module_authority_host.py`
  and `test_external_boot_authority.py`. Teardown needs no change: records hold digests only.
- Docs and ADR amendments: `task-test-not-applicable`; prose with no executable consumer, covered
  by `just records` and the doc guards.
- Live remote-lab proof: `focused-test` `tests/integration/test_authority_vmlinux_live.py`
  (`live_stack`, dormant without a remote authority lane): the file lands with the uploaded
  SHA-256, a real `drgn -k` resolves from it, and the restore removes it. x86_64 only; ppc64le
  stays open.
