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

### 1. `run_process_runtime` establishes one connection at startup, within a 10-second budget

`open_pool_or_fail` opens the pool and then takes a single connection under
`POOL_OPEN_TIMEOUT_SECONDS`. The first connect now completes at startup, outside any
caller's acquire budget, and `body` is never handed a pool with zero connections. On a
healthy stack this costs a few milliseconds.

**One connection, not `open(wait=True)`.** `wait()` returns only once the pool holds
`min_size` connections, and the worker's pool is `min_size=2`. Waiting for that would make
the worker hard-fail whenever Postgres is *reachable but at `max_connections`* — a state it
previously started and drained jobs in — and every crash-loop restart would re-attempt
connections against the already-saturated server, amplifying the failure. The defect is a
pool with **zero** connections; one connection removes it, and the pool's background tasks
still fill to `min_size` as capacity frees up, exactly as before. Partial availability is
therefore not converted into an outage.

This changes startup semantics for all three processes: **a database that cannot be reached
within the budget now ends the process** (`psycopg_pool.PoolTimeout`) instead of starting a
process that serves nothing. That is the right shape here because none of the three can do
useful work without Postgres — the server's every handler needs it, the worker polls the job
queue, the reconciler loops over inventory — so "up but permanently unable to connect" is a
state with no value that merely defers the error to a confusing place.

It is also the supervision contract this repo already documented. ADR-0114 §4 ships
`Restart=on-failure` with `RestartSec=5` precisely so "a process that starts before its
backend is reachable retries rather than failing terminally." The retry loop lives in the
supervisor, not inside the process; warming at startup is what puts the failure where the
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
  At 10s the process fails on its own terms, with the cause in its logs, before the second
  probe. Sizing this up means budgeting against that 25s *minus* everything the process does
  before the open — interpreter start, imports, `config.validate`, `init_telemetry`.

The open moves *inside* `run_process_runtime`'s `try`, so a failed open still runs the
teardown: `secret_registry.clear()` and `pool.close()`. (`wait()` closes the pool itself on
timeout; `close()` is idempotent, and the registry clear was previously skipped entirely on
this path.)

### 2. The startup failure is a `CategorizedError`, not a bare `PoolTimeout`

`open_pool_or_fail` translates `psycopg_pool.PoolTimeout` into a `CategorizedError` with
`ErrorCategory.INFRASTRUCTURE_FAILURE`, chaining the original as `__cause__`.

This follows directly from decision 1 rather than being incidental to it. `__main__`'s only
handler is `except CategorizedError`, which routes a failure through
`_report_categorized_error` — an ERROR record on the ADR-0090 structured stdout floor,
redacted, carrying the message and actionable details, plus a stable
`exit_code_for_category`. An uncategorized raise gets none of that: a multi-line traceback
on stderr and a generic exit 1. Decision 1 turns "the backend is not up yet" into a
*routine* outcome, and a routine outcome that a deployment scraping JSON logs cannot see,
alert on, or distinguish from any other crash is not an acceptable one. The message and the
`KDIVE_DATABASE_URL` hint are what the operator docs tell readers to look for.

### 3. Count swallowed usage-recording failures

`UsageTrackingMiddleware._record` increments a `kdive_mcp_usage_recording_failures` counter
after the WARNING it already logs. ADR-0148's swallow is unchanged; what changes is that the resulting data loss
is now countable at `/metrics` rather than only greppable in a log stream.

This matters beyond the startup case decision 1 removes: the saturated-pool drop ADR-0148
knowingly accepted has always been silent too, and a usage table that is quietly missing
rows is worse than one that is visibly missing them, because analysis built on it looks
complete. The counter is the smallest thing that makes the accepted loss measurable.

The instrument is named and scoped like `mcp/middleware/exposure.py`'s
`kdive_mcp_tool_exposure_fail_open` / `kdive_mcp_provider_schema_projection_failures` — its
nearest precedent, and the same category of signal (a path that degraded silently by
design) — including carrying **no attributes**. The obvious label would be the tool name,
but that is the raw name off the client's `tools/call`: FastMCP resolves the tool *inside*
`call_next`, so an unknown one reaches the `except` branch with arbitrary content, and the
SDK enforces no cardinality limit. Labelling would let a single authenticated client grow
the metric store without bound precisely while recording is already failing. The tool name
is in the WARNING beside the increment, and in `tool_invocation.tool` for calls that land.

The increment runs *after* the log and inside `contextlib.suppress(Exception)`. ADR-0148's
swallow is unconditional, so the instrument observing a failure must not be able to become
the thing that fails the call — nor to cost the operator the diagnostic as well as the count.

