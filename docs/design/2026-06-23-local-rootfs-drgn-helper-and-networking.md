# Spec — Local rootfs: stage `kdive-drgn` helper + enable guest SSH-NIC DHCP

- **Date:** 2026-06-23
- **Issue:** [#724](https://github.com/randomparity/kdive/issues/724) (M2.8 Epic B, closes #682)
- **ADR:** [ADR-0220](../adr/0220-local-rootfs-drgn-helper-and-networking.md)
- **Refines:** ADR-0219 (the `introspect.run` live plane this unblocks), ADR-0218 (the
  drgn-live SSH transport whose guest-side reachability this completes).

## Problem

`introspect.run` live drgn introspection is wired (#677, ADR-0219) over the drgn-live SSH
transport (#697, ADR-0218), but it cannot be **live-proven** on local-libvirt because the
`kdive-ready` guest rootfs the local `build-fs` plane produces is missing two things the
remote base image carries. Both currently fail-fast honestly (no false success); without
them a live attach cannot complete:

1. **`kdive-drgn` helper absent.** The live path SSH-execs `/usr/local/sbin/kdive-drgn
   <tasks|modules|sysinfo>` in the guest (`remote_libvirt/debug/introspect.py`
   `_DRGN_HELPER`, and the local `LocalLibvirtLiveIntrospect` live seam runs the identical
   fixed argv per ADR-0219). The repo ships the reviewed reference helper at
   `deploy/remote-libvirt-guest-helpers/kdive-drgn`. `rootfs_build.py` installs the `drgn`
   package (`DEFAULT_DEBUG_FS_PACKAGES`) but does **not** stage this helper, so a real guest
   returns `DEBUG_ATTACH_FAILURE` (non-zero exit: helper not found).

2. **Guest SSH-NIC may not auto-DHCP under direct-kernel boot.** ADR-0218 renders a SLIRP
   `-netdev user,...,hostfwd=tcp:127.0.0.1:<port>-:22` + `virtio-net-pci` NIC and records the
   guest is *expected* to bring the NIC up by DHCP (SLIRP hands `10.0.2.15`), but
   `rootfs_build` configures **no** guest networking and the NIC name is unpredictable under
   direct-kernel boot (no initramfs, no biosdevname/net.ifnames guarantees). If the base
   template's NetworkManager does not auto-DHCP an unconfigured ethernet device, sshd is
   unreachable on `root@127.0.0.1:<port>` → `TRANSPORT_FAILURE`.

## Scope

In `src/kdive/providers/local_libvirt/rootfs_build.py`'s `_real_virt_builder`, gated to the
**debug image** the same way the kdump units / NMI sysctl are gated:

- `--upload <repo>/deploy/remote-libvirt-guest-helpers/kdive-drgn:/usr/local/sbin/kdive-drgn`
  then `--run-command 'chmod 0755 /usr/local/sbin/kdive-drgn'`.
- Write a NetworkManager keyfile connection that DHCPs **any** ethernet NIC, with mode `0600`
  (NM ignores world-readable keyfiles), so the SSH NIC comes up regardless of its kernel name.

Out of scope: rebuilding/republishing the image (orchestrator follow-up) and the live
KVM attach round-trip that promotes `introspect.run` maturity (B6 #680). Maturity stays
`partial`.

## Decisions (see ADR-0220)

### D1 — Gate on the debug image, not on `kdump-utils`

The existing kdump staging keys off `if "kdump-utils" in packages`. The drgn helper and the
SSH NIC serve the **debug/introspection** image, identified by `drgn` ∈ packages
(`DEFAULT_DEBUG_FS_PACKAGES`). Gate the helper + networking on `"drgn" in packages` — the
build-host toolchain image (no `drgn`, no introspection) gets neither. This mirrors the
kdump gate's package-membership predicate and needs no new `virt_builder` parameter.

### D2 — Resolve the helper path from the source tree, fail loud if absent

The helper lives in the repo, not the installed package; `build-fs` runs as `python -m kdive
build-fs` from the source checkout (live-stack runbook), so the worker process sees the repo
tree. Resolve it as `Path(__file__).parents[4] / "deploy" / "remote-libvirt-guest-helpers" /
"kdive-drgn"` (mirrors `components/catalog.py`'s `parents[3]` fixtures resolution). If the
resolved path is **not a file**, raise `CONFIGURATION_ERROR` before any virt-builder run —
a missing reviewed helper must fail loud, never silently ship a guest that cannot introspect.

### D3 — Networking is an interface-name-independent NetworkManager keyfile

Write `/etc/NetworkManager/system-connections/kdive-ssh-nic.nmconnection`:

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

No `interface-name` → NM applies it to whichever ethernet device the SSH NIC enumerates as
(robust against the unpredictable direct-kernel NIC name). `method=auto` = DHCP; SLIRP's
built-in DHCP server (still active under `restrict=on`, ADR-0218) hands `10.0.2.15`.
`autoconnect-priority=-100` keeps it a low-priority fallback so it never shadows a
base-image connection on a NIC that already has one. Because the keyfile is **multi-line**, it
is staged the same way the readiness `.service` unit is — via `--upload` of a
`NamedTemporaryFile`, **not** `--write` (the file uses `--write` only for the single-line kdump
sysctl; a multi-line `--write FILE:CONTENT` argv is fragile and unexercised here). It is then
`chmod 0600` via `--run-command` (NM refuses to load a world-readable keyfile); the explicit
`chmod 0600` is the sole guarantee of the keyfile mode regardless of the upload default.

This leg is **live-confirm only** (no KVM in CI): the test asserts the keyfile + chmod argv
is present for the debug image, not that the guest acquires a lease. B6 (#680) confirms
reachability on the dev KVM host; if NM enables the NIC by a different mechanism than
expected, this is the one-line follow-up ADR-0218 named.

## Acceptance

- Unit: `_real_virt_builder` argv for a **debug** image (`drgn` ∈ packages) contains the
  `--upload …/kdive-drgn:/usr/local/sbin/kdive-drgn`, the `chmod 0755` run-command, a
  `--upload <tempfile>:/etc/NetworkManager/system-connections/kdive-ssh-nic.nmconnection`, and
  the keyfile `chmod 0600` run-command.
- Unit: a **non-debug** image (e.g. build-host packages, no `drgn`) gets **none** of the
  above (helper, chmod, keyfile).
- Unit: an absent helper source path raises `CONFIGURATION_ERROR` before virt-builder runs.
- Live (B6, out of this PR): `introspect.run` over the local drgn-live SSH transport attaches
  and returns a redacted report; maturity then promotes and Epic B #682 closes.
