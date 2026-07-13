# Accel-derived domain type, emulator, and per-arch CPU in the local-libvirt domain XML (#1142)

Date: 2026-07-13
Status: approved (design)
Issue: #1142 · Epic: #1139 (full ppc64le support) · ADR: `docs/adr/0340-accel-derived-domain-xml.md`
Depends on: #1140 (ADR-0338, `guest_arches` discovery)

## Problem

`providers/local_libvirt/lifecycle/xml.py` renders a domain that can only boot a native
guest under KVM:

- `_build_baseline_domain` hardcodes `ET.Element("domain", type="kvm")` (xml.py:127).
- No `<emulator>` is emitted, so libvirt picks `qemu-system-<host-arch>` — wrong for a
  foreign guest.
- `_append_host_cpu` pins `<cpu mode="host-passthrough">` unconditionally (xml.py:143-147).
- `_append_crash_capture_features` emits `<features><acpi/><vmcoreinfo/></features>`
  unconditionally — an x86 firmware assumption.

Booting a foreign-arch guest (a `ppc64le` profile on the x86_64 host) requires `<domain
type="qemu">` (TCG), the discovered `qemu-system-ppc64` emulator, and a pseries-appropriate
CPU/feature set.

## Inputs (already landed)

- ADR-0338: `parse_guest_arches(caps_xml, supported) -> {arch: {"accel", "emulator"}}` and
  `ResourceCapabilities.guest_arches()`. `accel` is `"kvm"` (arch offers a `<domain
  type='kvm'>`) or `"tcg"`; `emulator` is the arch-level `<emulator>` path.
- `domain/platform/arch_traits.py`: per-arch `machine`, `console_device`, `pin_nic_slot`,
  and `SUPPORTED_ARCHES`.

## Design

### Sourcing (ADR-0340)

The local-libvirt provisioner resolves `{accel, emulator}` for `profile.arch` inside
`provision()` from live capabilities.

**One shared resolver, not two copies.** The branch logic (empty → fail-open, arch-absent →
fail-closed, present → resolved) lives in **one** function so the provider and admission
cannot drift. Add `resolve_accel_emulator(guest_arches, arch) -> tuple[str, str] | None` to
`domain/catalog/resource_capabilities.py` (the domain layer, imported by both
`services/systems/validation.py` and `providers/local_libvirt`):

```python
def resolve_accel_emulator(guest_arches, arch) -> tuple[str, str] | None:
    if not guest_arches:            # empty map -> fail OPEN (caller substitutes legacy default)
        return None
    entry = guest_arches.get(arch)
    if entry is None:               # non-empty but arch absent -> fail CLOSED
        raise CONFIGURATION_ERROR(... accepted_values = sorted(guest_arches) ...)
    return (entry["accel"], entry["emulator"])
```

`resolve_accel` (admission, ADR-0339) is re-expressed as a thin wrapper — `resolved =
resolve_accel_emulator(...); return resolved[0] if resolved is not None else None` — so its
existing `str | None` contract, message, and `accepted_values` details are unchanged. The
provider calls the same helper:

```python
caps = conn.getCapabilities()                       # libvirtError -> INFRASTRUCTURE_FAILURE
guest_arches = parse_guest_arches(caps, SUPPORTED_ARCHES)
resolved = resolve_accel_emulator(guest_arches, profile.arch)   # raises on arch-absent
accel, emulator = resolved if resolved is not None else ("kvm", None)
```

A **parity test** feeds identical `guest_arches` maps to both entry points and asserts
identical branch outcomes (open / closed-raise / resolved), binding them so a future change
to the helper cannot silently diverge the two sites.

`reprovision` delegates to `provision`, so this is the single provider resolution site. Three
outcomes are explicit:

- **Empty `guest_arches`** (this local host not re-discovered since ADR-0338): the helper
  returns `None`; the provider substitutes `("kvm", None)`, i.e. today's legacy x86-KVM path.
  Matches ADR-0339 admission, which skips the arch check on an empty map.
