# Discover bootable guest arches, accelerator, and emulator per Resource

- **Issue:** #1140
- **ADR:** 0338
- **Epic:** #1139 (full ppc64le support), design `docs/design/2026-07-13-ppc64le-full-support.md` decision 1
- **Builds on:** ADR-0023 (local-libvirt Discovery plane), `domain/platform/arch_traits.py` (per-arch provisioning table)
- **Consumed by:** #1140-sibling issue 2 (admission validates `profile.arch ∈ guest_arches`; persists resolved accel on the System row)

## Problem

`LocalLibvirtDiscovery.list_resources` advertises only the **host** CPU arch
(`parse_capabilities_arch` over `<host><cpu><arch>`, `discovery.py`). Cross-arch
scheduling — a ppc64le guest under TCG on an x86_64 host — needs the set of guest
architectures a host can actually *boot*, the accelerator each would use (`kvm`
native, `tcg` foreign), and the emulator binary libvirt reports for it. None of
that is discovered today, so admission (issue 2) has nothing to validate a
`profile.arch` against and the domain-XML renderer (issue 3) has no discovered
emulator path to emit.

## Goal

Discovery advertises, per Resource, a `guest_arches` mapping:

```
{
  "x86_64":  {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
  "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"}
}
```

flowed through the existing inventory writeback and visible in `resources.get`.
It is a purely additive capability key; no schema change, no migration.

## Ground truth

Verified live against `virsh -c qemu:///system capabilities` on **both** the
x86_64 dev host and a real ppc64le (POWER10) host. libvirt emits one `<guest>`
block per `os_type`, each with one or more `<arch name=…>` children carrying a
`<wordsize>`, an `<emulator>`, many `<machine>` entries, and one `<domain type=…>`
per supported accelerator:

```xml
<guest>
  <os_type>hvm</os_type>
  <arch name='x86_64'>
    <wordsize>64</wordsize>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <machine …>…</machine>
    <domain type='qemu'/>
    <domain type='kvm'/>
  </arch>
</guest>
```

Findings that shape the design:

- **libvirt reports `ppc64le` verbatim** as its own `<arch name='ppc64le'>` — no
  arch-name normalization is needed. On the x86_64 host it is backed by
  `/usr/bin/qemu-system-ppc64` with a `qemu` (TCG) domain only; on the ppc64le host
  it is backed by `/usr/bin/qemu-system-ppc64le` (a **distinct** binary). The
  emulator path is host/distro-dependent and must be *read* from libvirt, never
  hardcoded.
- A single x86_64 host with `qemu-system-ppc` installed advertises **six** guest
  arches: `i686`, `ppc`, `ppc64` (big-endian), `ppc64le`, `s390x`, `x86_64`. The
  ppc64le host advertises `i686`, `ppc`, `ppc64`, `ppc64le`, `x86_64`. kdive can
  provision only `x86_64` and `ppc64le` (`arch_traits._TRAITS`). The stored mapping
  must therefore be **filtered to the kdive-supported set**, or the advertised
  arches would include ones no profile can select and no downstream seam supports.
- On the x86_64 host the native `x86_64` arch carries both `<domain type='qemu'/>`
  and `<domain type='kvm'/>` (`/dev/kvm` exists), so its accel resolves to `kvm`.
- On the POWER10 host `/dev/kvm` exists **yet libvirt advertises only
  `<domain type='qemu'/>` for the native `ppc64le` arch — no KVM domain** (this is
  a nested VM without usable KVM-HV). The accel rule therefore resolves `ppc64le`
  to `tcg` there, exercising the "native falls back to `tcg` when the KVM domain is
  absent" path against a real host, not just a synthetic fixture. A bare-metal
  KVM-HV POWER host would additionally advertise `<domain type='kvm'/>` for
  `ppc64le` (mirroring x86_64-under-KVM); that variant is the one synthetic fixture.

## Design

### New capability key

`domain/catalog/resource_capabilities.py` gains:

