# Spec — Arm kdump on a warm-provisioned local-libvirt System (#1319)

- **Issue:** [#1319](https://github.com/randomparity/kdive/issues/1319)
- **ADR:** [ADR-0390](../../adr/0390-provision-time-kdump-arming.md)
- **Date:** 2026-07-20

## Problem

The native `live_vm` gate (epic #1289 / #1293) mints a warm-store-provisioned System
with `scripts/live-vm/mint-system.sh`, force-crashes it, and expects a real
`/var/crash/<ts>/vmcore`.
`tests/providers/local_libvirt/test_retrieve_kdump.py::test_live_vm_kdump_capture_arc_no_staging`
fails — the guest is not kdump-armed.

Root cause: a warm **provision** boots the rootfs's own kernel through
`render_domain_xml` with the fixed baseline cmdline `root=/dev/vda console=<dev> rw` —
**no `crashkernel`**. Crashkernel was deliberately "the install/boot lane's job"
(`runs.install`, ADR-0206/0272), applied only when an agent installs a kernel-under-test.
A warm-own-kernel System never runs `runs.install` (its modules already ship in the
rootfs), so nothing ever reserves crash memory. Setting the profile's `crashkernel`
selected `capture_method=KDUMP` but never put `crashkernel=` on the actual boot cmdline.

## Decision (Option A)

Provision-time arms kdump for the warm-own-kernel case. When a local-libvirt profile
sets `crashkernel`, `render_domain_xml` appends `crashkernel=<size>` to the baseline
boot cmdline (and `fadump=on` when `debug.fadump` is set, mirroring the install lane's
`system_required_cmdline`). This relaxes the ADR-0206/0272 invariant "crashkernel is
never on the baseline boot" for the warm-own-kernel case, where there is no
install-lane kernel-under-test to size the reservation against — the rootfs's own
kernel is the arbiter of the token grammar (the profile field is already opaque).

The image already ships kdump armed: an image built kdump-capable (`debug`) runs
`systemctl enable kdump.service` and orders the `kdive-ready` marker `After=kdump.service`
(`src/kdive/images/families/_fedora_customize.py`, `rhel.py`), so a System provisioned
from such a warm rootfs arms kdump at boot with no operator step.

`scripts/live-vm/mint-system.sh` mints the shared provisioned-family System; its profile
now requests `crashkernel` (so `capture_method` resolves KDUMP and the cmdline reserves
memory) and provisions at 4 GiB (2 GiB + crashkernel cannot reach the readiness marker on
the x86 warm guest — proven live on runner-pdx).

## Scope

- `src/kdive/providers/local_libvirt/lifecycle/xml.py` — append `crashkernel=`/`fadump=on`
  to the System baseline cmdline from the profile. The customization-boot renderer is
  unchanged (it passes no crashkernel).
- `src/kdive/profiles/provisioning.py` — a `CrashkernelToken` type validates the profile
  `crashkernel` field for cmdline-injection safety now that it lands on a boot `<cmdline>`.
- `scripts/live-vm/mint-system.sh` — request kdump + 4 GiB in the minted profile.
- Tests: `tests/providers/local_libvirt/test_provisioning.py`,
  `tests/providers/local_libvirt/lifecycle/test_xml.py`.

Out of scope: the `runs.install` `kdump_env_absent` gate (`install.py`) stays — it
correctly guards the install-a-custom-kernel lane, which the warm-own-kernel path
bypasses. No DB migration. No payload/schema change.

## Acceptance

1. A local-libvirt profile with `crashkernel="256M"` renders a System cmdline ending
   `... rw crashkernel=256M`; `fadump` adds `fadump=on`.
2. A profile with no `crashkernel` renders the unchanged `root=/dev/vda console=<dev> rw`.
3. The customization-boot cmdline is unchanged (never armed).
4. `just ci` green. The runner-pdx native-gate kdump proof is operator-deferred.
