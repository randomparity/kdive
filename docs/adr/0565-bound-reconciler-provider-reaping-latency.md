# 0565 — Bound reconciler reaping latency with a lane budget and a reaper connect gate

## Status

Accepted (2026-08-17)

## Context

Two reconciler lanes await a provider call from inside a transaction that holds an advisory lock,
and neither the call nor the lane has a deadline:

- the ADR-0556 capture lane — `_dispatch_capture` inside `_reclaim_capture`'s fenced transaction,
  up to `capture_reap_batch` (default 25) sequential calls per pass;
- the ADR-0562 dump-volume lane — `reaper.delete_dump_volume` inside `_delete_if_still_orphaned`'s
  `(SYSTEM, system_id)`-locked transaction.

`_run_repair_plan` runs the lanes sequentially, one pooled connection each, with no per-lane
deadline. One unreachable declared host therefore holds a connection idle-in-transaction and delays
every later lane in the pass — allocation expiry, System repair, artifact GC.

The remote-libvirt reapers fan out over the whole declared fleet (`map_over_fleet` /
`find_over_fleet`), opening one connection per host, and `open_libvirt_protocol` is a bare
`libvirt.open(uri)`. libvirt's remote driver extracts a fixed set of URI parameters — `name`,
`command`, `socket`, `auth`, `sshauth`, `netcat`, `keyfile`, `pkipath`, `known_hosts`,
`known_hosts_verify`, `tls_priority`, `mode`, `proxy`, `no_sanity`, `no_verify` — and no timeout is
among them. A connect timeout has been an upstream wishlist item, not a knob. So the worst case for
one unreachable host is the operating system's TCP connect timeout (`tcp_syn_retries`, ~130 s on
Linux by default), once per unreachable declared host, and it is not a value an operator can tune.
ADR-0562 disclosed exactly this and named the fix direction — give the transport a connect timeout —
without deciding it. ADR-0556 disclosed the same residual for the capture lane. #1980 owns the
decision for both.

The obvious fix is unavailable. Wrapping the provider call in `asyncio.timeout` does not bound
provider mutation: the reapers drive synchronous libvirt clients through `asyncio.to_thread`, so
cancelling the await abandons the worker thread rather than stopping it. The fenced transaction
would then end — releasing the per-job ownership fence or the System lock — while the provider call
was still mutating host state. ADR-0556 is explicit that lock release alone is not evidence that
provider mutation stopped, so that trade buys latency at the cost of the ownership violation the
fence exists to prevent.

Two facts constrain what is left. The lock must be held across the call: ADR-0556 requires the fence
from before the reaper inspects host state through the completion write, and ADR-0562 requires the
System lock across the classification and the delete. And a `lock_timeout` does not help, for
ADR-0502's reason — it bounds a waiter's wait, not a holder's hold.

## Decision

Bound the two lanes with two limits at two different scopes. Neither cancels an in-flight provider
call.

**1. A per-lane pass budget, consulted only between candidates.** `reap_orphaned_captures` and
`reap_orphaned_dump_volumes` each take a `budget: timedelta`, start a monotonic deadline at the top
of the lane, and check it **before** opening the next candidate's transaction. A spent budget ends
the lane's pass and returns the count reclaimed so far; the unattempted candidates are re-derived on
the next pass. The check never runs while a transaction is open, so no transaction can be ended by
the budget while a provider call may still be mutating host state — that is the property #1980
requires be proved by a test rather than asserted in a comment, and
`tests/reconciler/cleanup/test_provider_reaping_budget.py` proves it against a real fence: the fake
reaper observes, from a second connection, that the fence is still held after the budget has
expired, and observes it from work shielded against cancellation, so an `asyncio.timeout` rewrite of
this decision reddens the test.

`KDIVE_RECONCILER_LANE_BUDGET_SECONDS`, default 30, is the operator's knob.

- **Unit** — seconds.
- **Reference clock** — the reconciler process's monotonic clock. Not the database clock: this
  bounds in-process work, not a row predicate.
- **Scope** — per lane, per reconciler pass. The capture lane and the dump-volume lane each get a
  full budget; the budget is not shared across a pass.
- **Consequence of violation** — the lane returns after the candidate in flight completes, having
  attempted fewer candidates than its batch allows. No transaction is cancelled, no candidate is
  abandoned mid-call, and the shortfall is not counted as a failure. The lane logs at INFO how many
  candidates it left unattempted.
- **Recovery action** — none is required: the next pass re-derives the remaining candidates. An
  operator whose backlog is not draining raises the budget or the reconcile interval.

**2. A bounded reachability gate on every remote-libvirt reaper connection.** `open_libvirt_reaper`
— the opener every fleet-fan-out reaper uses — first opens and immediately closes a plain TCP
connection to the URI's host and port with a bounded timeout. A host that does not accept within the
timeout raises `TRANSPORT_FAILURE` from the opener, which `_enter_host` already isolates as the
unreachable-host case it logs and skips. libvirt is never called for such a host, so the OS SYN
retry budget is never entered.

`KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS`, default 5, is the operator's knob.

- **Unit** — seconds.
- **Reference clock** — the host kernel's socket timer, via the Python socket timeout.
- **Scope** — per host, per reaper connection attempt. A fan-out over N declared hosts can spend it
  N times, so one provider call inside a fenced transaction is bounded at
  `connect_timeout × declared_hosts` for the connect portion, plus the reachable host's RPC time.
- **Consequence of violation** — that host is treated as unreachable for this call: logged and
  skipped, the fan-out continues to the next declared host. The capture lane defers the row behind
  its backoff deadline; the dump-volume lane leaves the volume for the next pass. Neither is counted
  as a fault.
