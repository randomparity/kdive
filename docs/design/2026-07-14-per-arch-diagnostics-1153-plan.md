# Implementation plan — per-arch diagnostics, dep-checker probes, cross-arch docs (#1153)

Derived from `2026-07-14-per-arch-diagnostics-1153.md` (approved) and
[ADR-0352](../adr/0352-per-arch-guest-accel-diagnostics.md).

- **Branch:** `feat/per-arch-diagnostics-1153` (off `origin/main`).
- **Guardrails (run before every commit):** `just lint`, `just type`, targeted
  `uv run python -m pytest <files> -q`; `just lint-shell` for the shell edits; `just
  docs-links` for the docs; the full `just ci` before push.
- **No** migration, schema, or new dependency. Pure additive surfaces.
- Tasks are independent enough to land as separate commits; suggested order 1→4.
  Tasks 2 and 3 (shell) and Task 4 (docs) do not depend on Task 1 (Python) and may be
  built in any order, but the docs (Task 4) must name packages consistent with the
  dep-checker (`package_for`), so review Task 2's mapping before finalizing Task 4.

---

## Task 1 — `guest_arch_accel` doctor check (worker-vantage, local-libvirt)

**Where it fits:** Acceptance-1 — "doctor output distinguishes native-KVM vs TCG-only
guest arches." A new worker-vantage check on the single local-libvirt diagnostic
contribution, modeled on `multiarch_gdb`/`pseries_fadump`.

**Files:**
- `src/kdive/diagnostics/checks.py` — add `GUEST_ARCH_ACCEL_ID = "guest_arch_accel"`.
- `src/kdive/diagnostics/provider_checks.py` — add `GuestArchAccelReport` (frozen
  dataclass), `GuestArchAccelProbe` type alias, `GuestArchAccelCheck`, and the fix
  constant.
- `src/kdive/diagnostics/guest_arch_accel.py` (new) — `qemu_system_binary(arch)`, the
  URI-selected KVM probe, `default_guest_arch_accel_probe(...)`, and the
  contribution-wiring helpers.
- `src/kdive/diagnostics/multiarch_gdb.py` — add `GuestArchAccelCheck` to
  `_worker_checks()` and its descriptor to `_unavailable_worker_checks()` (the one local
  contribution carries every local worker check).
- `tests/diagnostics/test_guest_arch_accel.py` (new).

**Design details (from spec §A):**
- `qemu_system_binary(arch)`: `{"x86_64": "qemu-system-x86_64", "ppc64le":
  "qemu-system-ppc64"}` — the asymmetric ppc64 name (POWER has no `-ppc64le`). Raise or
  return `None` for an unknown arch (only called over `SUPPORTED_ARCHES`).
- `GuestArchAccelReport`: `accel_by_arch: Mapping[str,str]` (arch-sorted, only arches
  whose emulator is present, values `"kvm"`/`"tcg"`), `native_arch: str`,
  `native_supported: bool`, `native_emulator_present: bool`, `native_qemu_binary: str |
  None`.
- KVM probe is **URI-selected** (spec §Background):
  `qemu:///session` → `lambda: os.access("/dev/kvm", os.R_OK | os.W_OK)`;
  any other URI (incl. default `qemu:///system`) → `lambda: os.path.exists("/dev/kvm")`.
- `default_guest_arch_accel_probe(*, host_arch=platform.machine(),
  supported=SUPPORTED_ARCHES, which=shutil.which, kvm_present=None, uri=None)`: when
  `kvm_present` is None, resolve `uri` (default from `kdive.config.get(LIBVIRT_URI)`) and
  build the URI-selected probe; loop `sorted(supported)`, record accel per present
  emulator; assemble the report. All seams injectable for tests.
- `GuestArchAccelCheck.run()`:
  - **FAIL** iff `native_supported and not native_emulator_present`; `fix` =
    `f"{native_qemu_binary} not found on PATH; install it via your distribution package
    manager (see scripts/check-setup-deps.sh for per-distro hints)"`,
    `failure_category=MISSING_DEPENDENCY`, `data=accel_by_arch`.
  - **PASS** otherwise; `data=accel_by_arch`; `detail` lists each present arch as
    `"<arch> (KVM native)"` / `"<arch> (TCG-only)"`, and when the **native** arch is
    present-but-`tcg`, prefixes `"native arch <X> is TCG-only (host KVM unavailable); "`.
    Empty map → `"no qemu system emulator found on PATH; no guest arch is schedulable
    here"`.
  - No explicit ERROR branch (framework `run_check` maps a leaked exception).

