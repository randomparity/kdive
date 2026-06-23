# ADR 0216 — gdb-MI resume scans the continue command's own records for an early `*stopped`

- **Status:** Accepted
- **Date:** 2026-06-22
- **Issue:** #711 (local-libvirt `debug.continue` stalls over the QEMU gdbstub)
- **Refines:** [ADR-0034](0034-debug-plane-gdbmi-tier.md) (the debug-plane gdb-MI tier; this
  fixes a stop-delivery defect in that tier's `continue`/`wait_for_stop` machinery)

## Context

`debug.continue` on local-libvirt stalls: `debug.start_session` → `live`,
`set_breakpoint("schedule")` → `set`, `read_registers` → real kernel values, but
`continue` returns `infrastructure_failure` (`transport_stall`, `"gdb/MI RSP went silent:
interrupt issued but no *stopped arrived; link stalled"`).

A live teammate drive pinned the root cause and falsified the issue's own hypothesis. The
breakpoint address (`nokaslr` fixes the load base) and symbolization are correct: a raw
`gdb` `break schedule; continue` over the **same** gdbstub fires fine, and raw gdb-MI with
kdive's exact sequence (`mi-async on`, `-break-insert schedule`, `-exec-continue`) returns
`^running` then `*stopped,reason="breakpoint-hit"`. So gdbstub, breakpoint, `nokaslr`, and
KVM all work — the earlier "KVM + `STRICT_KERNEL_RWX` needs a hardware breakpoint" theory
was disproved by that control test.

The defect is in our engine. `ExecutionControl.resume()`
(`src/kdive/providers/shared/debug_common/execution.py`) calls
`self._engine.execute_mi_command(attachment, verb)` to issue `-exec-continue`. That call's
pygdbmi-backed reader blocks reading the MI stream for the command timeout and so captures
**both** `^running` **and** the early `*stopped` — the `schedule` breakpoint fires within
milliseconds, well inside the read window. But `resume()` discarded the returned
`list[MiRecord]` (only `execute_mi_command` itself inspected them, and only for `^error`).
It then called `wait_for_stop()`, which reads the stream **fresh** — the `*stopped` was
already consumed by the continue command's reader, so the fresh poll finds nothing, waits
out the full window, issues `-exec-interrupt`, and reports `transport_stall`. The kernel was
never actually stuck; the stop notification was simply read and dropped one call too early.

## Decision

`resume()` scans the records returned by `execute_mi_command(attachment, verb)` for a
`*stopped` **before** falling through to `wait_for_stop`. If the continue command's own
records already carry the stop, `resume()` returns it immediately; otherwise it polls
`wait_for_stop` exactly as before (the slow-breakpoint / true-stall paths are unchanged).

The record → `GdbStopRecord` extraction is factored into one private helper,
`ExecutionControl._stop_from_records(records)` — "the parsed stop for the first `*stopped`
record, or None" — shared by both `resume()` and `wait_for_stop()`. The helper returns the
un-redacted `GdbStopRecord` (matching `wait_for_stop`'s existing contract); `resume()`
applies `redact_stop` once on whichever path produced the stop, so redaction is identical
regardless of which reader observed it. `execute_mi_command` still raises on `^error`, so an
errored continue surfaces before any stop scan.

This is a **provider-neutral engine seam** (`shared/debug_common`), so the fix applies to
both local-libvirt and remote-libvirt gdbstub debugging.

## Consequences

- The common case — a hot-path breakpoint that fires immediately — now returns the stop on
  the first read instead of waiting out the interactive window and falsely interrupting.
- No transport, schema, port, migration, or MCP-surface change; the change is confined to
  `ExecutionControl` and its tests.
- `debug.*` (B1) maturity stays `partial`. This change is unit-proven (an injected engine
  double drives both the captured-stop and the fall-through paths); live re-verification of
  the full `set_breakpoint → continue → read_registers` round-trip on the development KVM
  host is a #680 (B6) follow-up, owned by the live-driver, not promoted here.

## Considered and rejected

- **Duplicate the stop-record parsing in `resume()`.** Two copies of the
  `message == "stopped"` → `stop_record_from` logic drift apart; factoring the one
  `_stop_from_records` helper keeps a single definition of "what counts as a stop."
- **Stop blocking inside `execute_mi_command` (return early on `^running`).** The blocking
  read is what *captures* the early stop in the first place; making it non-blocking would
  move the race, not remove it, and would regress the slow-breakpoint path that legitimately
  needs to wait.
- **Treat `transport_stall` as success when registers read back.** Masks the symptom, leaves
  the dropped-notification bug in place, and conflates a real silent link with a consumed
  stop.
