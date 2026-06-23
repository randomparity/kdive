# ADR 0215 — Local-libvirt domain enables ACPI so the guest writes its VMCOREINFO fw_cfg note

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers
- **Issue:** [#708](https://github.com/randomparity/kdive/issues/708)
- **Refines (does not supersede):** [ADR-0211](0211-local-libvirt-host-dump-capture.md)
  (the local host_dump capture path this unblocks), and the #703 change that added the local
  domain's `<vmcoreinfo state="on"/>` feature — necessary but, as #708 found, not sufficient.
- **Mirrors:** the remote provider's domain XML, which already emits `<features><acpi/></features>`
  (`providers/remote_libvirt/lifecycle/xml.py`).

## Context

`vmcore.fetch method=host_dump` on the local-libvirt provider dumps guest RAM via QEMU's
`dump-guest-memory`. For drgn/crash to parse that core they need the kernel's VMCOREINFO note,
which QEMU reads from its `etc/vmcoreinfo` fw_cfg entry — a blob the *guest* kernel writes at boot
through the `qemu_fw_cfg` driver. #703 added the libvirt `<vmcoreinfo state="on"/>` domain feature
(QEMU's `-device vmcoreinfo`) and the build-config fragment added `CONFIG_FW_CFG_SYSFS=y` /
`CONFIG_VMCORE_INFO=y`.

The #708 B6 live re-drive confirmed both halves are live (the domain renders the feature; the built
kernel carries the symbols `=y`, verified via extract-ikconfig; QEMU is 10.1.5) yet host_dump cores
were still unparseable: *"unrecognized QEMU memory dump … load the qemu_fw_cfg kernel module before
dumping."* The guest never populated `etc/vmcoreinfo`.

Root cause: the local domain's `<features>` had only `<vmcoreinfo>` and **no `<acpi>`**. On x86 the
kernel's `qemu_fw_cfg` driver locates the fw_cfg device only via ACPI (`_HID QEMU0002`) — upstream
`CONFIG_FW_CFG_SYSFS depends on SYSFS && ACPI`, "use the ACPI subsystem to determine whether a QEMU
fw_cfg device is present." With no ACPI in the guest the driver never probes, so the kernel never
writes its VMCOREINFO note to the fw_cfg entry and the `<vmcoreinfo>` feature is inert. The remote
domain XML already advertises `<acpi/>`; the local provider — historically a direct-kernel boot that
omitted ACPI — never mirrored it.

## Decision

The local-libvirt domain XML advertises `<acpi/>` in `<features>`, alongside the existing
`<vmcoreinfo state="on"/>`, matching the remote provider. This is the one missing firmware feature
that lets the guest's `qemu_fw_cfg` driver probe and write the VMCOREINFO note QEMU needs to produce
a parseable host_dump core.

## Consequences

- The local guest exposes an ACPI firmware surface (the q35 machine type already supports it). This
  changes the guest's firmware enumeration under direct-kernel boot, where ACPI was previously
  absent.
- This is a necessary precondition for a parseable local host_dump core; it is **not**, on its own,
  a proof that host_dump now works end-to-end. A live KVM re-verification of
  `vmcore.fetch method=host_dump` → drgn/crash-parseable core is required before the host_dump /
  `introspect.from_vmcore` tool maturity can promote past `partial`. That live proof is owned by the
  M2.8 B6 milestone live-verification (#680), not this change. Tool maturity stays `partial` here.
- No MCP surface, port, schema, or migration change. One added `<features>` child in the rendered
  domain XML.

## Considered & rejected

- **Inject ACPI via a `<qemu:commandline>` passthrough instead of the libvirt `<acpi/>` feature.**
  Rejected: `<acpi/>` is the first-class libvirt feature for exactly this, the remote provider
  already uses it, and the passthrough would be a less portable, harder-to-read way to express the
  same thing.
- **Leave ACPI off and load `qemu_fw_cfg` by some other discovery path.** Rejected: on x86 ACPI is
  the discovery mechanism the upstream driver uses; there is no supported non-ACPI probe to rely on.
- **Promote host_dump maturity to `implemented` now.** Rejected: the fix is unproven on real KVM
  (this added firmware surface specifically needs a live boot re-verify); claiming a maturity CI
  cannot verify would be a phantom claim. The honest signal is the #680 live re-drive.
