# Implementation plan — accel-derived domain type, emulator, per-arch CPU (#1142)

Spec: `docs/design/2026-07-13-accel-domain-xml-1142.md` · ADR: `docs/adr/0340-accel-derived-domain-xml.md`

Branch: `feat/accel-domain-xml-1142` · Base: `main` · **No migration, no schema change**
(domain type is derived from `accel`, not stored).

TDD throughout: write the failing test first, then the code. Commit per task with a
conventional message ending in the repo's `Co-Authored-By` trailer. Keep guardrails green at
each commit — `just lint` (ruff), `just type` (ty, whole tree), `just test`; run `just ci`
before push. Unit-test-only (no `live_vm`/`live_stack`); live TCG boot proof is #1144.

## Ground truth (verified live in #1140, reuse verbatim for fixtures)

- **x86_64 host** advertises guest arches including `x86_64` (qemu+kvm,
  `/usr/bin/qemu-system-x86_64`) and `ppc64le` (qemu-only, `/usr/bin/qemu-system-ppc64`).
  Host `<cpu><arch>` = `x86_64`. So on this host: `x86_64 → {accel: kvm, emulator:
  /usr/bin/qemu-system-x86_64}`, `ppc64le → {accel: tcg, emulator: /usr/bin/qemu-system-ppc64}`.
- **ppc64le POWER10 host** advertises `ppc64le` (qemu-only, `/usr/bin/qemu-system-ppc64le`)
  and `x86_64` (qemu, `/usr/bin/qemu-system-x86_64`); no KVM domain for any arch → every arch
  resolves `tcg`.
- A `<guest>` block is `<os_type>hvm</os_type>` then `<arch name=X>…<emulator/><machine/>…
  <domain type='qemu'/>[<domain type='kvm'/>]</arch>`.

`GuestArch.emulator` is a non-optional `str` (`resource_capabilities.py:29`) — a native
x86_64-KVM entry still carries an emulator path. This is why the renderer must drop the
`<emulator>` by `accel == "kvm"`, not by `emulator is None`.

## Existing shape (do not re-derive)

- `src/kdive/providers/local_libvirt/lifecycle/xml.py` — `render_domain_xml` and helpers
  `_build_baseline_domain` (hardcodes `type="kvm"`), `_append_host_cpu` (`host-passthrough`),
  `_append_crash_capture_features` (`<acpi/><vmcoreinfo/>`), `_append_os`.
- `src/kdive/domain/platform/arch_traits.py` — `ArchTraits` (`machine`, `console_device`,
  `pin_nic_slot`) + `arch_traits()` + `SUPPORTED_ARCHES`.
- `src/kdive/domain/catalog/resource_capabilities.py` — `GuestArch` TypedDict,
  `ResourceCapabilities.guest_arches()`.
- `src/kdive/services/systems/validation.py` — `resolve_accel(guest_arches, arch) -> str | None`.
- `src/kdive/providers/shared/libvirt_xml.py` — `parse_guest_arches(caps_xml, supported)`.
- `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` — `LocalLibvirtProvisioning`,
  `_LibvirtConn` Protocol (has `defineXML`/`lookupByName` only), `provision()` (calls
  `render_domain_xml` at line ~267), `reprovision()` (delegates to `provision`).

Tests: `tests/providers/local_libvirt/test_provisioning.py` (has `_render` helper + golden
XML at ~line 64), `tests/adversarial/test_provider_xml.py`,
`tests/services/systems/test_system_validation.py` (asserts `resolve_accel`).

---

## Task 1 — shared `resolve_accel_emulator` helper (domain) + re-express `resolve_accel` + parity test

**Where it fits:** kills the divergence risk between the provider's provision-time resolution
and admission's `resolve_accel` by giving them one branch definition (spec "Sourcing → One
shared resolver").

**Test first** (`tests/domain/catalog/test_resource_capabilities.py` or the nearest existing
home — check where `guest_arches()` is tested and colocate):
- `resolve_accel_emulator({}, "x86_64")` → returns `None` (empty-map fail-open).
- `resolve_accel_emulator({"x86_64": {"accel": "kvm", "emulator": "/u/q-x86"}}, "x86_64")` →
  `("kvm", "/u/q-x86")`.
- `resolve_accel_emulator({"x86_64": {...}}, "ppc64le")` → raises `CategorizedError`
  `CONFIGURATION_ERROR`, `details["accepted_values"] == ["x86_64"]`.
- **Parity test** (spec AC "Parity"): for each of the three input classes (empty / present /
  absent), assert `resolve_accel(m, a)` and `resolve_accel_emulator(m, a)` agree —
  `resolve_accel` returns `None`/`accel`/raises exactly when the helper returns
  `None`/`(accel, _)`/raises.

