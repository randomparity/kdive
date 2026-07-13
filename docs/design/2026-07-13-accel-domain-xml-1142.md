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
`provision()` from live capabilities. It **mirrors admission's fail-open/fail-closed rule**
(`services/systems/validation.py:resolve_accel`) exactly, so the two resolution sites cannot
diverge:

```
caps = conn.getCapabilities()                       # libvirtError -> INFRASTRUCTURE_FAILURE
guest_arches = parse_guest_arches(caps, SUPPORTED_ARCHES)
if not guest_arches:                                # empty map -> fail OPEN (legacy path)
    return ("kvm", None)
entry = guest_arches.get(profile.arch)
if entry is None:                                   # non-empty but arch absent -> fail CLOSED
    raise CONFIGURATION_ERROR(... supported = sorted(guest_arches) ...)
return (entry["accel"], entry["emulator"])
```

`reprovision` delegates to `provision`, so this is the single resolution site. Two failure
modes are explicit:

- **Empty `guest_arches`** (host not re-discovered since ADR-0338 — remote/fault-inject have
  none, but this provider is local-libvirt so it is the "not yet re-discovered" case): fail
  **open** to `("kvm", None)`, i.e. today's legacy x86-KVM path. This matches ADR-0339
  admission, which skips the arch check on an empty map.
- **Non-empty `guest_arches` missing `profile.arch`**: fail **closed** with
  `CONFIGURATION_ERROR` naming the supported set. This is the TOCTOU guard: ADR-0340
  re-resolves from **live** caps at provision while admission validated the **persisted**
  capability_view at mint, so if the host's foreign-qemu binary was removed after a foreign
  System passed admission, `.get()` returns `None` and we must **not** silently render an
  incoherent `<domain type="kvm">` for a pseries guest — we raise the same clean, actionable
  error admission would have. (`resolve_accel` fails closed here; the provider must too.)
- **`conn.getCapabilities()` / connection `libvirtError`**: raise `INFRASTRUCTURE_FAILURE`.
  Reading local capabilities is the same cheap RPC discovery makes; a fault means an
  unhealthy host, not a licence to guess a domain type. `parse_guest_arches` already returns
  `{}` on malformed XML (→ the empty-map fail-open), so only a genuine RPC/connection fault
  reaches this arm. `_LibvirtConn` (the provider's narrow Protocol) gains
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
3. x86_64-under-KVM output byte-identical to today. → assert the current golden string is
   unchanged when `render_domain_xml` is called with the `("kvm", None)` defaults.
4. Provider resolves `{accel, emulator}` from live capabilities and forwards them, mirroring
   admission's rule. Three provisioning unit tests with a fake `getCapabilities`:
   - **empty `guest_arches`** → fail **open** `("kvm", None)`, legacy x86-KVM domain;
   - **non-empty but missing `profile.arch`** (foreign-arch drift) → **`CONFIGURATION_ERROR`**
     naming the supported set (asserts the provider does *not* render a kvm domain — the guard
     that distinguishes the two `.get()`-returns-`None` cases so they can never be conflated);
   - **`getCapabilities()` raises `libvirtError`** for a foreign arch → **`INFRASTRUCTURE_FAILURE`**,
     not a silent kvm domain.

Additional guard (from the #1140 review follow-up): a `parse_guest_arches` output round-trips
through `ResourceCapabilities.guest_arches()` unchanged, so a future field added on one side
but not the other fails a test. `GuestArch` is deliberately **not** extended here.

## Scope / non-goals

- No migration, no schema change (domain type is derived).
- No remote-libvirt / fault-inject change.
- pseries ACPI-analogue / VMCOREINFO crash-capture features: deferred to the kdump sub-issue
  (#1149, epic issue 9) — this PR emits **no** `<features>` for ppc64le rather than guessing.
- TCG deadline scaling: epic issue 4 (#1143), not here.
- Unit-test only; live TCG boot proof is epic issue 5 (#1144).