**Acceptance (reviewer-checkable):**
- Probe with `which` stub for both binaries + `kvm_present=lambda: True`, `host_arch=
  "x86_64"` → `accel_by_arch == {"ppc64le": "tcg", "x86_64": "kvm"}`.
- Same but `kvm_present=lambda: False` → native `x86_64` maps to `"tcg"`; check PASSes,
  detail contains "native arch x86_64 is TCG-only".
- `which` returns None for the native binary, `host_arch="ppc64le"` → check FAILs, fix
  contains `qemu-system-ppc64`.
- `host_arch="aarch64"` (unsupported), only `qemu-system-x86_64` present → PASS, data
  `{"x86_64": "tcg"}`, no FAIL.
- URI selection: `uri="qemu:///session"` builds an `os.access`-based probe;
  `qemu:///system` builds `os.path.exists` — assert by injecting a fake `os` seam or by
  constructing the probe and checking behavior against a stubbed node.
- `test_guest_arch_accel_is_in_the_single_local_contribution` and
  `test_registered_in_assembly` mirror the `pseries_fadump` tests
  (`tests/diagnostics/test_pseries_fadump.py:79-93`): exactly one local-libvirt
  contribution, `guest_arch_accel` in its unavailable-worker descriptors.

**Conventions:** Google docstrings; ADR-0352 cite in module docstring; the Check class
sits beside `MultiarchGdbCheck`/`PseriesFadumpCheck` in `provider_checks.py`; the probe
+ wiring beside `pseries_fadump.py`. Cyclomatic ≤8, ≤100 lines/func, 100-char lines.

**Guardrails:** `just lint`, `just type`, `uv run python -m pytest
tests/diagnostics/test_guest_arch_accel.py tests/diagnostics/test_pseries_fadump.py
tests/diagnostics/test_multiarch_gdb.py -q`, then a doctor integration smoke if one
exists (`rg -l guest_arch_accel tests/integration`).

**Rollback:** delete the new module + test, revert the three edited files; no state.

---

## Task 2 — `check-setup-deps.sh`: per-arch qemu probes + cross-arch advisory

**Where it fits:** Acceptance-2 — dep-checker tests cover both host arches with and
without the foreign qemu package.

**Files:** `scripts/check-setup-deps.sh`, `tests/scripts/test_check_setup_deps.py`.

**Changes (spec §C):**
- Add `SUPPORTED_ARCHES=(ppc64le x86_64)` and `host_arch="$(uname -m)"`.
- Generalize `qemu_system_binary()` → `qemu_binary_for_arch(arch)` mapping
  `x86_64→qemu-system-x86_64`, `ppc64le→qemu-system-ppc64` (keep the asymmetry). The
  future-tier native probe uses `qemu_binary_for_arch "${host_arch}"`, **only when**
  `host_arch` ∈ `SUPPORTED_ARCHES`.
- **Unsupported host arch:** when `host_arch` ∉ `SUPPORTED_ARCHES`, skip the native-qemu
  future-tier requirement and the cross-arch advisory; print exactly `host arch <X> is
  not a supported kdive provisioning arch (supported: ppc64le, x86_64)`.
- **Cross-arch advisory block** (after the tier reports, supported host only): for each
  `arch` in `SUPPORTED_ARCHES` that is not `host_arch`, resolve its binary; if present →
  `guest arch <arch>: available via TCG only (<binary>)`; if absent → `guest arch
  <arch>: not available; install <pkg> for TCG guests` where `<pkg>` = `package_for
  "$(qemu_binary_for_arch "${arch}")" "${distro}"`.
- Keep it report-only (never fail on cross-arch); shellcheck-clean; `shfmt -i 2`.

**Acceptance:**
- Existing `test_ppc64le_future_hint_names_the_power_qemu_package` still passes
  (native ppc64le → `qemu-system-ppc`).
- New tests (parametrized over host arch × foreign-qemu present/absent):
  - `uname=x86_64`, no `qemu-system-ppc64` on PATH, debian → advisory `guest arch
    ppc64le: not available; install qemu-system-ppc`.
  - `uname=x86_64`, `qemu-system-ppc64` stubbed present → `guest arch ppc64le: available
    via TCG only`, and **not** the install hint.
  - `uname=ppc64le`, no `qemu-system-x86_64` → advisory names `qemu-system-x86` (debian);
    with it present → "available via TCG only".
  - `uname=aarch64` → the "not a supported kdive provisioning arch" line, and neither the
    x86 native hint nor a cross-arch advisory.
  - opensuse variant asserts `qemu-ppc`/`qemu-x86` (matches `package_for`).

**Guardrails:** `just lint-shell` (shellcheck+shfmt), `uv run python -m pytest
tests/scripts/test_check_setup_deps.py -q`.

**Rollback:** revert the script + test; behavior returns to host-arch-only hint.

