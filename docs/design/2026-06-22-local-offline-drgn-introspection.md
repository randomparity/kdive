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

**Surface effect on `resources.describe`.** `resources.describe` projects
`runtime.supported_introspection` verbatim into the System's capability descriptor
(`mcp/tools/catalog/resources.py`). Before this change a local System reports no introspection
modes; after it, the System reports `offline-vmcore`. That is the intended, honest report: the
seam is wired, so the plane is admissible. It does **not** assert hardware-proof — that fact lives
in the tool's `maturity` flag (§3), which stays `partial`. The two are deliberately separate
signals: `describe` answers "would admission admit this plane?" (now yes), the maturity flag
answers "is the wired path proven on hardware?" (not yet). On a drgn-less host the admitted call
then returns `MISSING_DEPENDENCY` from the open seam — the honest runtime outcome. This combined
surface (describe advertises `offline-vmcore`; maturity stays `partial`) is the designed contract,
not an accident, and the acceptance criteria below pin both halves.

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

The honesty drift-guard moves with the state, and the replacement must be **provably at least as
strong** as the guard it removes. The removed guard
(`test_local_stubbed_planes_advertise_planned_provider_pointer`) asserts both `local-libvirt:
planned` *and* `remote-libvirt: implemented` are present in the pointer. `introspect.from_vmcore`
leaves `_LOCAL_PLANNED_PROVIDER_TOOLS`, and a new positive assertion on its pointer asserts:

- **present:** the stable marker `local-libvirt: wired` (the post-wiring, pre-promotion state) and
  `remote-libvirt: implemented`;
- **absent:** both `local-libvirt: planned` and `local-libvirt: implemented`.

Requiring the absence of *both* `planned` and `implemented` means the new guard cannot be satisfied
by an unchanged pre-wiring pointer (`planned`) **or** by a future over-promotion to `implemented`
before the B6 live proof — so it is strictly stronger than the substring it replaces, not weaker.
The pointer's exact wording is therefore: `local-libvirt: wired, pending live KVM proof (M2.8 B6
#680); remote-libvirt: implemented; fault-inject: n/a.`

## Acceptance criteria

- **CI (fakes):**
  - `LocalLibvirtVmcoreIntrospect.from_env()` returns a port whose `open_program`/`run_helper` are
    the real `debug_common.drgn_program` seams (not `None`), asserted without importing drgn.
  - With drgn absent (the CI host), `from_env().from_vmcore(...)` raises `MISSING_DEPENDENCY` from
    the drgn-open seam — proving the wired path reaches the import, not the old `None`-guard. Because
    `from_vmcore` fetches **two** objects before the open (the vmcore at `vmcore_ref`, then the
    vmlinux at `debuginfo_ref` — `introspect.py` lines 99 and 101, both ahead of the open at line
    110), the test's fetch fake must serve **both** refs, and `read_vmcore_build_id` must be the real
    `read_vmcoreinfo_build_id` over a vmcore blob whose `BUILD-ID=` line matches `expected_build_id`.
    Only that combination drives control past provenance and the second fetch into the import; a fake
    that serves only the core ref, or a build-id that mismatches, exercises a *different* failure
    (`CONFIGURATION_ERROR`) and would not prove the import is reached.
  - `build_runtime(...).supported_introspection == frozenset({"offline-vmcore"})`; the debug-
    transport and `live` introspection sets stay empty.
  - The introspect admission test admits `introspect.from_vmcore` on a local descriptor advertising
    `offline-vmcore` (it already covers the deny path with an empty descriptor; add/confirm the
    admit path so the acceptance criterion "admission admits offline introspection on local" is a
    test, not a claim).
  - `resources.describe` for a local System projects `offline-vmcore` into its introspection
    capability list (the §2 surface effect), asserted by the resources-tool test or descriptor
    projection test.
  - `introspect.from_vmcore` carries `maturity: "partial"` with the exact `providers` pointer
    `local-libvirt: wired, pending live KVM proof (M2.8 B6 #680); remote-libvirt: implemented;
    fault-inject: n/a.`. The honesty test asserts `local-libvirt: wired` and `remote-libvirt:
    implemented` are present and that **neither** `local-libvirt: planned` nor `local-libvirt:
    implemented` appears (see §3). Generated `docs/guide/reference/introspect.md` is regenerated to
    match (`just docs`), so `docs-check` stays green.
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
  build-id matches `expected_build_id` **and** a fetch fake that serves both the vmcore and vmlinux
  refs (the second fetch at `introspect.py:101` runs before the open), or it exercises a different
  failure than the import-reaching one it claims to.
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
