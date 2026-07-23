# ADR 0429 — Worker-side remote console read seam

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0235](0235-per-run-console-evidence.md) (the per-Run
  boot-window `ConsoleSnapshotter` this reader sits beside),
  [ADR-0095](0095-reconciler-remote-console-collector.md) (the reconciler-leader console collector
  that produces the S3 parts), [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the
  mandatory-redaction invariant), [ADR-0241](0241-per-run-console-slicing.md) (part-index window
  slicing).
- **Issue:** [#1431](https://github.com/randomparity/kdive/issues/1431) (part of #1423).

## Context

A worker cannot read a remote System's console on demand. The remote console is streamed
out-of-band by a reconciler-resident `ConsoleCollector` into rotating, redacted S3 parts under a
single-leader lock (`CONSOLE_HOSTING_LEADER`, ADR-0095), and the only worker-side reader that
exists — `RemoteLibvirtConsoleSnapshotter` (ADR-0235) — serves exactly one shape: a per-Run boot
window, assembled once at boot completion into an immutable artifact.

Two control tools (remote `diagnostic_sysrq` and `watch_for_crash`, delivered in #1435) need a
different shape: a short console read on a *running* System. The boot snapshotter's contract is
`ConsoleSnapshotter` (`providers/ports/console.py`), and it is best-effort in two ways those
consumers cannot inherit:

- `mark_boot_window` "never raises: the handler treats a failure as mark `0`."
- `snapshot` "never raises for an absent or partial console — capture is best-effort and must not
  fail the boot," and may trail the collector's pump latency.

Silently returning stale-or-empty is correct when the alternative is failing a boot. It is wrong
for a tool whose entire output is the console it just read: an empty artifact is indistinguishable
from "the kernel printed nothing." The two consumers also want *different* failure handling — a
one-shot post-SysRq read should surface an error, while a polled crash watch tolerates retry — so
assuming the boot-window best-effort semantics generalize is the main way this goes wrong.

A second, subtler hazard: the collector runs in the reconciler under the single-leader lock. A
worker read of the S3 parts alone cannot tell a *silent* console (leader alive, guest printed
nothing) from an *un-pumped* one (no live leader, so nothing is being uploaded at all). The seam
must make that distinction observable.

## Decision

**Add a worker-side strict console read seam, `RemoteConsoleReader`, distinct from the best-effort
`ConsoleSnapshotter`, that reads a running System's console over a caller-specified part-index
window and reports an explicit freshness and error contract.**

1. **Port** — `RemoteConsoleReader.read_window(conn, system_id, start_index=0) -> ConsoleWindowRead`
   in `providers/ports/console.py`. `ConsoleWindowRead` carries `data` (redacted bytes assembled
   from parts with index `>= start_index`), `next_index` (a poll cursor: highest observed index
   `+ 1`, or the requested `start_index` when the window is empty, so a poller never rewinds), and
   `pumped` (whether a console-hosting leader is alive).

2. **Freshness — `pumped` flag, not an exception.** The reader probes `pg_locks` across all
   backends for the `CONSOLE_HOSTING_LEADER` session advisory lock via a new
   `session_advisory_lock_held(conn, name)` helper (`db/locks.py`). `pumped=False` with empty
   `data` means "could not read"; `pumped=True` with empty `data` is a genuinely silent console.
   Exposing this as an observable state (rather than raising) lets each consumer choose: the
   one-shot SysRq read treats `pumped=False` as an error, the polled crash watch retries. This is
   how the seam "offers both contracts explicitly" rather than baking one policy in.

3. **Errors — propagate, do not swallow.** A part-store or database read failure propagates out of
   `read_window`; it is *not* caught and returned as empty (the opposite of the snapshotter). An
   unreachable store therefore never masquerades as a successful read of a silent console.

4. **Redaction at the seam.** The assembled bytes pass a fresh `Redactor` before return, so the
   mandatory-redaction invariant (ADR-0027) holds at this seam regardless of how the underlying
   parts were produced, and a secret registered after a part sealed is still caught. The parts are
   already redacted at collection, so this is defence-in-depth, not the sole guarantee.

5. **Boot-window snapshotter unchanged.** `RemoteLibvirtConsoleSnapshotter` and its
   `ConsoleSnapshotter` contract are untouched; the new reader reuses `RemoteConsolePartStore`'s
   read-only `list_part_indices`/`assemble` methods and adds no new part-write path.

The reader injects every host/store/probe seam so it is unit-testable without a libvirt host, an
object store, or a Postgres backend; `build_remote_console_reader` constructs the production
instance from the environment object store. Wiring the two consumers onto it is #1435's scope —
this ADR delivers the seam only.

## Consequences

- The two remote control tools in #1435 get a read primitive whose contract matches their needs:
  distinguishable emptiness, surfaced read failures, and a poll cursor for the crash-watch window.
- A `pumped=True` read is only as fresh as the collector's pump latency — the seam reports pump
  *liveness*, not "the console is caught up to this instant." That is inherent to the out-of-band
  S3-parts design (ADR-0095) and callers that need the freshest tail poll again on the cursor.
- `session_advisory_lock_held` is a general cross-backend advisory-lock probe; it is introduced for
  the leader-liveness check but is not console-specific.

## Alternatives considered

- **Reuse `ConsoleSnapshotter` / extend it with a `strict` flag.** Rejected: its documented "never
  raises, best-effort" contract is load-bearing for the boot path; a mode flag would make one port
  carry two contradictory error contracts, and the boot handler would have to prove it never
  passes the strict flag. A separate port keeps each contract single-meaning.
- **Raise on an un-pumped console instead of a `pumped` flag.** Rejected: the polled crash-watch
  consumer expects intermittent no-leader windows during failover as a normal, retryable state;
  routing that through exceptions would turn expected control flow into error handling. A store
  failure (genuinely exceptional) still propagates.
- **Query pump liveness from the S3 parts (e.g. a heartbeat object).** Rejected: it adds a write on
  the collector's hot path for a signal the leader lock already carries authoritatively, and a
  stale heartbeat could not distinguish a slow pump from a dead one the way the lock does.
