# ADR 0285 — SysRq diagnostic capture for local-libvirt Systems

- **Status:** Proposed
- **Date:** 2026-06-30
- **Deciders:** kdive maintainers

## Context

A ready local-libvirt System has no non-destructive way to ask the running kernel for a
live diagnostic dump (blocked tasks, held locks, per-CPU registers, memory, task state).
The only guest-injection path today is `control.force_crash`, which panics via NMI
(destructive, ADMIN + profile opt-in gate, ADR-0028/0130). ADR-0280 reaffirmed that kdive
exposes no interactive console and no agent-driven console write path; console output stays
one-shot and reference-based through `artifacts.{list,get}`. This decision adds the
non-destructive counterpart to `force_crash`: trigger an allowlisted magic-SysRq diagnostic
and capture what the kernel prints. See `../specs/top-level-design.md` "Control plane" and
`docs/design/2026-06-30-sysrq-diagnostic-capture-925.md` (#925).

## Decision

We will add `control.diagnostic_sysrq`, a CONTRIBUTOR-gated, non-destructive tool that
enqueues a `diagnostic_sysrq` worker job which injects one allowlisted magic-SysRq keystroke
into a ready local-libvirt guest and captures the resulting console output as a redacted,
System-owned artifact.

- **Allowlist by construction.** `command` is a friendly `SysRqCommand` StrEnum
  (`show_task_states`→`t`, `show_blocked_tasks`→`w`, `show_memory`→`m`, `show_locks`→`d`,
  `show_registers`→`p`, `show_backtrace_all_cpus`→`l`, `show_timers`→`q`), the single source
  of truth. Destructive keys (`c`/`b`/`o`/`s`/`u`/`e`/`i`/`f`/`k`) are not in the enum and
  are structurally unexpressible; a crash/reboot request returns `configuration_error` whose
  remediation names `control.force_crash`. This satisfies "destructive SysRq commands are
  rejected unless already represented by an existing destructive tool" with no runtime
  den-list to drift.
- **Injection.** A new `Controller.diagnostic_sysrq(domain_name, trigger)` port method sends
  `[KEY_LEFTALT, KEY_SYSRQ, KEY_<trigger>]` via libvirt `domain.sendKey(VIR_KEYCODE_SET_LINUX,
  …)` — the `virsh send-key` mechanism — mirroring `force_crash`'s single-libvirt-call shape.
  The SysRq handler is on the keyboard input path, so the dump reaches the captured serial
  console. `remote_libvirt` gets a Protocol-conformance stub that raises `control_failure`
  (`not_supported`); the tool never routes a non-local System to it.
- **Job, not synchronous.** Control ports are called only from worker handlers under the
  per-System advisory lock, and capture blocks for a bounded settle window. The tool admits
  synchronously and returns `{job_id, status: queued}` like `control.force_crash`; the worker
  injects, polls the console for growth with a bounded count-driven loop (no wall-clock),
  redacts the delta, and stores it. No System state moves; audit `sysrq:{command}`.
- **Capture delivery.** The job's `result_ref` is the redacted artifact's id, surfaced as
  `refs.result`; the bounded inline snippet is delivered by the existing `artifacts.get`
  24 KiB token-safe window (no new snippet-bounding code). The artifact is System-owned
  (`owner_kind='systems'`, `sensitivity=REDACTED`, `retention_class='console'`), object name
  `sysrq-diagnostic-<job_id>`, `run_id=NULL` (a diagnostic dump is not boot/console evidence,
  so it stays out of `runs.get`'s `console_artifacts` manifest).
- **No output** within the bound → the job fails `configuration_error`
  (`reason=no_console_output`, remediation about `kernel.sysrq` and an active serial console).
- **Authorization.** Minimum role CONTRIBUTOR (the debug/investigation loop), no
  destructive-op gate. Preconditions are fail-fast `configuration_error`s (malformed id,
  not-visible, unknown/destructive command, non-local-libvirt, not-READY), cross-project
  detail suppressed as `force_crash` does.
- **Persistence.** `JobKind.DIAGNOSTIC_SYSRQ`, `SysRqPayload(system_id, command)`, and
  migration `0055_diagnostic_sysrq_job_kind.sql` widening `jobs_kind_check` (forward-only,
  ADR-0015). Not a `DESTRUCTIVE_JOB_KIND`. `teardown_handler` reclaims `sysrq-diagnostic-*`
  System artifacts alongside `console-part-*`.

## Consequences

- Investigators get live, non-destructive kernel diagnostics on any ready local-libvirt
  System, without a debug session and without crashing the guest.
- Adds one Control-port method (two implementations, one a defensive stub), one job kind +
  migration, one payload, and a teardown-reclaim clause. The agent-facing surface guards
  (tool registry snapshot, control toolset doc / agent index) must be updated in the same PR.
- Capture is best-effort and point-in-time: a bounded settle window means an unusually slow
  or `kernel.sysrq`-disabled guest reads as `no_console_output` (with remediation). SysRq
  printk output is effectively immediate, so the bound is ample in practice.
- Relies on the guest having an emulated keyboard (QEMU default) and `kernel.sysrq` enabled
  for the requested command; both unmet cases surface as `no_console_output` remediation
  rather than a hang.

## Alternatives considered

- **Synchronous server-side tool.** Rejected: Control ports are worker-only, capture blocks
  for a settle window, and per-System serialization is a worker/advisory-lock concern —
  matching `force_crash`'s job shape keeps the invariants intact.
- **Raw SysRq character / runtime deny-list.** Rejected in favor of a friendly allowlist
  enum: a positive allowlist makes destructive commands structurally impossible and gives the
  agent literal, self-documenting identifiers, with no deny-list to drift.
- **Write to `/proc/sysrq-trigger` over SSH.** Rejected: needs guest SSH/networking and
  credentials, does not work for an early-boot or wedged guest, and diverges from
  `force_crash`'s libvirt-domain mechanism. `sendKey` reaches the guest through the
  hypervisor keyboard with no in-guest agent.
- **Reuse `console_rotate` parts instead of a dedicated artifact.** Rejected: threshold-based
  async rotation cannot tell the caller which part holds *their* dump; a dedicated
  `refs.result` artifact is the discoverable channel the acceptance criteria expect.
- **Run-correlate the artifact (`run_id`).** Rejected: it would surface diagnostic dumps in
  `runs.get`'s console-evidence manifest, blurring "console evidence" with "SysRq dump".
- **OPERATOR / ADMIN role.** Rejected: the operation is non-destructive and investigative;
  CONTRIBUTOR already scopes the debug/post-mortem loop. `force_crash` keeps ADMIN + gate.
- **A new `debug.*` or `systems.*` tool.** Rejected: placing it beside `control.force_crash`
  makes the destructive/diagnostic pairing discoverable.
