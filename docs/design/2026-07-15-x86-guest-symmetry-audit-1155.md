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

**Admission (new — inverted-key defense-in-depth).** Extend
`tests/integration/test_systems_admission_arch.py` with an inverted host constant
`_PPC_HOST_GUEST_ARCHES = {"ppc64le": {kvm…}, "x86_64": {tcg…}}` and a test that provisions the
default (x86_64) profile against a Resource carrying it, asserting the System records
**`accel == "tcg"`**. `accel == "tcg"` at admission is *already* asserted — but only for a
ppc64le guest against an x86_64 host (the fadump tests, `test_provision_admits_fadump_when_host_supports_it`).
Both cases flow through the same arch-agnostic dict lookup in `resolve_accel_emulator`
(`entry["accel"]`, zero arch special-casing), so this test's marginal coverage is
defense-in-depth against a *future* x86-specific special-case in the resolution path, not a
currently-exercised hole. It is cheap (reuses `_set_resource_guest_arches` and the real-DB
harness) and completes the inverted matrix symmetrically, which is the point of the suite.

**Domain XML (documents the invariant on an existing matrix cell).** The
`test_render_domain_by_arch_and_accel` parametrization in
`tests/providers/local_libvirt/test_provisioning.py` already includes the
`("x86_64", "tcg", "/usr/bin/qemu-system-x86_64", "qemu", "q35", None, True)` cell — asserting
domain type, machine, `<cpu>` absence, emulator, and features. It does not assert the console
device *in that cell*, though `console=ttyS0` for x86_64 is already covered elsewhere
(`_X86_KVM_GOLDEN`, and the cmdline assertion at `test_provisioning.py:366`). The console is
arch-derived (`_baseline_cmdline(traits.console_device)`, an identical code path for kvm and
tcg), so adding a `console=ttyS0` assertion to the tcg cell exercises no distinct branch — it
**documents** the "q35 + ttyS0 + type=qemu + qemu-system-x86_64" matrix the issue names in one
place, rather than closing an untested path. Add it for that completeness; do not bill it as a
coverage gap.

**Deadline (structural — no new test).** The deadline dimension of the inverted matrix holds by
construction and is already covered: `tcg_deadline_multiplier(accel: str | None)` and its sole
caller `LocalLibvirtInstall.boot(system_id, *, accel)` take **only** the accelerator — arch is
not an input at any level of the deadline path. "An x86_64 guest with `accel=tcg` scales"
therefore *is* `tcg_deadline_multiplier("tcg")`, which `test_tcg_uses_configured_multiplier`
(`test_deadlines.py`) already asserts. A new "x86_64 deadline" test would be a byte-for-byte
duplicate whose arch-independence claim is unfalsifiable (arch cannot be varied). So the spec
adds no deadline test; instead the ADR records that deadline scaling is arch-free by
construction as one reason symmetry holds, and this spec cites the existing multiplier test as
the matrix's deadline coverage.

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

The whole-tree assertion is a **subset** (`modules ⊆ allowlist`), not equality: a refactor that
*removes* a read from an allowlisted module must not fail the guard. An allowlist entry that no
longer reads the host is harmless (over-permissive by one line) and is caught by ordinary code
review, not this test — the test's job is to catch *new* reads leaking into guest-facing paths.

### Detection shape — a pure function, unit-tested by fixture (not by pinning live reads)

The detection logic is a **pure function** `host_arch_reading_modules(tree_root) -> set[str]`
(or, at the finest grain, `module_reads_host_arch(source: str) -> bool`) that AST-walks source
and records a hit when it sees either:
- `platform.machine` as an `ast.Attribute` (matches `platform.machine()` and the bare
  `platform.machine` default-argument reference in `engine.py`), or
- `os.uname` as an `ast.Attribute`.

Factoring detection as a pure function lets the walker itself be **unit-tested with synthetic
source fixtures**, which is what proves both non-vacuity and the AST-vs-grep discrimination
ADR-0354 rests on — *without* coupling to which modules happen to read the host today:

- **Positive fixture:** a synthetic source string containing `platform.machine()` (and one with
  `os.uname()`) is reported. This proves the walker matches *something* — the non-vacuity
  guarantee — independently of the live tree, so it does not re-introduce equality-guard
  brittleness (removing a read from an allowlisted module does not fail a fixture test).
- **Negative fixture:** a synthetic source whose only occurrence of `platform.machine()` is
  inside a docstring/comment is **not** reported. This locks the reason AST was chosen over
  grep, mirroring the real `provider_checks.py:409` docstring the whole-tree scan must ignore.

The whole-tree guard then just applies this proven function over `src/kdive/**/*.py`, keys hits
by repo-relative path (`kdive/...`) for a stable, readable allowlist, and asserts the subset.
Because non-vacuity is proven by the positive fixture, the whole-tree assertion never needs to
pin the three specific live reads — preserving the subset tolerance the design committed to.

The function does not resolve aliased imports (`from platform import machine`) — none exist in
the tree and catching them would need an import-alias pass; that is a documented limitation
(the negative/positive fixtures guard the walker's *logic*, not import-alias coverage).

## Acceptance criteria

- **AC1.** The inverted-matrix admission test provisions an x86_64 profile against a Resource
  advertising `{ppc64le: kvm, x86_64: tcg}` and asserts the System records `accel == "tcg"`.
  Verifiable by running the test; it fails if the arch-agnostic resolution ever grows an
  x86-specific special-case.
- **AC2.** The domain-XML `x86_64 + accel="tcg"` matrix cell asserts `type="qemu"`,
  `machine="q35"`, `console=ttyS0` in the cmdline, `<emulator>` = the x86_64 emulator path, and
  no `<cpu>` element — documenting the full inverted-matrix render in one place. Verifiable by
  running the test.
- **AC3.** The deadline dimension is covered structurally: the spec adds no new deadline test
  (the deadline path takes only `accel`, so an "x86_64 deadline" test would duplicate
  `test_tcg_uses_configured_multiplier`). Verifiable by confirming `test_deadlines.py` already
  asserts `tcg_deadline_multiplier("tcg")` scales, and that the ADR records deadline
  arch-independence.
- **AC4.** `test_host_arch_confinement.py` runs in the ordinary `just test` suite and passes on
  the current tree: the set of modules reading `platform.machine`/`os.uname` is a subset of the
  three-module allowlist. Verifiable by running `just test`.
- **AC5.** The detection function is unit-tested with synthetic fixtures: a source with an
  out-of-allowlist `platform.machine()` / `os.uname()` read is reported, and a source whose only
  occurrence is in a docstring/comment is **not** reported (the AST-vs-grep discrimination).
  This is a permanent shipped test, not a manual edit. Verifiable by running the test.
- **AC6.** Non-vacuity is proven by AC5's positive fixture (the walker matches a synthetic read),
  **not** by pinning the three live reads — so removing a read from an allowlisted module never
  fails the guard. Verifiable by reading the test: no assertion references the specific
  allowlisted modules' current reads.
- **AC7.** ADR-0354 records the invariant, the allowlist with a per-entry rationale, that the
  deadline path is arch-free by construction, the audit's finding of zero required production
  changes, and that live POWER proof is #1157.
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