- **Recovery action** — none is required by a caller. An operator on a genuinely slow-but-reachable
  fleet raises the timeout; one whose hosts are down fixes the host or removes it from the declared
  inventory.

The gate is scoped to the reaper opener rather than to `open_libvirt_protocol`, because the
reconciler is where an unreachable declared host is routine and unattended. A worker op runs against
one host the caller has already selected and has its own job lease; widening the gate to every
remote-libvirt path would change every provider plane's failure timing for a hazard #1980 does not
report.

Both lanes get both limits. The lane budget applies to each lane directly. The connect gate applies
to every connection the shared reaper seam (`remote_libvirt_reaper_connections`) opens, which is
what the dump-volume reaper uses today and what #1947's remote capture reaper will use when it
lands.

## Consequences

- One unreachable declared host costs the capture lane `connect_timeout × declared_hosts` per
  candidate instead of ~130 s per host, and cannot consume more than the lane budget plus one
  candidate of the pass. The later lanes in `_run_repair_plan` — allocation expiry, System repair,
  artifact GC — are delayed by that bounded amount rather than an untunable one.
- **The gate bounds the connect, not the call.** A host that completes the TCP handshake and then
  stalls — in the TLS handshake, or in a wedged libvirtd's RPC — is still unbounded, and still holds
  its transaction for as long as it stalls. What limits the blast radius there is the lane budget:
  such a host costs the pass one candidate, not the whole batch. Bounding a stalled RPC needs a
  terminable provider operation, which is ADR-0558's supervised-child shape, scoped to the
  `capture_traffic` job handler; extending it to reaping is a larger decision than #1980's hazard
  justifies and is not taken here.
- **The gate adds one TCP connect per host per reaper call.** On a reachable host that is an extra
  connection libvirtd accepts and sees closed before the TLS handshake, which its log records. That
  noise is the price of not entering the kernel's SYN retry budget, and it is bounded by the number
  of reaper calls a pass makes.
- **The gate is a check-then-act.** A host that accepts TCP at probe time and dies before
  `libvirt.open` is back to the unbounded case. The window is one round trip wide and the outcome is
  the pre-existing behavior, so the gate is strictly an improvement rather than a guarantee.
- **A permanently failing first dump-volume candidate can now truncate the lane silently.** The
  dump-volume lane is stateless — it re-lists volumes in provider order every pass — so a volume
  that always consumes the budget starves the ones behind it. Before this decision that volume
  stalled the whole pass instead, so this is not a regression, but the INFO line naming the
  unattempted count is the whole of the signal, the same drift hazard ADR-0562 already discloses for
  its per-System skips. The capture lane does not share it: every attempted candidate writes a
  backoff deadline, so a failing row sorts behind the untouched ones on the next pass.
- **Two knobs, not one.** They bound different scopes and differ by an order of magnitude in
  practice, so collapsing them would make one of the two meaningless. Each carries the full
  five-part limit contract above, in the ADR, in the generated config reference, and in the code
  that reads it.
- The `reaped_captures` and `reaped_dump_volumes` counters keep their meaning: a candidate the
  budget left unattempted is not counted, exactly as a deferred or declined one is not.
- No migration, no schema change, no MCP tool-surface change, no RBAC change. Two additive settings,
  one additive `ReconcileConfig` field, and one signature change on each of the two lane functions
  (a required keyword `budget`).

## Considered & rejected

- **`asyncio.timeout` / `asyncio.wait_for` around the provider call.** Cancels the await, not the
  synchronous libvirt work running in a `to_thread` worker. The transaction unwinds and releases the
  ownership fence or the System lock while the abandoned thread is still mutating host state —
  precisely what ADR-0556 forbids. It trades a latency problem for an ownership violation.
- **Do nothing; keep the residual.** The capture sweep is explicitly designed around hosts that may
  be unreachable, and its per-row backoff assumes a pass completes in a bounded time so the deadline
  it writes means something. An untunable ~130 s per unreachable host, up to 25 times per pass,
  makes that assumption false on exactly the fleets the sweep exists for.
- **A `statement_timeout` on the reconciler connection.** Bounds a SQL statement, and the hold here
  is spent in a provider call between statements, with the connection idle-in-transaction. It would
  not fire.
- **A `lock_timeout`.** Bounds how long a waiter waits, not how long a holder holds — ADR-0502's
  reason, restated by ADR-0562. The sweep is the holder.
- **A connect timeout as a libvirt URI parameter.** libvirt's remote driver extracts a fixed
  parameter set and no timeout is in it; an unrecognized parameter is passed through to the back end
  rather than honored. There is nothing to set.
- **Extending ADR-0558's supervised child process to reaping.** It is the only shape that bounds a
  stalled RPC rather than a stalled connect, and it is genuinely more than #1980's hazard needs: a
  supervisor, a quiescence protocol, and a spool per reaper call, for a lane whose dominant failure
  is a host that never answers. Recorded as the escalation if the connect gate proves insufficient.
- **Deriving the lane budget from the reconcile interval instead of a new setting.** Ties the two
  together: an operator lengthening the interval to reduce load would silently lengthen the hold on
  the System lock that every `runs.bind` waits behind. The knobs answer different questions.
- **Applying the connect gate to every remote-libvirt connection.** Changes the failure timing of
  every provider plane — provision, install, console, retrieve — for a hazard reported only against
  the reconciler, and each of those planes runs against a single host the caller already selected.
  Left to whichever decision reports a problem there.
