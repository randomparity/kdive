# 0674 — Count live workers before declaring the stack fresh

## Status

Accepted (2026-09-22)

## Context

ADR-0482 grades builds reported by aux `/readyz` listeners, but the live-stack
preflight probes only worker slot 1. The host lifecycle can run slots 1 through 8.
Debt 0002 records the resulting false-fresh verdict for an unprobed worker.

## Decision

The live-stack preflight uses bounded, read-only host process-table snapshots to
identify exact `python -m kdive worker` commands by PID. It samples before and
after HTTP probes, compares the stable count with returned worker builds, and
adds an actionable `unknown` for failed, empty, changing, or mismatched inventory.
The existing worker-1 probe and per-build grading remain.

The count is local to the test host, matching the existing host-process live-stack
topology. It is not inferred from `KDIVE_WORKER_COUNT` in the test environment.
Each snapshot is time and output bounded; uncertainty fails toward ADR-0482's
existing `unknown` verdict. The probe returns its final validated PID set with
the verdicts. The live-stack fixture caches that exact set, rechecks worker
PIDs before reuse, and probes again when the fleet changes.

## Consequences

A normal single-worker host keeps its prior build verdict. A multi-worker host
without matching probes warns `unknown`, including when an extra worker reports
a different build. Unreadable or oversized process inventory also warns instead
of claiming freshness. Bracketing and cache rechecks detect fleet changes at
their observation points; they cannot make process starts atomic with a test.

## Considered & rejected

- **Read `KDIVE_WORKER_COUNT` in pytest.** verified: `scripts/live-stack/lib.sh`
  reads it only when starting workers, so the test process's value cannot prove
  the deployed count (issue #2652, source at 7bd18ecb0a86261f26ef0e94507c59fb253e104f).
- **Read only fixed systemd unit states.** judgment: fit — it misses an
  unmanaged running worker that can still take jobs.
- **Probe the eight possible ports blindly.** judgment: fit — a listener's
  absence cannot prove whether its worker is running on another bind.