Like the exposure counters, it is deliberately **not** added to the Grafana dashboard catalog:
`tests/deploy/grafana_catalog.py` walks a fixed list of telemetry modules and
`test_grafana_dashboard.py` asserts exact equality between the catalog and the dashboard's
referenced series, so cataloguing it would require a dashboard panel. It is scrapeable from
the aux `/metrics` endpoint either way; a panel is a follow-up, not a precondition for
making the drop visible. `operating/kubernetes.md` names the series, what a non-zero rate
means, and that it is the signal to size the server pool against — without which the
data-driven follow-up promised below could not actually be run.

## Consequences

- The `tool_invocation` row can no longer be lost to the process's *first* connection still
  being in flight. That is the systematic case — every process, every start — and it is the
  one #1527 reproduced.
- **A residual remains, and it is not one ADR-0148 weighed either: pool growth.**
  `psycopg_pool` charges a growth connect to the acquiring client, not to a background task
  (`getconn` queues the caller and waits inside the caller's own timeout while
  `_maybe_grow_pool` schedules the connect), and the startup warm-up establishes one
  connection. The server's pool is `create_pool`'s `min_size=1` default, so the second
  concurrent tool call — and any call arriving after psycopg's idle shrink has taken the
  pool back toward `min_size` — can still wait on a cold connect inside the middleware's
  1-second budget and lose its row. This is the same "pool has not finished connecting"
  condition, one connection later, not the saturated-`max_size` contention ADR-0148 accepted.
  It is *not* fixed here. Raising `min_size` would fix it, but pool sizing is a Postgres
  connection-budget decision across three process types with no bearing on the startup
  defect this ADR is about, and guessing a number is how one arrives at the wrong one.
  Decision 3's counter is the instrument to size it against: `kdive_mcp_usage_recording_failures`
  on a warm server measures exactly this residual, so the follow-up is data-driven rather
  than speculative.
- Every remaining loss mode — the growth residual above and the saturation ADR-0148 accepted
  on its merits — now increments that counter.
- **A Postgres outage at process start becomes a restart loop rather than a not-ready
  process.** Under systemd that is a retry every ~15s (`RestartSec=5` plus the 10s budget),
  which is what ADR-0114 §4 describes. Under Kubernetes it is `CrashLoopBackOff`, whose
  backoff grows to a 5-minute cap — so after a long outage a pod can take up to ~5 minutes
  to come back rather than recovering the instant the database does. That is the real cost
  of this decision. It is accepted: the outage itself dominates that lag, and
  `CrashLoopBackOff` with `PoolTimeout` in the logs is a more diagnosable state than a pod
  that is Running and permanently not-Ready.
- **The supervision premise had a gap on one shipped surface, now closed.** "The retry lives
  in the supervisor" held for systemd and Kubernetes but not for `docker-compose.yml`, which
  declared no `restart:` policy on `server`/`worker`/`reconciler`. `depends_on` protects only
  the first `up`; every later recreate during a backend outage would have left the container
  `Exited (1)` permanently, where before this change it came up and recovered on its own. The
  every long-running service gains `restart: unless-stopped`, guarded by a test, and
  `operating/docker-compose.md` gains the same startup paragraph the other two surfaces got.
  The backends (`postgres`, `minio`, `oidc`) are policed alongside the app tier, not just it:
  policing only the app services would make a host reboot *worse* than no policy at all,
  since they would come back and crash-loop forever against backends that stayed stopped —
  reading as transient while permanently unable to progress. The `migrate` and `minio-init`
  one-shots stay unpoliced by design.
- `/livez`, `/readyz`, and `/metrics` are unavailable for up to 10 seconds longer at
  startup, because the aux listener starts after the pool opens. Sized to stay inside the
  chart's `initialDelaySeconds: 5` plus one probe period, as above.
- **`SIGTERM` is ignored for up to the open budget when the database is unreachable.**
  `run_worker` and `run_reconciler` install their signal handlers before
  `run_process_runtime`, and those handlers only set an event that nothing awaits during the
  open. Previously the open returned immediately and the window was ~0; now a `systemctl
  stop`, `compose down`, or pod delete that coincides with a database outage waits up to 10s,
  repeating on each crash-loop attempt. It stays well inside Kubernetes' 30s default
  termination grace, so nothing is lost or corrupted — it slows drains and rollouts during an
  outage. Accepted rather than fixed: racing the open against the stop event adds startup
  concurrency to buy back at most ten seconds inside a grace period three times that long.
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
  Warming at startup removes the condition instead of tolerating it, for every consumer of
  the pool rather than this one middleware.
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
- **`open(wait=True)` at `psycopg_pool`'s 30-second default timeout.** Rejected twice over:
  30s is past the kubelet's t≈25s kill, so the failure would surface as a killed container
  rather than a logged cause; and `wait()` blocks on the full `min_size`, the
  partial-availability hazard decision 1 rejects.
