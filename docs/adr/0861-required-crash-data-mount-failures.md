# 0861 — Fail unreadable required crash-data mounts

## Status

Accepted (2026-11-24)

## Context

Offline harvest reads the fixed /var/crash/*/vmcore and vmcore-incomplete layout after settling
the guest. Treating failed crash-data mounts as optional can hide a captured core and produce
missing-dump readiness guidance. Reader-open errors also bypass spool cleanup. Unrelated /home
faults must not deny healthy capture storage.

## Decision

For normal absolute directory mountpoints, require ancestors/equal of /var/crash and direct
capture-directory mounts under it. Rethrow their ordinary mount failures through the existing
reader infrastructure-error/failed-open cleanup owner. Compare POSIX components locally.
Keep unrelated, similarly prefixed and deeper non-capture directory mounts best-effort.
Include reader construction in the coordinator's existing cleanup block; close only an acquired
reader and remove the allocated spool on failed open. Preserve successful core handoff.

Do not change mount order/options, capture globs, actual absent/incomplete handling or the
settlement policy in [ADR0217](0217-local-kdump-wait-for-completion.md) and incomplete disclosure
in [ADR0251](0251-local-multidistro-rootfs-catalog.md). No recursive layout or symlink resolver.

## Consequences

Unreadable required storage cannot imply an empty observed capture directory. Existing close
remains best-effort and control propagation stays unchanged. The required set is bounded to the
current layout; failed reader opens no longer orphan a spool. Future query-layout changes must
revisit this policy rather than broaden it silently. No new runtime configuration, public shape or persistent data is introduced.

## Considered & rejected

- **Keep every non-root mount optional.** verified:
  `git show f0f5f1545bdc:src/kdive/providers/local_libvirt/retrieve/guestfs.py` shows the broad
  fallback. Actual reader/harvester probes on Python3.14.8/Linux return None after /var,
  /var/crash and direct capture-directory faults; a healthy direct mount produces a core.
- **Fail every mount.** verified: `just test-verbose
  tests/providers/local_libvirt/test_retrieve.py::test_harvest_survives_one_unmountable_filesystem`
  passed at that source, preserving /home tolerance while mounting healthy /var.
- **Leave construction outside cleanup.** verified: the actual reader/coordinator failed-open
  probe on Python3.14.8/Linux categorized a launch fault and closed the SDK handle but left one
  allocated spool; source shows construction precedes try/finally.
- **Add a general filesystem-layout resolver.** judgment: the existing fixed query layout and
  SDK mount map need one bounded condition, not another configuration or discovery subsystem.
