# x86_64-guest symmetry audit for ppc64le hosts (#1155)

Date: 2026-07-15
Status: approved (design)
Issue: #1155 · Epic: #1139 (full ppc64le support) · ADR: `docs/adr/0354-host-arch-guest-symmetry-invariant.md`
Depends on: #1141 (admission arch-validate + persist accel, ADR-0339, merged), #1142 (accel-derived domain XML, ADR-0340, merged) — both CLOSED

## Problem

The epic's goal is symmetric: the local-libvirt provider runs a foreign-arch guest under
TCG on *either* host. On the x86_64 validation host that means a ppc64le guest under TCG; on
a POWER host it means an x86_64 guest under TCG. The auto-discovery design
(`2026-07-13-ppc64le-full-support.md` §Arch capability and admission) makes the inverse case
fall out **structurally** — the provider derives guest behavior from `profile.arch` and the
libvirt-advertised accelerator, never from the host arch — but *nothing verifies* the
codebase actually holds no `host == x86_64` assumption. Every existing arch-parameterized
test runs on the x86_64 host, so a latent host-arch dependency in a guest-facing path would
be invisible until POWER10 bring-up (#1157).

This issue is a **unit-level audit plus an inverted host/guest matrix test suite**: prove no
guest-facing path reads the host arch, lock that invariant against regression, and fix any
assumption the audit surfaces. Live proof on real POWER hardware is explicitly deferred to
#1157.

## Audit result (ground truth)

A read of every arch-handling seam finds **no host-arch assumption in any guest-facing path**;
no production code change is required. The guest-facing facts are all derived from
`profile.arch` (a table lookup) plus the resolved accelerator:

- **Discovery** (`providers/shared/libvirt_xml.py:parse_guest_arches`) — derives the accel per
  guest arch from libvirt's *own* `<domain type='kvm'>` advertisement for that arch, not from
  the host arch. Already covered for the inverted matrix by
  `tests/providers/test_libvirt_xml.py::test_parse_guest_arches_synthetic_kvm_hv_ppc_host_is_kvm`,
  which asserts a ppc64le host advertises `{ppc64le: kvm, x86_64: tcg}`.
- **Admission** (`services/systems/validation.py:resolve_accel` →
  `domain/catalog/resource_capabilities.py:resolve_accel_emulator`) — validates `profile.arch`
  against the persisted `guest_arches` and reads the recorded accel; host-arch-independent.
- **Domain XML** (`providers/local_libvirt/lifecycle/xml.py`) — every guest-facing element
  (machine, console, `<cpu>`, `<features>`, `<emulator>`) is derived from `profile.arch` via
  `arch_traits()` plus the `accel`/`emulator` **arguments**. The renderer never reads the host.
- **Deadlines** (`providers/local_libvirt/lifecycle/deadlines.py:tcg_deadline_multiplier`) —
  keyed off the persisted `accel` string alone; arch- and host-agnostic.
- **Debug plane** (`providers/shared/debug_common/gdbmi/policy/arch.py:select_gdb_binary`,
  `.../gdbmi/core/engine.py`) — the host arch *is* read, but only to select a host-side gdb
  **binary** for a cross-arch attach (the `guest_arch` comes from the staged `vmlinux` ELF
  header). This is the allowed accelerator/tooling-selection exception.

The host arch (`platform.machine()`) is read in exactly three production sites, all
binary-selection:

1. `diagnostics/guest_arch_accel.py` — per-arch guest-accelerator doctor probe (ADR-0352).
2. `diagnostics/multiarch_gdb.py` — cross-arch gdb doctor probe (ADR-0347).
3. `providers/shared/debug_common/gdbmi/core/engine.py` — the gdb-engine's cross-arch binary
   selection (ADR-0347).

(`processes/worker.py` reads `socket.gethostname()` for a worker id — a hostname, not an arch.)

## Constraints and ground truth

- **The accelerator is the one legitimate host-derived signal.** AC#2 permits deriving
  behavior from host arch *for accelerator selection only*. The three sites above are exactly
  that carve-out; the guard test (below) encodes the boundary.
- **The x86_64 validation host cannot run the inverted matrix live.** An x86_64 guest under KVM
  is native there, not TCG, so proving "x86_64 guest under TCG on a POWER host" end-to-end
  needs POWER hardware (#1157). This issue verifies the inverted matrix at the **unit level**:
  the code paths are fed a ppc64le-host `guest_arches` mapping and an x86_64 `profile.arch`, and
  their guest-facing output is asserted — no host, no boot.
- **Symmetry is a data-flow property, not a host property.** Because the renderer takes
  `(profile, accel, emulator)` and never reads the host, a unit test that supplies
  `accel="tcg"`/`emulator="qemu-system-x86_64"` for an x86_64 profile exercises the *identical*
  code path a POWER host would drive. The unit-level proof is therefore faithful, not a proxy.

## Goal

- An **inverted host/guest matrix** proven at the unit level: a ppc64le host advertising
  `{ppc64le: kvm, x86_64: tcg}` admits an x86_64 guest as `accel=tcg`, renders it as a
  q35 + `ttyS0` + `type=qemu` + `qemu-system-x86_64` domain with no `<cpu>`, and scales its
  boot deadline.
- A **static confinement guard** encoding AC#2: host-arch reads stay confined to the three
  accelerator/gdb-selection modules; a future module that newly reads the host arch fails a
  non-gated test with an actionable message.
- **ADR-0354** recording the symmetry invariant, the enumerated allowlist, and the guard as
  its enforcement.

No production code change (the audit found none needed); no migration (test + docs only).

## Design

### Inverted-matrix behavioral tests

Each seam is exercised with the host/guest matrix inverted. Where a seam is already covered
for the inverted matrix, the ADR references the existing test rather than duplicating it.

**Discovery — already covered.** `test_parse_guest_arches_synthetic_kvm_hv_ppc_host_is_kvm`
asserts `{ppc64le: kvm, x86_64: tcg}`, and `test_parse_guest_arches_ppc_host_no_kvm_domain_is_all_tcg`
asserts the all-TCG POWER10 case. The ADR cites these; no new discovery test.

**Admission (new).** Extend `tests/integration/test_systems_admission_arch.py` with an inverted
host constant `_PPC_HOST_GUEST_ARCHES = {"ppc64le": {kvm…}, "x86_64": {tcg…}}` and a test that
provisions the default (x86_64) profile against a Resource carrying it, asserting the System
records **`accel == "tcg"`**. This is the real coverage gap: every current "records accel"
admission test uses a host where x86_64 is native (`accel=kvm`), so an x86_64-guest-under-TCG
admission has never been asserted. Uses the existing `_set_resource_guest_arches` helper and
the real-DB harness already in the file.

**Domain XML (new assertion on an existing matrix).** The
`test_render_domain_by_arch_and_accel` parametrization in
`tests/providers/local_libvirt/test_provisioning.py` already includes the
`("x86_64", "tcg", "/usr/bin/qemu-system-x86_64", "qemu", "q35", None, True)` cell — it asserts
domain type, machine, `<cpu>` absence, emulator, and features, but **not the console device**.
Extend that parametrization (or add a focused test) to assert `console=ttyS0` in the `<cmdline>`
for the x86_64+tcg cell, closing the one named gap ("q35 + ttyS0 + type=qemu +
qemu-system-x86_64 emulator"). The console is arch-derived from `arch_traits`, so this pins that
an x86_64 guest keeps `ttyS0` regardless of accel or (implied) host.

**Deadline (new).** Extend `tests/providers/local_libvirt/test_deadlines.py` to assert that an
x86_64 guest with a persisted `accel="tcg"` scales by the configured multiplier (symmetry with
the ppc64le-under-TCG case) — proving the deadline path keys off accel, not arch.

### Static confinement guard (encodes AC#2)

A new `tests/domain/platform/test_host_arch_confinement.py`, running in the ordinary `just test`
suite (non-gated), **AST-walks** every `src/kdive/**/*.py` and collects the modules that
contain a host-arch read — an `Attribute` access `platform.machine` or a call to `os.uname`.
It asserts the set of such modules is a subset of an allowlist:

```
_HOST_ARCH_READ_ALLOWLIST = frozenset({
    "kdive/diagnostics/guest_arch_accel.py",                     # per-arch accel doctor probe (ADR-0352)
    "kdive/diagnostics/multiarch_gdb.py",                        # cross-arch gdb doctor probe (ADR-0347)
    "kdive/providers/shared/debug_common/gdbmi/core/engine.py",  # cross-arch gdb binary (ADR-0347)
})
```

AST — not a text grep — so a docstring mention (`diagnostics/provider_checks.py:409` documents
`platform.machine()` in prose) or a comment is not a false positive; only a genuine attribute
read or call counts. On failure the message names the offending module and points at ADR-0354,
so a future author who adds a host-arch read either lands in an accel/gdb-selection module (and
extends the allowlist with a rationale in the same change) or learns the invariant they are
about to break.

The guard is a **subset** assertion (`modules ⊆ allowlist`), not equality: a refactor that
*removes* a read from an allowlisted module must not fail the guard. An allowlist entry that no
longer reads the host is harmless (over-permissive by one line) and is caught by ordinary code
review, not this test — the test's job is to catch *new* reads leaking into guest-facing paths.

### Detection shape

The AST walk visits every module and records a hit when it sees either:
- `platform.machine` as an `ast.Attribute` (matches `platform.machine()` and the bare
  `platform.machine` default-argument reference in `engine.py`), or
- `os.uname` as an `ast.Attribute`.

It keys hits by repo-relative path (`kdive/...`) for a stable, readable allowlist. It does not
resolve aliased imports (`from platform import machine`) — none exist in the tree and the guard
would need an import-alias pass to catch them; that is a documented limitation, and a companion
assertion pins that the three known reads *are* detected (so the walker cannot silently regress
to matching nothing and passing vacuously).

## Acceptance criteria

- **AC1.** The inverted-matrix admission test provisions an x86_64 profile against a Resource
  advertising `{ppc64le: kvm, x86_64: tcg}` and asserts the System records `accel == "tcg"`.
  Verifiable by running the test; it fails if admission ever derived accel from host arch.
- **AC2.** The domain-XML test asserts an x86_64 + `accel="tcg"` domain renders `type="qemu"`,
  `machine="q35"`, `console=ttyS0` in the cmdline, `<emulator>` = the x86_64 emulator path, and
  no `<cpu>` element. Verifiable by running the test.
- **AC3.** The deadline test asserts an x86_64 guest with persisted `accel="tcg"` scales by the
  configured multiplier (not `1.0`). Verifiable by running the test.
- **AC4.** `test_host_arch_confinement.py` runs in the ordinary `just test` suite and passes on
  the current tree: the set of modules reading `platform.machine`/`os.uname` equals the
  three-module allowlist. Verifiable by running `just test`.
- **AC5.** The guard fails when a host-arch read is introduced outside the allowlist. Verifiable
  by temporarily adding `platform.machine()` to a guest-facing module (e.g. `lifecycle/xml.py`)
  and observing the test fail with a message naming that module.
- **AC6.** The guard's walker is proven non-vacuous: a companion assertion confirms the three
  known allowlisted reads are actually detected (the walker matches something, not nothing).
- **AC7.** ADR-0354 records the invariant, the allowlist with a per-entry rationale, the
  audit's finding of zero required production changes, and that live POWER proof is #1157.
- **AC8.** No production source file changes (test + docs + ADR only). Verifiable by
  `git diff --name-only <base>` showing only `tests/`, `docs/`, and the ADR index.

## Non-goals

- Live proof of an x86_64 guest booting under TCG on real POWER hardware (epic #1157).
- Changing any production code path — the audit found none reads the host arch for guest
  behavior; introducing an abstraction "to be safe" would be premature.
- An import-alias-resolving AST pass in the guard (no aliased host-arch imports exist; the
  non-vacuity assertion guards against a silently-broken walker).
- Auditing remote-libvirt or fault-inject arch handling (remote-libvirt is a separate provider
  epic per the parent design's Out-of-scope; fault-inject advertises no guest arches).

## Known unverified

- On real POWER hardware an x86_64 guest under TCG would exercise the *identical* renderer/
  admission/deadline code these unit tests drive, but the end-to-end provision→boot has never
  run there. #1157 (POWER10 bring-up) is the live falsification gate; this issue proves the
  data flow, not the hardware.
- The confinement guard detects only `platform.machine` / `os.uname` attribute reads. A future
  host-arch signal read through a different API (e.g. reading `/proc/cpuinfo`, a new
  `os.*` call) would not be caught until the allowlist's detection set is extended. The ADR
  notes the guard covers the known host-arch APIs and must grow with any new one.
