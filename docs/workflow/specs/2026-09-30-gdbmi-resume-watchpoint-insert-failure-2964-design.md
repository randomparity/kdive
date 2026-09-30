# Resume-time watchpoint insert failure and step-verb stalls (#2964) — design

Decision record: [ADR 0712](../../adr/0712-debug-capability-table-refuses-single-step-and-watchpoints.md),
amendment of 2026-09-30.

## Problem

gdb inserts a hardware watchpoint only when the target resumes. When the insert fails, the
resume command (`-exec-continue`, `-exec-step`, `-exec-next`, `-exec-step-instruction`,
`-exec-finish`) gets `^error` with the text `Could not insert hardware watchpoint N.`. Today
`ExecutionControl.resume` lets `execute_mi_command` raise `debug_attach_failure` with only
`command` and a raw `payload`. The envelope then carries no `data.code`, and the detail is
`gdb/MI command failed: <verb>`.

Two causes give the same text: a stub that cannot insert a hardware watchpoint (ppc64le
KVM-HV, proof record #2739 section 3), and x86 debug-register exhaustion (ADR-0277
Consequences). The text cannot tell them apart.

Issue #2964 also asks for a distinct code for a step verb that never stops, "when the evidence
allows it". The wire evidence does not allow it (see Design 3).

## Design

1. `commands/watchpoints.py` adds
   `watchpoint_insert_failure(exc: CategorizedError, verb: str) -> CategorizedError | None`.
   It returns `None` unless `exc` is `debug_attach_failure` and its `payload.msg` matches
   `_INSERT_FAILED_RE = re.compile(r"Could not insert hardware watchpoint (\d+)\.")`. On a
   match it returns a new `CategorizedError`:
   - category `DEBUG_ATTACH_FAILURE` (unchanged from today);
   - details: the original `command` and `payload`, plus `code: "watchpoint_insert_failed"`,
     `verb`, and `watchpoint` (the first number N, a string);
   - message: `gdb/MI could not insert hardware watchpoint N on resume: the target cannot
     insert a hardware watchpoint, or too many are armed; remove one with
     debug.clear_watchpoint (see debug.list_watchpoints), then retry`.
2. `core/execution.py` `resume` wraps its first `execute_mi_command(attachment, verb)` call.
   On `CategorizedError` it calls `watchpoint_insert_failure(exc, verb)`; a result is raised
   `from exc`, `None` re-raises `exc` unchanged. Nothing else in `resume` changes.
3. Step verbs: no code change. Tests pin today's results. Reasons, recorded in the ADR
   amendment:
   - an interrupt that gets no stop is the same on the wire as a real RSP stall, which must
     keep `transport_stall`;
   - an interrupt that gets a stop proves a live link, but `-exec-step` and `-exec-next` can
     time out on x86 (code with no line table, ADR-0379), and QEMU steps with interrupts
     blocked, so an x86 `stepi` over `hlt` can also not complete;
   - `ExecutionControl` does not know the guest arch or accelerator.

`-break-watch` classification (ADR-0277) does not change.

## Failure model

1. **Actors and deployments** — an MCP caller of `debug.continue` or `debug.advance` on a
   live gdb/MI session; local-libvirt and remote-libvirt (shared engine); x86_64 and ppc64le
   guests.
2. **Invariants and assets at stake** — the public error contract of the resume ops: category
   and `retryable` stay `debug_attach_failure` / true on x86 and ppc64le; a real transport stall
   keeps `transport_stall`; no x86 result changes except the added `data.code`, `data.verb`,
   `data.watchpoint` and the new detail (operator-approved).
3. **Accepted failure classes** — no MI transcript shows either shape of the failure. The
   design matches the synchronous `^error`, because gdb's `proceed()` inserts breakpoints
   before it resumes (gdb `infrun.c`, `breakpoint.c`). A gdb that reports the failure
   asynchronously (after `^running`) keeps today's result. Several failed watchpoints report
   only the first number: the next actions are the same.
4. **Covered elsewhere** — the pre-resume refusal on ppc64le KVM (ADR 0712, #2963); a
   distinct step-verb code needs the guest arch on the attachment (follow-up candidate). The
   `debug.set_watchpoint` docstring and `toolsets-debug.md` do not name
   `watchpoint_insert_failed`; their "stub that refuses the insert" sentence means the
   set-time `-break-watch` refusal (follow-up candidate, outside this surface).

## Success

- A resume `^error` whose msg contains `Could not insert hardware watchpoint 2.` raises
  `debug_attach_failure` with `code: watchpoint_insert_failed`, `verb`, `watchpoint: "2"`.
- A resume `^error` with any other msg raises the original error unchanged.
- `-exec-step`, `-exec-next` and `-exec-step-instruction` with no stop and no interrupt stop
  still raise `transport_stall`. `-exec-step-instruction` with an interrupt stop
  (`signal-received`) still returns `timed_out: True`; `test_step_interrupts_on_timeout`
  already pins this for `-exec-step`.

## Validation

Unit tests in `tests/providers/local_libvirt/test_debug_gdbmi.py` drive `ExecutionControl`
with MI records shaped like proof record sections 3 and 4.
