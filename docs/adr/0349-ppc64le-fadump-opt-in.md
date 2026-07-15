# ADR 0349 — fadump on ppc64le: a gated CaptureMethod with a QEMU feature floor

- **Status:** Accepted
- **Date:** 2026-07-14
- **Issue:** #1151
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0346 (#1148 ppc64le kdump — per-arch crashkernel default, host-side
  module injection, the kdump-enabled rootfs scaffold and live driver fadump reuses),
  ADR-0338 (#1140 `guest_arches` discovery on the JSON capabilities column + defensive
  typed reader — the precedent for the fadump host signal), ADR-0339 (#1141 admission
  validates a profile against the discovered capability before the granted→active flip —
  the pattern the fadump host gate mirrors), ADR-0300 (#989 per-install crashkernel
  reservation seam), ADR-0318 (#1052 kernel-config crash-capture gate), ADR-0049 (the
  `CaptureMethod` vocabulary)

## Context

fadump (firmware-assisted dump) is the POWER-specific crash-capture alternative the epic
design named alongside kdump (`2026-07-13-ppc64le-full-support.md` §Crash capture,
decision 2). kdump kexec-boots a capture kernel; fadump instead asks platform firmware to
preserve memory across a memory-preserving reboot (the guest issues `ibm,os-term`, the
platform preserves the registered regions, the next boot exposes them as `/proc/vmcore`),
and then reuses the *same* kdump userspace scripts to save the core. It is more reliable
than kexec-kdump because the dump is taken from a fully firmware-reset system.

Three facts shape the decision:

1. **fadump needs a firmware RTAS call the emulator may not implement.** The platform must
   export `ibm,configure-kernel-dump` (PAPR §7.4.9) plus its device-tree properties.
   QEMU's `pseries` machine implements these **only from QEMU 10.2** (`hw/ppc/spapr_fadump.c`;
   the series was under review on qemu-devel through Oct 2025 and shipped in 10.2). A
   fadump-requesting provision on an older QEMU must fail at admission with a clear
   category, never boot a guest that hangs or never captures.

2. **fadump-under-TCG is unverified.** The memory-preserving reboot is pure `spapr`
   emulation (`trigger_fadump_boot`, `do_fadump_register`), not KVM-HV hardware, so it
   *should* work under TCG — but that is a hypothesis until a live capture proves it. The
   local host runs QEMU 10.2.2 (Fedora 44), whose `qemu-system-ppc64` exports
   `ibm,configure-kernel-dump`, so a live TCG attempt is warranted rather than a foregone
   native-only verdict.

3. **The `CaptureMethod` vocabulary has no fadump member.** It is
   `CONSOLE/HOST_DUMP/GDBSTUB/KDUMP`. fadump is a distinct mechanism but shares kdump's
   retrieve path.

## Decision

### 1. fadump is a new `CaptureMethod.FADUMP`, not a modifier on `KDUMP`

`domain/capture.py` gains `FADUMP = "fadump"`. `LocalLibvirtProfilePolicy.capture_method`
resolves it under the existing crashkernel check — `if crashkernel is not None: if
debug.fadump: return FADUMP; return KDUMP` — so FADUMP is only ever resolved for a System
that carries a reservation.

Modeling fadump as a first-class method (rather than a boolean flag that leaves
`capture_method()` reporting `KDUMP`) makes it legible in telemetry, the `capture_method`
observability label, `supported_capture_methods`, and `vmcore.fetch` resolution, and — the
load-bearing reason — forces **every** `is CaptureMethod.KDUMP` branch to be an explicit
share-or-diverge decision. A flag would leave those branches reading `KDUMP` and silently
covering fadump by accident; a distinct member turns each into a conscious choice, audited
once (§4) so no capture site ignores fadump unintentionally. The cost is that audit; the
benefit is that a future capture-path change cannot silently regress fadump.

### 2. The opt-in is `debug.fadump` + the existing `crashkernel` reservation

`LibvirtDebugOptions.fadump: bool = False` joins `preserve_on_crash`/`gdbstub` in the
`debug` block that already declares which capture methods a System is provisioned for. The
reservation reuses `LibvirtProfile.crashkernel` — the kernel deprecated
`fadump_reserve_mem=` in favour of `crashkernel=`, which fadump reads for its boot-memory
reservation — so no new reservation field and no new `arch_traits` field are added; the
ADR-0346 per-arch default (512M ppc64le) is reused. The boot cmdline appends `fadump=on`
after the `crashkernel=<size>` token for the FADUMP method only; `_PLATFORM_OWNED_CMDLINE_TOKENS`
gains `"fadump="` so a `runs.install` override cannot inject a conflicting token.

