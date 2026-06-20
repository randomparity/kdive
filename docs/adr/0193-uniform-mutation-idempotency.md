# ADR 0193 — Uniform `idempotency_key` on object-creating / job-enqueuing mutations

- **Status:** Accepted
- **Date:** 2026-06-20
- **Deciders:** kdive maintainers
- **Issue:** [#619](https://github.com/randomparity/kdive/issues/619) (part of #618; source AX_REVIEW.md A1, folds in M2/M3)
- **Builds on (does not supersede):** [ADR-0040](0040-admission-lifecycle-concurrency.md) §3
  (the principal-scoped `(principal, key)` request/renew idempotency this generalizes),
  [ADR-0016](0016-repository-layer-locks-idempotency.md) (the M0 job `dedup_key` ledger),
  [ADR-0019](0019-tool-response-envelope.md) (the `ToolResponse` envelope this replays).
- **Spec:** [`../design/uniform-mutation-idempotency.md`](../design/uniform-mutation-idempotency.md)

## Context

The transport-reset retry contract (ADR-0085 et al.) blesses re-invoking only idempotent
**reads** after a transient transport drop. A transport reset during the initial
enqueue/create of a *mutation* can double-act: the server commits a durable object or
enqueues a job, the response never reaches the client, and the client's blind retry
creates a *second* Run/System/Investigation or enqueues a *second* job.

`idempotency_key` already exists, but only on `allocations.request` / `allocations.renew`
(ADR-0040 §3). That path:

- stores the client key in `idempotency_keys` (PK `(principal, key)`, `kind` discriminator,
  `result jsonb`), recorded **in the same transaction** as the durable grant/extend so a
  crash between the two is impossible (no double-grant on replay);
- on a repeated key, re-reads the live Allocation named by the stored `allocation_id` and
  returns it (replay → original object, no second action);
- is principal-scoped — a global key namespace would let one tenant's client-chosen key
  resolve another's object, a cross-tenant disclosure bug;
- writes the key only in the **success** transaction, so denials are not cached;
- is reaped by a kind-agnostic reconciler GC pass
  (`gc_idempotency_keys`, `DELETE … WHERE created_at < now() - retention`,
  default 7 days) — the table's only reaper.

The primitive is built and proven; it was simply never generalized. The other mutating
tools fall into two groups:

1. **Object-creating, no stable natural dedup key** — `runs.create`, `systems.provision`,
   `systems.define`, `investigations.open`. Each mints a row with a server-generated UUID;
   a blind retry creates a *second* row. **These are the real correctness gap.**
2. **Job-enqueuing on an existing object** — `runs.build`, `runs.install`, `runs.boot`,
   `vmcore.fetch`, `control.force_crash`, `systems.teardown`, `systems.reprovision`,
   `systems.provision_defined`. These enqueue via `queue.enqueue(conn, …, dedup_key)` with a
   dedup key derived from the *object* (e.g. `f"{run_id}:build"`, `f"{system_id}:capture_vmcore:{method}"`),
   so the job layer is *already* idempotent (`jobs.dedup_key` is `UNIQUE`; a repeat returns
   the same job). `control.power` is the deliberate exception — its dedup key embeds a fresh
   `uuid4()` so each power action is a distinct job.

The acceptance criterion is uniform: *every* object-creating / job-enqueuing mutation
accepts `idempotency_key`, and a keyed retry returns an **identical envelope** (replay), not
a second action. The allocation helper cannot serve this directly: it stores only an
`allocation_id` and re-reads an Allocation, so it cannot replay a Run/System/job envelope.

## Decision

**Generalize ADR-0040 §3 to the whole object-creating / job-enqueuing surface by reusing the
`idempotency_keys` table verbatim and storing the returned `ToolResponse` envelope itself.**
No schema change, no migration; the existing GC already reaps every kind.

### 1. Store the envelope, keyed per principal per kind

A new shared module `kdive/services/idempotency/envelope.py` provides two helpers that
operate on a caller-supplied `AsyncConnection` (so the caller controls the transaction):

- `async resolve_envelope_replay(conn, *, principal, key, kind) -> ToolResponse | None`
  — `SELECT result FROM idempotency_keys WHERE principal=%s AND key=%s AND kind=%s`;
  returns `ToolResponse.model_validate(row["result"]["envelope"])` or `None`.
- `async record_envelope(conn, *, principal, key, project, kind, envelope) -> None`
  — `INSERT … VALUES (key, principal, project, kind, jsonb)` where the jsonb is
  `{"envelope": envelope.model_dump(mode="json")}`. A `UniqueViolation` on the
  `(principal, key)` PK is mapped to a `CategorizedError(CONFLICT)` —
  *"idempotency key already in use"* — fail-closed (see §4).

`kind` is the **tool name** (e.g. `"runs.create"`, `"vmcore.fetch"`). This namespaces keys
per tool: the same client key used on `runs.create` and `runs.build` is two independent
records, never a cross-tool collision. The allocation kinds (`allocations.request` /
`allocations.renew`) keep their existing `{"allocation_id": …}` result shape and their own
helper (`services/allocation/idempotency.py`) unchanged — they re-read the *live*
Allocation, which is the right behavior for a long-lived leased object; the two helpers
coexist in the same table under disjoint `kind` values.

### 2. Record in the mutation's own transaction (atomicity)

Each handler resolves replay **before** doing work, and records the success envelope
**inside the same `conn.transaction()`** that commits the durable object / enqueues the job.
This makes "object committed but key not recorded" impossible: either both land or neither
does, so a retry can never double-create. This mirrors the allocation path exactly
(`record_key` is called inside the grant/extend transaction).

Concretely:

- **Object-creating handlers** (`runs.create`, systems admission `create_for_allocation`,
  `investigations.open`): the `idempotency_key` (and `ctx.principal`) thread down to the
  function that owns the insert transaction. After building the success envelope, but still
  inside that transaction, the handler calls `record_envelope`. Up-front (outside the write
  path) the handler calls `resolve_envelope_replay`; a hit short-circuits to the stored
  envelope with no lock taken and no work done.
- **Job-enqueuing handlers** (`runs.build`, `vmcore.fetch`, `control.*`, etc.): the job
  layer is already idempotent on its object-derived dedup key, so envelope recording is the
  *uniformity + identical-replay* layer on top. The handler resolves replay up-front;
  on a miss it enqueues (as today) and records the envelope inside the same enqueue
  transaction. `control.power` accepts the key too: when supplied it is folded into the
  job dedup key in place of the per-call `uuid4()`, making a keyed power action idempotent;
  absent, behavior is unchanged (every call is a new job).

### 3. Only success envelopes are recorded; replay is success-only

Following ADR-0040 §3, the key is written only on the success path inside the committing
transaction. A failure envelope (`status="error"`) is **never** recorded — a denial or
validation error is not cached, so the client can correct the input and retry the same key.
Replay therefore always returns a prior *success*.

### 4. Concurrency, scope, and conflict semantics

- **Principal-scoped.** Resolution and recording filter on `ctx.principal`; the PK
  `(principal, key)` means one tenant's key can never resolve another's envelope.
- **Concurrent duplicates** are serialized by the PK: two same-key calls that both miss the
  up-front read both attempt the work; the first to commit wins, the loser's
  `record_envelope` INSERT raises `UniqueViolation` → `CONFLICT`, and its transaction rolls
  back (no second object). This is the ADR-0040 §3 contract; the loser retries and now
  hits the recorded replay.
- **Cross-tool key reuse is rejected within a tool only by construction**, not across tools:
  because `kind` is part of the lookup but not the PK, the *same* `(principal, key)` used on
  two different tools collides on the PK and the second tool's record raises `CONFLICT`. This
  is acceptable and safe (fail-closed): a client should use a fresh key per logical
  operation. Documented in the envelope guide.

### 5. Retention / GC

Unchanged. `gc_idempotency_keys` already deletes any row past the retention window
regardless of `kind`, so generalizing the kinds needs no GC change. The replay/GC window
(default 7 days, `KDIVE_*` reconciler config) is documented in `async-jobs.md` (M2): a key
replays only within the retention window; after GC a repeat is treated as a fresh request.

### 6. Documentation (M2 / M3)

- **M3** — the mutation-retry idempotency contract is lifted out of per-tool prose into the
  shared `docs/guide/response-envelope.md` as a top-level "Idempotent retries" section, so
  it is stated once for the whole surface (additive; does not touch the pagination section
  #620 edits).
- **M2** — `docs/guide/async-jobs.md` documents the replay/GC window and that a keyed
  enqueue replays the same job envelope within the window.

## Consequences

- One mechanism, one table, no migration. The existing GC, retention config, and
  principal-scoping carry over for free.
- Identical-envelope replay for every object kind, because the envelope itself is stored
  (vs. the allocation path's live re-read). This satisfies the acceptance test
  (one object / one job, byte-identical envelope) directly.
- Each object-creating service grows an `idempotency_key` parameter threaded to its insert
  transaction. This is mechanical but touches several service functions; the alternative
  (a post-commit wrapper) is unsound (see Rejected).
- A stored envelope is a *frozen snapshot* — replaying `runs.create` returns the original
  `created` envelope even if the Run has since advanced. This is correct for create/enqueue
  replay (the point is "did my create happen?"), and matches job-envelope replay which
  reflects the job at enqueue time. Callers wanting current state use the object's `*.get`.
- The stored `result` jsonb now holds full envelopes (larger rows) for the new kinds. Bounded
  by the 7-day GC and the envelope's own size (no log dumps per ADR-0019). No index change.
- `control.power` gains optional idempotency without changing its default
  (every-call-is-distinct) behavior.

## Considered & rejected

- **A post-commit handler wrapper** (resolve replay before, record envelope after the
  handler returns). Rejected: the record would be in a *separate* transaction from the
  durable write, leaving a crash window where the object committed but the key did not — a
  retry then double-creates, the exact bug this fixes. Same-transaction recording is the
  whole point.
- **Reusing the allocation helper as-is** (store `{object_id}`, re-read the object on
  replay). Rejected: it cannot rebuild a Run/System/job *envelope* (it only knows
  Allocations), and re-reading returns *current* state, not the original `created`/`queued`
  envelope the acceptance test asserts is identical.
- **A new `idempotency_keys` column / table for envelopes.** Rejected: the existing
  `result jsonb` already holds arbitrary JSON; storing `{"envelope": …}` needs no DDL and
  keeps one GC path.
- **Making the key part of the PK with `kind`** (`(principal, kind, key)`). Rejected:
  widening the PK is a migration, and same-key-across-tools collision-as-`CONFLICT` is the
  safer fail-closed default (a client reusing one key for two distinct operations is a bug).
- **Wiring `idempotency_key` into pure state-transition tools** (`runs.cancel`,
  `allocations.release`, `investigations.close`, debug ops). Rejected as out of scope:
  these mutate an existing object by id and are naturally idempotent (re-releasing a
  released allocation is a no-op / stale-handle), so they create no second object or job.
- **Caching failure envelopes.** Rejected: ADR-0040 §3 records keys only on success so a
  client can fix bad input and retry the same key; caching a denial would wedge that.
