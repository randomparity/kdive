# Multi-client allocation stress script (#2769)

## Problem

Nothing drives a running KDIVE stack from many clients at once. The concurrency proofs in
`tests/adversarial/` call the admission service in-process on separate database connections.
They bypass the HTTP/MCP transport, authentication, argument binding, and the worker and
reconciler lifecycle. The `live_stack` tier runs single-client spines. An operator has no way to
check how a deployed stack holds up when several agents request, renew, release and provision
allocations at overlapping moments, walk away from grants, or send malformed arguments while it
is under that load.

## Scope

One operator-run script, `scripts/live-stack/stress-allocations.py`, run against a stack already
brought up by `stack-services.sh` and `onboard.sh`:

    uv run python scripts/live-stack/stress-allocations.py --clients 8 --duration 120 --seed 7

- **Transport.** Each simulated client owns one `kdive.mcp.dev_harness.LiveStackClient`
  (`over_http(url, token)`), with URL and token taken from `kdive.cli.transport.Session.from_env()`.
  That means `KDIVE_SERVER_URL` plus `KDIVE_TOKEN` or the login cache, the same way `kdivectl`
  resolves them. The script adds no dependency and no `KDIVE_*` variable. Every option is a CLI
  flag.
- **Knobs.** `--project` (default `demo`), `--clients` (8), `--duration` seconds (60),
  `--seed` (random, always printed), `--invalid-ratio` (0.2), `--race-ratio` (0.2),
  `--abandon-ratio` (0.1), `--lease` hours (0.02, that is 72 s), `--call-timeout` seconds (60),
  `--drain-timeout` seconds (600), `--provision-profile FILE` (off). A value out of range is a
  usage error: the three ratios must each be in [0, 1] and sum to at most 1, `--lease` must be in
  (0, 24], and `--drain-timeout` must be at least the lease plus 60 s, so an abandoned grant can
  expire and be swept within it. With `--provision-profile` the floor adds 240 s, because a
  System is torn down by a worker job the reconciler enqueues after its allocation ends.
- **Keys and leases.** Every `allocations.request` and `systems.provision` carries an idempotency
  key, and every request carries `window` = `--lease`, invalid-catalog requests included (only
  the entry that corrupts `window` differs), so a wrongly accepted grant is still settled. A key is
  `stress-<run id>-<uuid from the client RNG>`. The run id is a fresh random value per run,
  printed beside the seed, because the server keeps keys per principal for 7 days and a re-run
  of a seed must not replay the previous run's grants.
- **Workload.** Each client repeats one weighted action until the duration ends: an invalid call
  (`--invalid-ratio`), a race (`--race-ratio`), an abandonment (`--abandon-ratio`), or a churn
  cycle (the remainder).
  - **Churn cycle.** `allocations.request` with a random `shapes.list` shape or the smallest
    shape's custom triple, `on_capacity` `deny` or `queue`, and a fresh key or a replay of that
    client's previous request (same key and arguments). Then an optional `allocations.renew`
    (`extend` = `--lease`), a jittered hold of 0–2 s, and `allocations.release`. A queued
    (`requested`) grant is released at once, while it waits in the queue. With
    `--provision-profile`, half of the granted cycles call `systems.provision` with that profile.
    Half of those release immediately, racing the provision job; the other half release after
    the hold. A client provisions again only once `systems.get` shows its previous System
    `torn_down` or `failed`, so each client has at most one System in flight. That read is a
    capacity safeguard for the provisioning flag; the operator approved it under exclusion (c) on
    2026-09-25. No other read steers the workload.
  - **Races.** One is picked at random: (a) a double release, where two concurrent releases hit
    one grant; (b) renew racing release; (c) one idempotency key sent from three concurrent
    requests with identical arguments, each on its own MCP session: the client's and two
    short-lived ones it opens for the race. A session that fails to open is a report note.
  - **Abandonment.** A request with `on_capacity` `deny` that the client never releases. Either
    the client walks away after the grant (after provisioning it, when the churn rules would
    provision), or it cancels the request in flight after a random 0–50 ms. A cancelled call is
    outcome `abandoned`, not `timeout`. Queued requests are never abandoned: a `requested` row has
    no lease, and the server reaps it only after its 24 h queue wait.
  - **Invalid calls.** One is picked from a fixed catalog: missing `project`; `shape` plus a
    custom triple; a partial triple; `vcpus` 0 and −1; an unknown shape name; `on_capacity`
    `"maybe"`; `window` 0; `project` the caller cannot reach; release and renew of `"not-a-uuid"`
    and of a random UUID. Renew with `extend` −1 and with `"abc"` goes against a grant the call
    first obtains and releases afterwards. If that grant is denied, the entry is skipped. An
    unknown `arch` is left out: a host advertising no arch is eligible for any arch, so a grant
    there is correct behavior.
  - **Seeding.** Each client draws from `random.Random(f"{seed}:{index}")`. A seed replays each
    client's action sequence. It does not replay the interleaving between clients, and keys differ
    per run. Races (a) and (b) send concurrent MCP requests on the client's one session.
