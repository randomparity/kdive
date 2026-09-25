# Multi-client allocation stress script (#2769)

## Problem

Nothing drives a running KDIVE stack from many clients at once. The concurrency proofs in
`tests/adversarial/` call the admission service in-process on separate database connections.
They bypass the HTTP/MCP transport, authentication, argument binding, and the worker and
reconciler lifecycle. The `live_stack` tier runs single-client spines. An operator has no way to
check how a deployed stack holds up when several agents request, renew, release and provision
allocations at overlapping moments, or send malformed arguments while it is under that load.

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
  `--call-timeout` seconds (60), `--sample-interval` seconds (1), `--drain-timeout` seconds (300),
  `--provision-profile FILE` (off), `--json-out FILE` (off). A value out of range is a usage
  error.
- **Workload.** Each client repeats one weighted action until the duration ends. The action is
  either an invalid call (`--invalid-ratio`), a race (`--race-ratio`), or a churn cycle
  (the remainder).
  - **Churn cycle.** `allocations.request` with a random `shapes.list` shape or the smallest
    shape's custom triple, `on_capacity` `deny` or `queue`, and a fresh key, no key, or a replay
    of that client's previous key. Then an optional `allocations.renew` (`extend` 0.1 h), a
    jittered hold of 0–2 s, and `allocations.release`. A queued (`requested`) grant is released
    after one `allocations.wait` of at most 2 s. With `--provision-profile`, half of the granted
    cycles call `systems.provision` with that JSON profile. Half of those release immediately,
    racing the provision job. The other half release after the hold.
  - **Races.** One is picked at random: (a) a double release, where two concurrent releases hit
    one grant; (b) renew racing release; (c) one idempotency key sent from three concurrent
    requests with identical arguments.
  - **Invalid calls.** One is picked from a fixed catalog: missing `project`; `shape` plus a
    custom triple; a partial triple; `vcpus` 0 and −1; an unknown shape name; `on_capacity`
    `"maybe"`; `window` 0; `project` the caller cannot reach; release and renew of `"not-a-uuid"`
    and of a random UUID. Renew with `extend` −1 and with `"abc"` goes against a grant the call
    first obtains and releases afterwards. If that grant is denied, the entry is skipped. An
    unknown `arch` is left out: a host advertising no arch is eligible for any arch, so a grant
    there is correct behavior.
  - **Seeding.** Each client draws from `random.Random(f"{seed}:{index}")`. A seed replays each
    client's action sequence. It does not replay the interleaving between clients. The calls
    within a race share one client session, as concurrent MCP requests on it.
- **Monitor.** A separate task calls `resources.availability` every `--sample-interval` seconds.
  It checks each host item whose `data.schedulable` is true against `in_use <= cap`. An
  unschedulable item reports `cap` 0 and admits nothing.
- **Drain.** A `finally` block cancels the clients and releases every allocation the script
  recorded that is not yet terminal. It then polls with `allocations.wait` (`timeout_s=0`) and
  `systems.get` until the drain deadline. This also runs on Ctrl-C.
- **Report.** To stdout, and as JSON with `--json-out`: calls per tool and outcome, latency
  p50, p95 and max per tool, a histogram of error categories, and the violation list. Exit status
  0 means no violation, 1 means at least one violation, 2 means a usage or preflight failure
  (bad flag, unreadable profile, no token, stack unreachable, empty `shapes.list`). On Ctrl-C the
  script drains, prints the report, and exits 130.
- **Invalid-call bucket.** The report adds per-entry outcome counts for the invalid catalog, so
  envelope rejections and tool-error rejections are listed apart from the valid workload.

**Outcome classes.** Every call is exactly one of `ok` (no error category), `envelope-failure`
(`error_category` set), `tool-error` (`LiveStackToolError`, which is an MCP `is_error` result),
`transport` (any other exception), or `timeout` (`--call-timeout` exceeded). The operator decided
on 2026-09-24 that a `tool-error` counts as a valid rejection of an invalid call and is reported
in its own bucket.

**Invariants.** A violation is recorded when:

1. a monitor sample shows `in_use > cap` on a schedulable host item;
2. any call ends in `transport` or `timeout`;
3. a call from the valid workload (churn, races, monitor, drain) ends in `tool-error`;
4. an invalid-catalog call ends in `ok`. The script also records the returned id so the drain
   releases it;
5. a double-release race does not end with both calls `ok`;
6. an idempotency replay, sequential or concurrent, returns an `ok` whose `object_id` differs
   from another `ok` response for the same key;
7. at the drain deadline, a recorded allocation is not `released`, `expired` or `failed`, or a
   recorded System is not `torn_down` or `failed`. The state is read from the envelope `status`,
   or from `data.current_status` on a failure envelope.

Denials and other `envelope-failure` results on valid calls are counted, not treated as
violations. Examples: a capacity or quota denial, or `stale_handle` on a release that lost a race.

### Failure model

1. **Actors and deployments.** A local operator runs it against a stack they own, as the
   contributor-or-higher token from `onboard.sh`. Designed for the host stack from
   `stack-services.sh`, with or without `--skip-libvirt`. `--provision-profile` needs the variant
   with libvirt. It is not for shared or production deployments.
2. **Invariants and assets at stake.** Allocations and Systems it creates hold real capacity and
   budget until drained. A missed cleanup strands them. The exit status is what an operator acts
   on.
3. **Accepted failure classes.**
   - The monitor samples. An overshoot shorter than the sample interval can go unseen, and the
     in-process `tests/adversarial/test_admission_concurrency.py` holds the exact proof.
   - An operator changing host caps mid-run can trigger a false invariant 1. Documented in the
     runbook.
   - A `kill -9` skips the drain. Leftovers expire on their lease and are reaped by the
     reconciler.
   - Latency figures come from one Python process and include client-side scheduling.
4. **Covered elsewhere.** Server-side fixes for anything the script finds go to separate issues
   filed through `$bounty` (charter exclusion). `just` and CI wiring are the operator's (charter
   exclusion).

## Validation

- `focused-test`: `tests/scripts/test_stress_allocations.py` loads the script with
  `importlib.util.spec_from_file_location` and runs `run_stress` against an in-memory fake
  client pool that implements the tools above:
  - a well-behaved fake yields exit 0 with no violations;
  - three fakes each plant one defect: `in_use > cap`, an `ok` on an invalid call, and a
    different id on a key replay. Each must yield exit 1 and name its invariant;
  - a fake whose release never succeeds makes the drain report invariant 7;
  - unit cases cover outcome classification, flag validation (exit 2), and percentile and report
    rendering.

  Red: each case fails before its code exists. Green:
  `uv run python -m pytest tests/scripts/test_stress_allocations.py -q`.
- `task-test-not-applicable`: the runbook section in `docs/operating/runbooks/live-testing.md`
  and the pointer in `scripts/live-stack/README.md` are prose with no executable consumer.
- Live proof, run by the implementer: one run against a live stack, and one with
  `--provision-profile` where a libvirt stack is reachable, with outcomes reported on the PR.
