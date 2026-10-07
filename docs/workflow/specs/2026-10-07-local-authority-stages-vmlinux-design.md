# The local-libvirt authority stages the uploaded vmlinux (#3130)

Decision: [ADR-0724](../../adr/0724-external-boot-plan-carries-an-optional-debuginfo-member.md)
item 5, amended by this change with the local recovery decision. Issue: #3130, piece 2 of 3 of
#3123. Builds on #3129, which put the optional `debuginfo` member in the plan.

## Problem

The local-libvirt external-boot authority publishes the kernel, initrd and modules, but it
ignores the plan's `debuginfo` member. drgn-live then finds no DWARF `vmlinux` at
`/usr/lib/debug/lib/modules/<release>/vmlinux` and reports `debuginfo_unloadable` (ADR-0723).
The legacy install path (`guest_kernel_writer._stage_vmlinux`) stages that file; the authority
path must do the same, and recovery must put back what was there before.

## Scope

1. **Port types** (`providers/ports/external_boot.py`). Each new member defaults to `None` and
   is left out of `model_dump` when `None`, as `ExternalBootPlan.debuginfo` is, so every
   materialization, recovery point and recovery record stored before this change keeps its
   canonical bytes and identity.
   - `ProviderStateIdentity.debuginfo: ComponentState | None`. This is the recovery state's
     debuginfo component (criterion 2).
   - `MaterializedArtifacts.debuginfo: OpaqueProviderRef | None` and
     `ExternalBootMaterialization.verified_debuginfo_sha256: Digest | None`. A validator
     requires both or neither, as it does for the initrd.
2. **Materialize** (`RealLocalExternalBootMaterializer`). With a member, `_fetch_and_validate`
   streams the exact object version to `.debuginfo.next` through `_stream_exact_version`,
   which now checks `size_bytes` for a `DebuginfoSource` too, and links it to `debuginfo` in
   the projection directory. Revalidation compares the local file's digest and size with the
   plan. It does not stream the object a second time, as the initrd check does: a second
   1.5 GiB copy is not in the ADR-0724 reservation. The new names join the owned payload and
   temporary sets (`PAYLOAD_NAMES`, `_OWNED_TEMPORARY_NAMES`, `_artifact_ref_parts`, the
   uncommitted-payload cleanup and the partial-abort references), so the existing cleanup and
   abort paths remove them.
3. **Guest file unit** (new `GuestDebuginfoFile` in `lifecycle/boot/external_boot.py`). It owns
   three names in `/usr/lib/debug/lib/modules/<release>/`: live `vmlinux`, staging
   `.kdive-<activation>-vmlinux-staging`, and old `.kdive-<activation>-vmlinux-old`. It
   observes each name as absent or as a `PresentComponentState` whose manifest is
   `sha256("kdive-debuginfo-file-v1\0" + canonical JSON of mode, uid, gid, size, sha256)`. The
   content digest comes from libguestfs `checksum("sha256", …)`, so no bytes leave the
   appliance. A name that is not a regular file, or a path directory that is a symlink or not a
   directory, is a conflict. Every step is chosen from the observed layout of the three names
   and the known prior (P) and target (T) states, so a restart repeats or skips a step with no
   new recorded phase:
   - `publish(prior, target)`: from `(P, any, —)`, remove a staging file that is not T, create
     missing directories, upload the projection's `debuginfo` to staging, set mode 0644 and
     owner 0:0, and require it to read back as T; then move live to old when P is present;
     then move staging to live. The end state is `(T, —, P)`.
   - `restore(prior, target)`: from `(T, —, P)` or `(—, T, P)`, move old to live, or remove
     live when P is absent; from `(P, any, —)`, remove staging. The end state is `(P, —, —)`.
   - Any other layout raises `ValueError` and changes nothing, like the module publication's
     conflict rows.
