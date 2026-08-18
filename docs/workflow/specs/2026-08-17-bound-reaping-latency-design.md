# Bound provider-call latency in the reconciler's two host-state reaping lanes (#1980)

Decision record: [ADR-0565](../../adr/0565-bound-reconciler-provider-reaping-latency.md).
Prior disclosure of the residual this closes:
[ADR-0562](../../adr/0562-host-dump-volume-capture-lease-fence.md) *Consequences*, for the
dump-volume lane. The capture lane carries its disclosure in `_dispatch_capture`'s docstring
rather than in [ADR-0556](../../adr/0556-reclaim-orphaned-captures-across-providers.md), which
records the ownership fence the bound must not break but not the latency residual.

## Problem

`reap_orphaned_captures` and `reap_orphaned_dump_volumes` each await a provider call from inside a
transaction holding an advisory lock, with no deadline on the call and no deadline on the lane. One
unreachable declared libvirt host holds a pooled Postgres connection idle-in-transaction for the
operating system's TCP connect timeout (~130 s), once per unreachable host per provider call.
`_run_repair_plan` runs the catalog sequentially, so every lane placed *after* the reaping lanes
waits behind it — the write-lease collection, the upload sweeps, the artifact-GC lanes, the
rootfs-reclaim lanes, the console-collector sweep, and the image sweeps. Allocation expiry and
System repair are placed *before* the reaping lanes; what reaches them is the `SYSTEM` lock, via
`services/runs/bind.py`'s `_bind_locked`, which blocks on `SYSTEM` while holding `ALLOCATION`.

The capture lane is dormant today — both provider kinds ship a `NullCaptureReaper` pending #1947 and
#1948, and `dispatchable_capture_kinds` excludes them from selection — so only the dump-volume lane
exercises the hazard right now. The budget is a forward guard for the lane whose batch of 25 will
make it 25 times larger.

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

`reap_orphaned_captures(..., budget: timedelta = DEFAULT_LANE_BUDGET)` and
`reap_orphaned_dump_volumes(..., budget: timedelta = DEFAULT_LANE_BUDGET)` each start a monotonic
deadline **once their candidate list is in hand** and consult it **only between candidates**, before
opening the next candidate's transaction. A spent budget ends the lane's pass and returns the count
reclaimed so far. The keyword is defaulted rather than required so no caller can obtain an unbounded
lane by omitting it, and so the ~24 existing call sites in `tests/` need no mechanical edit.

Starting the deadline after the candidate list, not at the top of the lane, is load-bearing for the
dump-volume lane: `list_dump_volumes` is itself a whole-fleet fan-out costing up to
`connect_timeout × declared_hosts`, so a deadline started ahead of it would already be spent before
the loop on any fleet with more down hosts than `budget ÷ connect_timeout` — three, against the
defaults — and the lane would attempt zero candidates on every pass, forever.

The placement is the whole of R2: the check is lexically outside every `conn.transaction()` block in
both lanes, so a spent budget can only prevent the *next* transaction from opening. It can never
unwind one that is open.

Setting: `KDIVE_RECONCILER_LANE_BUDGET_SECONDS`, integer seconds, default 10, read by the
`reconciler` process. Reaches the lanes through a new `ReconcileConfig.lane_budget` field.
`ops.reconcile_now`'s on-demand pass builds its own `ReconcileConfig` in
`src/kdive/mcp/tools/ops/reconcile/reconcile.py` and reads no reconciler setting — it already
inherits the compiled-in defaults for `capture_settle`, `capture_reap_batch`, the retry pair and
`dump_volume_grace`, so it inherits this one the same way. It is bounded there, just not tunable
there; making that path setting-aware is a separate change to every reconciler knob, not to this
bound.

Full limit contract, which must appear in the ADR, the setting's `help`, and the docstring of each
lane: unit **seconds**; reference clock the **reconciler process's monotonic clock**; scope **per
lane, per pass** (each lane gets a full budget, not a share of one); consequence of violation **the
lane returns after the in-flight candidate completes, having attempted fewer candidates than its
batch allows — not counted as a failure, logged at INFO with the unattempted count**; recovery
action **none required, the next pass re-derives the remainder; an operator whose backlog is not
draining raises this or the reconcile interval**.

