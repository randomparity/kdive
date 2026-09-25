# 0680 — Local-libvirt provision reports `ready` only after the first boot's readiness marker

## Status

Accepted (2026-09-25)

Amends [ADR-0272](0272-provision-baseline-kernel-boot.md) "Considered & rejected", Option 2:
local-libvirt provision now gates `ready` on a readiness poll.

## Context

The job-lane local-libvirt `provision` (and `reprovision`, which calls it) returns as soon as
`domain.create()` returns, and the worker then commits the System `ready`. The guest's first boot
is still running: cloud-init's per-instance stage, an SELinux relabel, kdump arming. The first
kdive step that powers the domain off can interrupt it; #2757 left SSH host keys as 0-byte files
that way. ADR-0679 made later power-offs clean, but a System that reports `ready` mid-boot still
lies about being usable. ADR-0272 rejected readiness gating as a larger, separable change; #2771
is that change.

The provider-authority lane (`LocalAuthoritySystemProvider`) already withholds its
provision-ready proof until its own retained-console probe sees the marker (`boot_ready`), so the
gap is the job lane only.

## Decision

1. **Wait in the provider.** After define/start, the job-lane `LocalLibvirtProvisioning.provision`
   polls the same one-shot console probe `runs.boot` uses (`_real_readiness`) until the guest
   answers or a monotonic deadline passes. The deadline is `KDIVE_LIBVIRT_BOOT_WINDOW_S` scaled by
   `tcg_deadline_multiplier(accel)` (ADR-0341), where `accel` is the one provision already
   resolves from live capabilities; the same window also caps the poll count, as in `runs.boot`.
   `reprovision` inherits the wait. The probe is an injected seam; a provisioner built without it
   (unit tests, the authority lane) does not wait.
2. **What `ready` means.** The image's `kdive-ready` unit ran: the serial console device exists,
   the family kdump unit reached a terminal state, and `network-online.target` was reached. It
   does not mean cloud-init's final stage finished, and it does not mean the guest's writes are
   on disk; ADR-0679 owns durable power-off.
3. **Failure.** The window elapsing, a crash signature before the marker, or the domain exiting
   raises `PROVISIONING_FAILURE`. Its details carry `first_boot` (`timeout` or `not_ready`) plus
   the existing closed `probe_error` and `crash_signature` values. A probe that itself raises (a
   console read fault) propagates with its own category. The handler drives the System `failed`
   with the category and dead-letters the job.
4. **Cleanup.** Any `CategorizedError` raised after provision reached define/start destroys and
   undefines the System's domain, best effort, before the ADR-0435 reclaim of the overlay and
   baseline this call created. A failed System keeps no running domain.
5. **Retry.** On the same connection, before the ADR-0576 truncate and `defineXML`, provision
   looks the System's domain up. When it is already active, provision redefines it but neither
   truncates the console nor calls `create()`; it waits on the existing log. That domain was
   started by an earlier attempt of the same provision after its own truncate, so the log holds
   the whole boot.
6. **Authority lane.** Its provisioner gets no wait seam, so the marker is waited for once, by the
   lane's existing `boot_ready` probe. It shares `provision()`, so decisions 4 and 5 apply to it.

## Consequences

- A local-libvirt provision or reprovision job now lasts through the guest's first boot: about a
  minute or two on KVM, longer under TCG, and at most the scaled window (900 s KVM, 9000 s at the
  default TCG multiplier) plus one probe. The worker heartbeat renews the job lease for the whole
  wait (ADR-0018), and a running wait cannot be cancelled early. Minting paths such as
  `scripts/live-vm/mint-system.sh` and warm-store minting pay the same time.
- The wait occupies the claiming worker's dispatch lane, which runs one job at a time: the default
  lane for a provision, the ADR-0550 state-fenced lane for a reprovision. On a one-worker host a
  teardown, a Run step, or another System's snapshot queues behind it; this lengthens the fenced
  lane wait ADR-0550 accepted as short.
- A `systems.teardown` that lands during the wait destroys the domain, so the provision job ends
  dead-lettered as `provisioning_failure` (`first_boot=not_ready`) rather than superseded; the
  System still reaches `torn_down`.
- A local rootfs whose image has no `kdive-ready` unit, or a profile whose guest cannot reach it
  (too little memory, a broken first boot), now fails provision after the window instead of
  reaching `ready` and failing at its first `runs.boot`. Every kdive-built image carries the unit.
- The authority lane's `boot_ready` window is not TCG-scaled; this ADR does not change it.
- `systems.check_ssh_reachable` keeps its note that sshd may bind a moment after `ready`: the
  marker is not ordered after `sshd.service`.

## Considered & rejected

- **Do nothing; keep ADR-0272's contract.** judgment: `ready` stays a claim the guest has not made,
  and ADR-0679 alone does not protect the first boot from a step that needs the guest up.
- **Gate in the `systems.*` job handler.** judgment: the console seam, the accelerator, and the
  domain teardown all live in the provider; remote-libvirt and fault-inject would need a no-op
  branch for a wait they cannot perform.
- **`BOOT_TIMEOUT` / `READINESS_FAILURE` categories.** verified: `rg -n 'BOOT_TIMEOUT|READINESS_FAILURE'
  src/kdive/jobs/models.py src/kdive/mcp/tools/lifecycle/runs/common.py` (main @ d2b0686ed) shows
  both carry Run and external-boot recovery meaning; a System failure reads as a provisioning fault.
- **Run the wait outside the worker's dispatch lane.** judgment: it needs a new lane or a
  resumable wait job, a larger change than the gate itself; left as a follow-up.
- **Power-cycle an already-running domain on retry.** judgment: it interrupts the first boot this
  record exists to protect.
- **Order the marker after `cloud-final.service`.** judgment: an image rebuild for a stronger
  meaning nothing here needs; the operator excluded it from #2771.
