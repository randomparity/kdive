# Host-dump spool ownership (#3220)

## Problem and authority

The local provider gives libvirt a worker-private 0700 temporary directory, so the
fixed-worker deployment's separate daemon account cannot write the core. Issue #3220
requires capture, permission tests, preparation guidance and four live functional cells.
Campaign 2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18 approved scope and
exclusions under SHA256 5ce7042937eaa47a8e1cfb10ba07265e720dac9be3d037e655b927ac17b29b50;
the operator approved this permission design under frozen proposal SHA256
d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418.
[ADR-0774](../../adr/0774-host-dump-spool-ownership.md) records the boundary.

## Design

Keep `_real_host_dump_capture(UUID) -> Path | None` and its caller's streaming ownership.
Prepare `/var/lib/kdive/host-dump` as root:kdive-live-libvirt 3770. Walk from `/` using
no-follow directory descriptors: each ancestor must be root-owned, a real directory,
and not group/world writable; the root must have exactly the approved owner/group/mode.
Reject root extended access/default ACLs through descriptor xattr inspection; an
unreadable ACL state fails closed. Created objects inherit no extended/default ACLs.
No configurable alternative or temporary-directory fallback is added.

Under the validated root create a unique worker-owned directory, initially private,
then set and verify inherited group and 2710. Exclusively precreate a regular `vmcore`
owned by the worker and shared group, set and verify 0660. Directory group traversal
allows libvirt to reopen the existing file without granting group directory listing or
write access. Root sticky mode prevents other group members replacing worker children.
The daemon/worker group is trusted to read cores; this is not tenant isolation.
Libvirt 12.0.0 source preserves an existing regular destination's ownership/mode.

Use the existing Ansible shared-directory entry point and lifecycle installer to install
the root, refusing symlinks in both paths. Before changing existing root attributes,
refuse extended access/default ACLs and parent default ACLs; repeat after root creation.
Preparation never removes host ACLs automatically. Include the root in Ansible's existing SELinux svirt_image_t policy; the
standalone installer must install the same fcontext rule and restore labels when SELinux
is enforcing, failing with a remedy when required utilities are absent.

## Failure model

- Prevent missing/unsafe roots, symlinks, writable or foreign ancestors and mismatched
  created ownership/modes; report actionable CONFIGURATION_ERROR before invoking a dump.
- Detect libvirt dump failure as existing INFRASTRUCTURE_FAILURE. Keep connection closure,
  the MAX_CORE_BYTES ceiling and owned spool cleanup on dump/stat/size failures.
- Accept trust in the shared daemon/worker group, including a malicious member reading cores.
  Cross-tenant sandboxing is outside the approved deployment contract.
- Qualify actual daemon write/read and SELinux confinement through separately authorized
  live default/recovery, direct/gateway host_dump cells. Unit tests cannot establish this.

## Success and validation

Focused provider tests exercise a real filesystem with a substituted root and explicit
identity fixtures: root and ancestor validation, symlinks, outsider access/default ACLs despite misleading modes, exact modes/ownership,
exclusive precreation, successful bytes, missing/inactive domains, connection closure,
dump/stat/ceiling failures and cleanup. Tests first fail against the old implementation.
Provisioning tests parse Ansible tasks/defaults and execute extracted installer helpers
with harmless command boundaries to prove root ownership/mode, SELinux command/failure
behavior and ordering. Run `just lint`, whole-tree `just type`, focused pytest and relevant
Ansible checks, then the managed prepush full gate. No runtime interface/schema changes.

## Scope and operations

Only host_dump_capture.py, corresponding provider/provisioning tests, local_worker_host,
the lifecycle installer, operating guidance and this decision/specification change.
#3225 owns capture-operation validation/finalization; #3219 owns fixture prerequisites.
No host/guest changes, rebuild or live reservation follows from code approval. After final
review propose a bounded four-cell plan with exact deployed candidate, resources, retained
core policy and owned cleanup; live evidence is required before MERGE-READY.