The setting's parser rejects a non-positive value, so no operator can set a budget that attempts
nothing and thereby disable both sweeps. The lanes carry no sentinel for it; the parser is the
guard, and the default of 10 is a third of `DEFAULT_INTERVAL` (30 s) so both lanes spending their
whole budget still leaves a third of an interval — minus each lane's in-flight-candidate
overshoot — for the rest of the catalog.

### Limit 2 — reaper connect gate

`open_libvirt_reaper` — the opener every fleet-fan-out reaper uses — performs a bounded TCP connect
to the URI's host and port and closes it, before calling `libvirt.open`. A host that does not accept
within the timeout raises `CategorizedError(TRANSPORT_FAILURE)`, which `_enter_host` already
isolates as the unreachable-host case it logs and skips; libvirt is never called for that host, so
the kernel's SYN retry budget is never entered.

Setting: `KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS`, integer seconds, default 5, read by the
`worker` and `reconciler` processes (the provider settings module's existing reader set).

Full limit contract: unit **seconds**; reference clock the **reconciler's monotonic clock**, shared
by every address one host resolves to (`socket.create_connection` applies its own timeout inside a
per-address loop, so a dual-stack host would otherwise cost the budget once per A/AAAA record);
scope **per host, per reaper connection attempt, connect only** — a fan-out spends it once per
*unreachable* host it walks before it finds the target, so the all-hosts-down worst case for one
provider call is `connect_timeout × declared_hosts`, and **name resolution sits outside it** because
`getaddrinfo` takes no timeout; consequence of violation **that host is
treated as unreachable for this call — logged and skipped, the fan-out continues to the next
declared host, the capture lane defers the row behind its backoff and the dump-volume lane leaves
the volume for the next pass, neither counted as a fault**; recovery action **none required by a
caller; an operator on a slow-but-reachable fleet raises the timeout, one whose hosts are down fixes
or removes them from the declared inventory**.

The endpoint comes from the URI the reaper is about to open. `validate_remote_uri` already
guarantees the scheme is `qemu+tls`, so the port defaults to libvirt's TLS port 16514 when the URI
carries none. A URI with no host is a configuration error the gate reports rather than probing
`localhost`. That configuration error reaches the operator only through `_enter_host`'s
unreachable-host warning, which logs `exc_info=True` — so the message and the URI are in the
traceback, but the headline reads like a down host. ADR-0565 accepts that rather than narrowing
every reaper's failure isolation.

### What is deliberately not bounded

A host that completes the TCP handshake and then stalls — in the TLS handshake, or in a wedged
libvirtd's RPC — is still unbounded and still holds its transaction for as long as it stalls. Limit
1 caps the blast radius at one candidate per lane per pass. ADR-0565 records the two escalations it
weighed for that residual (`virConnectSetKeepAlive`, and ADR-0558's supervised-child shape) and
takes neither.

The dump-volume lane's `list_dump_volumes` fan-out runs before the deadline starts and is therefore
not preemptible by the budget: a lane's pass ceiling is its budget, plus the candidate in flight when
the budget expires, plus that one un-budgeted listing. R1 is unaffected because the listing runs
with the connection idle rather than idle-in-transaction — `reap_stale_host_dump_volume_leases`
closes its own transaction before returning, and `reap_orphaned_dump_volumes` asserts
`require_top_level_transaction` for that reason.

`leaked_domains` and `leaked_probe_guests` open through the same reaper seam and so get the connect
gate, but no budget: both make their provider calls *outside* the locked transaction
(`repair_leaked_domains` closes its `(SYSTEM, system_id)` block before `reaper.destroy`;
`repair_leaked_probe_guests` destroys between two separate transactions), so neither has the
idle-in-transaction hazard #1980 names.

The two limits also multiply: a lane advances roughly `budget ÷ per-candidate cost` candidates per
pass, and per-candidate cost rises by the connect timeout for each unreachable host the fan-out
walks. ADR-0565 records the relation and why it is left to the operator rather than derived.

## Files

| File | Responsibility |
|---|---|
| `src/kdive/config/core_settings.py` | declare `RECONCILER_LANE_BUDGET_SECONDS` |
| `src/kdive/providers/remote_libvirt/settings.py` | declare `REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS` |
| `src/kdive/providers/remote_libvirt/connection/reachability_gate.py` | new — parse the endpoint, bounded TCP probe, typed failure |
| `src/kdive/providers/remote_libvirt/reaping/connections.py` | `open_libvirt_reaper` calls the gate before `libvirt.open`; also the opener for `RemoteLibvirtInfraReaper` |
| `src/kdive/reconciler/cleanup/provider_reaping.py` | the two lanes take `budget`, check it between candidates; R4's comment updates |
| `src/kdive/reconciler/loop.py` | `ReconcileConfig.lane_budget`, threaded into both lane repairs |
| `src/kdive/processes/reconciler.py` | read the setting into `ReconcileConfig` |
| `src/kdive/providers/infra/reaping.py` | record on the `CaptureReaper` port that a concrete reaper must open through the shared reaper seam to inherit the connect gate |
| `docs/adr/0562-host-dump-volume-capture-lease-fence.md` | append an amendment: the hold is now bounded, and the fix was narrowed to the reaper seam rather than every remote-libvirt path |
| `docs/guide/reference/config.md` | regenerated (`just config-docs`) |

## Testing

- **R2 (the load-bearing test)** — a database test per lane, in `tests/reconciler/`. The lane runs
  with a budget an order of magnitude shorter than the provider call, so the budget expires early in
  the call and the observation below is taken long after any competing rollback would have
  completed. The fake reaper does its work inside `asyncio.shield`, so cancelling the awaiting
  coroutine does not stop it — the same asymmetry a `to_thread` worker has.

  The observation is taken at the **end** of the shielded work, not at budget expiry: the fake sleeps
  past the budget by a stated margin, then asks a **second** pooled connection whether the lane's
  lock is still held, and records the answer. "Still held" is
  `SELECT pg_try_advisory_xact_lock(<key>)` returning false inside a transaction on that second
  connection. The key differs per lane and must be taken from `kdive.db.locks`, not re-derived:
  - capture lane — `CAPTURE_JOB_FENCE_KEY_SQL`, i.e.
    `hashtextextended('kdive:job:' || job_id::text, 1951)`. It is deliberately not `_lock_key`; a
    test that reached for `_lock_key` here would probe a key nobody holds and fail on a good tree.
  - dump-volume lane — `_lock_key(LockScope.SYSTEM, system_id)`.

  Assertions, in order of what each proves: **(1)** the recorded answer is "still held" — this is the
  R2 evidence, and under an `asyncio.timeout`-around-the-call rewrite the transaction has unwound by
  then and the answer flips; **(2)** the lane still counts the candidate and (capture lane) marks the
  row reclaimed — which proves the call was allowed to finish, but not on its own that no transaction
  ended mid-call, since `_dispatch_capture` and `_delete_if_still_orphaned` both catch `Exception`
  and would turn a `TimeoutError` into a defer.

  The dump-volume fixture must satisfy every precondition on the locked path or the lane never takes
  the lock and the probe observes a lock nobody held: a volume name carrying a parseable System UUID,
  `mtime_epoch_s` older than `grace`, no live `host_dump_volume_lease` row for that System, and no
  active capture job for it.

  Mutation-verify both: break the bound so it does release the lock mid-call, watch the test redden,
  restore, clear `__pycache__`, and confirm green.
- **Budget gates between candidates, never during one** — with two candidates and a budget spent by
  the first, exactly one provider call is dispatched and the second candidate is untouched (no
  reap-state row written for it).
- **The setting rejects a non-positive budget** — `config.validate` fails on `0` and on `-1`,
  naming the setting.
- **A slow candidate listing does not starve the lane** — a dump-volume lane whose
  `list_dump_volumes` takes longer than the whole budget still attempts its first candidate. This is
  the livelock the deadline's placement exists to prevent, so it gets its own test.
- **The gate's reach** — `open_libvirt_reaper` is also `RemoteLibvirtInfraReaper`'s opener, so the
  `leaked_domains` and `leaked_probe_guests` lanes gain the gate; their existing tests must still
  pass with it wired.
- **Budget not spent** — the full batch is attempted, unchanged from today.
- **Connect gate** — every resolved address shares one deadline (a two-address fake resolver gets a
  strictly shrinking timeout, never a fresh budget); a later address is still tried when an earlier
  one refuses; an unresolvable host is a `TRANSPORT_FAILURE`; a malformed or zero port is a
  `CONFIGURATION_ERROR` naming the URI. `require_reachable` succeeds against a real listening socket
  on `127.0.0.1:0`;
  raises `TRANSPORT_FAILURE` when the injected connector raises `TimeoutError` or `OSError`; the
  endpoint parser resolves an explicit port, defaults to 16514, and raises `CONFIGURATION_ERROR` for
  a host-less URI rather than probing `localhost`. A test that `open_libvirt_reaper` calls the gate
  before the libvirt opener.
- **Fan-out still isolates** — a host whose gate fails is skipped and the remaining hosts are still
  swept (the existing `map_over_fleet` / `find_over_fleet` behavior, re-proved through the gate).

## Threat model

The change is security-relevant only in that it adds one outbound network action and one new
operator-settable bound.

- **Boundary added** — an outbound TCP connect from the reconciler to a libvirt host. The
  destination is taken from the URI `open_libvirt_reaper` is about to open, which
  `remote_connection` has already passed through `validate_remote_uri` — fail-closed to the
  `qemu+tls` scheme, with `no_verify` and an operator-set `pkipath` forbidden. The gate parses only
  the host and port out of that already-validated URI and sends no bytes; it cannot reach a
  destination the subsequent `libvirt.open` would not have reached anyway.
- **Second boundary added** — an implicit DNS lookup, from the same operator-declared host name.
  Its control is **none**: `getaddrinfo` takes no timeout, so this crossing is unbounded and sits
  ahead of the gate's own deadline. Stated rather than omitted because silence reads as coverage.
  The lane budget does **not** cap it: the dump-volume lane's deadline starts after
  `list_dump_volumes`, so that lane's whole-fleet resolver cost is outside the budget entirely, and
  in the capture lane the budget caps how many candidates are started, not the resolver time the
  in-flight one spends inside its fenced transaction. Declaring hosts by IP literal removes the
  crossing.
- **Actors** — the operator who declares the inventory, and the reconciler process itself. No
  untrusted actor supplies an input to either limit: both are `KDIVE_*` settings, snapshotted at
  process start by `Registry.load`, and neither is reachable from an MCP tool argument.
- **Control per boundary** — the destination is constrained by the existing URI validation; the
  probe is bounded by the new timeout; a failure is reported as `TRANSPORT_FAILURE` naming the URI,
  which the codebase already logs for an unreachable host, and carries no secret. The gate runs
  *inside* `materialized_pkipath` — `remote_connection` materializes the per-op pkipath and then
  calls the injected `open_connection` — so an unreachable host costs one materialize-and-delete
  cycle of TLS material on worker-local storage before the gate refuses. The gate reads none of that
  material, and `materialized_pkipath`'s `finally` deletes the directory on the refusal path exactly
  as it does on a libvirt connect failure.
- **Out of scope** — the gate is not a security control and must not be read as one: it proves only
  that something accepted a TCP connection, and TLS mutual authentication remains the sole control
  over who the reconciler is talking to. It does not defend against a host that accepts and stalls;
  that is a denial-of-availability bounded by limit 1, not by the gate.
- **Existing guardrail relied on** — `validate_remote_uri`, unchanged, which `remote_connection`
  runs before it composes the per-op pkipath. The gate additionally re-checks the **scheme** itself,
  because it is a public seam #1947's capture reaper is told to open through and a caller that
  skipped validation would otherwise choose the probe's destination. It calls
  `validate_remote_transport`, the subset of that validator which stays true after the per-op
  pkipath is composed on — scheme and `no_verify`, not the `pkipath` prohibition, since by the time
  the opener runs `remote_connection` has composed exactly that onto the URI. Split out of the
  validator rather than duplicated, so the two checks cannot drift.
- **Leak control** — the URI the gate receives carries `?pkipath=<mkdtemp dir>`, and that directory
  holds the op's 0600 client key. Every message and error detail the gate emits is stripped to
  scheme, host, port and path first — the netloc is rebuilt rather than reused, so any userinfo goes
  with the query — because `_enter_host` logs the raise with `exc_info=True`.
- **Operational side effect** — the probe connects and closes before the TLS handshake, once per
  declared host per reaper call. That is the shape connection-scanning detectors match, so a fleet
  behind fail2ban or an IDS needs the reconciler's address allowlisted.

## Out of scope

- Landing a concrete capture reaper for either provider kind (#1947, #1948). The lane budget covers
  the capture lane today; the connect gate covers whatever reaper the shared seam opens.
- Extending the connect gate to the worker's provider planes.
- Any supervised-child-process shape for reaping, and any bound on a host that stalls *after* the TCP
  handshake — #1981.
- Surfacing budget truncation beyond the lane's INFO line — #1982.
