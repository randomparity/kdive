# ADR 0153 — A guest-agent exit with neither exitcode nor signal is a failure, not success

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** KDIVE maintainers
- **Builds on (does not supersede):** [ADR-0078](0078-object-store-in-target-install-seam.md)
  (the in-target `guest-exec`/`guest-exec-status` seam this hardens).

## Context

`GuestAgentExec` (`src/kdive/providers/remote_libvirt/guest/agent.py`) runs every
worker-composed, allowlisted in-guest command over the qemu-guest-agent two-phase
`guest-exec`/`guest-exec-status` protocol and derives the command's exit status from the
reaped-process status reply. The derivation, `_exit_status`, mapped three reply shapes:

- `exitcode` present ⇒ that code (normal exit);
- no `exitcode`, `signal` present ⇒ `128 + signal` (a killed process — OOM, timeout-kill,
  SIGSEGV — never reads as success); and
- **neither field ⇒ `0`** (treated as a clean exit).

For a `guest-exec` process that the agent reports as `exited: true`, a reply carrying *neither*
`exitcode` nor `signal` is **abnormal** — the agent normally reports exactly one of the two for
a reaped process. Defaulting that degraded shape to `0` converts an unknown outcome into a
**false pass**: any in-guest command (clone, `make`, config write, build-id read, upload) whose
status comes back degraded is silently treated as having succeeded.

This was surfaced by a live black-box build campaign on `ub24-big-build` (`kind =
ephemeral_libvirt`), where the guest-agent channel was demonstrably flaky under load (a sibling
run failed with `agent unreachable`) and a build proceeded past a `git fetch` that had left no
`FETCH_HEAD` — a masked fetch failure (issue [#517](https://github.com/randomparity/kdive/issues/517);
build runs `6b99aa8d`, `d39a408e`). A degraded `guest-exec-status` reply is therefore a live
possibility, and the masking turns it into a corrupt build that reads as green.

## Decision

We will **treat an `exited: true` reply with neither `exitcode` nor `signal` as an error, not a
success.** `_exit_status` raises `CategorizedError(INFRASTRUCTURE_FAILURE)` —
"guest agent reported a process exit without an exit code or signal" — for that shape instead of
returning `0`. A command whose true outcome cannot be determined must not read as succeeded.

- The normal-exit path (`exitcode` present, including a non-zero failing code) is unchanged.
- The signal-kill mapping (`128 + signal`) is unchanged.
- The raise is `INFRASTRUCTURE_FAILURE`, matching the seam's existing classification for a
  malformed/undecodable agent reply (`_decode_capture`, the malformed-status-reply guard in
  `_await_exit`): a degraded exit reply is the same class of "the agent gave us something we
  cannot trust," not a `transport_failure` (the agent *did* answer) and not a command-level
  `build_failure` (we never observed the command's own exit code).

## Consequences

- **No new field, column, or migration.** The change is one branch in a pure helper; callers
  already propagate `CategorizedError` from `run()`. The failing build now surfaces a categorized
  infrastructure failure rather than continuing past a command of unknown outcome.
- **Failure contract change.** A caller that previously received `exit_status == 0` for the
  degraded shape now receives a raised `CategorizedError(INFRASTRUCTURE_FAILURE)`. Every existing
  caller already treats a raised `CategorizedError` from the exec seam as the command failing, so
  this aligns the degraded shape with the rest of the failure surface; nothing relied on the old
  silent `0` except the masking itself.
- **No false negatives introduced.** The only newly-failing case is the one the agent itself
  reports abnormally (exited, but with no determinable outcome). A genuinely successful command
  carries `exitcode: 0`; a genuinely-failed command carries a non-zero `exitcode` or a `signal`;
  both keep their existing, correct mappings.
- **Companion defects are out of scope.** The same campaign surfaced two adjacent defects tracked
  separately: the build-clone discarding the masked fetch's stderr
  ([#518](https://github.com/randomparity/kdive/issues/518)) and the build-VM readiness probe
  checking only for a default route ([#519](https://github.com/randomparity/kdive/issues/519)).
  This ADR is the root-cause masking fix only.

## Alternatives considered

- **Return a sentinel non-zero status (e.g. `-1` or `255`) instead of raising.** Rejected: a
  numeric status implies the command's own exit code was observed; it was not. Callers compare
  `exit_status` against expected codes and would have to special-case a magic sentinel, whereas
  every caller already handles a raised `CategorizedError`. Raising states honestly that the
  outcome is unknown.
- **Map it to `transport_failure`.** Rejected: the agent answered, and the status poll completed;
  the channel is not the failure. The reply is *structurally* unusable, which is exactly what the
  seam's other `INFRASTRUCTURE_FAILURE` guards (non-JSON reply, non-object reply, undecodable
  capture, malformed status reply) already mean. Reusing that category keeps the seam's error
  taxonomy coherent.
- **Keep returning `0` but log a warning.** Rejected: a warning does not stop the build, so the
  corrupt artifact still ships and still reads as green — this is precisely the false-pass the
  issue reports.