- **Monitor.** A separate task calls `resources.availability` every second. It checks each host
  item whose `data.schedulable` is true against `in_use <= cap`. Items with `schedulable` false
  (cordoned, not available, or no valid cap) are skipped.
- **Preflight.** Before any load: the stack answers, `shapes.list` returns at least one shape, and
  `resources.availability` shows at least one schedulable host with `cap` above 0. Otherwise the
  script exits 2 with the reason.
- **Drain.** The script tracks three sets: allocations it owns (granted and not yet released),
  allocations left to lease expiry (abandoned), and Systems. It also keeps the key and arguments
  of every keyed call that no reply confirmed: a timeout, transport failure or cancellation, and
  a `tool-error` on a valid call, since a handler can raise after admission has committed. An
  invalid call's `tool-error` is a binding rejection and confirms nothing was created. The drain
  runs in a `finally` that also covers Ctrl-C, on a session of its own, so a session the load
  broke cannot stop it. A session whose teardown fails, or a drain session that cannot open, is
  a report note, never a crash. Its deadline starts when it starts and bounds every
  drain call, each call's timeout included; what the deadline cuts off stays unsettled. Every
  drain call is reported under its own label.
  1. Replay each kept request once with the same key. A replayed `deny` request that is granted
     joins the abandoned set, since only it carries the short lease. Every other replayed grant
     is owned: a queued request the server promoted holds the server's default lease (4 h), not
     `--lease`.
  2. Release each owned allocation. An `ok`, or a failure whose `current_status` is terminal,
     settles it.
  3. Replay each kept provision once. Coming after step 2, a provision that never committed now
     fails against a released allocation instead of creating a System. A `system_id` joins the
     Systems. A replay that fails with a category leaves nothing to track.
  4. Until the deadline, poll the rest: re-release owned leftovers, read abandoned allocations
     with `allocations.wait` (`timeout_s=0`), and read Systems with `systems.get`.
  Whatever is still unsettled when the run ends is invariant 5. The first Ctrl-C during the load
  ends the load and starts the drain; a Ctrl-C during the drain stops it, and the report still
  lists what was left.
- **Report.** To stdout: the seed and run id, calls per tool and outcome, latency p50, p95 and max
  per tool, a histogram of error categories, the count of granted requests (live grants from the
  load, not replays of finished ones; with a warning line when it is zero), the count of
  `transport`, `timeout` and `tool-error` outcomes on valid calls, a note for each client whose
  session was lost, and the violation list. Exit status 0 means no violation, 1 means at least one
  violation, 2 means a usage or preflight failure (bad flag, invalid profile, no token, failed
  preflight). On Ctrl-C the script drains, prints the report, and exits 130.
- **Invalid-call bucket.** The report adds per-entry outcome counts for the invalid catalog, so
  envelope rejections and tool-error rejections are listed apart from the valid workload.
- **Profile.** `--provision-profile` is validated with
  `kdive.profiles.provisioning.ProvisioningProfile` before any load; an invalid file exits 2. The
  script drops `vcpu`, `memory_mb` and `disk_gb` from it: the server fills sizing from each
  allocation and rejects a restated size that differs, and the run's grants come in several
  sizes. The report warns when the flag is set and no `systems.provision` succeeded.

**Outcome classes.** Every call is exactly one of `ok` (no error category), `envelope-failure`
(`error_category` set), `tool-error` (`LiveStackToolError`, which is an MCP `is_error` result),
`transport` (any other exception), `timeout` (`--call-timeout` exceeded), or `abandoned` (a
deliberate in-flight cancel). The operator decided on 2026-09-24 that a `tool-error` counts as a
valid rejection of an invalid call and is reported in its own bucket. On 2026-09-25 the operator
decided that `transport`, `timeout` and `tool-error` on a valid call are counted and reported,
not violations: the charter forbids them only for invalid input, and it treats a lost reply on
valid traffic as expected. A grant such a call strands is still held to invariant 5.

