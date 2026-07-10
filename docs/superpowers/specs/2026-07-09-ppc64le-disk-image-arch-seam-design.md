# ppc64le disk-image / VM-provisioning arch seam

Date: 2026-07-09
Branch: `feat/ppc64le-enablement`
Status: approved (design)

## Problem

`just ci` is green on ppc64le, but that gate exercises none of the VM-provisioning or
disk-image path — it is all `live_vm`-gated. That path is hardcoded for x86. The single
existing arch-aware seam is `ProvisioningProfile.arch`, which flows only into
`<os type arch=…>`; nothing else reads it. Four families of x86 assumption remain:

- **A — Domain XML** (`providers/local_libvirt/lifecycle/xml.py`): machine type `q35`;
  the SSH NIC pinned to `virtio-net-pci,addr=0x10`.
- **B — Console contract** (the sharpest break): `console=ttyS0` in the baseline cmdline
  (`xml.py`), in the platform-required cmdline (`services/runs/steps.py:_REQUIRED_CONSOLE`),
  and in the readiness-marker unit (`images/families/_fedora_customize.py`, which echoes the
  marker to `/dev/ttyS0` gated on `dev-ttyS0.device`). On pseries the serial console is
  `hvc0` (spapr-vty); `ttyS0` never exists, so the readiness marker is never emitted and
  **every ppc64le provision would time out.**
- **C — Catalog** (`fixtures/local-libvirt/rootfs_catalog.toml`): all 11 rows are
  `arch = "x86_64"`; there is no ppc64le image.
- **D — Tooling** (`scripts/check-setup-deps.sh`): the FUTURE tier hardcodes
  `qemu-system-x86_64`.

## Scope

Local-libvirt path only, unit-tested. Remote-libvirt renderer and any live-on-POWER proof
are explicitly **out of scope** (deferred to follow-up issues). The POWER10 host has
`/dev/kvm` but no `qemu-system-ppc64`/`libvirt`/`virt-builder`, so an end-to-end boot proof
is not available on this branch.

## Design

### One traits table, keyed on `profile.arch`

New module `src/kdive/domain/platform/arch_traits.py`:

```python
@dataclass(frozen=True, slots=True)
class ArchTraits:
    arch: str
    machine: str          # libvirt <os machine=>:      q35 / pseries
    console_device: str   # console=<x> token + /dev/<x>: ttyS0 / hvc0
    pin_nic_slot: bool     # q35 needs explicit addr=0x10; pseries auto-assigns

_TRAITS = {
    "x86_64":  ArchTraits("x86_64",  "q35",     "ttyS0", pin_nic_slot=True),
    "ppc64le": ArchTraits("ppc64le", "pseries", "hvc0",  pin_nic_slot=False),
}

def arch_traits(arch: str) -> ArchTraits:
    """Resolve platform traits for a profile arch.

    Raises CategorizedError CONFIGURATION_ERROR on an unknown arch (fail fast — never a
    silent x86 fallback).
    """
```

### Consumers routed through the table

1. **`local_libvirt/lifecycle/xml.py`** — `machine` falls back to
   `arch_traits(profile.arch).machine` (an explicit `domain_xml_params["machine"]` still
   wins); the baseline cmdline becomes `root=/dev/vda console={traits.console_device} rw`;
   the SSH NIC emits `addr=0x10` only when `traits.pin_nic_slot`. `_DEFAULT_MACHINE` and the
   `_BASELINE_CMDLINE` constant are removed.

2. **`services/runs/steps.py`** — `system_required_cmdline` / `cmdline_for` gain an explicit
   `arch` parameter (no default — a defaulted arch would silently render x86). The leading
   token becomes `console={arch_traits(arch).console_device}`. Callers already hold the
   System's profile: `mcp/tools/lifecycle/runs/view.py` and `jobs/handlers/runs/install.py`.

3. **`images/families/_fedora_customize.py`** — `readiness_unit` gains a `console_device`
   argument; `dev-<device>.device` and `echo … > /dev/<device>` derive from it. The sole
   caller is `providers/local_libvirt/rootfs_build.py:399`, where the build spec's arch is in
   scope.

### Catalog

Add one real ppc64le row, `fedora-kdive-ready-44-ppc64le`, sourced from Fedora's published
ppc64le Cloud Base qcow2 (sha256-pinned, same release as the x86 `fedora-kdive-ready-44`).
One proven row validates the `arch` column end-to-end; porting all distros is out of scope
(YAGNI).

### Tooling / docs

- `scripts/check-setup-deps.sh`: FUTURE tier probes `qemu-system-$(uname -m)` mapped per
  arch (`qemu-system-ppc64` on ppc64le, `qemu-system-x86_64` on x86_64), with the package
  name mapped per distro as today.
- `docs/operating/install.md`: the ppc64le section notes the pseries/`hvc0`/auto-slot
  provisioning facts and that live VM provisioning on POWER is unproven.

## Tests

- `arch_traits`: both arches resolve; unknown arch raises `CONFIGURATION_ERROR`.
- `xml.py`: a ppc64le profile renders `machine="pseries"`, `console=hvc0`, and **no** pinned
  NIC `addr`; x86 unchanged.
- `steps.py`: `system_required_cmdline` emits `console=hvc0` for ppc64le, `console=ttyS0` for
  x86.
- `_fedora_customize`: `readiness_unit("hvc0")` renders `dev-hvc0.device` and
  `/dev/hvc0`.
- Catalog test covers the ppc64le row (arch, source, makedumpfile fields).
- dep-checker test: ppc64le os-release yields the `qemu-system-ppc64` hint.

## Known unverified (flagged, not asserted)

`pin_nic_slot=False` for pseries and the ACPI-vs-device-tree fw_cfg/VMCOREINFO behavior are
runtime pseries details that cannot be validated without the qemu/libvirt/ppc64le-image
bootstrap this branch does not set up. They are the correct *documented* defaults but carry a
"needs live validation on POWER" caveat; the ACPI `<features>` block is left unchanged this
branch (a wrong guess there is riskier than deferring).
