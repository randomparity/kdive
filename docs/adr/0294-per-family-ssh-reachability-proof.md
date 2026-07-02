# ADR 0294 — Prove per-family live SSH reachability with a gated live-stack test

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** KDIVE maintainers

## Context

Issue #956 (from `BLACK_BOX_REVIEW.md` Finding 1) reported that
`debian-kdive-ready-*` guests were SSH-unreachable — the always-rendered loopback
`hostfwd` (ADR-0281) accepted the host TCP handshake but the guest NIC never leased,
so `ssh` timed out at banner exchange and `authorize_ssh_key` failed
`transport_failure`. The root cause named was the debian family disabling cloud-init
while staging no NIC-up config (the rhel family had a NetworkManager DHCP keyfile, the
debian family had nothing). A follow-up comment then reported a rhel-family
(`rocky-kdive-ready-9`) guest failing identically despite its keyfile, widening the
finding to "no guest is reachable over the always-rendered forward."

Both #956 comments predate **PR#964 (ADR-0288, #962)**, which resolved both root causes
uniformly and was live-proven the same day:

- Both families now route first-boot through `cloud_init_first_boot_args(ctx)`: a baked
  NoCloud seed plus a `99-kdive.cfg` drop-in declaring `dhcp4: true` /
  `match: {name: "e*"}`, stripping any base network-disable drop-in and removing
  `/etc/cloud/cloud-init.disabled`. The hand-rolled NetworkManager SSH-NIC keyfile was
  deleted; cloud-init owns NIC DHCP and sshd host-key generation for every family.
- `readiness_unit()` now orders `kdive-ready` `After=network-online.target` (+ `Wants=`),
  so the serial `ready` marker implies the DHCP lease is up — closing the race
  (`authorize_ssh_key` at `ready` before the lease → `transport_failure`) the rhel-family
  comment hit.

So the **primary defect no longer reproduces**. What remains is bullet 2 of the issue's
own suggested fix: no automated test proves the always-rendered forward reaches a guest
sshd, and it is unproven **per family** rather than assumed. A regression (a family
reintroducing a network-disable drop-in, a base-image change, or a readiness-ordering
edit) would go uncaught.

Two documentation artifacts were also left stale by #962: the `debian.py` comment still
claims "cloud-init's cloud-ifupdown-helper DHCPs the NIC" (false — cloud-init is no longer
disabled, and the mechanism is the cloud.cfg `dhcp4` netplan config, not
`cloud-ifupdown-helper`), and `PLANNED_SIGNALS`' `ssh_reachable` entry (ADR-0286) still
says "sshd/keygen liveness is broken", which #962 fixed.

## Decision

Close #956 by proving the reachability contract per family and clearing the stale docs,
without re-implementing any networking fix (there is none to make).

1. **Add a `live_stack`-marked per-family reachability test** in the local live-stack
   spine module. It provisions a `*-kdive-ready` image with `ssh_credential_ref` set (so
   the forward + virtio NIC render, ADR-0281/0240), waits for `ready`, asserts
   `systems.ssh_info` returns a `worker_loopback` endpoint, and asserts
   `systems.authorize_ssh_key` drains to a **succeeded** job. The drained success is the
   load-bearing proof: the worker SSHes into the guest over the per-System managed key,
   which only succeeds if the NIC leased, the forward bridged, and sshd answered. The
   test releases the allocation on exit and needs no build/install/boot (the forward
   renders at provision and the baseline kernel boots to `ready`, ADR-0272).

2. **Parametrize over `{debian, rhel}`** via two new test-only env vars,
   `KDIVE_GUEST_IMAGE_DEBIAN` and `KDIVE_GUEST_IMAGE_RHEL`, each skipping only its own
   parameter when unset/missing (the ADR-0035 §4 skip idiom). A single-family host proves
   the family it has. The vars are registered in the env reference.

3. **Correct the stale docs.** Replace the `debian.py` cloud-ifupdown-helper comment with
   the cloud.cfg `dhcp4`/NoCloud reality, and rewrite the `ssh_reachable`
   `PlannedSignal` rationale to say reachability now works and the open question is
   static-signal-vs-runtime-probe, repointing its `tracking_issue` to the follow-up.

4. **Defer the `ssh_reachable` health signal to a fresh issue** — do not promote it here.
   The issue text asks for a runtime probe on `ssh_info` (a live TCP/banner check with
   its own failure modes); the existing `PlannedSignal` is the static image-capability
   layer (`images.describe`). The design fork is unsettled, so it gets its own issue that
   references #956.

The test is gated (deselected by `just test`, run by the operator via
`just test-live-stack` on a KVM host) and un-gates nothing. No schema/migration, tool,
RBAC, error-category, or config change.

## Consequences

- **Easier.** The reachability contract is proven per family by the product's own SSH
  path (`authorize_ssh_key`), not by assumption or a one-off operator run; a family that
  reintroduces a network-disable drop-in or breaks the readiness ordering fails the test.
  The two stale docs no longer mislead readers or agents about the (now cloud-init)
  networking mechanism.

- **Harder / residual risk.** The proof is operator-run: CI cannot boot a guest (KVM +
  multi-minute rootfs), so like every `live_stack` test it protects against regression
  only when an operator runs it. The per-family coverage is only as complete as the
  images the operator seeds; a host with one family's image proves one family. This is
  the accepted cost of a KVM-gated proof and is called out in the test's skip messages.

- **Reopen / follow-up.** The `ssh_reachable` health signal is deferred, not dropped —
  the fresh issue carries it with the static-vs-runtime fork stated. If a future family
  or base image is found unreachable, this test is the regression harness to extend.

- **Obligations.** The env vars are documented in the config/env reference; the
  `debian.py` comment and the `PlannedSignal` rationale are kept truthful.

## Alternatives considered

- **Reuse the single `KDIVE_GUEST_IMAGE` for both families.** Rejected: it cannot prove
  *per family* — a host would prove whichever single family its image is, leaving the
  issue's "per family, not assumed" ask unmet.

- **Assert reachability with a raw host-side `ssh` banner probe in-test.** Rejected: it
  duplicates the worker's SSH path with a second, differently-configured client. The
  `authorize_ssh_key` job is the product's own reachability path; asserting it proves the
  contract agents actually use, over the wire.

- **Add a build → install → boot before the check.** Rejected as unnecessary: the forward
  renders at provision and the baseline kernel reaches `ready` (ADR-0272); adding the
  build spine only lengthens the test without exercising the reachability property.

- **Promote `ssh_reachable` to a registered signal now.** Rejected per scope: the
  static-image-signal vs runtime-probe design is unsettled and belongs in its own issue,
  not folded into a test-and-docs close of #956.

- **A `live_vm`-only (non-stack) reachability test.** Rejected: reachability is a
  spine-level, over-the-wire property (provision renders the forward; a worker job proves
  sshd answers), so it belongs in `live_stack`, matching the existing drgn-live spine test.