**Code:**
- Add `resolve_accel_emulator(guest_arches: Mapping[str, GuestArch], arch: str) -> tuple[str,
  str] | None` to `src/kdive/domain/catalog/resource_capabilities.py`. Empty map → `None`;
  arch absent → raise `CategorizedError(CONFIGURATION_ERROR, details={"requested_arch": arch,
  "accepted_values": sorted(guest_arches)})` with a message naming the supported set; else
  `(entry["accel"], entry["emulator"])`. Reuse the exact message/`details` shape currently in
  `resolve_accel` so #1141's tests stay green.
- Re-express `services/systems/validation.py:resolve_accel` as: `resolved =
  resolve_accel_emulator(guest_arches, arch); return resolved[0] if resolved is not None else
  None`. Keep its docstring/ADR-0339 citation; import the helper from the domain layer.

**Acceptance:** the three helper tests + parity test pass; existing
`tests/services/systems/test_system_validation.py` + `test_admission.py` +
`test_systems_admission_arch.py` stay green unchanged. `just lint`/`just type`/`just test` green.

**Guardrails/notes:** layering OK — both `services` and `providers/local_libvirt` already
import `domain.catalog.resource_capabilities`. Do **not** change `resolve_accel`'s public
signature or error text.

---

## Task 2 — `arch_traits` gains `kvm_cpu_mode` + `emit_acpi_features`

**Where it fits:** routes the per-arch CPU mode and the x86-only ACPI/VMCOREINFO block through
the one arch table (spec "Renderer"), so the renderer stays branch-free.

**Test first** (`tests/domain/platform/test_arch_traits.py` — or wherever `arch_traits` is
tested):
- `arch_traits("x86_64").kvm_cpu_mode == "host-passthrough"` and `.emit_acpi_features is True`.
- `arch_traits("ppc64le").kvm_cpu_mode == "host-model"` and `.emit_acpi_features is False`.

**Code:** in `src/kdive/domain/platform/arch_traits.py` add two fields to the frozen
`ArchTraits` dataclass (`kvm_cpu_mode: str`, `emit_acpi_features: bool`) with docstring lines,
and populate both `_TRAITS` rows: `x86_64 → host-passthrough / True`, `ppc64le → host-model /
False`.

**Acceptance:** the two trait tests pass; any test constructing `ArchTraits` positionally is
updated (grep for `ArchTraits(` in tests). `just type` green (frozen dataclass, all fields set).

---

## Task 3 — renderer: accel-derived domain type, `<emulator>`, per-arch `<cpu>`/`<features>`

**Where it fits:** the core of the issue (`xml.py`). Consumes `accel`/`emulator` + `arch_traits`.

**Test first** — add to `tests/providers/local_libvirt/test_provisioning.py` (extend the
`_render` helper to accept `accel="kvm"`, `emulator=None` and forward them):
1. **Byte-identical, defaults** — `_render()` (x86_64 profile, `("kvm", None)`) equals the
   current golden string (keep/point at the existing golden assertion).
