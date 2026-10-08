# Cross-slot baseline and staging cleanup

## Authority and problem

Issue #3084 and frozen scope6049520496 require another worker slot and the host reconciler to
reclaim a System's baseline and install-staging artifacts. The campaign approved these scopes
and exclusions; the operator explicitly approved this reviewed permission contract.
Build-workspace #3150 and blanket permission rewrites are excluded. POWER #2818 is preserved.

Actual installed-host reproduction used unchanged production creators under slot1 with umask0022.
Baseline, staging System and Run directories were2755 under the configured setgid group. Both
slot2 and the operator received PermissionError through both existing storage removal functions.
This is a creator-mode defect, not absent supplementary membership or missing cleanup calls.

## Design and ownership

Keep local-libvirt storage as the owner of artifact filesystem operations. Add one internal
`ensure_shared_artifact_directory(path)` operation to its existing storage module; callers create
only their existing directory paths, then pass those paths to it. The operation opens the leaf
with O_DIRECTORY|O_NOFOLLOW, inspects the descriptor, and returns unchanged when group rwx is
already present. If bits are missing, require the directory owner to equal the effective UID,
then fchmod the descriptor with the existing mode OR0070. Otherwise raise a categorized
configuration error naming the directory and instructing owner/admin repair. Close the descriptor
on every path. Do not replace inherited group ownership or change any regular file permission.

The three callers are the baseline `.part` directory immediately after `_reset_dir` creates it,
and the persistent staging `<system_id>` and `<system_id>/<run_id>` directories in
`LocalLibvirtInstaller._make_run_dir`. Create and prepare the System directory before creating the
Run directory, so a retry owned by a different slot can traverse the already-shared parent.
The setting already distinguishes INSTALL_STAGING from optional INSTALL_SCRATCH: leave the latter's
separate-scratch creation unchanged. No public API, config key or deployment role changes.

Baseline extraction still downloads the selected pair and atomically renames its prepared `.part`
directory. Provisioning, teardown, retries and the reconciler continue using the existing storage
removal functions; no independent chmod or alternate cleanup path is added to those consumers.
Roots retain their existing provisioned setgid/provider-group contract. Build customization also
calls the baseline extractor: preserve existing world bits so its hypervisor traversal is not
removed. The helper cannot grant parent traversal or correct a misprovisioned group/root.

## Legacy repair and failure model

Legacy directories owned by another slot cannot be chmodded by an unprivileged peer. Document a
bounded owner/admin operation on operator-identified paths after work for that System is quiesced:
verify real nonsymlink directories, expected creator and configured shared group; grant g+rwx only
to the exact baseline and each exact System/Run directory needing it. Keep files, siblings and roots
unchanged. Do not follow symlinks, recurse over a root, chown or automatically elevate privilege.
The operator explicitly approved this bounded legacy repair with the permission rule.

- Actors: administrator-managed shared provider roots, installed slot users and operator/reconciler
  sharing the configured group. Callers derive child names from System/Run UUIDs.
- Assets: artifact bytes, owner/group, atomic baseline publication, unrelated paths and existing
  cleanup ownership. Directory write is shared only with the existing group, never other users.
- Boundaries: selected artifact directory to a chmod syscall; leaf descriptor checks and creator
  ownership gate limit mutation. A foreign restrictive owner or symlink/non-directory fails.
- Parent provisioning and group membership remain operator-owned. Peer provider-group members
  already possess parent rename/delete access; this does not promise isolation from a malicious
  provider peer or concurrently reconfigured root. Per-System work remains serialized upstream.
- Accepted failure: foreign-owned restrictive legacy directory requires explicit repair; no
  automatic takeover. Wrong roots, inaccessible ancestors and unavailable guest prerequisites
  remain errors, never passing cleanup evidence.

## Success and verification

Existing interfaces and removal consumers remain; only the creators supply the missing directory
access. Unit/structural tests must prove actual modes under0022 and0077, preserved other bits and
group, unchanged regular-file bytes/modes, same-owner restrictive-directory repair, already-shared
foreign-owner acceptance, restrictive foreign-owner refusal and symlink/non-directory refusal.
Do not mock permission-setting or selector logic; inject effective-UID observation only where a
single-user unit environment cannot represent a second owner. Real multi-UID proof remains required.

Creator integration tests must exercise the baseline reset and persistent staging System/Run paths,
including repeat use; optional separate scratch must remain unchanged. Existing baseline atomicity,
install error mapping, provisioning teardown and reaper tests remain selected regression coverage.
A controlled removal of the sharing call must make a creator contract test fail.

On the assigned disposable x86 host, run the candidate's real creators as slot1 under0022 and0077,
then actual removal functions as slot2 and separately as the operator. Verify baseline, staging
System/Run and files are absent, unrelated sentinel bytes/modes unchanged, and an outsider cannot
access the protected roots. Exercise legacy restrictive directories: peer refusal first, exact
approved owner repair, then peer cleanup. Retain candidate and source identities. Run a real
baseline extraction from an existing staged image plus a bounded install-staging creation; a
synthetic kernel file alone is insufficient to claim the extraction caller is integrated.

No full release or native POWER qualification follows from these x86 cleanup results.
