# 0859 — Preserve local retrieve native failure boundaries

## Status

Accepted (2026-11-24)

## Context

Local retrieve conflates all domain lookup failures with absence. Dump metadata is unguarded,
and native connection cleanup can replace the primary failure or strand a completed spool.
Secure spool preparation and streaming remain governed by ADR-0774 and the current capture port.

## Decision

Use one private context-managed domain/connection boundary in the existing guestfs module for
host-dump, settlement and force-off. Only lookup NO_DOMAIN is benign; other libvirt operation
faults become chained infrastructure errors with bounded System/operation context. Preserve
existing dump failure text/cause and categorize metadata OSError separately.

Keep ordinary connection close best-effort, matching existing guestfs reader cleanup: emit only
fixed operation and exception type, then preserve the primary failure or valid result. Control
BaseExceptions propagate. Transfer host spool ownership only after context exit; attempt existing
best-effort removal on every undelivered-spool failure. Keep preparation and provenance unchanged.

## Consequences

Transport failure cannot imply safe harvesting. Valid capture bytes survive an ordinary close
failure, which remains observable. Native release and filesystem removal remain best-effort;
this does not guarantee immediate handle release or deletion on a broken filesystem.

## Considered & rejected

- **Keep coarse lookup/cleanup handling.** verified:
  `git show 8b32834c5eff:src/kdive/providers/local_libvirt/retrieve/host_dump_capture.py`
  shows blanket lookup handling and completion set before unconditional connection close.
  `guestfs.py::_real_domain_settled` has the same blanket lookup catch at that commit.
- **Reuse hotplug error translation.** verified: `libvirt_hotplug.py::libvirt_failure` at that
  commit has different absence/network policy and adds native error text to details.
- **Raise close errors and discard valid captures.** judgment: a failed connection release does
  not invalidate completed bytes; preserve the result and log bounded cleanup evidence.
- **Add another runtime service or generic runner.** judgment: guestfs already coordinates local
  retrieve domains and supplies host-dump helpers; one three-caller boundary suffices.
