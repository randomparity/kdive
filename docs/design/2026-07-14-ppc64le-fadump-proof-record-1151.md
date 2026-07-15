# Proof record — ppc64le fadump capture under TCG (#1151)

Date: 2026-07-14
Issue: #1151 · Epic: #1139 · Spec: `2026-07-14-ppc64le-fadump-1151.md` · ADR-0349

> **Status: DOCUMENTED VERDICT — fadump registers under QEMU 10.2 TCG, but an end-to-end capture
> requires native POWER.** The live run (2026-07-14, this branch's build `gaa97e8dae`) drove the
> full lifecycle: fadump admission accepted, the guest booted with `crashkernel=512M fadump=on`,
> and the kernel logged **`rtas fadump: Registration is successful!`** — the mechanism, not just
> the flag. The fadump-configured guest then hit a recurring RTAS-emulation `Oops` under TCG
> (detail below) and never reached kdive readiness, so the crash→capture cycle could not complete.
> Per ADR-0349 §5 (AC 5), this is the documented native-POWER verdict, with the QEMU 10.2 RTAS
> floor confirmed live. The opt-in / admission-gate / discovery / doctor code is complete and
> CI-green (`just ci`, 8049 passed) and shipped regardless — the gates proved correct end-to-end.

## Feasibility gate — the question this proof answers

The epic flagged two unknowns (design "Known unverified", issue 12): the QEMU version floor for
pseries fadump, and whether fadump works under TCG at all. AC 5 permits **either** a live TCG
capture **or** a documented verdict that fadump needs native-POWER validation, with the QEMU floor
recorded here.

## Confirmed preconditions (established before any capture)

- **QEMU floor = 10.2, present locally.** The fadump series (`ibm,configure-kernel-dump` RTAS)
  landed in **QEMU 10.2** (`hw/ppc/spapr_fadump.c`; the series was still in review on qemu-devel
  through Oct 2025). The dev host runs `qemu-system-ppc64` **10.2.2** (Fedora 44), whose binary
  exports the RTAS — verified directly:

  ```
  $ strings /usr/bin/qemu-system-ppc64 | grep -i 'configure-kernel-dump\|fadump'
  do_fadump_register
  trigger_fadump_boot
  ibm,configure-kernel-dump
  ../hw/ppc/spapr_fadump.c
  ibm,configure-kernel-dump-version
  ibm,configure-kernel-dump-sizes
  ```

  So the discovery probe (`detect_pseries_fadump`) records `pseries_fadump=True` on this host, and
  admission will accept a fadump provision here.

- **`CONFIG_FA_DUMP=y` in the baseline kernel** (the finding-2 precondition — fadump needs it in
  the *running* kernel, strictly stronger than kdump's `CONFIG_CRASH_DUMP`). Confirmed in the
  #1148 uploaded bundle's kernel config (`6.19.10-300.fc44.ppc64le`):

  ```
  $ tar xzf kernel.tar.gz -O lib/modules/6.19.10-300.fc44.ppc64le/config | grep CONFIG_FA_DUMP
  CONFIG_FA_DUMP=y
  ```

  So `fadump=on` will be honored by the kernel, not silently ignored (which would silently
  kdump-fall-back — the finding-1 risk).

- **kdump-enabled ppc64le rootfs + ≥2 GB RAM** — reused from #1148: the
  `fedora-kdive-ready-44-ppc64le.qcow2` rootfs (kexec-tools + `kdump.service` + dracut kdump
  module — fadump reuses this userspace to save the core) and the 2048 MB fadump fixture. The
  bundle + rootfs are staged at `/home/dave/kdive-ppc-proof/`.

## Code proven by unit/service tests (CI-green)

- Profile opt-in `debug.fadump` with the parse-time invariant (arch=ppc64le + a reservation);
  `capture_method → FADUMP`; `system_required_cmdline` emits `crashkernel=512M fadump=on`.
- Discovery `detect_pseries_fadump` (fail-closed, QEMU-10.2 floor) recorded on the capability
  column; admission rejects a fadump provision on an unsupporting host with `CONFIGURATION_ERROR`
  before the granted→active flip (both mint sites), consuming no capacity.
- The KDUMP-site share/diverge/add audit; retrieve unchanged (the overlay harvest is
  method-agnostic — FADUMP is stored under `vmcore-fadump`); `vmcore.fetch` resolves FADUMP.
- The `pseries_fadump` doctor check (reports SUPPORTED on this host) + the shell advisory, which
  reports live on this host: `OK: qemu-system-ppc64 10.2 implements pseries fadump (>= 10.2)`.

## The live run (2026-07-14, build `gaa97e8dae`)

Driver: `tests/integration/test_live_stack.py::test_ppc64le_fadump_captures_a_vmcore_under_tcg`
(`live_stack`-marked). It provisions a FADUMP System, asserts **`fadump=on` + `crashkernel=512M`**
in the running domain's `<cmdline>`, `force_crash`es it, and asserts a captured **`vmcore-fadump`**
core. On a non-native-POWER host it now skips with the native-POWER reason (see "Test disposition"
below); the run recorded here was driven manually with that skip lifted, to reach the verdict.

The lifecycle advanced cleanly up to the guest kernel:

1. **Admission accepted fadump** — only because discovery advertised `pseries_fadump=True` for the
   host's QEMU 10.2.2 (the fail-closed gate, ADR-0349). A first attempt was denied
   `pseries_fadump_unsupported` (`qemu_floor: 10.2`) — see "Discovery-refresh limitation" — proving
   the gate rejects a host that does not advertise the capability.
2. **Install + boot** staged the uploaded bundle and booted a `pseries` domain whose `<cmdline>`
   carried `crashkernel=512M fadump=on` (asserted live via `virsh dumpxml`).
3. **fadump registered with firmware** — the guest kernel console (captured as a redacted artifact
   even on the readiness failure — the BLACK_BOX diagnosability path, epic #1018):

   ```
   fadump: Reserved 512MB of memory at 0x00000020000000 (System RAM: 2048MB)
   fadump: Initialized [0x20000000, 512MB] cma area ... reserved for firmware-assisted dump
   rtas fadump: Registration is successful!          <-- the mechanism (registered==1), live
   ...
   [  OK  ] Started sshd.service - OpenSSH server daemon.
   kdive login:
   ```

   This is the finding-1 mechanism proof: QEMU 10.2.2's `ibm,configure-kernel-dump` RTAS
   **accepted the fadump registration under TCG** — it is *not* a silent kdump fallback.

4. **Then the TCG RTAS emulation Oopsed**, blocking readiness:

   ```
   [  246.247524] Unrecoverable FP Unavailable Exception 800 at 2fff0000
   [  246.250275] Oops: Unrecoverable FP Unavailable Exception, sig: 6 [#1]
   [  246.251628] Workqueue: events rtas_event_scan
   [  246.253184] [c0...] rtas_call+0x408/0x500
   [  246.253232] [c0...] rtas_event_scan+0x98/0x350
   ```

   The periodic `rtas_event_scan` RTAS call dispatched into `0x2fff0000` — an address *inside* the
   fadump-reserved region `[0x20000000, +512MB]` — and hit an FP-Unavailable exception, Oopsing a
   kworker. The scan is periodic, so it recurs and destabilizes the guest; the boot job exhausted
   all 3 attempts with `error_category=readiness_failure` ("System booted but a run-readiness check
   failed"). The full evidence excerpt is at
   `docs/design/artifacts/2026-07-14-ppc64le-fadump-1151-console-excerpt.txt`.

**Causation is clean.** The **identical rootfs + kernel** (`fedora-kdive-ready-44-ppc64le`,
`6.19.10-300.fc44.ppc64le`) in the #1148 **KDUMP** proof booted ready on attempt 1 and captured a
`vmcore-kdump` on this same host earlier the same day. The only delta is `fadump=on`. And because
the crash→capture path (`ibm,os-term` → the memory-preserving reboot) rides the *same* RTAS that is
already Oopsing during normal event scanning, extending the readiness deadline cannot yield a
capture under TCG — the defect is in QEMU's pseries fadump RTAS emulation, not in kdive or the boot
window.

## Verdict — DOCUMENTED (native-POWER required)

fadump under QEMU 10.2 TCG **registers** (RTAS floor confirmed live) but **cannot complete an
end-to-end capture**: the guest's periodic RTAS scan Oopses post-registration under emulation. Per
AC 5, the verdict is: **fadump end-to-end capture requires native-POWER (KVM) validation**, carried
by the POWER10 bring-up. The QEMU **10.2** floor is confirmed as the version at which pseries both
*exports* and *accepts* the `ibm,configure-kernel-dump` RTAS.

The opt-in / admission-gate / discovery / doctor code ships now — the live run proved every gate
end-to-end: admission accepted on a capable host (and denied on a stale one), the cmdline carried
`fadump=on`, and the kernel registered fadump. Only the emulated capture is deferred.

## Test disposition

Because a fadump capture provably cannot complete under TCG, the driver **skips on any
non-`ppc64le` host** (where ppc64le necessarily runs under TCG) with a reason pointing here, and
serves as the **native-POWER capture driver** — it will exercise the crash→capture cycle unchanged
on a real POWER host (KVM). The #1148 KDUMP driver keeps running under TCG (kdump has no such RTAS
interaction).

## Discovery-refresh limitation (surfaced by this proof, not introduced by it)

The first live provision was denied `pseries_fadump_unsupported` even though the host's QEMU is
10.2.2. Cause: deploy/onboard discovery registers a resource **insert-only-when-absent**
(`ensure_discovered_resource_registered`, composition.py) and there is **no** path that refreshes an
existing row's `capabilities` jsonb. The live-stack's local-libvirt row was inserted by an older
build, so it carried `guest_arches` but not the newer `pseries_fadump` key; the reader returns
`False` for a missing key → the gate **fail-closed denies** (a safe false-negative: a clear
`CONFIGURATION_ERROR` naming the 10.2 floor, never a hung/failed capture).

This is **pre-existing and epic-wide** — every capability key in epic #1139 (`guest_arches`,
`accel`, now `pseries_fadump`) reaches an existing row only when that row is recreated; a fresh
deploy inserts the row *with* the key. It is **not** a #1151 regression, and it degrades safely.
kdive is pre-first-release, so there are no deployments to upgrade in place. Remediation (used here
to run the proof): re-register the host through the existing upsert path,
`register_discovered_resource`, which refreshes `capabilities` in place (UUID/FKs preserved).
**Follow-up:** a periodic/deploy capability *refresh* (wiring the upsert path to onboarding or the
reconciler discovery pass) is worth a separate issue against the epic — it would let a new
capability key roll out to existing hosts without a row recreation. Deliberately out of scope for
#1151, which follows the established set-at-creation pattern.