- **Non-empty `guest_arches` missing `profile.arch`**: the helper raises `CONFIGURATION_ERROR`
  naming the supported set. This is the TOCTOU guard: ADR-0340 re-resolves from **live** caps
  at provision while admission validated the **persisted** capability_view at mint, so if the
  host's foreign-qemu binary was removed after a foreign System passed admission, `.get()`
  returns `None` and we must **not** silently render an incoherent `<domain type="kvm">` for a
  pseries guest — we raise the same clean error admission raises.
  - *Interaction with the idempotent-retry contract.* `provision()` is idempotent for a retry
    within the `PROVISIONING` window (the handler only calls it while `state ==
    PROVISIONING`; a `ready` System's re-dispatched job returns early without provisioning). A
    retry re-reads caps: in the normal case the arch is still advertised and the same values
    resolve (harmless). The only way a retry raises here is a host that **genuinely lost** the
    arch capability mid-provision — in which case failing the half-provisioned foreign System
    closed is the intended, correct outcome (the host can no longer support it), not a
    regression. This is deliberate; the alternative (returning a running-but-unsupportable
    domain) is worse.
- **`conn.getCapabilities()` / connection `libvirtError`**: raise `INFRASTRUCTURE_FAILURE`.
  This groups the caps read with the provider's **other pre-define host-state reads** —
  `_recorded_ssh_port` / `_recorded_gdb_port` both map a connect fault to
  `INFRASTRUCTURE_FAILURE` (provisioning.py:326-328, 360-362) — while the mutating
  `_define_and_start` action maps its connect fault to `PROVISIONING_FAILURE`
  (provisioning.py:377-379). The boundary is "reading host state vs. performing the
  define/start action," and the caps read is on the reading side. `parse_guest_arches` already
  returns `{}` on malformed XML (→ the empty-map fail-open), so only a genuine RPC/connection
  fault reaches this arm. `_LibvirtConn` (the provider's narrow Protocol) gains
  `getCapabilities(self) -> str`.

No job-handler or cross-provider change.

### Renderer (`render_domain_xml` gains `accel: str = "kvm"`, `emulator: str | None = None`)

| element | rule |
|---------|------|
| `<domain type=…>` | `kvm` if `accel == "kvm"`, else `qemu` |
| `<devices><emulator>` | emitted **only** when `accel != "kvm"` (TCG), with the discovered path; native KVM omits it (libvirt default is correct) |
| `<cpu>` | KVM: `<cpu mode="{traits.kvm_cpu_mode}">` (`host-passthrough` x86_64 / `host-model` ppc64le). TCG: **no `<cpu>` element** |
| `<features>` | emitted only when `traits.emit_acpi_features` (x86_64 → yes, ppc64le → no) |

`arch_traits` gains two fields: `kvm_cpu_mode: str` and `emit_acpi_features: bool`.

A TCG domain (`accel != "kvm"`) with `emulator is None` raises `CONFIGURATION_ERROR` — a TCG
domain cannot boot without a binary. (`parse_guest_arches` never yields a TCG entry without
an emulator, so this is a defensive guard, not a normal path.)

`domain_type` is derived from `accel`, not stored, so `GuestArch` is **not** extended and no
parser/reader drift is introduced.

### The four (arch × accel) combinations

Host on which each is reachable in this epic, and the rendered facts:

| arch | accel | `<domain type>` | `<emulator>` | machine | `<cpu>` | `<features>` |
|------|-------|-----------------|--------------|---------|---------|--------------|
| x86_64 | kvm | `kvm` | absent | `q35`* | `mode="host-passthrough"` | present (acpi+vmcoreinfo) |
| x86_64 | tcg | `qemu` | present | `q35`* | absent | present |
| ppc64le | kvm | `kvm` | absent | `pseries`* | `mode="host-model"` | absent |
| ppc64le | tcg | `qemu` | present | `pseries`* | absent | absent |

\* `domain_xml_params["machine"]` still overrides the `arch_traits` default (unchanged).

## Acceptance criteria (from the issue)

1. Rendered XML asserted for all four (arch × accel) combinations: domain type, emulator
   presence + path, machine, CPU element presence/mode. → `tests/providers/local_libvirt`.
2. Explicit `domain_xml_params["machine"]` override still wins. → existing behavior, add a
   ppc64le-arch assertion.
3. x86_64-under-KVM output byte-identical to today. Two cases, because the real deployed input
   is `accel=kvm` **with a non-null emulator**, not `("kvm", None)`:
   - the `("kvm", None)` defaults (empty-map fail-open path) → current golden string unchanged;
   - `render_domain_xml(..., accel="kvm", emulator="/usr/bin/qemu-system-x86_64")` (the real
     post-ADR-0338 native x86 resolution) → **same** golden string, i.e. the `<emulator>` is
     dropped. This pins that the drop is driven by `accel == "kvm"`, **not** by `emulator is
     None`; a regression changing the gate to `if emulator is not None` would fail this case
     while passing the first.
4. Provider resolves `{accel, emulator}` from live capabilities and forwards them via the
   shared `resolve_accel_emulator` helper. Provisioning unit tests with a fake
   `getCapabilities`:
   - **empty `guest_arches`** → fail **open** `("kvm", None)`, legacy x86-KVM domain;
   - **non-empty, native x86_64** (`accel=kvm`, non-null emulator) → forwards
     `("kvm", "/usr/bin/qemu-system-x86_64")` and the rendered domain omits `<emulator>`;
   - **non-empty but missing `profile.arch`** (foreign-arch drift) → **`CONFIGURATION_ERROR`**
     naming the supported set (asserts the provider does *not* render a kvm domain — the guard
     that distinguishes the two `.get()`-returns-`None` cases so they can never be conflated);
   - **`getCapabilities()` raises `libvirtError`** for a foreign arch → **`INFRASTRUCTURE_FAILURE`**,
     not a silent kvm domain.

Additional guards:

- **Parity** (from finding 1): identical `guest_arches` maps fed to `resolve_accel_emulator`
  and to admission's `resolve_accel` yield identical branch outcomes (open / raise / resolved).
- **Shape round-trip** (from the #1140 review follow-up): a `parse_guest_arches` output
  round-trips through `ResourceCapabilities.guest_arches()` unchanged, so a future field added
  on one side but not the other fails a test. `GuestArch` is deliberately **not** extended here.

## Scope / non-goals

- No migration, no schema change (domain type is derived).
- No remote-libvirt / fault-inject change.
- pseries ACPI-analogue / VMCOREINFO crash-capture features: deferred to the kdump sub-issue
  (#1149, epic issue 9) — this PR emits **no** `<features>` for ppc64le rather than guessing.
- TCG deadline scaling: epic issue 4 (#1143), not here.
- Unit-test only; live TCG boot proof is epic issue 5 (#1144).

**Interim window (sequencing).** After this PR a foreign-arch profile (ppc64le on the x86
host) passes admission (#1141, merged) **and** renders a correct TCG pseries domain that
libvirt will start — but two epic pieces are not yet landed: TCG-scaled readiness deadlines
(#1143) and pseries crash-capture features (#1149). So an operator provisioning a ppc64le
System in the window between this PR and #1143 should expect the System to boot slowly under
TCG and may hit the x86-KVM-tuned readiness deadline (surfacing as a provision/boot timeout);
and a host_dump/kdump workload on ppc64le will produce no VMCOREINFO until #1149. This PR does
**not** gate foreign-arch provisioning — the epic sequences #1143 immediately after — but the
limitation is called out here so it is not mistaken for a defect in this change.