**Invariants.** A violation is recorded when:

1. a monitor sample shows `in_use > cap` on a schedulable host item;
2. an invalid-catalog call ends in `ok`, `transport` or `timeout`. The script tracks the id an
   accepted call returns so the drain settles it;
3. a double-release race where both releases replied does not end with both `ok`. A release
   that got no reply (`transport`, `timeout`, `tool-error`) is a counted valid-call error, and
   invariant 5 still covers its allocation;
4. an idempotency replay, sequential or concurrent, returns an `ok` whose `object_id` differs
   from another `ok` response for the same key;
5. when the run ends, an allocation the script created is not `released`, `expired` or `failed`,
   a System it created is not `torn_down` or `failed`, or a call whose reply never arrived could
   not be replayed. An abandoned allocation that is still occupying means lease expiry did not
   reclaim it. The state is read from the envelope `status`, or from `data.current_status` on a
   failure envelope.

Denials and other `envelope-failure` results on valid calls are counted, not treated as
violations, as are the valid-call errors above. Examples: a capacity or quota denial, or
`stale_handle` on a release that lost a race.

### Failure model

1. **Actors and deployments.** A local operator runs it against a stack they own, as the
   contributor-or-higher token from `onboard.sh`. Designed for the host stack from
   `stack-services.sh`, with or without `--skip-libvirt`. `--provision-profile` needs the variant
   with libvirt. It is not for shared or production deployments.
2. **Invariants and assets at stake.** Allocations and Systems it creates hold real capacity and
   budget until released or expired. The short lease bounds how long any of them can outlive the
   run. The exit status is what an operator acts on.
3. **Accepted failure classes.**
   - The monitor samples. An overshoot shorter than the sample interval can go unseen, and the
     in-process `tests/adversarial/test_admission_concurrency.py` holds the exact proof.
   - An operator changing host caps mid-run can trigger a false invariant 1. Documented in the
     runbook.
   - A `kill -9` skips the drain. A `deny` grant expires on its short lease. A queued request is
     reaped after its 24 h queue wait, or, once promoted, expires on the server's default lease.
  - A Ctrl-C during the drain stops it. What stays unsettled is reported, and short-lease grants
     still expire.
   - The reconciler sweeps every 30 s and the interval is not configurable, so lease-expiry
     reclamation takes up to the lease plus 30 s; `--drain-timeout` is bounded below to cover it.
   - Latency figures come from one Python process and include client-side scheduling.
4. **Covered elsewhere.** Server-side fixes for anything the script finds go to separate issues
   filed through `$bounty` (charter exclusion). `just` and CI wiring are the operator's (charter
   exclusion).

## Validation

- `focused-test`: `tests/scripts/test_stress_allocations.py` loads the script with
  `importlib.util.spec_from_file_location` and runs `run_stress` against an in-memory fake
  stack that implements the tools above, including lease expiry on a short window:
  - a well-behaved fake yields exit 0 with no violations, with and without a provision profile,
    and every fake allocation ends terminal;
  - fakes that each plant one defect: `in_use > cap`, an `ok` on an invalid call, a different id
    on a key replay, a release that never succeeds, and a lease that never expires. Each must
    yield exit 1 and name its invariant (1, 2, 4, 3 and 5, and 5);
  - a fake that commits a request and never replies, with the run cancelled mid-flight: the drain
    replays the key, and every fake allocation ends terminal;
  - a run cancelled during the drain exits 130 and reports its leftovers as invariant 5;
  - the fake promotes queued rows with a long lease and tears Systems down one poll late, so the
    drain's release of promoted replays and its wait for Systems are exercised;
  - a stack with no schedulable host, and one with no shape, fail preflight;
  - unit cases cover outcome classification, flag validation (exit 2, including an invalid
    profile and the teardown floor), valid-call errors counted without a violation, and
    percentile and report rendering.

  Red: each case fails before its code exists. Green:
  `uv run python -m pytest tests/scripts/test_stress_allocations.py -q`.
- `task-test-not-applicable`: the runbook section in `docs/operating/runbooks/live-testing.md`
  and the pointer in `scripts/live-stack/README.md` are prose with no executable consumer.
- Live proof, run by the implementer: one run against a live stack, one interrupted with Ctrl-C
  mid-run (drain, report, exit 130), and one with `--provision-profile` where a libvirt stack is
  reachable, with outcomes reported on the PR.