A parse-time `model_validator` on `ProvisioningProfile` (mirroring
`_pair_boot_method_with_provider`) enforces the profile self-consistency invariant:
`debug.fadump=True` requires `arch=="ppc64le"` **and** a non-`None` `crashkernel`. fadump is
pseries-only, and a reservation-less fadump System would resolve to a non-capture method and
silently drop the flag. This is the first of two gates and needs no host — it fails
`CONFIGURATION_ERROR` at `ProvisioningProfile.parse`.

### 3. Host support is a discovered, fail-closed signal + an admission gate

`local_libvirt/discovery.py` records `PSERIES_FADUMP_KEY` on the Resource capabilities JSON
column. `detect_pseries_fadump(guest_arches)` reads the ppc64le emulator path already
discovered in `guest_arches["ppc64le"]["emulator"]`, runs `<emulator> --version`, and
returns `True` iff the parsed `(major, minor) >= (10, 2)` — the QEMU floor at which
`pseries` exports `ibm,configure-kernel-dump`.

The probe is **fail-closed**: no ppc64le arch, a version below the floor, or any probe
failure (missing binary, unparseable output, non-zero exit) returns `False`. A false
positive would boot a guest that hangs or never captures — the exact failure the issue
forbids — so uncertainty must deny. A false negative (a distro back-porting fadump to an
older QEMU) only declines fadump on a host that could support it, which admission reports
with an actionable message. A version floor is chosen over binary-string probing
(`strings | grep ibm,configure-kernel-dump`) because the floor is documented, stable, and
does not depend on symbol retention; it is chosen over a live boot-and-check because
discovery must not boot a guest.

`ResourceCapabilities.pseries_fadump() -> bool` is a defensive reader (mirroring
`guest_arches()`) returning `False` for an absent or non-`bool` value, so a stale row never
advertises fadump by accident. `_validate_fadump_supported` — a sibling of
`_resolve_new_system_accel` — is called at System mint, before the granted→active flip:
when the profile opts into fadump and the bound Resource's `pseries_fadump()` is `False`, it
raises `CONFIGURATION_ERROR` naming the QEMU floor and host. The raise is caught by the
existing `_failure_from_error` wrap, so a rejection consumes no capacity and returns a typed
envelope — never a hang.

`CaptureMethod.FADUMP` joins the provider's static `ProviderSupport.capture_methods` (the
provider *can* do fadump; a *given host* is gated by the discovered signal), so
`vmcore.fetch` can resolve an omitted method through the profile and the resource
description advertises it; the description also surfaces the per-host `pseries_fadump` flag.

### 4. Retrieve is shared; the KDUMP-site audit

The local retriever's `capture()` dispatches `if HOST_DUMP … else <overlay harvest>`, so
`FADUMP` (not host_dump) falls through to the identical `/var/crash/*/vmcore` overlay
harvest and is stored as `vmcore-fadump` — **no `retrieve.py` change**. Every other
`is CaptureMethod.KDUMP` site is audited (design §6): the install kdump-env/needs-modules
checks, the ADR-0318 vmcore gate, and the crashkernel-reservation guards **share**
(`in (KDUMP, FADUMP)`); `_VMCORE_METHODS` and the support set **add** FADUMP; the cmdline
and `capture_method` **diverge**; `boot_evidence.inert_capture` appends the resolved value.
remote-libvirt and fault-inject KDUMP sites are untouched — fadump is local-libvirt only.

### 5. Live capture proof (attempt first, documented-verdict fallback)