4. **Wiring** (`_RealLocalExternalBootOperation`).
   - `prepare` computes `target_state.debuginfo` from the materialized file and
     `source_state.debuginfo` from the guest's live name, read-only, in the guest context that
     captures the modules. Without a member both stay `None`.
   - `activate_modules` runs `publish` after the module publication completes and before it
     records `module-restored`.
   - `recover_modules` runs `restore` first, in the same guest context, before the module rows,
     including the path where the modules are already terminal. `_settle_unpublished_modules`
     runs it too.
   - When `target_state.debuginfo` is `None` (no member, or a record from before this change),
     none of this runs.
5. **Session seam** (`lifecycle/boot/session.py`). `InactiveGuest` gains `checksum(csumtype,
   path)` and `upload_projection_artifact(artifact, guest_destination)`. The upload opens the
   payload with `open_projection_artifact` and passes the descriptor to libguestfs `upload`, as
   `upload_artifact` does, so the file is not copied to a host temporary file.

The prior file stays in the guest under the old name while the activation is live. Recovery
renames it back, so its content, owner, mode, xattrs and inode return unchanged. No host copy
is made and no `RecoveryObjectBinding` kind is added: the payload `debuginfo` is removed with
the other payloads, and the old name lives in the guest overlay that recovery owns.

Not in scope: the guest free-space check (#3125), remote-libvirt delivery (#3124), catalog
`drgn_version` drift (#3122), remote authority staging (#3131), remote capacity floor (#3133).
Directories that `publish` creates stay after recovery; recovery restores the file, not the
directory tree. The authority's source/target classification (`observe_state`) keeps reading
modules and the definition only.

## Failure model

1. **Actors and deployments:** the local external-boot authority service (installed, root
   helper over libguestfs) acting for a worker; the guest's own root user, who owns its disk.
   Designed for local-libvirt on x86_64 and native ppc64le KVM-HV.
2. **Invariants and assets:**
   - The guest's prior file at the live name, or its absence, after recovery.
   - Identities of stored plans, materializations, recovery points and recovery records.
   - The authority writes only inside the guest's own filesystem, at the three names.
3. **Accepted failure classes:**
   - The guest changes a name while the activation is live: recovery refuses on the conflict
     and needs an operator. Same as the module tree; bounded because only root in the guest
     can do it.
   - Disk space: a prior file doubles guest usage during the activation. Covered by #3125.
   - A prior that is a symlink or other non-regular file: prepare refuses the install.
   - Created directories remain after recovery (empty, under `/usr/lib/debug`).
4. **Covered elsewhere:** free space (#3125); remote authority (#3131).

## Threat model

1. **Boundaries:** widened — the authority's guest write capability, from `/lib/modules` to
   three fixed names under `/usr/lib/debug/lib/modules/<release>/`. `release` is the validated
   `KernelRelease` from the materialization. Added — none; the object fetch reuses the
   exact-version stream, its digest check and its size bound.
2. **Actors:** the guest's root user (controls the overlay content); the worker (supplies the
   plan, already authenticated by the authority journal).
3. **Controls:** fixed names from validated release and activation id; no-follow directory
   checks; regular-file checks before checksum, move or remove; read-back identity before
   publication; libguestfs confines every path to the guest filesystem.
4. **Out of scope:** a guest that edits its own files during the activation (accepted above).

## Success

- With a member, an activation ends with live `vmlinux` equal to the uploaded object and mode
  0644, owner 0:0; recovery or rollback ends with the prior file or absence.
- Without a member, the call sequence on the guest is the same as before this change.
- A recovery record, recovery point and materialization written before this change parse,
  keep their identity, and recover as before.
- Live: `test_spine_live_script_over_the_wire` passes through the authority on native ppc64le
  with no `missing_debuginfo`/`debuginfo_unloadable`; the guest file's digest equals the upload;
  a second install restores the prior state. The x86_64 arm needs an authority host.

## Validation

Focused unit tests for each Scope item (see the plan), the layout table of item 3 including
every restart row and the conflict rows, golden identities for the port types, and the live
proof above.
