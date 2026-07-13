# ADR 0340 — Accelerator-derived domain type, emulator, and per-arch CPU in the local-libvirt domain XML

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-13
- **Issue:** #1142
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0338 (`guest_arches` discovery), ADR-0339 (admission arch-validation
  + accel persist), ADR-0294 (`host-passthrough` CPU for EL9), `domain/platform/arch_traits.py`

## Context

`providers/local_libvirt/lifecycle/xml.py` hardcodes `<domain type="kvm">`, emits no
`<emulator>` (relying on libvirt's default binary), and pins the guest CPU with a single
x86-oriented `<cpu mode="host-passthrough">`. That renders a domain that can only boot a
**native** guest under KVM. Booting a foreign-arch guest requires `<domain type="qemu">`
(TCG software emulation) and the matching `qemu-system-<arch>` binary, which libvirt's
default (`qemu-system-<host-arch>`) is not.

ADR-0338 landed the discovery input: local-libvirt advertises a `guest_arches` capability
— `{arch: {"accel": "kvm"|"tcg", "emulator": path}}` — read back through
`ResourceCapabilities.guest_arches()`. The accelerator and the emulator path are **per
host**: on an x86_64 host, `ppc64le` resolves `{accel: "tcg", emulator:
"/usr/bin/qemu-system-ppc64"}`; on a POWER10 host it resolves `{accel: "tcg", emulator:
"/usr/bin/qemu-system-ppc64le"}`. The emulator is therefore a discovered fact, never a
guess.

ADR-0339 (issue 2) persists the resolved `accel` on the System row so **repeated reads**
— `systems.get`, TCG deadline scaling (issue 4), cost accounting, arch-parameterized tests
— key off a recorded fact instead of re-deriving host state on every read. That ADR's
forward-looking note said "issue 3 renders `<domain type>` from it." This ADR refines that:
the persisted `accel` stays the source of truth for reads, but the **renderer** re-resolves
both `accel` and `emulator` together from live capabilities at the single provision-time
render (see Alternatives).

## Decision

We will derive the local-libvirt domain's accelerator facts at provision time and render
them per architecture × accelerator.

**Sourcing.** The local-libvirt provisioner resolves `{accel, emulator}` for
`profile.arch` from live libvirt capabilities (`conn.getCapabilities()` +
`parse_guest_arches(caps, SUPPORTED_ARCHES)`) inside `provision()`, and passes `accel` and
`emulator` to `render_domain_xml`. `reprovision` delegates to `provision`, so it is covered
by the same one resolution site. The branch logic is **one shared helper**, not two copies:
`resolve_accel_emulator(guest_arches, arch) -> tuple[str, str] | None` in
`domain/catalog/resource_capabilities.py`, called by both the provider and admission's
`resolve_accel` (re-expressed as a thin wrapper that drops the emulator and keeps its
`str | None` contract). A parity test binds the two entry points. The three outcomes:

- **Empty `guest_arches`** (host not re-discovered since ADR-0338): fail **open** to
  `("kvm", None)` — today's legacy x86-KVM path, matching the ADR-0339 admission fail-open.
- **Non-empty `guest_arches` missing `profile.arch`**: fail **closed** with
  `CONFIGURATION_ERROR` naming the supported set. Because ADR-0340 re-resolves from **live**
  caps at provision while admission validated the **persisted** capability_view at mint, a
  host that lost its foreign-qemu binary after a foreign System passed admission would make
  `dict.get()` return `None`; failing open there would render an incoherent
  `<domain type="kvm">` for a pseries guest that fails to start with an opaque libvirt error.
  We raise the same clean error admission raises instead. (`dict.get(arch)` returns `None`
  for both the empty and the arch-absent case; only the empty case may fail open.)
- **`conn.getCapabilities()` / connection `libvirtError`**: raise `INFRASTRUCTURE_FAILURE`,
  grouping the caps read with the provider's other pre-define host-state reads
  (`_recorded_ssh_port` / `_recorded_gdb_port`, both `INFRASTRUCTURE_FAILURE`) rather than the
  mutating `_define_and_start` action (`PROVISIONING_FAILURE`). The provider's narrow
  `_LibvirtConn` Protocol gains `getCapabilities(self) -> str`.

The fail-closed arm is deliberate even on a provision **retry**: the handler only calls
`provision()` while `state == PROVISIONING`, and a retry raises here only if the host
genuinely lost the arch mid-provision — failing the unsupportable foreign System closed is
correct, not a regression of the idempotent-retry contract.

No change to the provider-agnostic job handler and no change to the remote-libvirt or
fault-inject providers.

**Domain type.** `<domain type="kvm">` when `accel == "kvm"`, else `<domain type="qemu">`.

**Emulator.** `<devices>` emits `<emulator>` **only for TCG domains** (`accel != "kvm"`),
using the discovered path. Native-KVM domains omit it and rely on libvirt's default binary,
which is correct for the host arch — this is what keeps x86_64-under-KVM output
byte-identical. A TCG domain with no resolved emulator is a `CONFIGURATION_ERROR` (a TCG
domain cannot boot without a binary).

**CPU element.** Routed through `arch_traits`. A new `kvm_cpu_mode` field gives
`host-passthrough` for x86_64 (unchanged, ADR-0294) and `host-model` for ppc64le/pseries.
KVM domains emit `<cpu mode="{kvm_cpu_mode}">`; **TCG domains emit no `<cpu>` element** —
QEMU's per-machine default model is correct, and pinning a model would couple us to QEMU
versions.

**ACPI features.** The `<features><acpi/><vmcoreinfo/></features>` block becomes x86-only,
gated by a new `arch_traits` flag (`emit_acpi_features`: `True` for x86_64, `False` for
ppc64le). pseries fw_cfg/VMCOREINFO device behavior is deliberately left unrendered here and
is proven or corrected empirically in the kdump sub-issue (#1149, epic issue 9), not guessed
now.

**API.** `render_domain_xml` gains keyword params `accel: str = "kvm"` and `emulator: str |
None = None`. The `"kvm"`/`None` defaults are exactly the legacy x86-KVM path, so existing
callers/tests render byte-identically without edits.

`domain_type` is **derived** from `accel` at render time, not stored — so `GuestArch`
(`{accel, emulator}`) is not extended, and the parser/reader shape-drift the #1140 review
flagged cannot occur here.

## Consequences

- Foreign-arch guests become bootable: a `ppc64le` profile on an x86_64 host renders a
  `qemu`-type pseries domain with the discovered `qemu-system-ppc64` emulator and no pinned
  CPU model.
- x86_64-under-KVM rendered XML is unchanged (byte-identical), so no native-path regression.
- Adding an architecture stays "one `arch_traits` row" (`machine`, `console_device`,
  `pin_nic_slot`, `kvm_cpu_mode`, `emit_acpi_features`) — no new `if arch == …` branch in
  the renderer.
- `provision()` opens one extra short-lived libvirt connection per provision to read
  capabilities. This is a single write-time op (not a hot read path) and mirrors what
  discovery already does.
- Obligation carried forward: ppc64le crash-capture features (ACPI-analogue / VMCOREINFO on
  pseries) are unrendered until the kdump sub-issue proves the correct device set.

## Alternatives considered

- **Thread the persisted `System.accel` + a handler-resolved emulator through the provider
  call.** Faithful to ADR-0339's letter ("render from the recorded accel"), but it couples
  the provider-agnostic `provision`/`reprovision` job handler and the shared
  `_ProviderLifecycleCall` protocol to a local-libvirt-only concept (emulators; remote is
  KVM-only), forcing remote-libvirt and fault-inject to accept-and-ignore new kwargs.
  Rejected: the renderer's inputs belong in the provider layer, and re-resolving at the
  single provision-time render does not violate ADR-0339's actual goal (avoiding host
  re-derivation on **repeated reads**).
- **Always emit `<emulator>`, including for native x86-KVM.** Rejected: it changes
  x86_64-under-KVM output, violating the byte-identical acceptance criterion, for no benefit
  — libvirt's default binary is already correct for the native host arch.
- **Pin a `<cpu>` model for TCG domains.** Rejected: QEMU's per-machine default is correct
  and a pinned model couples the domain to specific QEMU versions.
- **Store a resolved `domain_type` on `GuestArch` / the System row.** Rejected: it is a pure
  function of `accel`, so storing it adds a second definition to keep in sync (the exact
  drift the #1140 review warned about) for no gain.