2. **Byte-identical, real native input** — `_render(accel="kvm",
   emulator="/usr/bin/qemu-system-x86_64")` equals the **same** golden string (the `<emulator>`
   is dropped because `accel == "kvm"`, not because emulator is None). *(spec AC#3)*
3. **Four combinations** (x86_64/ppc64le × kvm/tcg), each asserting on the parsed tree:
   - `<domain type>` = `kvm` (kvm) / `qemu` (tcg);
   - `devices/emulator` **absent** for kvm; **present** with the passed path for tcg;
   - `os/type@machine` = arch default (`q35`/`pseries`) unless overridden;
   - `<cpu>`: kvm → present with `mode` = `host-passthrough` (x86_64) / `host-model` (ppc64le);
     tcg → **absent**;
   - `<features>`: present (acpi+vmcoreinfo) for x86_64; **absent** for ppc64le.
4. **Machine override still wins** — a ppc64le profile with
   `domain_xml_params["machine"]="pseries-8.2"` renders that machine (spec AC#2).
5. **TCG with `emulator=None`** → `render_domain_xml(..., accel="tcg", emulator=None)` raises
   `CONFIGURATION_ERROR` (defensive guard).

**Code:** in `src/kdive/providers/local_libvirt/lifecycle/xml.py`:
- `render_domain_xml` signature gains `accel: str = "kvm"`, `emulator: str | None = None`
  (keyword). Thread both into `_build_baseline_domain`.
- `_build_baseline_domain`: `domain = ET.Element("domain", type=("kvm" if accel == "kvm" else
  "qemu"))`. After `_append_os`, if `accel == "kvm"` emit `<cpu mode=traits.kvm_cpu_mode>`
  (replaces the unconditional `_append_host_cpu`), else no `<cpu>`. Guard: if `accel != "kvm"`
  and `emulator is None`, raise `CONFIGURATION_ERROR`. Emit `<emulator>` as the first child of
  `<devices>` only when `accel != "kvm"`.
- Gate `_append_crash_capture_features` on `traits.emit_acpi_features`.
- Delete/replace `_append_host_cpu` (its ADR-0294 rationale moves onto the KVM CPU emission
  — keep the x86-64-v2 explanation comment near the `host-passthrough` mode).
- Update the `render_domain_xml` docstring: it now renders TCG/foreign-arch domains; note the
  `accel`/`emulator` params, the emulator-only-for-TCG rule, and that TCG omits `<cpu>`.

**Acceptance:** all Task-3 tests pass; the **entire** existing `test_provisioning.py` +
`test_provider_xml.py` suites stay green (defaults preserve x86-KVM byte-identity, so
unrelated render tests are unaffected). `just lint`/`just type`/`just test` green.

**Rollback/cleanup note:** if the golden string changes for the default/native-emulator cases,
stop — that is the byte-identical regression the AC forbids; do not update the golden to match.

---

## Task 4 — provider wiring: resolve `{accel, emulator}` from live caps in `provision()`

**Where it fits:** feeds the renderer from live libvirt capabilities, mirroring admission
(spec "Sourcing"). `reprovision` delegates to `provision`, so this one site covers both.

**Test first** — in `tests/providers/local_libvirt/test_provisioning.py`, using the existing
fake libvirt connection (extend it with a `getCapabilities()` returning a supplied caps XML;
reuse the #1140 hand-written caps fixtures):
- **empty guest_arches** (caps with no matching `<guest>`) → provision renders a
  `<domain type="kvm">` with no `<emulator>` (legacy path); assert via the defined XML.
- **native x86_64** (caps advertising `x86_64` kvm + emulator) → the domain type is `kvm` and
  there is **no** `<emulator>` (forwarded `("kvm", "/usr/.../qemu-system-x86_64")`, dropped by
  the renderer).
- **ppc64le present** (caps advertising `ppc64le` tcg + `/usr/bin/qemu-system-ppc64`) → a
  ppc64le profile renders `<domain type="qemu">` with `<emulator>/usr/bin/qemu-system-ppc64`.
- **arch absent** (non-empty caps missing the profile arch) → provision raises
  `CONFIGURATION_ERROR` (does **not** define a domain).
- **getCapabilities raises `libvirt.libvirtError`** → provision raises `INFRASTRUCTURE_FAILURE`.

**Code:** in `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`:
- Add `getCapabilities(self) -> str: ...` to the `_LibvirtConn` Protocol.
- Add `_resolve_guest_arch(self, arch: str) -> tuple[str, str | None]`: open a connection
  (reuse the `_connect()` pattern; wrap open/`getCapabilities` `libvirtError` → the provider's
  existing `_infra(...)`/`INFRASTRUCTURE_FAILURE` helper, matching `_recorded_ssh_port`).
  `guest_arches = parse_guest_arches(caps, SUPPORTED_ARCHES)`; `resolved =
  resolve_accel_emulator(guest_arches, arch)`; return `resolved if resolved is not None else
  ("kvm", None)`. Close the connection in a `finally`.
- In `provision()`, before `render_domain_xml`: `accel, emulator =
  self._resolve_guest_arch(profile.arch)` and pass `accel=accel, emulator=emulator` into
  `render_domain_xml`. Import `parse_guest_arches`, `SUPPORTED_ARCHES`,
  `resolve_accel_emulator`.

**Acceptance:** the five provider tests pass; existing provision/reprovision tests stay green
(they use the fake conn — give it a default `getCapabilities` returning caps that advertise
the x86_64 profile's arch so the legacy assertions hold, or an empty-caps default that yields
the `("kvm", None)` legacy path — pick whichever keeps the existing golden assertions
unchanged and document the choice in the fake). `just ci` green.

**Rollback/cleanup note:** the extra `getCapabilities` connection is opened and closed per
provision; ensure the `finally` closes it even on the fail-closed raise so a rejected foreign
provision leaks no libvirt connection.

---

## Task 5 — `GuestArch` shape round-trip guard (#1140 follow-up)

**Where it fits:** the owner's #1140 review follow-up — guard against parser/reader drift.
This PR does **not** extend `GuestArch` (domain type is derived), so this is a low-cost guard,
not a fix.

**Test:** a full-shape `parse_guest_arches` output (from a caps fixture with accel+emulator)
fed through `ResourceCapabilities.from_mapping({GUEST_ARCHES_KEY: parsed}).guest_arches()`
round-trips unchanged — every arch key and both fields survive. If a future field is added to
one side but not the other, this fails.

**Acceptance:** test passes; colocate with the `guest_arches()` tests. `just test` green.

---

## Final verification (before PR)

- `just ci` green (lint, type, lint-shell, lint-workflows, check-mermaid, test).
- `git grep -n 'host-passthrough'` — confirm the ADR-0294 rationale still reads correctly on
  the KVM path after `_append_host_cpu` is refactored.
- Confirm no golden XML string was edited to accommodate a change (byte-identity is by
  construction via the `("kvm", …)` path, not by rewriting the expected output).
- Confirm `resolve_accel`'s error message/`details` are unchanged (diff
  `tests/services/systems/test_system_validation.py` expectations — none should move).
