# Proof record — live TCG boot of the Fedora ppc64le row (#1144)

Date: 2026-07-13
Issue: #1144 · Epic: #1139 · Spec: `2026-07-13-ppc64le-fixture-live-proof-1144.md` · ADR-0342

This is the documented live proof required by #1144 AC #4/#5: a Fedora ppc64le guest boots
end-to-end under TCG on the x86_64 host, the `kdive-ready` marker lands on `hvc0`, and the guest
is SSH-reachable — retiring PR #1070's unverified pseries defaults.

## Environment

- Host: x86_64 (`homer`), `qemu-system-ppc64` present; libvirt advertises `ppc64le` as a bootable
  guest arch with `accel=tcg`, `emulator=/usr/bin/qemu-system-ppc64` (ADR-0338 discovery parser
  verified live).
- Guest image: the file-injection scaffold (spec §4) of the sha256-pinned Fedora ppc64le
  GenericCloud base — whole-disk ext4 (ADR-0272), SELinux permissive + `/.autorelabel`, cloud-init
  first-boot drop-in (ADR-0288), the `readiness_unit(kdump.service, "hvc0")` unit + enable symlink,
  `/dev/vda` fstab. Kernel: `6.19.10-300.fc44.ppc64le` (single baseline kernel, provisionable).

## Result 1 — direct TCG boot (console capture)

Booting the scaffold's extracted baseline kernel (an ELF `vmlinux`, 66 MB — powerpc has no
bzImage) directly under `qemu-system-ppc64 -machine pseries -accel tcg` with kdive's baseline
cmdline `root=/dev/vda console=hvc0 rw`, capturing the `hvc0` serial console:

```
[    0.000000] Linux version 6.19.10-300.fc44.ppc64le … #1 SMP PREEMPT_DYNAMIC
… (first boot) → SELinux autorelabel → reboot …
[  102.463270] reboot: Restarting system
[    0.000000] Linux version 6.19.10-300.fc44.ppc64le …           (second boot)
[   41.2] cloud-init[841]: Cloud-init v. 25.3 running 'init-local' …
         Starting kdive-ready.service - Signal kdive serial readiness...
kdive-ready                                                        ← marker on hvc0
[  OK  ] Finished kdive-ready.service - Signal kdive serial readiness.
[  OK  ] Started sshd.service - OpenSSH server daemon.
kdive login:
```

Proves, on the pseries-TCG cell:

- The ppc64le kernel **boots to userspace under TCG with no ISA/CPU fault** — QEMU's default
  pseries CPU meets Fedora 44's POWER9/ISA-3.0 baseline. This retires the ISA-baseline SIGILL risk
  ADR-0340 deferred to #1144, and confirms the "no `<cpu>` for TCG" rendering is correct for
  ppc64le.
- The `kdive-ready` marker is **emitted on `hvc0`** (spapr-vty). Confirms PR #1070's
  `arch_traits["ppc64le"].console_device = "hvc0"` — a `ttyS0` unit would have written to a console
  that does not exist on pseries and never surfaced.
- `sshd` starts and the guest reaches a login prompt.

Full capture retained out of tree at the run's `ppc64le-tcg-boot-proof.log`.

## Result 2 — kdive spine (`live_stack`) proof

`tests/integration/test_live_stack.py::test_ppc64le_guest_is_ssh_reachable_over_the_wire`
(`KDIVE_GUEST_IMAGE_PPC64LE` set to the published scaffold): **PASSED** in 80 s.

allocate → provision (`arch=ppc64le`) → `systems.get` reports `accel=tcg` (admission persisted it,
ADR-0339) → poll `systems.check_ssh_reachable` until reachable. Final verdict:

```json
{"reachable":true,"endpoint":{"host":"127.0.0.1","port":33101},"detail":"reachable",
 "checks":[{"layer":"tcp_connect","ok":true},{"layer":"ssh_banner","ok":true}]}
```

`ssh_banner:true` is a **real SSH banner from the guest** over the worker loopback forward — not a
port knock. Proves, through the real admission→provision→boot spine:

- The provider resolved `{accel:tcg, emulator:/usr/bin/qemu-system-ppc64}` from live caps and
  rendered+started a pseries-TCG domain (SELinux `svirt_tcg_t` context confirmed in the audit log);
  `accel=tcg` persisted on the System.
- The virtio SSH NIC **leased its DHCP address and bridged to the guest sshd without a pinned PCI
  slot** — confirming PR #1070's `arch_traits["ppc64le"].pin_nic_slot = False`.
- The intermediate `reachable:false` probes carried redacted `console_tail` snippets
  (sshd-keygen, OpenSSH host-key setup) — the guest booting to sshd, observable via the API.

## Retired PR #1070 unverified defaults

| default | verdict |
|---------|---------|
| `console_device = "hvc0"` | **confirmed** — `kdive-ready` observed on `hvc0` (Result 1) |
| `pin_nic_slot = False` | **confirmed** — SSH banner over the unpinned virtio NIC (Result 2) |
| `machine = "pseries"` | **confirmed** — the domain booted as pseries under both results |
| no `<cpu>` for TCG (ADR-0340) | **confirmed** — POWER9 default boots F44; no SIGILL (Result 1) |

## Environment note (not a #1144 defect)

The dev host's `local-libvirt` resource row predated ADR-0338, and its `capabilities.guest_arches`
was absent — migrate-time discovery (`register_all_discovery`) is insert-**if-absent**
(`ensure_discovered_resource_registered`), so it does not refresh an existing resource's
capabilities. Admission therefore fell open (`accel=NULL`) until the row's `guest_arches` was
refreshed to the genuine discovered value. A fresh deployment discovers `guest_arches` at first
registration, so this is a stale-resource artifact of this host, not a product defect this issue
introduces. Flagged as a possible discovery follow-up (a capability-schema addition does not reach
pre-existing resources without a re-register); out of scope for #1144.
