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
  when it is the host's native arch **and** the host has KVM; otherwise **TCG**. The
  authoritative per-provision accel is what libvirt reports in its capabilities
  (`parse_guest_arches` reads `<domain type='kvm'>`), persisted on the System at
  provision time. The host-KVM signal libvirt itself gates that on is the **presence**
  of `/dev/kvm` — a stat that succeeds regardless of the *worker* process's uid, so a
  presence probe does not diverge from libvirt for a non-root worker under
  `qemu:///system` (the way a worker-uid `/dev/kvm` R+W probe would).
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

- **Probe** `default_guest_arch_accel_probe(*, host_arch, supported, which, kvm_present)`:
  for each `arch` in `sorted(supported)`, resolve its qemu binary
  (`qemu_system_binary(arch)`); if `which(binary)` is present, record
  `accel = "kvm"` iff `arch == host_arch and kvm_present()` else `"tcg"`. Injected
  seams (`host_arch=platform.machine()`, `supported=SUPPORTED_ARCHES`,
  `which=shutil.which`, `kvm_present` = `/dev/kvm` **presence** probe —
  `os.path.exists`, *not* a worker-uid R+W test, per the accel rule above) so it is
  unit-tested with no real host. Returns the accel map plus whether the host's own
  native arch is schedulable here (its emulator present).
- **Check** `GuestArchAccelCheck` maps the probe result to a `CheckResult`:
  - **FAIL** when the host's own native arch is a supported arch **and** its emulator
    is absent — the host cannot schedule even its native guests, which `doctor` (the
    only post-deploy surface) must surface. `fix` names the exact native qemu package;
    `failure_category=MISSING_DEPENDENCY`. This is the one schedulability floor the
    check gates.
  - **PASS** otherwise, `data` = the accel map, `detail` naming the distinction, e.g.
    `"schedulable guest arches: x86_64 (KVM native), ppc64le (TCG-only)"`. A host whose
    own arch is **not** a supported arch (kdive cannot provision natively there anyway)
    also PASSes, reporting only whatever foreign emulators are present — no native
    expectation, no crash, no silent x86 assumption.
  - **ERROR** only if the probe itself leaks an exception (the framework's `run_check`
    maps it), so no explicit error branch is authored.

  The accel map is **data** (the distinction Acceptance-1 requires); the native-arch
  floor is the **pass/fail** — one coherent check: "which guest arches are schedulable
  and at what accel, and can the host schedule its own native arch."
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
- **Unsupported host arch**: when `uname -m` is not in the supported set (e.g.
  `aarch64`), the script does **not** require any native qemu (kdive cannot provision
  natively there) and prints one explicit line — `host arch <X> is not a supported
  kdive provisioning arch (supported: ppc64le, x86_64)` — rather than falling back to
  the x86 emulator. It still lists any supported-arch emulators present as TCG-only.

### C. `scripts/check-setup-deps.sh` — per-arch qemu probes + advisory

- Keep the **host-native** qemu in the FUTURE tier unchanged (missing → install hint).
- Generalize `qemu_system_binary()` → `qemu_binary_for_arch(arch)` and add a
  `SUPPORTED_ARCHES` list + host-arch detection. After the tiers, emit a **cross-arch
  advisory** block: for each foreign supported arch,
  - foreign qemu **present** → `guest arch <X>: available via TCG only (<binary>)`;
  - foreign qemu **absent** → `guest arch <X>: not available; install <pkg> for TCG
    guests` where `<pkg>` is `package_for qemu-system-<...> <distro>` (names the exact
    per-distro package).
- **Unsupported host arch**: when `uname -m` is not in `SUPPORTED_ARCHES`, skip the
  native-qemu future-tier requirement and the cross-arch advisory, emitting one
  explicit `host arch <X> is not a supported kdive provisioning arch (supported:
  ppc64le, x86_64)` line — never the x86 fallback. This mirrors the Python probe's
  "record only what is found, no native expectation" rule.
- Output must differ observably on foreign-qemu presence and on host arch, so
  Acceptance-2 (tests cover both host arches, with/without the foreign qemu package)
  is checkable via `uname`/PATH stubs (the existing test harness pattern). A third
  test stubs an unsupported `uname -m` and asserts the no-fallback line.

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

1. `doctor --json` on a host with both emulators and `/dev/kvm` present yields a
   `guest_arch_accel` row, `status=pass`, with `data` mapping the native arch to `kvm`
   and the foreign arch to `tcg`, and a `detail` naming both. On a host with `/dev/kvm`
   **absent**, the native arch maps to `tcg`. On a host missing its **native** arch's
   emulator, the row is `status=fail` with a fix naming the native qemu package. (unit
   + a `test_provider_checks`-style assertion)
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

- **No native emulator**: if the host's own (supported) arch has no emulator,
  `guest_arch_accel` **FAILs** with a fix — the one post-deploy schedulability floor
  doctor surfaces (a provision would otherwise fail only at use).
- **Host arch not in SUPPORTED_ARCHES** (a future/unknown host, e.g. aarch64): the
  probe records only the emulators it finds among `supported`; no native FAIL (kdive
  cannot provision natively there), no crash, no silent x86 assumption. PASS reporting
  whatever foreign emulators exist. The shell scripts mirror this with an explicit
  "not a supported provisioning arch" line.
- **`/dev/kvm` present but not readable/writable by the worker uid** (a non-root worker
  under `qemu:///system`): the presence probe still sees the node, so the native arch
  stays `kvm` — matching libvirt, which runs the domain as the qemu uid and gates
  `<domain type='kvm'>` on `/dev/kvm` presence, not on the worker's access. A worker-uid
  R+W probe would falsely report `tcg` here; the presence probe deliberately does not.
- **qemu binary asymmetry**: `qemu_binary_for_arch(ppc64le)` must be `qemu-system-ppc64`
  in both shell and Python; a regression test pins the mapping.

## Rollback

Pure additive: a new check id, two shell advisory blocks, and doc sections. Reverting
the commits removes the surfaces with no schema/migration/state impact.
