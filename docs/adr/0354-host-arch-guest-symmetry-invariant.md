# ADR-0354: Host-arch/guest symmetry invariant + confinement guard

Status: Accepted

Issue: #1155 · Epic: #1139 (full ppc64le support) · Supersedes: none · Depends on: ADR-0339
(admission arch-validate + persist accel), ADR-0340 (accel-derived domain XML), ADR-0347
(cross-arch gdb binary selection), ADR-0352 (per-arch guest-accel diagnostics)

## Context

The epic's goal is symmetric virtualization: the local-libvirt provider runs a foreign-arch
guest under TCG on *either* host — a ppc64le guest under TCG on x86_64, an x86_64 guest under
TCG on POWER. The auto-discovery design (ADR-0338/0339/0340) makes the inverse case fall out
structurally: guest-facing behavior is derived from `profile.arch` (a lookup in
`domain/platform/arch_traits.py`) plus the accelerator libvirt advertises for that guest arch
(`<domain type='kvm'>` in the capabilities XML), never from the host's own architecture.

But nothing *verified* that. Every arch-parameterized test runs on the x86_64 validation host,
so a latent `host == x86_64` assumption in a guest-facing path (discovery, admission, domain
XML, deadline, diagnostics) would stay invisible until POWER10 bring-up (#1157). #1155 audits
those seams and locks the invariant against regression.

The audit finding: **no guest-facing path reads the host arch.** The host arch
(`platform.machine()`) is read in exactly three production sites, all of which select a
host-side *binary* or *tooling capability*, never a guest-facing domain fact:

1. `diagnostics/guest_arch_accel.py` — the per-arch guest-accelerator doctor probe (ADR-0352).
2. `diagnostics/multiarch_gdb.py` — the cross-arch gdb doctor probe (ADR-0347).
3. `providers/shared/debug_common/gdbmi/core/engine.py` — the gdb-engine's cross-arch binary
   selection (ADR-0347; the *guest* arch there comes from the staged `vmlinux` ELF header).

No production code needed a fix; the deliverable is verification.

## Decision

Record and enforce the invariant:

> **No code path derives guest-facing behavior from the host architecture, except accelerator
> and host-side-tooling selection.**

Enforcement has two layers:

1. **Inverted-matrix behavioral tests** (unit level). A ppc64le host advertising
   `{ppc64le: kvm, x86_64: tcg}` is fed through the guest-facing seams and its output asserted:
   admission records `accel=tcg` for an x86_64 guest; the domain renders q35 + `ttyS0` +
   `type=qemu` + the x86_64 `<emulator>` with no `<cpu>`; the boot deadline scales. Because the
   renderer takes `(profile, accel, emulator)` and never reads the host, these unit tests drive
   the *identical* code path a real POWER host would — the proof is faithful, not a proxy.
   Discovery's inverted matrix is already asserted by
   `tests/providers/test_libvirt_xml.py::test_parse_guest_arches_synthetic_kvm_hv_ppc_host_is_kvm`.

2. **A static confinement guard** (`tests/domain/platform/test_host_arch_confinement.py`,
   non-gated, runs in `just test`). It AST-walks `src/kdive` for host-arch reads
   (`platform.machine` attribute access, `os.uname` calls) and asserts the set of modules doing
   so is a subset of the three-module allowlist above. AST — not a text grep — so a docstring or
   comment mention is not a false positive. A future module that newly reads the host arch fails
   the guard with a message naming it and pointing here; the author either lands in an
   accel/tooling-selection module (extending the allowlist with a rationale) or learns the
   invariant they are breaking. A companion non-vacuity assertion pins that the three known
   reads *are* detected, so the walker cannot silently regress to matching nothing.

The guard asserts `modules ⊆ allowlist` (subset, not equality): removing a read from an
allowlisted module must not fail it. Live proof of the inverted matrix on real POWER hardware
is deferred to #1157.

## Consequences

- The symmetry invariant is now executable, not just asserted in prose: any new guest-facing
  host-arch read fails CI at the source.
- The allowlist is a small, reviewed enumeration of the legitimate accelerator/tooling-selection
  sites. Adding an arch or a new host-side tool means a deliberate allowlist edit with a
  rationale, not a silent leak.
- No production behavior changes; no migration (test + docs + ADR only).
- The guard covers the known host-arch APIs (`platform.machine`, `os.uname`). A future host-arch
  signal read through a different API (`/proc/cpuinfo`, another `os.*` call, an aliased import)
  is not caught until the guard's detection set grows — a documented limitation, noted in the
  spec's Known-unverified.

## Considered & rejected

- **Behavioral inverted-matrix tests only, no static guard.** Proves symmetry for every current
  guest-facing seam, but cannot prevent a *future* new module from reading the host arch — the
  exact regression the issue exists to prevent. Rejected: AC#2 is a claim over "no code path,"
  which a fixed set of behavioral tests cannot enforce against unwritten code.
- **A text-grep guard instead of AST.** Simpler, but flags the `provider_checks.py` docstring
  that documents `platform.machine()` in prose, and any comment — false positives that would
  force noisy `# noqa`-style suppressions or an inline-comment allowlist. Rejected: AST cleanly
  distinguishes a real read from a mention.
- **Equality guard (`modules == allowlist`).** Would fail when a refactor removes the last
  host-arch read from an allowlisted module — punishing the *safe* direction. Rejected in favor
  of subset.
- **An import-alias-resolving AST pass.** No aliased host-arch imports exist in the tree; adding
  a whole-program alias-resolution pass to catch a hypothetical is premature. Rejected; the
  non-vacuity assertion guards against a silently-broken walker instead.
- **Introducing a host-arch abstraction "to be safe."** The audit found nothing reads the host
  arch for guest behavior, so wrapping a non-existent dependency is premature abstraction.
  Rejected.
- **A live x86_64-guest-under-TCG proof in this issue.** The x86_64 host runs x86_64 guests
  under KVM, not TCG, so the live inverted matrix needs POWER hardware. Rejected here; it is
  epic issue #1157.
