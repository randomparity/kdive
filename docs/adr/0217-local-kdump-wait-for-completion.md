# ADR 0217 — Local-libvirt kdump capture waits for the in-guest dump to complete

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers

## Context

ADR-0203 made local-libvirt `CaptureMethod.KDUMP` harvest the guest-written
`/var/crash/<ts>/vmcore` host-side from the System's qcow2 overlay via a read-only libguestfs
mount. Step 1 of that decision was "force-off then read": the seam destroys the domain (so
libguestfs reads a quiescent disk) and then lists/downloads the newest core.

That sequencing assumed the core already existed on the overlay by the time `vmcore.fetch` ran.
For the `injectNMI` crash path that assumption is false. The local `control.force_crash` injects
an NMI; with `kernel.unknown_nmi_panic=1` (ADR-0213) that NMI *panics* the guest, which is only
the **start** of kdump. kdump then `kexec`s a crash kernel, boots it, mounts root, and runs
`makedumpfile` to write `/var/crash/<ts>/vmcore` — tens of seconds of work.

A live re-drive on the rebuilt `fedora-kdive-ready` image (#705, B6/#680) pinned the resulting
race with job timestamps: `force_crash` (injectNMI) at 05:33:19, then `capture_vmcore` at
05:33:26 — **~7 s later** — and `_real_wait_for_vmcore` immediately called `_force_off_domain`
(`domain.destroy()`) and read `/var/crash` once. Seven seconds is far too short for the panic →
kexec → crash-kernel-boot → mount → write sequence, so the domain was destroyed *mid-kdump*,
`/var/crash` was empty on the overlay, and `vmcore.fetch(method=kdump)` returned
`READINESS_FAILURE: no complete core appeared within the capture window`. The function named
`_real_wait_for_vmcore` did not wait — it force-offed and read once.

`CRASHED` (the System state `vmcore.fetch` admits on) marks the panic, not the completed dump.

## Decision

The local KDUMP harvest **waits for the in-guest kdump to complete before forcing the domain off
and reading the overlay**, within a bounded window.

1. **Completion signal = the guest self-shuts-off.** kdump runs a `final_action` after writing
   the core. We pin that to `final_action shutdown` in the `kdive-ready` rootfs (a kdump.conf
   change in `local_libvirt/rootfs_build.py`, staged under the same `kdump-utils`-in-packages
   gate that enables `kdump.service` and the NMI-panic sysctl). A halted guest reports
   `VIR_DOMAIN_SHUTOFF`, which the worker polls for as the unambiguous "dump done" signal.
   Fedora's default `final_action` is `reboot`, which would never self-shut-off; pinning
   `shutdown` makes the signal reliable rather than racing a rebooted guest.

2. **Bounded poll, then harvest.** `_real_wait_for_vmcore` polls the domain power-state until
   `SHUTOFF` (or the domain is gone) within `_KDUMP_SETTLE_TIMEOUT_S` (120 s, at a 3 s interval),
   then force-offs (idempotent — quiesces only a guest that is still/again running) and harvests
   exactly as ADR-0203 §1–3 specified. The wait is never unbounded: on timeout it force-offs and
   harvests anyway. A core already written persists on the overlay even across a kdump reboot, and
   an absent core stays a `READINESS_FAILURE` — the existing "no core → cleanup spool → None"
   contract is unchanged.

3. **Unit-testable poll, live-only wiring.** The poll loop is a pure function
   `_poll_until_settled(is_settled, sleep, *, timeout_s, poll_interval_s)` driven by an injected
   domain-settled probe and a sleep seam, unit-tested with fakes for both the
   settles-after-N-polls and never-settles (bounded-timeout) cases. Only the live libvirt
   `domain.state()` probe (`_real_domain_settled`) and `time.sleep` wiring stay
   `# pragma: no cover - live_vm`, matching the rest of the harvest seam split.

No change to the MCP surface, the job/admission path, the capture orchestration, the object
store, the database schema, or any other provider. `vmcore.fetch(method=kdump)` maturity stays
`partial` — live KVM proof of the end-to-end panic → wait → harvest is a #680/#705 follow-up.

## Consequences

- Local Tier 3 kdump no longer races its own dump: the worker waits out the crash-kernel write
  before destroying the domain, so `/var/crash` is populated when libguestfs reads it.
- The `kdive-ready` image must be rebuilt and republished to carry `final_action shutdown`. That
  rebuild is an operator/orchestrator follow-up (no live infra in this change); the code is
  correct regardless — on a `reboot`-configured guest the bounded-timeout fallback still
  force-offs and harvests the persisted core.
- A successful capture now costs up to the settle window (~tens of seconds for a real dump,
  capped at 120 s) instead of returning immediately. This is inherent to waiting for kdump.
- The poll logic is CI-covered with fakes; real panic → wait → core fidelity remains a
  `live_vm`/runbook exercise, consistent with ADR-0203.

## Alternatives considered

- **Keep the immediate force-off (the ADR-0203 §1 behaviour).** Rejected: it is the bug — it
  destroys the guest mid-kdump and harvests an empty `/var/crash`.
- **Poll the overlay for a `/var/crash` core to appear instead of the domain state.** Rejected:
  reading the overlay with libguestfs while the guest is still writing it is exactly the unsafe
  live-disk read ADR-0203 force-offs to avoid; the dump is also written from the *crash kernel*,
  so a partially written core could be observed mid-write. The domain power-state is an
  out-of-band signal that does not touch the live disk.
- **Leave kdump on the Fedora default `reboot` and rely only on a fixed wait.** Rejected as the
  sole mechanism: a blind fixed wait either over-waits every capture or under-waits a slow dump.
  Pinning `final_action shutdown` gives a precise completion edge; the fixed-window timeout
  remains only as the safety fallback.
- **Unbounded wait for self-shutoff.** Rejected: a guest that never panics, hangs in the crash
  kernel, or is configured `reboot` would wait forever. The window is bounded and degrades to the
  existing readiness failure.
