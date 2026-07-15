# Per-arch diagnostics, dep-checker probes, and cross-arch install docs (#1153)

Date: 2026-07-14
Status: approved (design)
Issue: #1153
Epic: #1139 (full ppc64le support), sub-issue 14
Depends on: #1148 (closed)
ADR: [0352](../adr/0352-per-arch-guest-accel-diagnostics.md)

## Goal

Make "which qemu emulators are present, and what accelerator does each guest arch
get" an operator-facing answer, in all three surfaces an operator consults:

- the service `doctor` (post-deploy diagnostics),
- `scripts/check-local-libvirt.sh` (pre-deploy host preflight),
- `scripts/check-setup-deps.sh` (the dep-checker),

plus the cross-arch install/runbook docs that name the exact foreign-arch qemu
package per distro.

The capability itself already exists: discovery advertises `guest_arches` (#1140,
ADR-0338), admission validates against it (#1141), the domain XML derives its accel
(#1142), and the TCG deadline multiplier scales boot windows (#1143). This issue does
not add scheduling capability — it **surfaces the already-discovered accel facts** to
operators so a TCG-only guest arch is legible rather than a silent surprise at boot.

## Non-goals

- No new scheduling capability, admission behavior, or provider wiring.
- No libvirt/DB call in the new diagnostic check — it probes the worker host directly
  (PATH + `/dev/kvm`), matching `multiarch_gdb` (ADR-0347) and `pseries_fadump`
  (ADR-0349), so a stale inventory cannot mask a real host change (ADR-0091: a check
  observes the host, not kdive's recorded state).
- No CI runners for ppc64le, no big-endian ppc64 (epic out-of-scope).

## Background facts (already in tree)

- `arch_traits.SUPPORTED_ARCHES = {x86_64, ppc64le}` — the arches kdive can provision.
- qemu system-emulator binary per arch, **asymmetric** and not `uname -m`:
  `x86_64 → qemu-system-x86_64`, `ppc64le → qemu-system-ppc64` (POWER has no
  `-ppc64le` binary). Encoded today in `check-setup-deps.sh:qemu_system_binary()`
  (host-arch only).
- Accelerator rule (mirrors discovery, ADR-0338/0340): a guest arch runs under **KVM**
  when it is the host's native arch **and** `/dev/kvm` is usable; otherwise **TCG**.
- `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default `10.0`) scales boot-readiness
  deadlines for TCG guests; `KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S` (default 1800s)
  is the native base for the customization boot, TCG-scaled by the same multiplier.
- `CheckResult.data: Mapping[str,str]` is serialized verbatim into the `doctor --json`
  verdict (`mcp/tools/ops/diagnostics.py:204`).

## Design

### A. `doctor` check: `guest_arch_accel` (worker-vantage, local-libvirt)

A new stable check id `guest_arch_accel` reporting, per **schedulable** guest arch
(its qemu emulator is present on the worker's PATH), whether it runs under KVM or is
TCG-only. Modeled on `pseries_fadump` / `multiarch_gdb`:

- **Probe** `default_guest_arch_accel_probe(*, host_arch, supported, which, kvm_usable)`:
  for each `arch` in `sorted(supported)`, resolve its qemu binary
  (`qemu_system_binary(arch)`); if `which(binary)` is present, record
  `accel = "kvm"` iff `arch == host_arch and kvm_usable()` else `"tcg"`. Injected
  seams (`host_arch=platform.machine()`, `supported=SUPPORTED_ARCHES`,
  `which=shutil.which`, `kvm_usable` = `/dev/kvm` R+W probe) so it is unit-tested with
  no real host. Returns an ordered `Mapping[str,str]` of `{arch: accel}` for present
  emulators.
- **Check** `GuestArchAccelCheck` maps the probe to a **PASS** `CheckResult` whose
  `data` is the accel map and whose `detail` names the distinction, e.g.
  `"schedulable guest arches: x86_64 (KVM native), ppc64le (TCG-only)"`. With **no**
  emulator present the detail is `"no qemu system emulator found on PATH; no guest
  arch is schedulable on this host"` and `data={}` — still PASS: presence of the
  native emulator is already gated by `check-local-libvirt.sh` and the dep-checker
  future tier; this check's single responsibility is to *distinguish accel*, not to
  re-gate emulator presence (see ADR-0352 rejected alternatives). The framework's
  `run_check` maps any leaked probe exception to ERROR, so no explicit error branch is
  authored.
- **Wiring**: added to the single local-libvirt contribution in
  `diagnostics/multiarch_gdb.py` (`_worker_checks` + `_unavailable_worker_checks`), so
  it rides the one local dispatcher alongside the other two worker checks — no second
  provider contribution.

The **accel map lives in one place** for the Python side: a `qemu_system_binary(arch)`
helper co-located with the check module (mirrors how `multiarch_gdb` keeps its gdb
binary selection near the tool, not in `arch_traits`), covering both supported arches
with the asymmetric ppc64 name.

Acceptance-1 (doctor distinguishes native-KVM vs TCG-only) is met by the `detail` +
`data` on this PASS verdict, checkable in `doctor --json`.

### B. `scripts/check-local-libvirt.sh` — per-arch qemu probe + TCG advisory

- **Fix the x86 hardcode**: the required-command loop hardcodes `qemu-system-x86_64`.
  Replace with the **host-native** qemu binary (arch-derived), so a POWER host is not
  failed for lacking the x86 emulator and *is* failed for lacking `qemu-system-ppc64`.
- **Foreign-arch advisory**: for each supported arch that is not the host arch, if its
  qemu binary is present, print an informational line (the script's `OK:`/info
  vocabulary, not `note_fail`/`note_warn` — an available TCG arch is not a defect):
  `guest arch <X> available via TCG only (foreign emulator <binary> present; ~Nx
  slower — KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER)`. Absent foreign qemu → no line
  (cross-arch is optional; the dep-checker/install.md say how to enable it).

### C. `scripts/check-setup-deps.sh` — per-arch qemu probes + advisory

- Keep the **host-native** qemu in the FUTURE tier unchanged (missing → install hint).
- Generalize `qemu_system_binary()` → `qemu_binary_for_arch(arch)` and add a
  `SUPPORTED_ARCHES` list + host-arch detection. After the tiers, emit a **cross-arch
  advisory** block: for each foreign supported arch,
  - foreign qemu **present** → `guest arch <X>: available via TCG only (<binary>)`;
  - foreign qemu **absent** → `guest arch <X>: not available; install <pkg> for TCG
    guests` where `<pkg>` is `package_for qemu-system-<...> <distro>` (names the exact
    per-distro package).
- Output must differ observably on foreign-qemu presence and on host arch, so
  Acceptance-2 (tests cover both host arches, with/without the foreign qemu package)
  is checkable via `uname`/PATH stubs (the existing test harness pattern).

### D. Docs — name the exact packages per distro

- `docs/operating/install.md`: a "Cross-architecture guests" subsection — the
  foreign-arch qemu package per supported distro (Fedora `qemu-system-ppc`, Debian
  `qemu-system-ppc`, Arch via `qemu-system-ppc`, openSUSE `qemu-ppc`; and the x86
  siblings for a POWER host), the TCG performance expectation, and the
  `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` deadline-multiplier setting.
- `docs/operating/runbooks/image-lifecycle.md`: the cross-arch customization-boot
  story — a foreign-arch image customizes by booting once under TCG (ADR-0345),
  scaled by `KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S × multiplier`, so it is slower
  than a native customize; name the same package prerequisite.

Package names are cross-checked against `check-setup-deps.sh:package_for` so the docs
and the dep-checker cannot disagree.

## Success criteria (falsifiable)

1. `doctor --json` on a host with both emulators yields a `guest_arch_accel` row,
   `status=pass`, with `data` mapping the native arch to `kvm` and the foreign arch to
   `tcg`, and a `detail` naming both. On a host missing `/dev/kvm`, the native arch
   maps to `tcg`. (unit + a `test_provider_checks`-style assertion)
2. `check-setup-deps.sh` under a stubbed `uname -m=ppc64le` names `qemu-system-ppc`
   for the native tier and, with no `qemu-system-x86_64` on PATH, advises the x86
   package for cross-arch; under `uname -m=x86_64` it symmetrically advises
   `qemu-system-ppc`. With the foreign qemu **present** the advisory reads "available
   via TCG only", not the install hint. Both host arches × {foreign present, absent}
   are covered.
3. `check-local-libvirt.sh` on a ppc64le host does not fail for a missing
   `qemu-system-x86_64` and does fail for a missing `qemu-system-ppc64`; with a foreign
   qemu present it prints the "available via TCG only" line. (shell test via PATH +
   `uname` stubs)
4. `docs/operating/install.md` and `image-lifecycle.md` name the exact foreign-arch
   qemu package for each supported distro, matching `package_for`.

## Edge cases

- **No emulator at all**: `guest_arch_accel` PASSes with `data={}` and an explicit
  "nothing schedulable" detail; emulator-presence gating stays with the preflight/dep
  scripts (no double-fail).
- **Host arch not in SUPPORTED_ARCHES** (a future/unknown host): the probe records only
  the emulators it finds among `supported`; the host's own arch simply won't appear as
  KVM. No crash, no silent x86 assumption.
- **`/dev/kvm` present but not writable** (permissions): `kvm_usable()` is R+W, so the
  native arch degrades to `tcg` in the map — matching the real accel the domain XML
  would pick.
- **qemu binary asymmetry**: `qemu_binary_for_arch(ppc64le)` must be `qemu-system-ppc64`
  in both shell and Python; a regression test pins the mapping.

## Rollback

Pure additive: a new check id, two shell advisory blocks, and doc sections. Reverting
the commits removes the surfaces with no schema/migration/state impact.
