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

### 2. Boot-time RSP stub-liveness probe

A bounded probe that confirms the gdbstub is reachable and the target is halted: TCP-connect to
the loopback gdb port, send the RSP `?` (halt-reason) packet, and accept a stop-reply. Reuses
the connector's existing gdbstub bind/probe seam (ADR-0210 §1). The probe is **advisory** — it
makes the recorded outcome trustworthy; the authoritative attach probe still runs at
`debug.start_session` time via `open_transport`.

The probe must be bounded (short connect + read timeout) so a wedged socket cannot stall the
boot job.

### 3. Record the `crashed_halted_live` outcome

`jobs/handlers/runs_boot.py` `_run_boot_and_capture_outcome` — on a `READINESS_FAILURE` that is
**not** a matched expected crash:

- if `gdbstub` is provisioned **and** the RSP probe finds the stub live →
  capture the redacted console artifact (as the other outcomes do) and complete the `boot` step
  **succeeded** with:

  ```json
  {
    "system_id": "...",
    "boot_outcome": "crashed_halted_live",
    "evidence_kind": "console",
    "evidence_artifact_id": "...",
    "available_capture": ["gdbstub", "host_dump?", "console"]
  }
  ```

  `available_capture` lists the genuinely-available follow-up methods for this halted System,
  derived from the provisioning flags (`gdbstub` always present here; `host_dump` iff
  `preserve_on_crash`; `console` always). It is advisory metadata an agent/`runs.get` can read.
- otherwise → existing `abandon_run_step_best_effort` → Run `FAILED` (unchanged).

Ordering note: the new branch sits *after* the existing expected-crash match (so a declared,
matched expected crash still records `expected_crash_observed`) and *before* the `raise` that
abandons the step.

The crash branch only fires for `READINESS_FAILURE` (the guest-did-not-come-up signal), exactly
like the expected-crash branch — a genuine `INFRASTRUCTURE_FAILURE` is never reinterpreted as a
debuggable crash.

### 4. Admit `crashed_halted_live` at the precondition gate

`mcp/tools/debug/sessions_lifecycle.py` `_attach_preconditions` — after the existing
`expected_crash_observed` branch, add:

- `boot_outcome == "crashed_halted_live"` **and** `transport == "gdbstub"` → fall through to
  the System-ready / occupied checks (admit).
- `boot_outcome == "crashed_halted_live"` **and** `transport == "drgn-live"` → reject
  (`configuration_error`): a halted/panicked guest has no running sshd, so the drgn-live SSH
  transport cannot attach. The detail names the gdbstub alternative.

The Run is already `SUCCEEDED` and the `boot` step is `succeeded`, so the `run_state` and
`boot_first` checks pass without change. The System is **not** transitioned by the boot handler
on failure, so it remains `READY` and the existing `SystemState.READY` check passes; the design
relies on this and the `live_vm` test asserts it.

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
  booter.boot() raises READINESS_FAILURE (early-boot panic/hang)
    ├─ expected_boot_failure declared & console matches → expected_crash_observed (unchanged → postmortem)
    ├─ gdbstub provisioned & RSP probe live → crashed_halted_live (succeeded boot, Run SUCCEEDED)
    └─ else → abandon boot step → Run FAILED (unchanged)

debug.start_session(run, "gdbstub")
  _attach_preconditions:
    run SUCCEEDED ✓  boot step succeeded ✓
    boot_outcome == crashed_halted_live → admit (gdbstub) / reject (drgn-live)
    System READY ✓  stub free ✓
  open_transport (authoritative RSP probe) → live debug_session
```

## Error handling & edge cases

- **Probe live at boot, dead at attach** (VM torn down between): `open_transport` re-probes and
  fails authoritatively (`debug_attach_failure`/`transport_failure`). The recorded outcome is
  advisory; attach is the source of truth.
- **Hang without panic** + live stub: also recorded `crashed_halted_live` and admitted —
  attaching to a hung early boot is desirable and within scope (the gate is the live stub).
- **`preserve_on_crash` set, `gdbstub` unset**: no live stub → existing host_dump path,
  unchanged except that `preserve_on_crash` now actually renders (Component 1).
- **kdump-primary System** (`crashkernel` + `gdbstub`) with an early-boot panic: kdump captures
  nothing, but the live stub is found → `crashed_halted_live` → live attach. The fallback the
  issue calls for.
- **Idempotency**: unchanged — the boot step is claimed/completed once via
  `claim_run_step`/`complete_run_step`.
- **Redaction**: the console evidence is redacted by the existing `_capture_console_artifact`
  path before persistence; no raw guest output enters the envelope.
- **Declared expected crash** (`expected_crash_observed`): unchanged → post-mortem (ADR-0064
  preserved).

## Testing

Unit / TDD at each boundary:

- `xml.py`: renders `pvpanic` + `<on_crash>preserve</on_crash>` iff `preserve_on_crash`;
  unchanged when unset; coexists with the `gdbstub` and SSH passthroughs.
- boot handler: records `crashed_halted_live` on `READINESS_FAILURE` when `gdbstub` provisioned
  and an injected probe reports live; abandons (→ `FAILED`) when the probe reports dead or
  `gdbstub` unset; still records `expected_crash_observed` for a declared/matched crash; still
  records `ready` for a clean boot.
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
