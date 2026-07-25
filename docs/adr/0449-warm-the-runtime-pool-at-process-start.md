# ADR 0449 — Warm the runtime pool at process start, and count dropped usage rows

- **Status:** Accepted
- **Date:** 2026-07-24
- **Amends:** [ADR-0148](0148-rbac-scoped-tool-exposure.md) §3's best-effort
  usage-recording clause. The swallow itself is retained unchanged — a recording failure
  still never fails or delays a tool call. What is added is the condition ADR-0148 never
  considered (a pool with *no* connections yet, as opposed to a saturated one) and a counter
  so the drops it does accept are visible outside the log stream.
- **Depends on:** [ADR-0114](0114-production-release-readiness.md) §4 (the
  `Restart=on-failure` supervision contract this relies on), [ADR-0005](0005-postgres-object-store-state.md)
  (the shared `AsyncConnectionPool`), [ADR-0090](0090-opentelemetry-adoption-service-health.md) (the meter the
  new counter is created on).

## Context

`run_process_runtime` — the shared entry point for the server, worker, and reconciler —
opened the process's `AsyncConnectionPool` with a bare `await pool.open()`.
`psycopg_pool`'s `open()` defaults to `wait=False`: it returns *before any connection
exists* and leaves the first connect to a background task. Every caller downstream is
therefore handed a pool that may report zero available connections.

For the server that lands on a live edge. `build_app` registers `UsageTrackingMiddleware`
at its 1-second `acquire_timeout`, and `_record` swallows every failure into a WARNING by
design (ADR-0148 §3). If the pool's first connect is still pending when a tool call
returns, the acquire exceeds the budget, raises `PoolTimeout`, and the `tool_invocation`
row is lost with nothing but a log line to say so.

This is not hypothetical. #1527 diagnosed exactly this construction in the test suite: an
instrumented repro logged `SWALLOWED PoolTimeout: couldn't get a connection after 1.00 sec`,
and the real test file failed 14 times in 240 runs under saturation before the fix and 0 in
624 after. PR #1536 fixed the *test* manifestation by warming the pool there; it also removed
the suite's only accidental reproducer of the production analogue, and filed this as #1535.

Two things are worth separating. ADR-0148 deliberately accepted dropping a row **when the
pool is saturated** — contention on a working pool, where the alternative is delaying a
response. It did not weigh a pool that has *no connections at all because startup has not
finished*. That is a startup-ordering defect, not a contention trade-off, and it is fixable
without touching the contract ADR-0148 settled.

The production window is narrow — app build, uvicorn listen, client connect, and token
verification usually outlast the first connect — so this is a correctness fix, not a claim
of frequent loss.

## Decision

### 1. `run_process_runtime` opens the pool with `wait=True` and a 10-second budget

`await pool.open(wait=True, timeout=POOL_OPEN_TIMEOUT_SECONDS)`. The first connect now
completes at startup, outside any caller's acquire budget, and `body` is never handed a
pool with zero connections. On a healthy stack this costs a few milliseconds.

This changes startup semantics for all three processes: **a database that cannot be reached
within the budget now ends the process** (`psycopg_pool.PoolTimeout`) instead of starting a
process that serves nothing. That is the right shape here because none of the three can do
useful work without Postgres — the server's every handler needs it, the worker polls the job
queue, the reconciler loops over inventory — so "up but permanently unable to connect" is a
state with no value that merely defers the error to a confusing place.

It is also the supervision contract this repo already documented. ADR-0114 §4 ships
`Restart=on-failure` with `RestartSec=5` precisely so "a process that starts before its
backend is reachable retries rather than failing terminally." The retry loop lives in the
supervisor, not inside the process; `wait=True` is what puts the failure where the
supervisor can act on it. `psycopg_pool`'s own documentation for `wait()` frames the choice
the same way: use it "if you prefer your program to terminate in case the environment is not
configured properly, rather than trying to stay up the hardest it can."

**The timeout is 10 seconds**, chosen to sit in the gap between the two budgets that bound it:

- **Above**, by an order of magnitude, the 1-second acquire budget that this ADR exists to
  keep the connect out of. A cold page cache, a contended host, or a TLS handshake to a
  cross-AZ Postgres can plausibly consume a second or two; ten cannot be reached by anything
  short of a backend that is genuinely unavailable.
- **Below** the Helm chart's liveness budget. `kdive.auxProbes` sets
  `initialDelaySeconds: 5` and `periodSeconds: 10` against `/livez`, and Kubernetes'
  `failureThreshold` defaults to 3 — so probes at t=5, 15, 25 kill the container at t≈25s.
  The aux listener does not start until the pool is open, so a longer budget would mean the
  kubelet killing the container mid-open with a probe failure that cannot explain itself.
  At 10s the process raises `PoolTimeout` and exits on its own terms, with the cause in its
  logs, before the second probe.

