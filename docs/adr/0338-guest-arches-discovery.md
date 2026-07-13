# ADR 0338 — Discover bootable guest arches, accelerator, and emulator per Resource

- **Status:** Accepted
- **Date:** 2026-07-13
- **Issue:** #1140
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0023 (local-libvirt Discovery plane), `domain/platform/arch_traits.py`

## Context

Cross-arch scheduling — running a ppc64le guest under TCG on an x86_64 host — is
the spine of the ppc64le epic (`docs/design/2026-07-13-ppc64le-full-support.md`,
decision 1: auto-discover, auto-allow). To schedule a foreign-arch guest, kdive
must know three facts per host: which guest architectures it can **boot**, the
**accelerator** each would use (`kvm` for a native arch with `/dev/kvm`, else
`tcg`), and the **emulator** binary libvirt reports for it.

`LocalLibvirtDiscovery.list_resources` advertises only the host CPU arch
(`parse_capabilities_arch` over `<host><cpu><arch>`). Admission (issue 2) has
nothing to validate `profile.arch` against and the domain-XML renderer (issue 3)
has no discovered emulator path.

Verified against real `virsh capabilities`: libvirt emits one `<guest>` block per
`os_type`, each `<arch name=…>` carrying an `<emulator>` and one `<domain type=…>`
per accelerator. On an x86_64 host with `qemu-system-ppc` installed it advertises
**six** guest arches (`i686`, `ppc`, `ppc64` BE, `ppc64le`, `s390x`, `x86_64`);
`ppc64le` is reported verbatim, backed by `/usr/bin/qemu-system-ppc64`, with only
a `qemu` (TCG) domain. kdive can provision only `x86_64` and `ppc64le`
(`arch_traits._TRAITS`).

## Decision

Add an additive `guest_arches` capability key — `{arch: {"accel", "emulator"}}` —
populated at local-libvirt discovery and read through a defensive typed reader.
No schema change, no migration (capabilities are a JSON column).

**Filter to the kdive-provisionable arch set.** `arch_traits.py` exposes
`SUPPORTED_ARCHES` (derived from `_TRAITS`); `parse_guest_arches` skips any arch
not in it. The stored mapping therefore means exactly "arches this host can boot
*and* kdive can provision" — the set admission should gate on. Advertising the
full six would promise `s390x`/`i686`/`ppc`/`ppc64`-BE capability that no profile
arch maps to and that `arch_traits()` fails fast on.

**Accelerator rule:** `accel = "kvm"` iff the arch equals the host arch **and** the
arch offers a `<domain type='kvm'>`; else `"tcg"`. A native arch with no `/dev/kvm`
(no KVM domain) falls back to `tcg`. The stored value is the accelerator *name*
(`kvm`/`tcg`), not the libvirt domain type (`kvm`/`qemu`) — the name is the
scheduling-facing fact (issue 2 persists it on the System, issue 4 scales TCG
deadlines off it); the domain-type mapping is issue 3's rendering concern.

**Parser layering:** `parse_guest_arches(caps_xml, supported)` lives in the shared
`providers/shared/libvirt_xml.py` and takes the supported-arch set as a parameter
rather than importing `domain/`, keeping the low-level XML helper free of a
`providers/shared → domain` dependency and making the filter testable in isolation.
It parses with `defusedxml` (the XML crosses the libvirtd trust boundary) and
returns `{}` on any parse error, mirroring `parse_capabilities_arch` returning
`"unknown"` — a malformed document never crashes discovery.

**Emulator = the arch-level `<emulator>`.** libvirt permits a per-`<domain>`
`<emulator>` override (a Xen-era feature); kdive is QEMU-only and every real QEMU
capabilities document places the element at `<arch>` level. The parser reads that
and does not implement the nested override — a documented simplification, not an
oversight. An arch with no `<emulator>` element is skipped (kdive cannot boot a
guest without knowing the binary).

## Consequences

- `resources.get` and the inventory writeback now carry `guest_arches`; issue 2
  gates `profile.arch ∈ guest_arches` and persists the resolved accel; issue 3
  emits `<domain type>`/`<emulator>` from it. This ADR only produces the data.
- A host without a foreign-arch qemu binary silently advertises fewer arches — the
  auto-discover/auto-allow contract, no operator opt-in.
- Adding a new provisionable arch stays a single `arch_traits._TRAITS` row;
  `SUPPORTED_ARCHES` and the discovery filter pick it up automatically.
- remote-libvirt and fault-inject are untouched; they do not implement local
  guest-arch discovery (out of scope, epic decision 3 keeps validation on x86_64).

## Rejected alternatives

- **Advertise every arch libvirt reports (no filter).** Would list arches no kdive
  profile maps to and `arch_traits()` rejects — the mapping would promise
  capability the pipeline cannot deliver. Filtering to `SUPPORTED_ARCHES` makes the
  key an honest schedulable set.
- **Normalize `ppc64` → `ppc64le`.** Unnecessary — libvirt reports `ppc64le`
  directly. Big-endian `ppc64` is out of the epic's scope and is filtered, not
  remapped.
- **Import `arch_traits` inside `libvirt_xml`.** Rejected: a `providers/shared →
  domain` dependency on the low-level XML helper. Injecting `supported` keeps the
  layering clean.
- **Store the libvirt domain type (`kvm`/`qemu`) instead of the accel name.**
  Rejected: the accel name is the scheduling/accounting fact consumed downstream;
  the domain-type rendering belongs to issue 3's XML seam.
- **Handle the domain-nested `<emulator>` override.** Speculative — no real QEMU
  capabilities document uses it and kdive is QEMU-only. Documented, not silently
  dropped.
