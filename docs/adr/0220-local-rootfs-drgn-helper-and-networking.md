# ADR 0220 — Local rootfs stages the `kdive-drgn` helper and enables guest SSH-NIC DHCP

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Issue:** [#724](https://github.com/randomparity/kdive/issues/724) (M2.8 Epic B, closes #682)
- **Refines:** [ADR-0219](0219-local-libvirt-live-drgn-introspection.md) (the live
  `introspect.run` plane this unblocks — its "Rejected" section deferred these two gaps here),
  [ADR-0218](0218-local-libvirt-session-ssh-transport.md) (the drgn-live SSH transport whose
  guest-side reachability this completes — its Decision §1 named the guest-DHCP enablement as
  the one remaining live-proof risk and a one-line `rootfs_build` follow-up).
- **Builds on:** [ADR-0052](0052-bootable-rootfs-image-builder.md) (the in-guest sshd + managed
  authorized key the rootfs build already installs), [ADR-0079](0079-remote-live-debug-transport.md)
  /[ADR-0085](0085-drgn-live-transport-generalization.md) (the `kdive-drgn` in-guest helper
  contract the live path SSH-execs), [ADR-0213](0213-local-kdump-in-guest-prerequisites.md) (the
  same package-membership gate this reuses to stage debug-image-only assets).
- **Spec:** [`docs/design/2026-06-23-local-rootfs-drgn-helper-and-networking.md`](../design/2026-06-23-local-rootfs-drgn-helper-and-networking.md).

## Context

`introspect.run` live drgn introspection is wired (ADR-0219, #677) over the drgn-live SSH
transport (ADR-0218, #697). The live seam SSH-execs the fixed-argv in-guest helper
`/usr/local/sbin/kdive-drgn <tasks|modules|sysinfo>` and parses its section-JSON host-side,
identically to the remote provider (`_DRGN_HELPER`). It cannot be live-proven on local-libvirt
because the `kdive-ready` guest rootfs the local `build-fs` plane (`rootfs_build.py`,
ADR-0092) produces is missing two things the remote base image carries:

1. **The `kdive-drgn` helper.** `rootfs_build` installs the `drgn` package
   (`DEFAULT_DEBUG_FS_PACKAGES`) but does not stage the helper itself; the repo ships the
   reviewed reference implementation at `deploy/remote-libvirt-guest-helpers/kdive-drgn`. Absent
   → the SSH exec returns non-zero → `DEBUG_ATTACH_FAILURE` (ADR-0219's named gap, no false
   success).

2. **Guest networking on the SSH NIC.** ADR-0218 renders a loopback SLIRP `-netdev user`
   forward + `virtio-net-pci` NIC and records that the guest is *expected* to DHCP the NIC
   (SLIRP hands `10.0.2.15`), but `rootfs_build` configures **no** guest networking. Under
   direct-kernel boot (no initramfs, whole-disk-ext4, ADR-0030) the NIC name is not predictable
   and the base template's NetworkManager may not auto-DHCP an otherwise-unconfigured ethernet
   device. If it does not, sshd is unreachable on `root@127.0.0.1:<port>` → `TRANSPORT_FAILURE`.

Both gaps already fail-fast honestly; `introspect.run` maturity is `partial`. This ADR stages
both in the debug image so a live KVM drive (B6 #680) can complete the attach and promote
maturity, closing Epic B #682.

## Decision

In `_real_virt_builder`, gated to the **debug image** (`"drgn" in packages`, the package that
identifies the introspection rootfs — the build-host toolchain image carries neither `drgn` nor
the introspection contract and gets neither asset), append to the virt-builder argv:

### 1. Stage the reviewed `kdive-drgn` helper read-executable

```
--upload <repo>/deploy/remote-libvirt-guest-helpers/kdive-drgn:/usr/local/sbin/kdive-drgn
--run-command 'chmod 0755 /usr/local/sbin/kdive-drgn'
```

The helper source is resolved from the source tree as
`Path(__file__).parents[4] / "deploy" / "remote-libvirt-guest-helpers" / "kdive-drgn"`
(mirroring `components/catalog.py`'s fixtures resolution); `build-fs` runs as `python -m kdive
build-fs` from the source checkout (live-stack runbook), so the worker process sees the repo
tree. If the resolved path is **not a file**, `_real_virt_builder` raises `CONFIGURATION_ERROR`
before invoking virt-builder — a missing reviewed helper fails loud rather than silently
shipping a guest that cannot introspect. The uploaded program is exactly the repo's reviewed
helper, owned `root` (virt-builder uploads run as root in the appliance) and mode `0755`
(read+exec, not writable by non-root): no new injection surface — the SSH transport invokes it
with fixed argv (ADR-0219), and that argv safety is #677's, not re-litigated here.

### 2. Enable DHCP on the SSH NIC via an interface-name-independent NM keyfile

Stage `/etc/NetworkManager/system-connections/kdive-ssh-nic.nmconnection` with:

```ini
[connection]
id=kdive-ssh-nic
type=ethernet
autoconnect=true
autoconnect-priority=-100

[ipv4]
method=auto

[ipv6]
method=ignore
```

Because the keyfile is **multi-line**, it is staged via `--upload` of a `NamedTemporaryFile`
(mirroring the existing readiness `.service` unit, the only other multi-line asset this builder
stages) — **not** `--write`, which the file uses only for the single-line kdump sysctl and which
is fragile for multi-line content in the argv. The upload is followed by
`--run-command 'chmod 0600 /etc/NetworkManager/system-connections/kdive-ssh-nic.nmconnection'`
(NetworkManager refuses to load a world-readable keyfile); the explicit `chmod 0600` is the sole
guarantee of the keyfile mode regardless of the upload default. The connection omits
`interface-name`, so NetworkManager applies it to whichever ethernet device the SSH NIC
enumerates as — robust against the unpredictable direct-kernel NIC name. `[ipv4] method=auto` is
DHCP; SLIRP's built-in DHCP server is still active under ADR-0218's `restrict=on` (which blocks
only guest-initiated *egress*, not the DHCP handshake).
`autoconnect-priority=-100` keeps it a low-priority fallback so it never shadows a base-image
connection on a NIC that already carries one. IPv6 is `ignore` (the loopback forward is IPv4).

This leg is **live-confirm only**: no KVM in CI, so the unit test asserts the keyfile + chmod
argv is present for the debug image, not that the guest acquires a lease. B6 (#680) confirms
reachability on the dev KVM host. If NetworkManager turns out to enable the NIC by a different
mechanism, this is the one-line follow-up ADR-0218 §1 named; the change is contained to this
gated block.

### 3. Maturity is unchanged

`introspect.run` stays `partial`. The descriptor already advertises the wired capability
(ADR-0219); this ADR makes the guest *carry* what the wired path needs, but the live attach
round-trip that promotes maturity — and closes Epic B #682 — is owned by B6 (#680), after the
orchestrator rebuilds and republishes the `kdive-ready` image. Promoting here would be a phantom
claim CI cannot verify (the same descriptor-vs-maturity split B2/B4/ADR-0218 §6/ADR-0219 used).

## Consequences

- A `build-fs --kind debug` image now carries `/usr/local/sbin/kdive-drgn` (0755) and a
  DHCP-any-ethernet NM keyfile (0600); the build-host image is unchanged.
- `build-fs --kind debug` now **requires** the `deploy/remote-libvirt-guest-helpers/kdive-drgn`
  file to be present in the source tree (it always is in a checkout); an operator running from a
  stripped tree without it gets an actionable `CONFIGURATION_ERROR` instead of a guest that
  silently cannot introspect.
- After an image rebuild + republish, a live KVM `introspect.run` over the local drgn-live SSH
  transport can attach; B6 (#680) proves it and promotes `introspect.run` to `implemented`,
  closing Epic B #682.
- No MCP-surface, port, schema, or migration change. No new dependency (virt-builder already
  used; NetworkManager already in the Fedora base).

## Considered & rejected

- **Gate on `kdump-utils` (reuse the kdump predicate verbatim).** Wrong set: the build-host
  image could in principle want kdump without introspection, and the helper/NIC serve drgn
  introspection. `drgn` ∈ packages is the precise debug-image signal.
- **Add a `capabilities`/`kind` parameter to `virt_builder` to gate on `"drgn"` capability.**
  Over-built: the existing kdump gate already keys off package membership; mirroring it keeps one
  shape and avoids threading a new parameter through the seam and its recording test double.
- **Bind the NM connection to a fixed `interface-name` (e.g. `enp0s*`/`eth0`).** Fragile: the
  NIC name under direct-kernel boot is not guaranteed; an interface-name-independent keyfile is
  the robust form.
- **Ship the helper inside the wheel and resolve via `importlib.resources`.** The helper is an
  operator-staged guest asset under `deploy/`, not packaged Python; `build-fs` runs from the
  source checkout, so source-tree resolution matches how the rest of `deploy/` is consumed and
  avoids packaging a guest script into the server distribution.
- **Embed the helper text as a Python string constant in `rootfs_build`.** Duplicates the single
  reviewed reference helper (`deploy/remote-libvirt-guest-helpers/kdive-drgn`) and risks drift
  from the remote provider's allowlisted program; uploading the one repo file keeps one contract.
- **Promote `introspect.run` maturity in this PR.** Phantom claim — no booted guest in CI; B6
  (#680) owns the promotion after the live attach.
