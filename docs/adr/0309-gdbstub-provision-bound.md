# ADR-0309: keep gdbstub provision-bound; no enable-on-ready path (#1015)

- Status: Accepted
- Date: 2026-07-03
- Builds on [ADR-0210](0210-local-libvirt-live-debug-introspection.md) (the local-libvirt
  gdbstub endpoint resolver), [ADR-0079](0079-remote-live-debug-transport.md) and
  [ADR-0164](0164-diagnostics-worker-vantage-dispatch.md) (`gdb_addr` / `gdbstub_acl` as the
  gdbstub's actual security boundary), [ADR-0272](0272-provision-baseline-kernel-boot.md)
  (provision-time domain rendering that fails closed rather than guessing), and
  [ADR-0281](0281-always-render-ssh-forward.md) (the opposite call for the SSH forward — the
  contrast this ADR turns on). Sibling of the `profile_examples` debug-block work (#1014).

## Context

The gdbstub `-gdb tcp:127.0.0.1:<port>` QEMU argument is a `qemu:commandline` passthrough
rendered into the domain XML only when `section.debug.gdbstub` is set at **provision** time
(`provisioning.py:229` computes `gdb_port`; `xml.py:120`'s `_append_gdbstub` renders it). A
System provisioned without it has no `-gdb` in its domain definition, so
`debug.start_session(gdbstub)` fails `configuration_error`: "System … was not provisioned with
a gdbstub; reprovision with the profile's `debug.gdbstub` set" (`connect.py:260-264`). The only
recovery today is releasing the Allocation and provisioning a fresh one with the flag set.

The issue (BLACK_BOX_REVIEW.md Finding 3(b)) asks whether a plain reboot could add gdbstub to an
already-`ready` System, sparing the teardown. It cannot, and not only by omission: `-gdb` is a
QEMU **process** launch flag, not a live-attachable device. A guest-initiated reboot recycles the
running QEMU process without ever re-reading the domain XML's command line, so it would not pick
up a changed `<qemu:commandline>` even if one were written. Making the flag appear on a live
System requires `virDomainDestroy` + redefine + `virDomainCreate` — a cold relaunch of the QEMU
process, not a reboot. That already discards all in-memory guest state, the same cost a fresh
boot pays; the only thing it would save is the Allocation and the rootfs/overlay rebuild.

Whether or not it is mechanically cheaper than full teardown, the harder question is whether it
should exist at all — that's a security-relevant capability decision, not a plumbing one. The
gdbstub RSP endpoint is **unauthenticated by design** (the RSP protocol has no auth); its only
protection is confinement — loopback-only for local-libvirt (`HostPolicy.require_loopback`,
ADR-0083 §2), or the operator-configured `gdb_addr` ACL for remote-libvirt
(`gdbstub_acl`, ADR-0164/ADR-0184). Attaching a gdbstub session hands the caller raw read/write
over the guest's memory and registers. Today that exposure is a property of the profile an
operator or agent chose *before* the Allocation was granted — visible in the request, reviewable
before any resource is committed. An enable-on-ready path would let that exposure be added to a
System *after* it is already running and possibly mid-debug-session, without the original
provisioning request ever having named it.

This is the opposite call from ADR-0281 (#937), which made the SSH loopback forward render on
**every** domain regardless of profile. That was safe to always-render because the forward is
inert plumbing — it grants no access until a caller separately authorizes a key
(`authorize_ssh_key`) or drgn credential. Gdbstub has no equivalent second gate: reaching the
port *is* the access.

## Decision

Gdbstub stays a provision-time-only knob. No enable-on-ready / redefine-and-relaunch path is
built for local-libvirt or remote-libvirt.

- **Rationale.** The `-gdb` flag is part of the domain definition established at provision,
  consistent with how this codebase already treats provision-time knobs that fail closed rather
  than acquire a runtime override (ADR-0272's baseline-kernel selection is the same pattern: a
  property fixed at provision, discoverable up front, not silently changeable later). Gdbstub is
  additionally unauthenticated at the transport layer, so unlike the SSH forward (ADR-0281) there
  is no cheap way to render it inertly and gate access afterward — rendering it *is* granting it.
  Keeping the decision at provision keeps it at the one point where the profile is reviewed and
  the Allocation's risk is accepted, rather than introducing a second point where an
  already-committed System's exposure can change.
- **The cheap forward-path.** Set `debug.gdbstub` in the profile at provision time. #1014 makes
  this knob visible to an agent by adding a `debug` block to `systems.profile_examples`
  (ADR-0308), so the forward-path costs one field on the provisioning call, not a documentation
  search.
- **No code change.** This ADR ratifies existing behavior; `provisioning.py`, `xml.py`, and
  `connect.py` are unchanged. No new MCP tool, RBAC change, schema/migration, or config change.

## Consequences

- A System provisioned without `debug.gdbstub` cannot gain a gdbstub session without releasing
  its Allocation and reprovisioning with the flag set. That constraint is unchanged from today;
  this ADR records why it is deliberate rather than an outstanding gap.
- The existing `configuration_error` in `connect.py` remains the correct terminal answer for
  `debug.start_session(gdbstub)` against a System not provisioned for it.
- An agent that wants gdbstub availability decides that at provision time — pairs with #1014's
  `profile_examples` visibility so the knob is discoverable before the Allocation is spent.

## Rejected alternatives

- **Redefine the domain and reboot to add `-gdb` in place.** Rejected on two grounds: it is not
  mechanically a reboot (the flag is a QEMU launch argument, requiring destroy+redefine+relaunch,
  which already discards guest state), and it would let an unauthenticated debug surface be added
  to a System after its Allocation was granted, outside the request that established the
  System's risk profile.
- **Always render `-gdb` loopback-bound and gate access with a separate credential**, mirroring
  ADR-0281's SSH-forward treatment. Rejected: the gdbstub RSP protocol has no auth of its own to
  gate; reachability is the access. There is no inert-then-authorized state to render into.
- **Leave the constraint undocumented, treating it as a known limitation.** Rejected: the
  black-box review found this indistinguishable from an oversight without a written rationale;
  the cheap forward-path (provision-time `debug.gdbstub`) also needed a place to be pointed at.