The open moves *inside* `run_process_runtime`'s `try`, so a failed open still runs the
teardown: `secret_registry.clear()` and `pool.close()`. (`wait()` closes the pool itself on
timeout; `close()` is idempotent, and the registry clear was previously skipped entirely on
this path.)

### 2. Count swallowed usage-recording failures

`UsageTrackingMiddleware._record` increments a
`kdive_mcp_usage_recording_failures` counter, labelled by `tool`, alongside the WARNING it
already logs. ADR-0148's swallow is unchanged; what changes is that the resulting data loss
is now countable at `/metrics` rather than only greppable in a log stream.

This matters beyond the startup case decision 1 removes: the saturated-pool drop ADR-0148
knowingly accepted has always been silent too, and a usage table that is quietly missing
rows is worse than one that is visibly missing them, because analysis built on it looks
complete. The counter is the smallest thing that makes the accepted loss measurable.

The instrument is named and scoped like `mcp/middleware/exposure.py`'s
`kdive_mcp_tool_exposure_fail_open` / `kdive_mcp_provider_schema_projection_failures` — its
nearest precedent, and the same category of signal (a path that degraded silently by
design). Like those, it is deliberately **not** added to the Grafana dashboard catalog:
`tests/deploy/grafana_catalog.py` walks a fixed list of telemetry modules and
`test_grafana_dashboard.py` asserts exact equality between the catalog and the dashboard's
referenced series, so cataloguing it would require a dashboard panel. It is scrapeable from
the aux `/metrics` endpoint either way; a panel is a follow-up, not a precondition for
making the drop visible.

## Consequences

- The `tool_invocation` row can no longer be lost to a pool that has not finished opening.
  The remaining loss modes are the ones ADR-0148 accepted on their merits, and all of them
  now increment a counter.
- **A Postgres outage at process start becomes a restart loop rather than a not-ready
  process.** Under systemd that is a retry every ~15s (`RestartSec=5` plus the 10s budget),
  which is what ADR-0114 §4 describes. Under Kubernetes it is `CrashLoopBackOff`, whose
  backoff grows to a 5-minute cap — so after a long outage a pod can take up to ~5 minutes
  to come back rather than recovering the instant the database does. That is the real cost
  of this decision. It is accepted: the outage itself dominates that lag, and
  `CrashLoopBackOff` with `PoolTimeout` in the logs is a more diagnosable state than a pod
  that is Running and permanently not-Ready.
- `/livez`, `/readyz`, and `/metrics` are unavailable for up to 10 seconds longer at
  startup, because the aux listener starts after the pool opens. Sized to stay inside the
  chart's `initialDelaySeconds: 5` plus one probe period, as above.
- No schema change, no migration, no MCP or RBAC surface change.

## Alternatives considered

- **Widen `UsageTrackingMiddleware`'s 1-second acquire budget.** Rejected: `_record` runs on
  the response path, after the handler returns but before the middleware yields the result,
  so the budget is a direct latency ceiling on every tool call. Widening it to cover a cold
  connect would let a saturated pool add seconds to responses — trading a rare lost row for
  a routine slowdown, and undoing the one thing ADR-0148 §3 was most careful about. Keep the
  steady-state budget tight; fix the startup condition at startup.
- **Make only the *first* acquire tolerant and keep the steady-state budget.** Achieves the
  same end as decision 1 with strictly more machinery — per-instance first-call state in a
  middleware that is otherwise stateless, and a warm-up window whose boundary is fuzzy.
  `wait=True` removes the condition instead of tolerating it, in one line, for every
  consumer of the pool rather than this one middleware.
- **Leave `wait=False` and record the accepted risk against ADR-0148** (the issue's second
  option). Rejected: the risk ADR-0148 accepted was a *contention* trade-off with a real
  benefit on the other side of it. A zero-connection pool has no such upside — nothing is
  gained by starting the body early — so there is nothing to trade, only a defect to record.
- **Start the aux listener before opening the pool**, so `/livez` and `/readyz` stay up
  during the wait and a down database yields a not-ready process instead of an exit. This is
  a coherent design, and it is the one to revisit if the restart-loop cost above ever bites.
  Not taken here: it inverts ADR-0114 §4's supervision contract, it makes "up but useless"
  a supported steady state for three processes that cannot work without Postgres, and it
  restructures the runtime's startup and teardown ordering — a materially larger change than
  the defect warrants, on a path where the 10-second budget already keeps the health surface
  inside the probe's `initialDelaySeconds`.
- **`wait=True` with `psycopg_pool`'s 30-second default timeout.** Rejected on the liveness
  arithmetic above: 30s is past the kubelet's t≈25s kill, so the failure would surface as a
  killed container rather than a logged `PoolTimeout`.
