# Bound provider-call latency in the reconciler's two host-state reaping lanes (#1980)

Decision record: [ADR-0565](../../adr/0565-bound-reconciler-provider-reaping-latency.md).
Prior disclosures of the residual this closes: [ADR-0556](../../adr/0556-reclaim-orphaned-captures-across-providers.md),
[ADR-0562](../../adr/0562-host-dump-volume-capture-lease-fence.md).

## Problem

`reap_orphaned_captures` and `reap_orphaned_dump_volumes` each await a provider call from inside a
transaction holding an advisory lock, with no deadline on the call and no deadline on the lane. One
unreachable declared libvirt host holds a pooled Postgres connection idle-in-transaction for the
operating system's TCP connect timeout (~130 s), once per unreachable host per provider call, and
`_run_repair_plan` runs the lanes sequentially, so every later lane in the pass waits behind it.

An `asyncio.timeout` around the call is not available as a fix: the reapers drive synchronous
libvirt clients through `asyncio.to_thread`, so cancelling the await abandons the worker thread and
the transaction ends — releasing the ownership fence or the System lock — while the provider call is
still mutating host state.

## Requirements

Each is a criterion from #1980, restated so it can be failed against.

- **R1** — An unreachable provider host cannot hold a reconciler connection idle-in-transaction for
  longer than a bound that is (a) stated with its full five-part limit contract and (b) settable by
  an operator through a `KDIVE_*` setting.
- **R2** — No bound introduced by this change ends a transaction while a provider call may still be
  mutating host state. Proved by an automated test that reddens if the implementation is rewritten
  to cancel an in-flight call, not by a comment.
- **R3** — Both the ADR-0556 capture lane and the ADR-0562 dump-volume lane are covered.
- **R4** — The residual comments in `provider_reaping.py` that cite #1980 are updated to describe
  the bound that now exists, and no longer describe an unbounded call.
- **R5** — `just ci` is green.

## Design

Two limits at two scopes, neither of which cancels an in-flight provider call. ADR-0565 records the
decision and the alternatives; this section states the shape the implementation must have.

### Limit 1 — per-lane pass budget

`reap_orphaned_captures(..., budget: timedelta)` and `reap_orphaned_dump_volumes(..., budget:
timedelta)` each start a monotonic deadline at the top of the lane and consult it **only between
candidates**, before opening the next candidate's transaction. A spent budget ends the lane's pass
and returns the count reclaimed so far.

The placement is the whole of R2: the check is lexically outside every `conn.transaction()` block in
both lanes, so a spent budget can only prevent the *next* transaction from opening. It can never
unwind one that is open.

Setting: `KDIVE_RECONCILER_LANE_BUDGET_SECONDS`, integer seconds, default 30, read by the
`reconciler` and `server` processes (`ops.reconcile_now` runs `reconcile_once` too, so a brake set
on only one of them would leave the other unbounded). Reaches the lanes through a new
`ReconcileConfig.lane_budget` field.

Full limit contract, which must appear in the ADR, the setting's `help`, and the docstring of each
lane: unit **seconds**; reference clock the **reconciler process's monotonic clock**; scope **per
lane, per pass** (each lane gets a full budget, not a share of one); consequence of violation **the
lane returns after the in-flight candidate completes, having attempted fewer candidates than its
batch allows — not counted as a failure, logged at INFO with the unattempted count**; recovery
action **none required, the next pass re-derives the remainder; an operator whose backlog is not
draining raises this or the reconcile interval**.

A budget of zero or less is treated as no budget at all — every candidate is attempted. The lane
must not degrade to "attempt nothing", which would silently disable both sweeps.

### Limit 2 — reaper connect gate

`open_libvirt_reaper` — the opener every fleet-fan-out reaper uses — performs a bounded TCP connect
to the URI's host and port and closes it, before calling `libvirt.open`. A host that does not accept
within the timeout raises `CategorizedError(TRANSPORT_FAILURE)`, which `_enter_host` already
isolates as the unreachable-host case it logs and skips; libvirt is never called for that host, so
the kernel's SYN retry budget is never entered.

