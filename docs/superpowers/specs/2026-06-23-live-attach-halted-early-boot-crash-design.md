# Live attach to a halted early-boot crash (#747)

- **Status:** Approved (design)
- **Date:** 2026-06-23
- **Issue:** [#747](https://github.com/randomparity/kdive/issues/747) (part of #746)
- **ADR:** [ADR-0233](../../adr/0233-live-attach-halted-early-boot-crash.md)

## Problem

`debug.start_session(run_id, "gdbstub")` rejects a Run whose boot ended in an early-boot
panic, even when the System was provisioned with `gdbstub:true` and the QEMU `-gdb` stub is
still reachable on the halted VM. This is exactly the class — an early-boot panic — where a
live gdb session is most valuable and where the alternative capture methods are weakest:

- **kdump** (`crashkernel`) cannot capture an early-boot panic at all: the kexec capture
  kernel is not armed until late boot (post-initramfs), so a panic before that produces no
  vmcore.
- **host_dump** (`preserve_on_crash`) can dump preserved guest memory but only yields a
  post-mortem core, not a live session.
- **gdbstub** works at the earliest boot instant (host-side QEMU facility, nothing in-guest),
  but `debug.start_session` denies it for a crashed boot.

Two gaps block live attach today:

1. **Representation gap.** An unexpected early-boot panic raises a `READINESS_FAILURE`, the
   boot handler calls `abandon_run_step_best_effort` (the `boot` `run_steps` row is deleted),
   and the Run goes `FAILED`. `_attach_preconditions` then keys on a *succeeded* `boot` step
   (`sessions_lifecycle.py:481-489`) and rejects with `boot_first` — a misleading message
   ("boot it before starting a live session") for a VM that is in fact halted on the panic
   with a live stub.
2. **Phantom instrumentation.** `preserve_on_crash` is documented as adding "a pvpanic device
   + `<on_crash>preserve</on_crash>`" (`profiles/provisioning.py:88-89`), but
   `render_domain_xml` (`providers/local_libvirt/lifecycle/xml.py`) renders neither. Nothing
   in `src/` emits `on_crash`/`pvpanic`. The deterministic "halt on panic" the scenario
   depends on does not actually exist.

A third, intentional gate (`sessions_lifecycle.py:490-497`) rejects a *declared* expected
crash (`boot_outcome == "expected_crash_observed"`) and routes it to post-mortem. That gate
is the A/B kernel-testing flow (ADR-0064) and stays unchanged.

## Feasibility (proven, not assumed)

A focused QEMU spike on the dev host booted the kdive-debug kernel with no rootfs to force an
early-boot VFS panic, with a pvpanic device and a `-gdb` stub. Result:

```
[1.456050] Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)
QEMU still alive (domain preserved, not destroyed)
gdb → target remote :1234 → connected
  rip 0xffffffff846e17db <delay_tsc+75>
  #3 vpanic (...) at kernel/panic.c:776
  #4 panic  (...) at kernel/panic.c:787
  #5 mount_root_generic (...) at init/do_mounts.c:230
```

gdb attached to the halted vCPU *inside the panic path* with full `vmlinux` symbol
resolution. The capability is real on this host; the `live_vm` test below proves it through
kdive's own provisioning path.

## Decision summary

When a Run's boot ends in a crash/hang that leaves a **live gdbstub**, record a **succeeded**
boot step with `boot_outcome = "crashed_halted_live"` (Run → `SUCCEEDED`) and let
`debug.start_session(…, "gdbstub")` admit it. The gate for recording the outcome is **`gdbstub`
provisioned + a boot-time RSP liveness probe finds the stub reachable** — not
`preserve_on_crash`, not `capture_method`. Keying on the live stub makes live-gdb a fallback
that works *alongside* a kdump- or host_dump-primary System whose early-boot panic defeated the
primary method.

No new run state, no new agent-facing knob, no migration (`boot_outcome` is a schemaless value
in `run_steps.result`).

## Components (in dependency order)

### 1. Make `preserve_on_crash` real

`providers/local_libvirt/lifecycle/xml.py` — when
`profile.provider.local_libvirt.debug.preserve_on_crash` is set, render:

- a `pvpanic` device under `<devices>`, and
- `<on_crash>preserve</on_crash>` on the domain.

This is independent of the `gdbstub` rendering and of `capture_method`. It makes the halt
deterministic (panic → pvpanic → libvirt preserves the domain with vCPUs stopped) instead of
relying on the guest kernel spinning in `panic()`'s delay loop. This is a standalone bugfix
(closes the phantom-feature gap) and a prerequisite for a reliable scenario.

The `provisioning.py` docstring already describes this behavior, so no doc change is needed
there beyond confirming it now matches.

### 2. Boot-time RSP stub-reachability probe

Reuse the **existing** bounded reachability probe `rsp_reachable`
(`providers/shared/debug_common/rsp.py`, ADR-0032/0083): it opens a loopback TCP socket, sends
one read-only RSP `?` (halt-reason) packet, accepts only a valid checksummed frame (a stale or
non-RSP listener is rejected), and is byte-bounded and timeout-bounded so a wedged or hostile
socket cannot stall the boot job. No new probe primitive is written.

**The probe is reachability-only, not the crash signal.** Connecting an RSP client to a QEMU
`-gdb` stub is *not* a passive read: QEMU stops the vCPU while a client is attached and may
resume it on disconnect. Therefore:

- "Did it crash?" is decided by **console evidence** (Component 3), not by the probe. The probe
  answers only "is the stub reachable?".
- For a genuine panic the resume-on-disconnect is harmless: the vCPU resumes spinning in
  `panic()`'s loop and the authoritative attach at `debug.start_session` re-halts it in the same
  place. The probe must **never** send a continue/detach-resume beyond the single `?` exchange
  (`rsp_reachable` already does not).
- A slow-but-healthy boot is **not** mislabeled, because Component 3 records the new outcome only
  when the console shows a panic; a boot with no panic line falls through to the existing
  `abandon → FAILED` path regardless of stub reachability.

The authoritative attach probe still runs at `debug.start_session` time via `open_transport`;
this boot-time probe only gates whether the new outcome is recorded.

### 3. Record the `crashed_halted_live` outcome

`jobs/handlers/runs_boot.py` `_run_boot_and_capture_outcome` — on a `READINESS_FAILURE` that is
**not** a matched expected crash:

- capture the redacted console artifact (as the other outcomes do); then, if **all three** hold —
  (a) `gdbstub` is provisioned, (b) the captured console matches a generic kernel-panic signature
  (a shared `_GENERIC_PANIC_PATTERN`, e.g. `Kernel panic - not syncing`, searched with the same
  redaction-safe `search_text` the expected-crash path uses), and (c) `rsp_reachable` finds the
  stub reachable — record the boot audit event (`_record_boot_audit`, exactly as the `ready` and
  `expected_crash_observed` branches do — this is the one path that reverses the ADR-0064 gate, so
  its audit trail is required) and complete the `boot` step **succeeded** with:

  ```json
  {
    "system_id": "...",
    "boot_outcome": "crashed_halted_live",
    "evidence_kind": "console",
    "evidence_artifact_id": "...",
    "available_capture": ["gdbstub", "console"]
  }
  ```

  `available_capture` lists the genuinely-available follow-up methods for this halted System,
  derived from the provisioning flags: `"gdbstub"` and `"console"` are always present here, and
  `"host_dump"` is appended **iff** `preserve_on_crash` is set. The allowed strings are exactly
  the `CaptureMethod` values (`gdbstub`, `host_dump`, `console`). It is advisory metadata an
  agent/`runs.get` can read; the test asserts the exact list for each flag combination.
- otherwise (no panic signature, no `gdbstub`, or stub unreachable) → existing
  `abandon_run_step_best_effort` → Run `FAILED` (unchanged).

The **console-panic signature (b) is the crash signal**, not the probe: it is what distinguishes a
real early-boot panic from a slow-but-healthy boot that merely tripped the readiness timeout, so
the probe's halt-on-connect side effect cannot convert a healthy boot into a recorded crash.

Ordering note: the new branch sits *after* the existing expected-crash match (so a declared,
matched expected crash still records `expected_crash_observed`) and *before* the `raise` that
abandons the step.

The crash branch only fires for `READINESS_FAILURE` (the guest-did-not-come-up signal), exactly
like the expected-crash branch — a genuine `INFRASTRUCTURE_FAILURE` is never reinterpreted as a
debuggable crash.

**Determinism of the halt.** `preserve_on_crash` (Component 1) makes the halt deterministic: on
panic, pvpanic fires and libvirt holds the domain in `VIR_DOMAIN_CRASHED` with vCPUs stopped, so
the stub is reachable on a stably-halted target. Per the chosen scope (gate on `gdbstub` + panic
evidence, not on `preserve_on_crash`), the new outcome can also be recorded for a `gdbstub`-only
System (e.g. kdump-primary, no `preserve_on_crash`) whose early-boot panic kdump could not
capture. That case is only deterministic when the guest is configured to **halt** on panic
(`panic=0`, the kernel's default — an infinite `panic()` loop) rather than reboot (`panic=N>0`
or `panic=-1`, which would carry the kernel past the panic before attach). The kdive-provisioned
direct-kernel cmdline does not set a rebooting `panic=`, so the default halting behavior holds;
the `live_vm` test exercises the `preserve_on_crash` path (the deterministic one) and this
assumption is called out so a future cmdline change that adds `panic=N` is recognized as
breaking it.

### 4. Admit `crashed_halted_live` at the precondition gate

`mcp/tools/debug/sessions_lifecycle.py` `_attach_preconditions` — after the existing
`expected_crash_observed` branch, add:

- `boot_outcome == "crashed_halted_live"` **and** `transport == "gdbstub"` → fall through to
  the System-ready / occupied checks (admit).
- `boot_outcome == "crashed_halted_live"` **and** `transport == "drgn-live"` → reject
  (`configuration_error`): a halted/panicked guest has no running sshd, so the drgn-live SSH
  transport cannot attach. The detail names the gdbstub alternative.

The Run is already `SUCCEEDED` and the `boot` step is `succeeded`, so the `run_state` and
`boot_first` checks pass without change. The System remains `READY`: verified against the code —
only an explicit `force_crash` (`jobs/handlers/control.py:136`) transitions a System to
`CRASHED`, and the reconciler never syncs a libvirt `VIR_DOMAIN_CRASHED` domain into the System
row (its only System repair is orphaned-Allocation teardown), so a `preserve_on_crash` domain
that libvirt holds in `VIR_DOMAIN_CRASHED` leaves the System row `READY`. A unit test asserts the
System is still `READY` across the crashed-but-halted boot, and the `live_vm` test confirms the
`SystemState.READY` re-check under the lock passes. (If a future change adds libvirt-domain →
System-state reconciliation, this admit branch must be revisited to accept `CRASHED` for the
`crashed_halted_live` case.)

### 5. Surface real options (no new knob)

The provisioned instruments remain the only declaration of crash-handling intent (ADR-0049); we
do **not** add an on-panic action parameter. Instead the available paths are surfaced where the
agent already looks:

- the `crashed_halted_live` boot result carries `available_capture` (above);
- a successful `debug.start_session` already returns `suggested_next_actions` for the live
  session.

This gives the agent a real choice *at the moment of panic*, constrained to what is physically
possible, without a second config surface.

## Data flow

```
runs.boot (worker)
  booter.boot() raises READINESS_FAILURE (early-boot panic)
    ├─ expected_boot_failure declared & console matches → expected_crash_observed (unchanged → postmortem)
    ├─ gdbstub provisioned & console shows panic & rsp_reachable → crashed_halted_live (succeeded boot, Run SUCCEEDED)
    └─ else → abandon boot step → Run FAILED (unchanged)

debug.start_session(run, "gdbstub")
  _attach_preconditions:
    run SUCCEEDED ✓  boot step succeeded ✓
    boot_outcome == crashed_halted_live → admit (gdbstub) / reject (drgn-live)
    System READY ✓  stub free ✓
  open_transport (authoritative RSP probe) → live debug_session
```

## Error handling & edge cases

- **Probe reachable at boot, dead at attach** (VM torn down between): `open_transport` re-probes
  and fails authoritatively (`debug_attach_failure`/`transport_failure`). The recorded outcome is
  advisory; attach is the source of truth.
- **Hang without a panic line** (slow boot that tripped the readiness timeout but never
  panicked): the console-panic signature (b) does not match, so the outcome is **not** recorded —
  the boot abandons to `FAILED` as today. This deliberately excludes the case where the probe's
  halt-on-connect could otherwise freeze a still-progressing boot.
- **`preserve_on_crash` set, `gdbstub` unset**: no stub at all → existing host_dump path,
  unchanged except that `preserve_on_crash` now actually renders (Component 1).
- **kdump-primary System** (`crashkernel` + `gdbstub`) with an early-boot panic: kdump captures
  nothing, but the live stub is found → `crashed_halted_live` → live attach. The fallback the
  issue calls for.
- **Idempotency**: unchanged — the boot step is claimed/completed once via
  `claim_run_step`/`complete_run_step`.
- **Redaction**: the console evidence is redacted by the existing `_capture_console_artifact`
  path before persistence; no raw guest output enters the envelope.
- **Artifact store unavailable**: the panic-signature crash signal needs the captured console
  bytes, which `_capture_console_artifact` only returns when an object store is configured. With
  no store the bytes are absent, the panic signature cannot match, and the boot abandons to
  `FAILED` — the same inherited limitation the `expected_crash_observed` path has (it also
  requires the captured artifact). The new outcome is therefore never recorded in a store-less
  deployment; this is intended parity, not a silent regression.
- **Boot audit**: the `crashed_halted_live` branch records the boot audit event like the other
  terminal outcomes, so the gate reversal is observable in the audit log.
- **Declared expected crash** (`expected_crash_observed`): unchanged → post-mortem (ADR-0064
  preserved).

## Testing

Unit / TDD at each boundary:

- `xml.py`: renders `pvpanic` + `<on_crash>preserve</on_crash>` iff `preserve_on_crash`;
  unchanged when unset; coexists with the `gdbstub` and SSH passthroughs.
- boot handler: records `crashed_halted_live` on `READINESS_FAILURE` only when all three of
  (`gdbstub` provisioned, console matches the generic panic signature, injected probe reports
  reachable) hold; abandons (→ `FAILED`) when any is false — including the **no-panic-line but
  probe-reachable** case (proves the panic signature, not the probe, is the crash signal) and the
  probe-unreachable case; asserts the exact `available_capture` list for the `gdbstub`-only vs
  `gdbstub`+`preserve_on_crash` flag combinations; still records `expected_crash_observed` for a
  declared/matched crash and `ready` for a clean boot; asserts the System row stays `READY` and a
  boot audit row is written for the new outcome.
- `_attach_preconditions`: admits `crashed_halted_live` for `gdbstub`; rejects it for
  `drgn-live`; keeps `expected_crash_observed` → postmortem and the `boot_first` rejection.

`live_vm` (run on this host, the falsifiable proof):

- provision a `gdbstub` (+`preserve_on_crash`) System through the real local-libvirt path, boot
  a kernel that early-panics, and assert `debug.start_session(run, "gdbstub")` opens a live
  gdbstub session against the halted stub (and `end_session` detaches cleanly).

## Out of scope / considered & rejected

See ADR-0233 for the full list. Notably excluded: a new run state; an on-panic action knob;
gating on `preserve_on_crash` or `capture_method`; reversing the A/B expected-crash flow; and
admitting `drgn-live` to a halted crash. **Attach-before-boot / halt-at-start** (provisioning
the VM to start paused so breakpoints can be set before initcalls) is a distinct capability and
is deferred to future work.
