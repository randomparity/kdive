# ADR 0294 — Pin a v2-capable guest CPU and prove per-family live SSH reachability

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** KDIVE maintainers

## Context

Issue #956 (from `BLACK_BOX_REVIEW.md` Finding 1) reported that `*-kdive-ready` guests
were SSH-unreachable — the always-rendered loopback `hostfwd` (ADR-0281) accepted the
host TCP handshake but the guest never answered the banner, so `authorize_ssh_key`
failed `transport_failure`. The title widened it to "reproduces on both debian and rhel
ready images." The original comments predate **PR#964 (ADR-0288, #962)**, which routed
both families' first-boot through cloud-init (a baked NoCloud seed + a `99-kdive.cfg`
`dhcp4`/`match: {name: "e*"}` drop-in) and ordered `kdive-ready`
`After=network-online.target`. #962 was live-proven on **debian** the same day.

To close #956 we first added a `live_stack`-marked **per-family** reachability test
(below) rather than assume the contract held per family. Running it on a KVM host against
freshly built current-HEAD images revealed:

- **debian** (`debian-kdive-ready-13`): PASSED — real `authorize_ssh_key` round-trip.
- **rhel** (`rocky-kdive-ready-9`): **FAILED** `transport_failure` — and the failure
  reproduces on a from-scratch current-HEAD image, so it is **not** fixture staleness.

Root-causing the rhel failure from the guest serial console showed the guest never
reaches userspace at all:

```
Run /init as init process
Fatal glibc error: CPU does not [support x86-64-v2]
Kernel panic - not syncing: Attempted to kill init!
```

