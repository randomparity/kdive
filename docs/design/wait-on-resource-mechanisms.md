# Wait-on-resource mechanisms

- **Issue:** [#430](https://github.com/randomparity/kdive/issues/430)
- **ADR:** [`../adr/0118-wait-on-resource-mechanisms.md`](../adr/0118-wait-on-resource-mechanisms.md)
- **Status:** Draft

## Problem

When an agent decides to wait for a resource to become available, it should not burn
context (tokens, turns) busy-polling. Issue #430 asks two things: how an agent shares
resource status in a token-efficient way while waiting, and what hints help it decide
whether to wait at all.

The issue's "design thoughts" enumerate async handles, bounded long-poll, leases with
TTL/renewal, server-as-source-of-truth recovery, idempotency, cancellation, and
reference-not-log-dump output. **Most of that already ships in kdive** (verified in
source, not assumed):

| Issue's design thought | Already in kdive |
|---|---|
| Split start from wait (async handle) | Long ops are durable jobs; the tool returns `{job_id, status: running}` and the agent polls (`mcp/tools/lifecycle/runs/build.py`, `domain/models.py` `JobKind`) |
| Bounded `wait_for(id, timeout)` long-poll | `jobs.wait(job_id, timeout_s)` polls server-side up to `MAX_WAIT_S=300` (`mcp/tools/catalog/jobs.py:125`) |
| Server is source of truth; in-flight work discoverable | `jobs.list`, `allocations.list` |
| Idempotency (no double-book) | `idempotency_key` on `allocations.request` / `allocations.renew` |
| Leases with TTL + renewal + server-side expiry | `lease_expiry`, `allocations.renew` (`services/allocation/renew.py`), reconciler expiry sweep + `queue_timeout` reaper (`services/allocation/promotion.py`) |
| Explicit cancellation | `jobs.cancel`, `allocations.release` (incl. the `requested → released` cancel edge) |
| Context-frugal output; references not log dumps | The `ToolResponse` envelope (`mcp/responses.py`, ADR-0019) |
| Queue-depth visibility | `resources.availability` → `data.queue_depth` (`mcp/tools/catalog/availability.py`, ADR-0070) |

So #430 is a **gap analysis**, not a new subsystem. Three gaps remain for
token-efficient waiting. This is not milestone- or epic-sized; it is one ADR + one
spec + the implementing commits on `feat/wait-on-resource-430`, closing #430.

### The three gaps

1. **No bounded wait for a queued allocation.** A `requested` (queued) allocation can
   only be discovered by repeated `allocations.get` calls across agent turns — the exact
   token-waste #430 targets. `jobs.wait` solved this for jobs; allocations are the
   missing half. (Confirmed: there is no `allocations.wait`; `allocations.*` is
   `request`/`get`/`release`/`renew`/`list`, `mcp/tools/lifecycle/allocations.py:332`.)

2. **No per-request queue position.** When a request is enqueued (`on_capacity=queue`),
   the response carries only `status: requested` and the id — no signal of how far back
   it sits. `resources.availability` exposes a fleet-wide `queue_depth` aggregate but not
   *this* request's position, so the agent cannot weigh "wait" against "pick a free
   system."

3. **No retryable-vs-terminal signal.** Every failure carries an `error_category`
   (`domain/errors.py`), but the agent must already know which categories are transient.
   #430 explicitly asks for "a clear retryable-vs-terminal distinction" so an agent's
   retry logic "doesn't hammer a permanent failure."

## Decision summary

See [ADR-0118](../adr/0118-wait-on-resource-mechanisms.md) for the full record. Each gap
lands at a single insertion point in existing, proven code. The wait tool reuses the
`jobs.wait` poll loop, the position is one count query, and `retryable` is *derived* from
`error_category`. The only schema change is one additive nullable column
(`allocations.failure_category`) so a failed queued allocation can report its cause to a
waiting agent (see Gap 1) — a forward-only migration with no backfill.

### Gap 1 — `allocations.wait(allocation_id, timeout_s=30.0)`

A read-only tool that is a direct structural mirror of `wait_job`
(`mcp/tools/catalog/jobs.py:125`). It polls `ALLOCATIONS.get` every `POLL_INTERVAL_S`,
holding no pool connection while it sleeps, and returns when the allocation **leaves the
`requested` state** or the (clamped, `≤ MAX_WAIT_S`) deadline elapses.

- **Settle predicate:** `state is not AllocationState.REQUESTED`. A queued request
  settles into `granted` (promoted by the reconciler), `released` (cancelled), or
  `failed` (budget-terminate or `queue_timeout` reap) — the exact out-edges of
  `requested` in `domain/state.py`. Calling `wait` on an already-settled allocation
  returns its current envelope immediately, the same semantics `jobs.wait` has on a
  terminal job.
- **Failed-settle must report *why* (the one place that needs a schema change).** The
  most important settle for a *waiting* agent is `failed`, and it most needs to know
  whether the cause was a `queue_timeout` (retryable — re-queue) or a budget terminate
  (`allocation_denied`, terminal — stop). But `_envelope_for_allocation` today hardcodes
  `infrastructure_failure` for *every* failed allocation
  (`mcp/tools/lifecycle/allocations.py:58-65`), and the `allocations` table stores no
  failure cause (verified: no such column across `db/schema/*.sql`). Composed with Gap 3,
  a budget-terminated queued request would surface as `infrastructure_failure` →
  `retryable=true` → the agent re-queues a request that can never be granted — the exact
  spin this feature exists to stop, on the path it adds. So this design adds **one
  additive nullable column, `allocations.failure_category text`** (a new forward-only
  migration; no backfill — existing failed rows stay NULL). The two queued-terminate
  transitions persist the cause: `_terminate` writes `allocation_denied`, `_reap_one`
  writes `queue_timeout` (`services/allocation/promotion.py`). `_envelope_for_allocation`
  reads the column for a `failed` allocation and falls back to `infrastructure_failure`
  when it is NULL (back-compat for any other failed path that does not yet set it). This
  makes the derived `retryable` correct on the wait path: `queue_timeout → true`,
  `allocation_denied → false`.
- **Why `requested` is the only waited state:** a `granted`/`active` allocation is
  already usable; the agent waits *for a grant*, not past it. Waiting for lease expiry or
  release is not a use case (those are agent- or reconciler-driven, not awaited).
- **Auth / no-leak:** identical to `allocations.get` — `require_project` + `viewer` on
  the owning project; an absent or ungranted id returns the same `not_found` envelope as
  a missing row (no existence leak); a malformed id is `configuration_error`. A
  non-finite or non-positive `timeout_s` degrades to a single read (mirrors `wait_job`).
- **Envelope:** the standard `_envelope_for_allocation` output, carrying the
  queue-position hint of Gap 2 while still `requested`. `suggested_next_actions` stays
  `["allocations.get", "allocations.release"]`; the tool is `read_only` annotated.

### Gap 2 — queue-position hint on a `requested` allocation

`allocations.get` and `allocations.wait` compute and surface, for a `requested` row
only, two `data` keys:

- `queue_position` — the row's 1-based rank among `requested` rows for the **same
  target**, ordered `created_at, id` (the exact FIFO order `promote_pending` selects on,
  `promotion.py:80`).
- `queue_ahead` — `queue_position - 1`, the count strictly ahead of it (a convenience so
  the agent need not subtract; `0` means "next in line for this target").

"Same target" is the by-id target (`requested_resource_id` equals this row's) or, for a
by-kind request, the same `requested_kind`. This matches how promotion re-resolves
candidates (`_candidate_hosts`, `promotion.py:279`).

- **Honesty constraint (load-bearing).** Promotion is **work-conserving and per-host**: a
  younger request on a free host is promoted ahead of an older one waiting on a busy host
  (`promotion.py` module docstring). Queue position is therefore an **advisory hint, not
  a guaranteed ETA or ordering**. The tool docstring, the ADR, and the generated
  reference all state this; the field name is `queue_position` (a position), never
  `estimated_seconds` (we hold no historical timing data — see Out of scope).
- **Disclosure.** The count spans projects (the queue is global per host), but it is a
  count, not identities, and `resources.availability.queue_depth` already exposes
  cross-project queued counts (ADR-0070). This discloses nothing new. The position is
  computed regardless of which projects the rows ahead belong to, because promotion
  ordering is itself cross-project.
- **`allocations.list` omits the hint** to avoid an N+1 query (one position query per
  row). Position is a per-request datum available via `get`/`wait`; a list is a roster.
- **Query.** One count:
  `SELECT count(*) FROM allocations WHERE state='requested' AND (<same-target predicate>)
  AND (created_at, id) < (<this row's created_at, id>)`, then `+1`. The existing partial
  index `idx_allocations_requested_created_at ON allocations (created_at) WHERE
  state='requested'` (`db/schema/0016_pending_queue.sql`) serves the `created_at` age
  range; the same-target column(s) (`requested_kind` / `requested_resource_id`) and the
  `id` tiebreak are residual filters on top of that range, the same ordering
  `promote_pending` selects on. This is adequate for expected queue sizes; a covering
  index on `(requested_kind, requested_resource_id, created_at, id)` is a future option if
  queues grow large enough to matter.

### Gap 3 — `retryable`, derived from `error_category`

Retryability is a **pure function of `error_category`**. Rather than thread a new
argument through the ~15 failure construction sites, the envelope **derives** it once, in
the model validator that already enforces "`error_category` is set iff the status is a
failure" (`responses.py:80`).

- **New field:** `retryable: bool | None` on `ToolResponse`. `None` on any success
  envelope; a `bool` exactly when `error_category` is set. It mirrors `error_category`'s
  presence one-to-one and is **never set by callers** — the validator computes it from a
  static classification table, so it cannot drift from the category and needs zero
  call-site changes. A value supplied by a caller is overwritten by the derived one.
- **Placement at the envelope top level, not in `data`:** retryability is cross-cutting
  agent control-flow, the same shape on every plane. ADR-0019's whole premise is "the
  agent learns one envelope"; a per-op `data` key would force the agent to look in a
  different place per tool. ADR-0113's advertised `{"type":"object"}` schema is
  unaffected by adding a model field.
- **Wire shape (deliberate):** `retryable` is **present-but-null** on every success
  envelope, not omitted — the field is always serialized (no `exclude_none`), so a client
  reads one stable key shape: `null` means success, `true`/`false` means a classified
  failure. This is a change to the *serialized payload of every tool response* across all
  planes, even though no construction site changes: the model gains a field that
  `model_dump` emits everywhere. So the implementation must audit whole-response
  equality assertions in the suite (the envelope is exercised by ~35 `model_dump` sites
  and the `tests/cli/test_structured_content_envelope.py` wire test) and regenerate any
  affected fixtures in the same change — a mechanical but non-zero surface the plan owns.

#### Classification table

The table is exhaustive over `ErrorCategory` (every value classified, so a future
category addition is a deliberate edit, enforced by a test that asserts the table's keys
equal the enum). The bias is **conservative: when a category's transience is ambiguous,
it is terminal**, because the flag's purpose (#430) is to stop an agent hammering a
permanent failure — a false "terminal" costs at most a missed retry the agent can still
make deliberately, while a false "retryable" invites a spin loop, the precise waste this
closes.

`retryable = true` (a bare re-invocation may succeed once a transient condition clears,
with no change by the caller):

| Category | Why transient |
|---|---|
| `infrastructure_failure` | A host/network/runtime hiccup; commonly clears |
| `provisioning_failure` | Provisioning is an infra op that races boot/host; retry often succeeds |
| `boot_timeout` | A timeout, not a deterministic boot failure |
| `readiness_failure` | Readiness gates are time-dependent |
| `transport_failure` | A network/transport blip |
| `transport_conflict` | A single-client transport another holder will release |
| `debug_attach_failure` | Attach races the target; retry often succeeds |
| `control_failure` | A transient control-plane op |
| `capacity_exhausted` | All hosts busy now; capacity frees with no caller change |
| `queue_timeout` | A wait timeout — the canonical "try again later" |

`retryable = false` (deterministic, or the correct recourse is to *change* something, not
blind-retry):

| Category | Why terminal |
|---|---|
| `configuration_error` | Malformed input; the same call fails identically |
| `missing_dependency` | A setup/env gap, not a transient condition |
| `build_failure` | A deterministic build error; needs a source/config change |
| `install_failure` | A deterministic install error; needs a change |
| `stale_handle` | The handle is dead; re-fetch, do not retry the same op |
| `lease_expired` | The lease lapsed and the System is gone; request a new allocation |
| `not_implemented` | Will never succeed |
| `not_found` | The same id resolves to no visible row on retry |
| `conflict` | A uniqueness/state collision; re-read and re-decide, not blind-retry |
| `authorization_denied` | A grant the token lacks; retry cannot grant it |
| `quota_exceeded` | The per-project concurrency cap; release something or queue — not blind-retry |
| `allocation_denied` | Over budget (never clears by waiting) **or** host-cap full; see note |

**`allocation_denied` note (the one conflated category).** The synchronous deny path
emits `allocation_denied` for *both* an over-budget denial (terminal) and a host-cap-full
denial (transient), distinguished only by `data.reason` (`at_capacity` vs the budget
reason — `promotion.py:172` `_is_budget_terminate`). A single category→bool function
cannot split them, so the table marks `allocation_denied` **terminal**. This is the
correct hint for both sub-cases: the budget sub-case is genuinely terminal, and the
capacity sub-case has a *better* affordance than blind-retrying a deny — re-issue the
request with `on_capacity=queue` and then `allocations.wait` (Gaps 1–2). Steering the
agent to queue rather than spin on a synchronous capacity deny is the intended behavior.
The finer distinction stays available in `data.reason` for an agent that wants it.

## Out of scope (stated here, not deferred to sub-issues)

- **Estimated duration from historical timings.** A trustworthy `estimated_seconds`
  needs a per-(kind, shape) timing history — a new metrics table and write path, i.e. a
  real subsystem. Gap 2 ships a *position*, which is honest with the data we already
  hold; an ETA would be a guess dressed as a number. If demand appears, it is its own
  ADR.
- **`kdivectl --watch` / `--follow`.** A client-side convenience loop over the MCP
  primitives. The MCP tools above *are* the contract; the CLI wrapper can follow later
  without re-deciding anything here.
- **MCP `notifications/progress` push.** Client/host support is uneven (#430's own
  caveat); the poll-based design above does not depend on it.

## Testing

Behavior and edges, driven at the handler boundary (pool + ctx injected, no transport),
the project convention:

- **`allocations.wait`:** returns immediately on an already-`granted`/`failed`/`released`
  allocation; blocks then returns on a `requested → granted` transition (inject a fake
  `sleep` and flip the row, mirroring `wait_job`'s test seam); returns the current
  envelope (not an error) at deadline while still `requested`; `not_found` for
  absent/ungranted/malformed ids; non-positive and non-finite `timeout_s` do a single
  read; `viewer` is required.
- **Failed-settle cause (the Gap-1 schema change):** a `queue_timeout`-reaped allocation
  reports `error_category=queue_timeout` with `retryable=true`; a budget-terminated
  allocation reports `error_category=allocation_denied` with `retryable=false` (the
  load-bearing case — proves a waiting agent is not told to re-queue an over-budget
  request); a `failed` allocation with `failure_category` NULL still reports
  `infrastructure_failure` (the unchanged fallback). These hold through both
  `allocations.get` and `allocations.wait`, since both render via
  `_envelope_for_allocation`.
- **Queue position:** a lone `requested` row reports `queue_position=1`, `queue_ahead=0`;
  with three same-kind queued rows, the middle reports `2`/`1`; a by-id request counts
  only same-`requested_resource_id` rows; a by-kind request counts only same-kind rows; a
  cross-kind queued row does not shift the count; the hint is absent on a `granted` row
  and absent from `allocations.list` items.
- **`retryable`:** a property test asserting the classification table's key set equals the
  `ErrorCategory` enum (no category unclassified, none stale); `retryable is None` on
  every success envelope and a `bool` on every failure; a caller-supplied `retryable` is
  overwritten by the derived value; one explicit case per category fixing its expected
  bool so a reclassification is a visible diff; `from_job` failures carry the derived
  value.

## Rollback

The tool surface is removal-only: drop `allocations.wait`, the two position `data` keys,
and the `retryable` field/validator clause; none persist anything to unwind. The migration
runner is forward-only (ADR-0015), so the one added column (`allocations.failure_category`)
is not "un-migrated" — left in place it is a harmless nullable column that nothing reads
once `_envelope_for_allocation`'s read is reverted (the writes in `promotion.py` become
dead and can be removed in a follow-up). Regenerate the tool reference (`just docs`) in the
same change so the committed reference matches the live registry (the `docs-check` CI gate).
