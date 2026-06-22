# Spec — local-libvirt offline drgn introspection (`introspect.from_vmcore`)

- **Issue:** #676 (M2.8 Epic B, B2)
- **ADR:** [ADR-0210](../adr/0210-local-libvirt-live-debug-introspection.md) §2 (anchor;
  do not re-decide). Builds on [ADR-0033](../adr/0033-drgn-introspection-from-vmcore.md)
  (the offline introspection contract), [ADR-0208](../adr/0208-provider-capability-descriptor.md)
  (the descriptor each plane flips on as it lands), and
  [ADR-0209](../adr/0209-capability-aware-mcp-admission.md) (the fail-fast that gates the plane
  until it is wired).
- **Design doc:** [m2.8-local-libvirt-service-parity](../design/m2.8-local-libvirt-service-parity.md)
- **Status:** Accepted

## Problem

`LocalLibvirtVmcoreIntrospect.from_env()` builds the port with its drgn seams left `None`
(`open_program`/`run_helper`) and with `_real_read_vmcore_build_id` raising `MISSING_DEPENDENCY`
unconditionally. So `introspect.from_vmcore` against a local-libvirt Run raises
`MISSING_DEPENDENCY` up front, *before* it ever touches the captured core — the off-gate
short-circuit. The local provider's runtime descriptor therefore leaves `supported_introspection`
empty, and capability-aware admission (ADR-0209) rejects offline introspection on local with
`capability_unsupported`.

The orchestration around those seams is already real and unit-tested: the object-store fetch
(`_real_fetch_object`), build-id provenance check, drgn-open/helper dispatch path, byte-cap, and
the single redaction boundary (`assemble_report`). The *only* missing piece is wiring `from_env()`
to the production drgn seams, which already exist, provider-neutral, in
`providers/shared/debug_common/drgn_program.py` and are exactly what the remote provider's
`RemoteLibvirtVmcoreIntrospect.from_env()` already wires.

## Decision

Per ADR-0210 §2, wire `LocalLibvirtVmcoreIntrospect.from_env()` to the real drgn seams and flip
the local provider's `supported_introspection` descriptor to advertise `offline-vmcore`. The port
interface, the `from_vmcore` orchestration, and the shared seams are unchanged.

### 1. Wire the real seams in `from_env()`

`LocalLibvirtVmcoreIntrospect.from_env()` constructs the port with the three production seams from
`debug_common.drgn_program`, mirroring the remote provider exactly:

- `read_vmcore_build_id = read_vmcoreinfo_build_id` — reads the crashed kernel's GNU build-id from
  the core's VMCOREINFO `BUILD-ID=` line; raises `CONFIGURATION_ERROR` when a core carries no such
  line.
- `open_program = open_vmcore_program` — lazily imports drgn, opens a `drgn.Program` over the
  staged core + vmlinux, and adapts it to the helpers' `_Program` protocol. A genuinely absent
  `drgn` import raises `MISSING_DEPENDENCY` *from inside the seam* (`_require_drgn`).
- `run_helper = run_introspection_helper` — dispatches one fixed helper (`tasks`/`modules`/
  `sysinfo`) by name.

The local-only `_real_read_vmcore_build_id` placeholder (which raised `MISSING_DEPENDENCY`
unconditionally) is **removed** — replaced, not deprecated. The local `_real_fetch_object`
placeholder already does the real object-store read and stays.

Consequence on the `MISSING_DEPENDENCY` contract: with the seams wired, `from_vmcore` no longer
short-circuits on `self._open_program is None`. On a host that lacks `drgn`, the call now fetches
the core, verifies provenance, then raises `MISSING_DEPENDENCY` from `open_vmcore_program` when it
tries to import drgn — the same category, surfaced one step later, against a provider that *does*
support the plane (invariant 3). This is the intended post-wiring behavior, identical to remote.

### 2. Flip the descriptor

`build_runtime` in `providers/local_libvirt/composition.py` passes
`supported_introspection=frozenset({"offline-vmcore"})` to `ProviderRuntime`. This is the only
descriptor change: `supported_debug_transports` (B1) and the `live` introspection mode (B3) stay
out of scope and remain unset by this change. Admission (ADR-0209) then admits
`introspect.from_vmcore` on a local-libvirt Run instead of returning `capability_unsupported`.

### 3. Maturity stays `partial` (deliberate deviation from ADR-0210's literal text)

ADR-0210 and design-doc invariant 2 say each B plane "promotes its maturity in the same PR that
wires it." Design-doc **invariant 5** says live capability is *proven on hardware* before maturity
promotes to `"implemented"`, and CI cannot drive a real KVM host. These two invariants are in
tension for the seam-wiring PR; the milestone resolves it by separating two distinct facts the
surface reports:

