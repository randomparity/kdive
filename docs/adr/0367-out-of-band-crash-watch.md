# ADR 0367 — Repeat-until-crash (#984) as an out-of-band console watch, not an in-guest loop-runner

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** kdive maintainers

## Context

Issue #984 asked for a stress/repeat-until-crash-signal primitive for race-condition test
cases: a job that iterates a reproducer command while tailing the console for the
crash-signature regex, stopping and capturing on the first hit. It was filed scoped to compose
"audited in-guest execution (#909) + the readiness crash matcher."

Two decisions have since invalidated that premise:

- **#909 (bounded in-guest command execution) was closed** as SSH-equivalent. The v0.3.0
  release-readiness epic (#1199) adopted the governing principle *a capability earns an MCP
  tool only if it is out-of-band* — anything achievable over the guest's root SSH stays a
  documented prompt pattern, not a tool. The #998 SSH-equivalent tool proposals were closed on
  that basis.
- **[ADR-0366](0366-race-debugging-out-of-band.md)** resolved the sibling race-observation
  issue #986 as documentation and wrote the principle down, **explicitly assigning the
  reproducer loop to "the agent's own code run over root SSH"** and naming #984 as the
  separately-tracked primitive.

#984 is a **hybrid**. Its *command-loop* half is SSH-equivalent — the agent already runs its
reproducer over root SSH, and re-adding a kdive-driven in-guest command loop is precisely the
#909 capability the principle forbids. Its *crash-signature-on-the-console* half is genuinely
out-of-band: **a panic drops the guest's SSH channel**, so the agent cannot observe the crash
it just provoked from inside the guest. The serial console is the durable, out-of-band record —
and today nothing watches a *running* System's console for the crash signature (that match runs
only during boot readiness).

## Decision

Build **only the out-of-band half**: a new durable worker job `watch_for_crash` and its MCP
tool `control.watch_for_crash`. The tool watches a READY local-libvirt System's serial console
for the boot-readiness crash matcher (`_CRASH_SIGNATURE`) until a clamped wall-clock deadline,
returns on the **first** match past the watch's start offset with the redacted matched slice,
the matched signature, and elapsed-to-signal, and otherwise returns a "not fired" verdict at
the deadline. It returns the verdict inline in the job's `result_ref` (the ADR-0164 pattern,
as `check_ssh_reachable` does), with no new artifact row.

Because the start offset is snapshotted at worker pickup (queue latency and at-least-once
retries can both put a real panic *before* it), the deadline path probes domain liveness and
**never reports a healthy guest unless the domain is actually live**: a domain that exited with
no matching signature returns a distinct `exited_no_signature` verdict that routes the agent to
the full console. `deadline_s` is clamped to a modest cap (default 60s, max 300s) so a
pure-wait watch cannot hold a worker slot long enough to starve short lifecycle jobs on the
shared dispatch lane.

kdive does **not** run agent-supplied commands in the guest. The agent drives its own
reproducer loop over root SSH (ADR-0366); kdive supplies the one thing SSH cannot: catching the
crash on the console after SSH is gone. `_CRASH_SIGNATURE` is promoted to a public
`first_crash_signature` helper in `readiness.py` so readiness and the watch share one
definition.

This places `watch_for_crash` in the `control` toolset beside `force_crash` /
`diagnostic_sysrq` / `power` — the family whose members earn their existence by acting on (or
observing) a guest that SSH can no longer reach.

## Consequences

- The repeat-until-crash outcome is delivered without re-introducing in-guest command
  execution, staying inside the epic's out-of-band line and consistent with the #909 closure
  and ADR-0366.
- The agent owns the reproducer loop and therefore the iteration count; kdive reports
  elapsed-to-signal, the datum it can observe out-of-band. The issue's "iteration count at
  first signal" is reframed accordingly (recorded in the spec).
- A running System now has a first-class, deterministic crash-signature watch, closing the gap
  where the crash matcher only ran at boot readiness.
- One new job kind → one forward-only migration (`0069`) widening `jobs_kind_check`; no table
  or column change. No new artifact surface.
- `race-debugging.md` Route 3 gains a concrete tool where it previously said "until it lands,
  the loop is guest-side SSH."

## Alternatives considered

- **A full in-guest loop-runner (the issue as literally written).** kdive SSHes into the guest,
  runs the agent's command in a loop bounded by max-iterations + wall-clock, tails the console,
  returns the iteration count at the hit. Rejected: it re-introduces arbitrary in-guest command
  execution — the exact capability #909 was closed to remove — and crosses the epic's
  out-of-band line for the command-execution half. The command loop is SSH-equivalent (ADR-0366
  assigns it to the agent); only the console watch earns a tool. Fidelity to the literal
  "iteration count" criterion does not justify reversing a settled principle.
- **Resolve #984 as documentation, like #986.** Extend `race-debugging.md` so the agent loops
  its reproducer over SSH and greps the console artifacts itself. Rejected: unlike #986 (whose
  outcome was fully covered by existing drgn-live + tracepoints), #984's out-of-band half is a
  genuine gap — there is no deterministic, blocking crash-signature watch for a running System,
  only manual polling of console artifacts. Documenting a manual poll leaves the out-of-band
  capability unbuilt; ADR-0366 itself kept #984 as a tracked primitive rather than folding it
  into the docs resolution.
- **Emit a durable crash-window console artifact (like `diagnostic_sysrq`) instead of an inline
  verdict.** Rejected as redundant: the full console is already persisted by `console_rotate` /
  run console evidence and reachable via the `artifacts` tools. An inline, bounded, redacted
  matched slice (the `check_ssh_reachable` verdict pattern) answers "did it crash, on what
  signature, when" without duplicating console storage or adding a row and a store transaction.
- **A caller-supplied / custom signature pattern.** Deferred, not built: no user need is
  established, and the readiness matcher is the single source of truth the issue names. Adding a
  pattern knob now would be a speculative surface.
