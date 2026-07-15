# ADR 0353 — A `live_vm_tcg` tier: an orthogonal marker over the stack spine, gated by discovered guest arch

- **Status:** Accepted
- **Date:** 2026-07-15
- **Issue:** #1154
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0352 (#1153 per-arch guest-accel diagnostics — owns the authoritative
  `qemu_system_binary` map this gate reuses), ADR-0346 (#1148 ppc64le kdump capture — the
  proof this tier makes repeatable), ADR-0035 (§4 skip idiom for live tiers), ADR-0042
  (`live_stack` wire-transport tier)

## Context

The epic (`2026-07-13-ppc64le-full-support.md` §Diagnostics, docs, tests) requires a
guest-arch dimension in the live-VM tests, with foreign-arch (TCG-emulated) runs held to a
**separate marker** so the fast native tier stays fast — a ppc64le guest under TCG boots an
order of magnitude slower than a native KVM guest.

Four ppc64le TCG proofs already exist (#1144 SSH-reachable, #1146 uploaded-bundle boot,
#1148 kdump capture, #1151 fadump capture) but only as one-off runs: all four live in
`tests/integration/test_live_stack.py` carrying only `@pytest.mark.live_stack`, with no way
to select "the emulated foreign-arch spine" as a repeatable tier. Each gates the emulator
with an ad-hoc `shutil.which("qemu-system-ppc64")` literal that duplicates the authoritative
qemu-binary map added for the operator diagnostic in #1153.

Two structural facts constrain the shape of any fix:

- **Vehicles are not interchangeable.** `live_vm`-marked tests drive provider ports directly
  against a System the operator has *already* provisioned; they never allocate/provision/boot.
  `live_stack`-marked tests drive the full MCP HTTP transport and are the repo's **only**
  end-to-end provision→boot→crash→retrieve spine. A foreign-arch provision→boot proof can
  therefore only be a `live_stack` test — there is no stack-free path that provisions.
- **The qemu-binary map is single-sourced.** `qemu_system_binary(arch)`
  (`diagnostics/guest_arch_accel.py`, ADR-0352) already maps each `SUPPORTED_ARCHES` row to
  its system emulator (note the asymmetry: `ppc64le→qemu-system-ppc64`, no `-ppc64le` binary).

## Decision

**Introduce `live_vm_tcg` as an orthogonal *tier tag*, not a new vehicle.** The four proofs
keep `@pytest.mark.live_stack` (the vehicle that provisions) and **add**
`@pytest.mark.live_vm_tcg` (the tier: "boots an emulated foreign-arch guest"). Selection is
orthogonal:

- `just test-live-tcg` → `-m live_vm_tcg` (modeled on `test-live-stack`: `--strict-markers`,
  exit-5 tolerated as a clean skip).
- `just test-live` → `-m "live_vm and not live_vm_tcg"` (native tier, unchanged runtime; the
  `and not live_vm_tcg` is a cheap invariant guard, since the four proofs are not `live_vm`).
- `just test` (`-m "not live_vm and not live_stack"`) is unchanged — the proofs are still
  excluded through their `live_stack` marker.

**Reroute the emulator gate through a discovery-driven `require_guest_arch(arch)` helper**
(added to `tests/integration/live_stack/conftest.py`, the ADR-0035 §4 idiom). It reuses
`qemu_system_binary` (single source), `pytest.skip`s when the arch's emulator is not on PATH,
and returns the resolved accelerator (`"kvm"` when `arch` is the host's native arch and the host
KVM probe passes, else `"tcg"`). The default KVM signal reuses the #1153 **URI-selected** probe
(`kvm_probe_for_uri(resolved_libvirt_uri())`) — the identical signal the provider uses to persist
the System's accel (`os.access` R+W under `qemu:///session`, `os.path.exists` otherwise) — so the
gate's accel and the persisted accel cannot drift under any URI. The bootability skip itself stays
URI-blind. It takes injected `host_arch`/`which`/`kvm_present` seams (mirroring
`default_guest_arch_accel_probe`) so all four branches are unit-tested with no real host. The
four proofs funnel their emulator check through one shared preflight
(`_ppc64le_reachability_preflight`), so a single edit reroutes all four and retires the
`_PPC64LE_EMULATOR` literal.

**The returned accel is consumed, not cosmetic.** The reachability preflight surfaces
`expected_accel` in its return tuple, and the #1144 proof asserts the **persisted** accel from
`systems.get` equals it — replacing a hardcoded `== "tcg"` that would latently fail on a native
POWER host — giving the tier a falsifiable "booted under the host-implied accelerator" check.

**Tier membership is CI-pinned.** Because `test-live-tcg` mirrors `test-live-stack`'s
exit-5-is-a-clean-skip idiom, an emptied `-m live_vm_tcg` selection would read green. A
**non-gated** meta-test (in the ordinary `just test` suite, beside the existing
`test_exit_criteria.py` marker pins) asserts exactly the four named proofs carry both
`live_stack` and `live_vm_tcg` and no other test carries `live_vm_tcg`, so a dropped or stray
marker fails CI at the source rather than only under a manual `--collect-only`.

Register `live_vm_tcg` in `pyproject.toml` `markers`; document the three tiers in AGENTS.md
and the operator install/lifecycle docs. Test-only + docs — no production code, no migration.

## Consequences

- The emulated foreign-arch spine is a first-class repeatable tier (`just test-live-tcg`),
  skipping cleanly on a host without `qemu-system-ppc64` (a skip, never a failure) and, run
  without the stack up, skipping via the unchanged `require_stack()` gate.
- `just test-live` native runtime is unchanged and guarded against a future dual-marked test.
- One qemu-binary map serves both the operator diagnostic and the test gate — they cannot
  drift. A future arch added to `SUPPORTED_ARCHES` + the map makes `require_guest_arch` work
  for it with no test-layer edit.
- On a POWER host the gate resolves `ppc64le→kvm` and the proofs run natively; that path is
  gated on hardware (epic issue 17), so this ADR does not assert it.

## Rejected alternatives

- **Parametrize the native `live_vm` boot tests with an arch fixture over discovered
  `guest_arches`.** Rejected: native `live_vm` tests operate on one already-provisioned System
  (single arch, env-configured); a `@parametrize(arch=…)` matrix would emit instances that
  cannot boot the other arches and would all skip except the operator's provisioned one —
  fabricating a matrix that never runs while inflating the suite. The discovery-driven skip on
  the tests that *do* boot the arch is the honest realization of the "guest-arch dimension."
- **Build a stack-free `live_vm_tcg` harness driving the provider directly.** Rejected: the
  full provision→boot→crash→retrieve spine only exists over the stack; native `live_vm` tests
  do not provision. A bespoke harness would duplicate the entire orchestration.
- **Make `live_vm_tcg` imply `live_vm`.** Rejected: it would pull a 10×-slower TCG boot into
  `just test-live`, violating the "native runtime unchanged" acceptance invariant. The two
  markers are disjoint tiers.
- **Re-home the proofs out of `test_live_stack.py` into a `live_vm` module.** Rejected: they
  genuinely need the HTTP stack and share the spine helpers; dual-marking in place is minimal
  and honest.
- **Keep the ad-hoc `shutil.which("qemu-system-ppc64")` gate (or add a fresh binary literal).**
  Rejected: it duplicates and would drift from the #1153 `qemu_system_binary` map; the gate
  reuses that single source.
- **Reuse the full async `default_guest_arch_accel_probe()` in the skip gate.** Rejected: it is
  async (awkward from a synchronous `pytest.skip` gate) and bundles the per-arch loop the gate
  does not need. The gate instead reuses the probe's *component* helpers directly —
  `qemu_system_binary` for bootability and `kvm_probe_for_uri(resolved_libvirt_uri())` for the
  accel — which single-sources both signals against the provider without pulling in the async
  loop.

## Rollout

Additive and backward compatible. No migration, no schema change, no new dependency, no
production or agent-facing behavior change — a new marker + recipe, a rerouted skip gate, and
docs. Existing tiers (`just test`, `test-live`, `test-live-stack`) collect the same sets.