---

## Task 3 — `check-local-libvirt.sh`: per-arch native qemu + TCG advisory

**Where it fits:** Acceptance-1's preflight complement; fixes the x86 hardcode.

**Files:** `scripts/check-local-libvirt.sh`, and its test if one exists (`fd
check_local_libvirt tests/`); else add `tests/scripts/test_check_local_libvirt.py`
mirroring the dep-checker test harness (PATH + `uname` stubs, `KDIVE_KVM_NODE`/env
overrides already supported).

**Changes (spec §B):**
- Replace the hardcoded `qemu-system-x86_64` in the required-command loop (line ~74)
  with the **host-native** binary (`qemu_binary_for_arch "$(uname -m)"`), so a POWER host
  is not failed for the x86 emulator and *is* failed for a missing `qemu-system-ppc64`.
- Add a `SUPPORTED_ARCHES` list + `qemu_binary_for_arch()` (same mapping as Task 2).
- **Foreign-arch advisory:** for each supported non-host arch whose binary is present,
  print an informational line (the `OK:`/`printf` vocabulary — **not** `note_fail`/
  `note_warn`): `guest arch <X> available via TCG only (foreign emulator <binary>
  present; scaled by KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER)`. Absent → no line.
- **Unsupported host arch:** print `host arch <X> is not a supported kdive provisioning
  arch (supported: ppc64le, x86_64)` and skip the native-qemu requirement (do not
  fall back to x86).
- **Leave `_has_kvm` unchanged** (the R+W readiness gate is intentional; spec §B notes
  the divergence). No behavior change to the KVM node check.

**Acceptance:**
- `uname=ppc64le`, PATH has `virsh qemu-img` + a `qemu-system-ppc64` stub, `KDIVE_KVM_NODE`
  pointing at a writable temp node, `id` stub in libvirt group, `virsh` stub connecting →
  does **not** emit a `qemu-system-x86_64 not found` FAIL.
- `uname=ppc64le`, no `qemu-system-ppc64` on PATH → emits a FAIL naming
  `qemu-system-ppc64`.
- `uname=x86_64` with a `qemu-system-ppc64` stub present → prints "guest arch ppc64le
  available via TCG only".
- `uname=aarch64` → the "not a supported kdive provisioning arch" line; no x86 native
  FAIL.

**Guardrails:** `just lint-shell`, `uv run python -m pytest tests/scripts/ -q`.

**Rollback:** revert; the script returns to hardcoded x86 required qemu.

---

## Task 4 — Cross-arch install docs

**Where it fits:** Acceptance-3 — docs name the exact packages per supported distro.

**Files:** `docs/operating/install.md`, `docs/operating/runbooks/image-lifecycle.md`.

**Changes (spec §D):**
- `install.md`: a "Cross-architecture guests" subsection —
  - the foreign-arch qemu package **per supported distro**, taken verbatim from
    `check-setup-deps.sh:package_for`: Fedora `qemu-system-ppc`, Debian/Ubuntu
    `qemu-system-ppc`, Arch `qemu-system-ppc`, openSUSE `qemu-ppc`; and the x86 siblings
    for a POWER host (`qemu-system-x86` / openSUSE `qemu-x86`);
  - the accelerator story: native arch runs under KVM, foreign arch under TCG (emulated,
    ~10× slower);
  - the `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` setting (default `10.0`, ≥1.0, 1.0
    disables scaling) that scales boot-readiness deadlines for TCG guests.
- `image-lifecycle.md`: the cross-arch customization-boot note — a foreign-arch image
  customizes by booting once under TCG (ADR-0345), scaled by
  `KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S` (default 1800s) × the multiplier, so it is
  slower than a native customize; name the same foreign-arch qemu package prerequisite.
- Cross-check every package string against `package_for` so docs and dep-checker cannot
  disagree (Acceptance-4).

**Acceptance:** each supported distro's foreign qemu package appears in `install.md` and
matches `package_for`; both settings named with defaults; `just docs-links` green; no
doc-style-guard violations (no "robust"/"comprehensive"/etc.).

**Guardrails:** `just docs-links`, `just docs-check` (if it covers these paths).

**Rollback:** revert the doc edits.

---

## Cross-task verification (pre-push)

- Full `just ci` green.
- Confirm the three surfaces agree on package names (grep `qemu-system-ppc` across
  `install.md`, `check-setup-deps.sh`, `image-lifecycle.md`).
- Confirm no new check id collision: `rg -n 'guest_arch_accel' src/kdive/diagnostics`.
- The new check appears in the doctor verdict: if a doctor integration test enumerates
  check ids, add `guest_arch_accel` to its expected set.
