# 0774 — Shared host-dump spool ownership

## Status

Accepted (2026-10-16)

Implements the operator-approved #3220 permission design.

## Context

Fixed workers and the local libvirt daemon run as separate accounts. The default
0700 temporary directory blocks daemon core creation, and a daemon-owned core need
not be readable by the worker that streams and removes it.

## Decision

Provision `/var/lib/kdive/host-dump` as root:kdive-live-libvirt 3770, reached through
root-owned real ancestors without group/world write access. Fail closed when absent
or unsafe. Create worker-owned shared-group 2710 capture directories and exclusive
0660 regular core destinations. Validate ownership/modes and reject symlinks.
Reject extended access/default ACLs on the spool root rather than trusting mode bits
alone; fail closed when ACL safety cannot be established. Both preparations refuse
symlinks and incompatible ACLs before changing existing root attributes.
Prepare the path and SELinux svirt_image_t labeling through both host installers.

## Consequences

The trusted daemon/worker group can read core contents. This boundary does not provide
tenant isolation. Existing streaming, size ceiling and cleanup ownership remain intact.
Libvirt/confinement compatibility still requires real deployed live capture proof.

## Considered & rejected

- **Keep the private temporary directory.** verified: #3220 records libvirt's permission
  denial opening the worker's 0700 spool under the fixed-worker deployment.
- **Share only the directory.** judgment: precreating the file keeps worker ownership
  explicit instead of relying on the daemon's creation ownership and umask.
- **Run workers as root.** judgment: unnecessary privilege for this shared-group contract.