- `GUEST_ARCHES_KEY = "guest_arches"`, added to `_KNOWN_KEYS`.
- A `GuestArch` `TypedDict` (`{"accel": str, "emulator": str}`).
- `ResourceCapabilities.guest_arches() -> dict[str, GuestArch]`: a defensive
  reader over the persisted JSON, mirroring `pcie_descriptors()` — it drops any
  entry whose value is not a dict with string `accel`/`emulator`, so a stale or
  hand-edited row never crashes a consumer. Returns `{}` when the key is absent.

`accel` is the accelerator *name* (`kvm` / `tcg`), not the libvirt domain type
(`kvm` / `qemu`). The name→domain-type mapping is issue 3's concern; here we
record the fact. The reader validates entry *shape* (string `accel`/`emulator`)
but not the `accel` *value* domain: this parser is the only writer and only emits
`kvm`/`tcg`, so value validation is deferred to issue 3's consumer, which maps the
accel to a domain type and can fail-closed on an unexpected value.

### Parser (`providers/shared/libvirt_xml.py`)

`parse_guest_arches(caps_xml: str, supported: Collection[str]) -> dict[str, GuestArch]`:

1. Parse `caps_xml` with `defusedxml` (crosses the libvirtd trust boundary, like
   the other parsers here). On a parse error return `{}` — never crash discovery.
2. For each `<guest>` with `os_type == "hvm"`, for each `<arch name=X>`:
   - Skip `X` unless `X in supported` (the kdive-provisionable set, injected by
     the caller so this module keeps no dependency on `domain/`).
   - Skip if the arch has no `<emulator>` text (can't boot without knowing the
     binary; a defensive skip, not seen on real QEMU hosts where the element is
     always present).
   - `accel = "kvm"` iff the arch offers a `<domain type='kvm'>`; else `"tcg"`.
     libvirt advertises a KVM domain for an arch **only** when KVM can actually
     accelerate that guest arch on this host, so the domain advertisement is the
     authoritative KVM-availability signal — it already encodes host-nativeness
     (and correctly yields `kvm` for a same-family arch libvirt can accelerate,
     which an exact host-arch string compare would wrongly downgrade to `tcg`).
     A native arch with no `/dev/kvm` carries no KVM domain and falls back to `tcg`.
   - `emulator` = the arch-level `<emulator>` text.
3. On duplicate `<arch name=X>` across `<guest>` blocks (not seen in practice),
   first wins; deterministic.

Injecting `supported` (rather than importing `arch_traits`) keeps `libvirt_xml`
free of a `providers/shared → domain` dependency and makes the filter unit-testable
with an arbitrary set.

### `arch_traits` exposes its supported set

`domain/platform/arch_traits.py` gains `SUPPORTED_ARCHES: frozenset[str]` derived
from `_TRAITS` keys — the single source of truth for "arches kdive can
provision". Discovery imports it and passes it to `parse_guest_arches`, so adding
an arch stays one `_TRAITS` row.

### Discovery wiring

`LocalLibvirtDiscovery.list_resources` adds one capability entry:

```python
GUEST_ARCHES_KEY: parse_guest_arches(conn.getCapabilities(), SUPPORTED_ARCHES),
```

`conn.getCapabilities()` is already called for the `arch` key; the second parse
of the same small document is negligible and keeps the two parsers independently
composable. No other provider (remote-libvirt, fault-inject) is touched — they do
not implement the local guest-arch discovery and are out of scope for this issue.

### Emulator resolution — deliberate simplification

libvirt permits a per-`<domain>` `<emulator>` override (a Xen-era feature); kdive
is QEMU-only and every real QEMU capabilities document places `<emulator>` at the
`<arch>` level (verified). The parser reads the arch-level element and does not
implement the domain-nested override. Documented here so the omission is a
decision, not an oversight.

## Error handling / degradation

- Malformed/attack capabilities XML → `{}` (discovery still advertises host arch,
  vcpus, memory, disk, PCIe). Consistent with `parse_capabilities_arch` returning
  `"unknown"` rather than raising.
- A host with no `qemu-system-<foreign>` binary simply omits that arch — no error,
  no opt-in knob (epic decision 1: auto-discover, auto-allow).
