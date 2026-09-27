# Local-libvirt module labels at external-boot preparation

## Problem

The local-libvirt installed target identity is derived from an unlabeled module archive. SELinux
guests label the installed tree on boot, and release/recovery rejects those labels as drift.
Issue #2783 and ADR-0583 require the guest's labels in the identity while preserving genuine
drift detection. ADR-0691 records the provider-specific decision.

## Scope

Preparation reads `/etc/selinux/config` from the inactive guest. If SELinux is enabled, it reads
the selected policy's `file_contexts` and optional companion specifications under the guest's
`/etc/selinux/<type>/contexts/files/`. Only bounded regular files and a validated policy type
are accepted. Host libselinux's raw lookup evaluates the final release path for each normalized archive entry
using its file type. The returned context, with the on-disk NUL terminator, becomes the
`security.selinux` xattr in the target manifest. Preparation stores that manifest in the
recovery point before authority-bound activation. Materialized archive validation remains
independent of guest policy.

The libguestfs Python binding takes the label as a string and a separate byte length. The
provider passes the context without an embedded NUL and a length including its terminator;
libguestfs writes the same NUL-terminated bytes that the target manifest records.

Activation reopens the inactive guest and its policy, populates the activation staging tree with
the computed labels, observes the actual staged tree, and requires its manifest to equal the
recorded target before moving any live name. This also detects policy changes after preparation.
Source capture and restoration preserve their existing xattrs. Remote-libvirt labeling and
authority protocol changes are excluded.

Current ownership is split between local-libvirt preparation and the shared recovery writer.
The provider owns policy loading and label decisions; the existing guest-tree adapter applies
labels during writes, while the shared writer continues to copy normalized entries. The local
activation caller compares an observed staged manifest instead of relying on the writer's
archive-derived return value. No shared recovery behavior changes.

### Failure model

| Class | Trigger | Outcome | Recovery |
|---|---|---|---|
| Accepted | SELinux disabled or no guest SELinux configuration | Existing unlabeled identity | None |
| Fail closed | Missing, oversized, malformed, or unreadable enabled policy | Preparation stops before target mutation | Repair guest policy or host prerequisite; retry |
| Fail closed | No context for a module entry or native evaluator failure | Preparation stops | Repair policy; retry |
| Fail closed | Policy changes, xattr write fails, or staged labels differ | Activation stops before publication | Restore policy or start a new preparation |
| Conflict | Guest changes a label after publication | Release/recovery refuses exact target match | Diagnose drift |

### Trust boundaries

The inactive guest supplies configuration and file-context expressions to a privileged worker.
The worker reads only fixed policy paths derived from a restricted policy type, rejects symlinks
and nonregular policy files, and bounds each file and their aggregate size before host libselinux
parses them. The module archive supplies normalized entry paths and types to the evaluator; the
archive validator rejects unsafe paths before evaluation. The resulting labels cross into the
guest image as xattrs only while the fenced operation owns the inactive domain. A staged-tree
readback must equal the prepared identity before a live module name moves. Recovery compares the
actual guest xattrs rather than trusting the policy again.

## Success

- A SELinux guest's recorded installed target digest includes its policy labels for regular files,
  directories, and symlinks under the release tree.
- The staged tree is checked against that exact digest before publication, and unchanged labels
  remain equal after guest relabeling.
- An altered `security.selinux` value remains a recovery conflict.
- The host provisioning declares the native libselinux prerequisite for supported worker hosts.

## Validation

- Focused policy tests: distinct labels for `modules.dep` and `.ko`, file modes, optional policy
  companions, disabled SELinux, invalid/missing policy and unmatched paths; controlled faulty
  labels must fail.
- Focused lifecycle tests: preparation binds labeled identity; activation applies labels before
  the staging comparison; real label drift still conflicts; failure leaves the live name intact.
- `just lint`, `just type`, relevant provider tests and `just ci` before push.
- Run the native live VM tier on a host meeting its runbook requirements, including a SELinux
  guest. If unavailable, report the exact prerequisite and the arms that did run.