- **The descriptor** (`supported_introspection`) states *the seam is wired* — the code path now
  exists and admission should admit it. This flips here. Reporting it as still-absent after wiring
  would itself be a lie (the negative direction of invariant 2).
- **The tool maturity** (`partial` → `implemented`, ADR-0175) states *the wired path is proven*.
  Per invariant 5 that requires the live KVM run, which is B6 (#680), not this PR.

So this PR keeps `introspect.from_vmcore` at `maturity: "partial"`, updates its `providers` pointer
to state local-libvirt is **wired, pending live KVM proof (M2.8 B6 #680)** rather than the
pre-wiring "planned (M2.8 B2)", and leaves the `implemented` promotion to the orchestrator's
post-merge live run. The catalog therefore never claims more than is true: admission admits the
plane (it is wired), and the maturity flag still tells an agent the path is not yet hardware-proven.

The honesty drift-guard moves with the state: `introspect.from_vmcore` leaves the
`_LOCAL_PLANNED_PROVIDER_TOOLS` "planned" set, and a new positive assertion pins its pointer to the
"wired, pending live" wording so the honesty test keeps teeth.

## Acceptance criteria

- **CI (fakes):**
  - `LocalLibvirtVmcoreIntrospect.from_env()` returns a port whose `open_program`/`run_helper` are
    the real `debug_common.drgn_program` seams (not `None`), asserted without importing drgn.
  - With drgn absent (the CI host), `from_env().from_vmcore(...)` over a core carrying a valid
    VMCOREINFO `BUILD-ID=` line that matches `expected_build_id` raises `MISSING_DEPENDENCY` from
    the drgn-open seam — proving the wired path reaches the import, not the old `None`-guard.
  - `build_runtime(...).supported_introspection == frozenset({"offline-vmcore"})`; the debug-
    transport and `live` introspection sets stay empty.
  - The introspect admission test admits `introspect.from_vmcore` on a local descriptor advertising
    `offline-vmcore` (it already covers the deny path with an empty descriptor; add/confirm the
    admit path so the acceptance criterion "admission admits offline introspection on local" is a
    test, not a claim).
  - `introspect.from_vmcore` carries `maturity: "partial"` with a `providers` pointer that no longer
    says "local-libvirt: planned" and instead states local-libvirt is wired pending live proof;
    `remote-libvirt: implemented` is preserved. Generated `docs/guide/reference/introspect.md` is
    regenerated to match (`just docs`), so `docs-check` stays green.
  - The full existing orchestration/redaction/byte-cap unit suite for the offline port stays green
    (behavior unchanged).
- **Live (KVM host) — orchestrator post-merge, NOT this PR:** `introspect.from_vmcore` runs the
  helpers against a real captured core on the development KVM host. Maturity promotes to
  `implemented` only after that run (B6, #680).

## Out of scope

- B1 (#675): gdbstub/drgn-live transport resolution, `supported_debug_transports`.
- B3 (#677): `LocalLibvirtLiveIntrospect` wiring, the `live` introspection mode. The Live class in
  `introspect.py` is **not touched** by this change.
- Promoting `introspect.from_vmcore` to `implemented` (B6 live proof owns that).
- Any change to the shared `drgn_program` seams or the `assemble_report` redaction boundary.

## Risks & failure modes

- **Provenance gate ordering with drgn absent.** Post-wiring, a build-id mismatch surfaces
  `CONFIGURATION_ERROR` *before* the drgn import is attempted (provenance runs before open). A core
  with no VMCOREINFO `BUILD-ID=` line surfaces `CONFIGURATION_ERROR` from `read_vmcoreinfo_build_id`,
  also before the import. Only a provenance-valid core on a drgn-less host reaches the
  `MISSING_DEPENDENCY` from the open seam. The CI test must therefore feed a core whose VMCOREINFO
  build-id matches `expected_build_id` to exercise the import-reaching path.
- **Honesty regression.** Removing `introspect.from_vmcore` from the "planned" guard set without a
  replacement positive assertion would let a future edit silently revert the pointer to a dishonest
  "implemented" or drop the live-proof caveat. Mitigation: the new positive assertion pins the
  exact post-wiring wording.
- **Descriptor over-reach.** Flipping more than `offline-vmcore` (e.g. accidentally adding `live`)
  would advertise an unwired B3 plane. Mitigation: the composition test asserts the exact set
  `frozenset({"offline-vmcore"})` and that the debug-transport set stays empty.
- **Cross-agent conflict.** B3 (#677) edits the same `introspect.py` (the Live class) and the same
  `composition.py` line in a later wave; it rebases on top of this change. This change touches only
  the Vmcore class, the `from_env` it owns, and the single `supported_introspection=` kwarg — the
  minimal additive surface, so the rebase is a one-line union on the descriptor.