- An arch libvirt reports but kdive cannot provision (`s390x`, `i686`, `ppc`,
  big-endian `ppc64`) is filtered out, so admission (issue 2) will reject a request
  for it via the same `guest_arches` membership check rather than failing deeper in
  provisioning.

## Testing

Unit only; no `live_vm` requirement (acceptance criterion). `FakeLibvirtConn`
already stubs `getCapabilities()`.

- **Parser** (`tests/providers/test_libvirt_xml.py`):
  - x86_64 host **with** `qemu-system-ppc64`: `{x86_64: kvm/…-x86_64,
    ppc64le: tcg/…-ppc64}`; the six-arch real fixture is filtered to the two
    supported arches (`s390x`, `i686`, `ppc`, big-endian `ppc64` dropped).
  - x86_64 host **without** the ppc64 binary: `{x86_64: kvm/…}` only.
  - ppc64le host, native arch with **no** KVM domain (verbatim from the real
    POWER10 host): `{ppc64le: tcg//usr/bin/qemu-system-ppc64le, x86_64:
    tcg//usr/bin/qemu-system-x86_64}` — this is the real "native falls back to
    `tcg` when the KVM domain is absent" case.
  - ppc64le host **with** KVM-HV — a **synthetic** fixture (no real KVM-HV POWER
    host is available; the POWER10 host advertises no KVM domain). Take the real
    ppc64le guest block and add a `<domain type='kvm'/>`:
    `{ppc64le: kvm//usr/bin/qemu-system-ppc64le, x86_64: tcg//usr/bin/qemu-system-x86_64}`.
  - Arch with no `<emulator>` element → skipped.
  - `os_type != "hvm"` guest block → skipped.
  - Malformed XML and the XXE/defused-entity document → `{}`.
- **Reader** (`tests/domain/catalog/test_resource_capabilities.py`):
  `guest_arches()` returns `{}` for an absent key; drops entries with a
  non-dict value or a non-string `accel`/`emulator`; passes a well-formed mapping
  through. `GUEST_ARCHES_KEY` is in `_KNOWN_KEYS` (so it does not leak into
  `extras()`).
- **Discovery** (`tests/providers/local_libvirt/test_discovery.py`):
  `list_resources` advertises `guest_arches` from the fake's caps XML; a host
  whose caps report only x86_64 advertises `{x86_64: …}`.
- **`arch_traits`**: `SUPPORTED_ARCHES == frozenset(_TRAITS)` and contains exactly
  `{"x86_64", "ppc64le"}`.

## Rejected alternatives

- **Advertise every arch libvirt reports (no filter).** Rejected: it would list
  `s390x`/`i686`/`ppc`/`ppc64`-BE as schedulable, but no kdive profile arch maps to
  them and `arch_traits()` fails fast for them — the mapping would promise
  capability the pipeline cannot deliver. Filtering to `SUPPORTED_ARCHES` makes
  `guest_arches` mean exactly "arches this host can boot *and* kdive can
  provision", which is the set admission should gate on.
- **Normalize `ppc64` → `ppc64le` in the parser.** Unnecessary: libvirt already
  reports `ppc64le` as a distinct arch. Big-endian `ppc64` is explicitly out of the
  epic's scope and is simply not in `SUPPORTED_ARCHES`, so it is filtered, not
  remapped.
- **Import `arch_traits` inside `libvirt_xml`.** Rejected: it would give the
  low-level shared XML helper a dependency on `domain/`. Injecting `supported`
  keeps the layering clean and the filter testable.
- **Store the resolved libvirt domain type (`kvm`/`qemu`) instead of the accel
  name (`kvm`/`tcg`).** Rejected: the accel name is the human/scheduling-facing
  fact (issue 2 persists it on the System, issue 4 scales TCG deadlines off it);
  the domain-type mapping is a rendering detail owned by issue 3's XML seam.
- **Handle the domain-nested `<emulator>` override.** Rejected as speculative: no
  real QEMU capabilities document uses it and kdive is QEMU-only. Documented, not
  silently dropped.