A `live_stack` driver attempts a real fadump capture under TCG on the x86_64 host, reusing
the #1148 kdump-enabled ppc64le rootfs and bundle. The proof must prove the **mechanism,
not just the outcome**: the kernel silently kdump-falls-back when fadump cannot reserve or
register memory (and still writes a `/var/crash/vmcore` kdive labels `vmcore-fadump`), so
`fadump=on` in the cmdline and the object key only prove the flag was set. Confirm-first
preconditions: `CONFIG_FA_DUMP=y` in the running kernel (strictly stronger than kdump's
`CONFIG_CRASH_DUMP`), the kdump userspace, and ≥2 GB RAM. Discriminating attribution: the
provisioned guest observed **pre-crash** with fadump `registered==1` (via whichever sysfs
interface the kernel exposes — the modern `/sys/kernel/fadump/` directory or the legacy flat
files) and `kexec_crash_loaded==0` (the fadump-active runtime signal ruling out the kdump
fallback),
plus the domain cmdline `fadump=on`, `crashkernel=512M fadump=on` in `/proc/cmdline`, and a
non-empty `EM_PPC64` core under `vmcore-fadump` with makedumpfile fields recorded. The
ADR-0318 config gate stays kdump-symbol-only (it cannot tell a fadump kernel from one that
falls back), so this runtime signal — not a static config check — is the fadump safeguard.
Unlike the kdump AC, fadump-under-TCG may legitimately prove unusable: if the capture cannot
be made to work after honest iteration, the verdict is documented (this QEMU 10.2 floor +
native-POWER validation deferred to #1152), which is the issue's explicit feasibility-gate
fallback — not shipped as indeterminate.

**Live-proof outcome (2026-07-14 — DOCUMENTED, native-POWER required).** The live run drove the
full lifecycle on build `gaa97e8dae`: admission accepted fadump on the QEMU-10.2.2 host (and denied
it on a stale-capability host — both gate directions exercised), the domain booted with
`crashkernel=512M fadump=on`, and the guest kernel logged **`rtas fadump: Registration is
successful!`** — the mechanism (`registered==1`), confirming QEMU 10.2's `ibm,configure-kernel-dump`
RTAS *accepts* registration under TCG, not a silent kdump fallback. The fadump guest then hit a
recurring `rtas_event_scan` RTAS `Oops` (dispatch into the fadump-reserved region) under TCG and
never reached readiness; the identical rootfs+kernel booted ready and captured under **kdump**
(#1148), isolating `fadump=on` as the cause. Because the crash→capture path rides the same Oopsing
RTAS, no boot-window tuning recovers it under emulation. **Verdict:** fadump end-to-end capture
requires native-POWER (KVM) validation (carried by the POWER10 bring-up); the QEMU **10.2** floor is
confirmed live. The `live_stack` driver now skips on non-ppc64le hosts and serves as the
native-POWER driver. Full evidence: `docs/design/2026-07-14-ppc64le-fadump-proof-record-1151.md`.

## Consequences

- A ppc64le System opts into fadump with `debug.fadump=True` + a `crashkernel` reservation;
  its boot cmdline carries `fadump=on`. kdump remains the default spine (no `debug.fadump`).
- fadump on an unsupporting host (QEMU <10.2, or non-ppc64le, or a never-discovered
  resource) fails fast at admission with a typed `CONFIGURATION_ERROR`, consuming no
  capacity — never a hang.
- The capture is harvested and retrieved through the same pipeline as kdump, stored under a
  distinct `vmcore-fadump` key. A fadump core needs the same crash symbols (ADR-0318 gate)
  and the same kdump userspace as kdump.
- No migration, no schema change: the host signal rides the JSON capabilities column, the
  opt-in rides the stored profile.
- `capture_method` is a first-class fadump signal in telemetry/surfacing; every capture
  site treats fadump explicitly.

## Rejected alternatives

- **A boolean fadump modifier that leaves `capture_method()` reporting `KDUMP`.** Smaller
  diff, but fadump would be invisible in telemetry/surfacing and every `is KDUMP` branch
  would cover fadump implicitly — a future capture-path change could silently regress it.
  The distinct member's audit cost is paid once; the safety is permanent. (The issue owner
  chose the distinct member.)
- **Detect fadump by scanning the QEMU binary (`strings | grep ibm,configure-kernel-dump`).**
  Rejected: depends on symbol/string retention (a stripped or differently-built binary
  false-negatives), and is less legible than a documented version floor. The floor is the
  stable, PAPR-anchored fact.
- **Detect fadump by booting a pseries guest and reading the device tree at discovery.**
  Rejected: discovery must not boot a guest (cost, side effects); the version floor answers
  the question without a boot.
- **Persist `fadump=on` on the System row (a new migration, like `accel` at mig0067).**
  Rejected: the opt-in is re-derived from the stored profile at each read; `accel` is
  persisted because it is resolved from live host state at provision, whereas fadump-on is
  a pure profile fact. No migration needed.
- **A fadump-specific reservation field / a larger fadump `arch_traits` default.** Rejected:
  fadump reads `crashkernel=` for its boot-memory reservation, and 512M (ADR-0346) is the
  same order kdump reserves; an operator wanting more uses the ADR-0300 per-install seam.
- **Offer fadump on remote-libvirt too.** Rejected: out of scope (separate provider epic);
  their KDUMP sites and capture-method sets are untouched.
- **Assert the opt-in/admission/doctor code unit-only and skip the live attempt.** Rejected:
  the AC requires either a live capture or a documented verdict, and the local QEMU 10.2.2
  makes a live TCG attempt feasible — so the attempt is made first, with the documented
  verdict as the honest fallback only if it fails.

## Rollout

Additive and backward compatible. No migration; no behavior change on any non-fadump path
(kdump, x86_64, host_dump unchanged). fadump is inert unless a profile sets
`debug.fadump=True`, which requires ppc64le + a reservation + a QEMU-≥10.2 host, each
enforced fail-closed. If the live TCG proof fails, the code still ships (the gates are
correct regardless) with a documented native-POWER validation verdict.