Setting: `KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS`, integer seconds, default 5, read by the
`worker` and `reconciler` processes (the provider settings module's existing reader set).

Full limit contract: unit **seconds**; reference clock the **host kernel's socket timer**; scope
**per host, per reaper connection attempt** — a fan-out over N declared hosts can spend it N times,
so one provider call inside a fenced transaction is bounded at `connect_timeout × declared_hosts`
for the connect portion plus the reachable host's RPC time; consequence of violation **that host is
treated as unreachable for this call — logged and skipped, the fan-out continues to the next
declared host, the capture lane defers the row behind its backoff and the dump-volume lane leaves
the volume for the next pass, neither counted as a fault**; recovery action **none required by a
caller; an operator on a slow-but-reachable fleet raises the timeout, one whose hosts are down fixes
or removes them from the declared inventory**.

The endpoint comes from the URI the reaper is about to open. `validate_remote_uri` already
guarantees the scheme is `qemu+tls`, so the port defaults to libvirt's TLS port 16514 when the URI
carries none. A URI with no host is a configuration error the gate reports rather than probing
`localhost`.

### What is deliberately not bounded

A host that completes the TCP handshake and then stalls — in the TLS handshake, or in a wedged
libvirtd's RPC — is still unbounded and still holds its transaction for as long as it stalls. Limit
1 caps the blast radius at one candidate per lane per pass. Bounding a stalled RPC needs a
terminable provider operation (ADR-0558's supervised-child shape), which ADR-0565 records as the
escalation and does not take.

## Files

| File | Responsibility |
|---|---|
| `src/kdive/config/core_settings.py` | declare `RECONCILER_LANE_BUDGET_SECONDS` |
| `src/kdive/providers/remote_libvirt/settings.py` | declare `REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS` |
| `src/kdive/providers/remote_libvirt/connection/reachability_gate.py` | new — parse the endpoint, bounded TCP probe, typed failure |
| `src/kdive/providers/remote_libvirt/reaping/connections.py` | `open_libvirt_reaper` calls the gate before `libvirt.open` |
| `src/kdive/reconciler/cleanup/provider_reaping.py` | the two lanes take `budget`, check it between candidates; R4's comment updates |
| `src/kdive/reconciler/loop.py` | `ReconcileConfig.lane_budget`, threaded into both lane repairs |
| `src/kdive/processes/reconciler.py` | read the setting into `ReconcileConfig` |
| `docs/guide/reference/config.md` | regenerated (`just config-docs`) |

## Testing

- **R2 (the load-bearing test)** — a database test per lane. The lane runs with a budget shorter
  than the provider call. The fake reaper does its work inside `asyncio.shield`, so a cancellation
  of the awaiting coroutine does not stop it — the same asymmetry a `to_thread` worker has. After
  the budget has expired, the shielded work asks a **second** connection whether the lane's lock is
  still held (`pg_try_advisory_xact_lock` on the same key must fail while the lane holds it), and
  records the answer. Assertions: the recorded answer is "still held", the candidate is marked
  reclaimed, and the lane counts it. Under an `asyncio.timeout`-around-the-call rewrite the
  transaction unwinds first and the recorded answer flips, so the test reddens.
- **Budget gates between candidates, never during one** — with two candidates and a budget spent by
  the first, exactly one provider call is dispatched and the second candidate is untouched (no
  reap-state row written for it).
- **Budget of zero or less disables the budget** — every candidate is attempted.
- **Budget not spent** — the full batch is attempted, unchanged from today.
- **Connect gate** — `require_reachable` succeeds against a real listening socket on `127.0.0.1:0`;
  raises `TRANSPORT_FAILURE` when the injected connector raises `TimeoutError` or `OSError`; the
  endpoint parser resolves an explicit port, defaults to 16514, and rejects a host-less URI. A test
  that `open_libvirt_reaper` calls the gate before the libvirt opener.
- **Fan-out still isolates** — a host whose gate fails is skipped and the remaining hosts are still
  swept (the existing `map_over_fleet` / `find_over_fleet` behavior, re-proved through the gate).

## Threat model

The change is security-relevant only in that it adds one outbound network action and one new
operator-settable bound.

- **Boundary added** — an outbound TCP connect from the reconciler to a libvirt host. The
  destination is taken from `RemoteLibvirtConfig.uri`, which is operator-declared inventory that
  `validate_remote_uri` already fail-closes to the `qemu+tls` scheme with `no_verify` and operator
  `pkipath` forbidden. The gate parses only the host and port out of that already-validated URI and
  sends no bytes; it cannot reach a destination the subsequent `libvirt.open` would not have
  reached anyway.
- **Actors** — the operator who declares the inventory, and the reconciler process itself. No
  untrusted actor supplies an input to either limit: both are `KDIVE_*` settings, snapshotted at
  process start by `Registry.load`, and neither is reachable from an MCP tool argument.
- **Control per boundary** — the destination is constrained by the existing URI validation; the
  probe is bounded by the new timeout; a failure is reported as `TRANSPORT_FAILURE` naming the URI,
  which the codebase already logs for an unreachable host, and carries no secret (the TLS material
  is never touched by the gate — the pkipath is materialized after it).
- **Out of scope** — the gate is not a security control and must not be read as one: it proves only
  that something accepted a TCP connection, and TLS mutual authentication remains the sole control
  over who the reconciler is talking to. It does not defend against a host that accepts and stalls;
  that is a denial-of-availability bounded by limit 1, not by the gate.
- **Existing guardrail relied on** — `validate_remote_uri`, unchanged.

## Out of scope

- Landing a concrete capture reaper for either provider kind (#1947, #1948). The lane budget covers
  the capture lane today; the connect gate covers whatever reaper the shared seam opens.
- Extending the connect gate to the worker's provider planes.
- Any supervised-child-process shape for reaping.