with the CPU reported as `x86_64-v1 ... QEMU Virtual CPU version 2.5+`. The cause is in
the provider, not the image: `render_domain_xml` emitted **no `<cpu>` element**, so
libvirt/QEMU fell back to the default `qemu64` model, which is **x86-64-v1**. EL9 /
RHEL-family glibc requires **x86-64-v2**, so the guest's `ld.so` aborts PID 1 before any
userspace starts — no NIC, no sshd — and the always-rendered forward TCP-connects (QEMU's
listener) but gets no banner (the guest is dead). This is the exact #956 symptom.
Debian 13's baseline is still x86-64-v1, so it booted regardless, which **masked** the
defect: ADR-0288's cloud-init work genuinely fixed debian, but the rhel path's real
blocker was the CPU model, and the rhel leg had never been live-proven post-ADR-0288
(#823 already noted "EL9 boot needs x86-64-v2"). Confirmed by a controlled boot of the
same image: default CPU → glibc panic; `-cpu host` → `systemd[1]` boots past init.

Two documentation artifacts were also left stale by #962: the `debian.py` comment still
claimed "cloud-init's cloud-ifupdown-helper DHCPs the NIC" (false — the mechanism is the
cloud.cfg `dhcp4` config, and cloud-init is no longer disabled), and the `ssh_reachable`
`PlannedSignal` (ADR-0286) still said "sshd/keygen liveness is broken."

## Decision

Fix the rhel/EL9 defect in the provider, prove the reachability contract per family, and
clear the stale docs.

1. **Pin the guest CPU to the host in the local-libvirt domain XML** — add
   `<cpu mode='host-passthrough'/>` to `render_domain_xml`. host-passthrough gives the
   guest the host's CPU (x86-64-v2 or better on any modern KVM host), so every supported
   guest family — including EL9 — boots past its glibc baseline check. It also matches the
   debug/introspection intent of a local ephemeral VM (the guest sees the host's full ISA,
   which drgn/crash/gdb benefit from) and mirrors the host's other KVM domains, which
   already run `-cpu host`.

2. **Add a `live_stack`-marked per-family reachability test** in the local live-stack
   spine. It provisions a `*-kdive-ready` image (a minimal `direct-kernel` profile, **no**
   `ssh_credential_ref` — the forward + virtio NIC render on every domain post-ADR-0281),
   waits for `ready`, asserts `systems.ssh_info` returns a `worker_loopback` endpoint, and
   asserts `systems.authorize_ssh_key` drains to a **succeeded** job. The drained success
   is the load-bearing proof: the worker SSHes into the guest over the per-System managed
   key, which only succeeds if the guest booted, the NIC leased, the forward bridged, and
   sshd answered. A non-succeeded drain raises with the family id and the job's
   `error_category`. It is this test that surfaced the CPU defect.

3. **Parametrize over `{debian, rhel}`** via two test-only env vars,
   `KDIVE_GUEST_IMAGE_DEBIAN` and `KDIVE_GUEST_IMAGE_RHEL`, each skipping only its own
   parameter when unset/missing (the ADR-0035 §4 skip idiom). The vars are registered in
   the env reference.

4. **Correct the stale docs.** Replace the `debian.py` cloud-ifupdown-helper comment with
   the cloud.cfg `dhcp4`/NoCloud reality, and rewrite the `ssh_reachable` `PlannedSignal`
   rationale to say reachability now works and the open question is
   static-signal-vs-runtime-probe, repointing its `tracking_issue` to the follow-up.

5. **Defer the `ssh_reachable` health signal to a fresh issue** — do not promote it here.
   The issue text asks for a runtime probe on `ssh_info`; the existing `PlannedSignal` is
   the static image-capability layer (`images.describe`). The design fork is unsettled, so
   it gets its own issue referencing #956.

The CPU change is a pure domain-XML addition (no schema/migration, tool, RBAC,
error-category, or config change). The test is gated (deselected by `just test`, run by
the operator on a KVM host) and un-gates nothing.

## Consequences

- **Fixed.** EL9/RHEL-family guests boot past the glibc x86-64-v2 barrier and are
  reachable over the always-rendered forward; both families are now live-proven on a KVM
  host, not assumed. The per-family test is the regression harness that caught this and
  will catch a re-breakage (a CPU-model regression, a network-disable drop-in, or a
  readiness-ordering edit).

- **host-passthrough ties the guest CPU to the host.** For this local, single-host,
  ephemeral debug/introspection provider that is the intended behavior (it is what the
  host's other domains use) and has no live-migration concern. A guest built expecting a
  narrower ISA still runs — host-passthrough is a superset.

- **Remote-libvirt is out of scope and unverified here.** `remote_libvirt`'s domain XML
  also emits no `<cpu>`; if it provisions EL9 guests it may share this defect. That path
  needs a remote host to test and is not part of #956 (labeled `provider:local-libvirt`);
  it is flagged for a separate issue rather than changed blind.

- **Residual risk.** The proof is operator-run: CI cannot boot a guest (KVM +
  multi-minute rootfs), so per-family coverage is only as complete as the images the
  operator seeds. This is the accepted cost of a KVM-gated proof, called out in the test's
  skip messages.

- **Follow-up.** The `ssh_reachable` health signal is deferred, not dropped — the fresh
  issue carries it with the static-vs-runtime fork stated.

## Alternatives considered

- **`<cpu mode='host-model'/>` instead of host-passthrough.** host-model asks libvirt to
  synthesize a portable model close to the host. Rejected as unnecessary for a single-host
  local provider: host-passthrough is simpler, exposes the host's full ISA (better for the
  drgn/crash/gdb debug intent), and matches the co-located domains. host-model would be the
  right call if these guests were migrated across heterogeneous hosts, which they are not.

- **A named `x86-64-v2` CPU model.** This pins exactly the EL9 minimum and would be maximally
  portable/deterministic. Rejected: it caps the guest at the baseline (worse for debug
  introspection than the host's real features) and buys portability this local provider does
  not need. host-passthrough already satisfies v2 on any modern host.

- **Declare EL9 unsupported and only fix docs.** Rejected: Rocky/CentOS/RHEL are a
  supported family (#823, ADR-0251); the fix is one XML element.

- **Reuse a single `KDIVE_GUEST_IMAGE` for both families.** Rejected: it cannot prove *per
  family* — a host would prove whichever single family its image is, and it would not have
  isolated the rhel-only CPU defect from the passing debian leg.

- **Assert reachability with a raw host-side `ssh` banner probe in-test.** Rejected: it
  duplicates the worker's SSH path with a second client. The `authorize_ssh_key` job is the
  product's own reachability path; asserting it proves the contract agents actually use.

- **Add a build → install → boot before the check.** Rejected as unnecessary: the forward
  renders at provision and the baseline kernel reaches `ready` (ADR-0272).

- **Set `ssh_credential_ref` on the profile.** Rejected: post-ADR-0281 the forward renders
  on every provision regardless, so it buys nothing and would import the drgn-live
  secret-seeding skip gate, under-proving the contract.

- **Promote `ssh_reachable` to a registered signal now.** Rejected per scope: the
  static-signal vs runtime-probe design is unsettled and belongs in its own issue.
