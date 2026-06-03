# Repository Layer, Advisory Locks & Idempotency Ledger — Design

**Issue:** #7 (M0) · **Depends on:** #6 (schema + migration runner, merged) ·
**Decisions:** [ADR-0016](../../adr/0016-repository-layer-locks-idempotency.md) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)

## Goal

The data-access layer for the M0 walking skeleton: typed async CRUD over each
durable object, per-Allocation/per-System serialization via Postgres advisory
locks, and idempotent step execution. Three new modules under `src/kdive/db/`:

- `repositories.py` — async `insert` / `get` / `update_state` per object, returning
  `kdive.domain.models` instances; every state change is guarded by
  `kdive.domain.state.can_transition`.
- `locks.py` — `async with advisory_xact_lock(conn, scope, key)`, wrapping
  `pg_advisory_xact_lock` (transaction-scoped, pooler-safe per ADR-0005).
- `idempotency.py` — `run_step(conn, run_id, step, fn)`: return the stored result
  if the `(run_id, step)` ledger row exists, else run `fn`, store, return.

This layer sits between the domain models (#5) / schema (#6) below it and the MCP
handlers, worker, and reconciler (later issues) above it. It owns *how state is
read and written*; it does not own *when* (that is the handlers' policy).

## Non-goals

- No handler, worker, reconciler, or MCP wiring — those consume this layer later.
- No `audit_log` writer — auditing lands with the handler issue that emits
  transitions; `audit_log` is append-only and has no lifecycle to manage here.
- No `PROJECT_BUDGET` lock scope — admission control is ADR-0007's issue; shipping
  the scope now would be a speculative, unused value.
- No `*Create` input models — see "Insert contract" below.

## Components

### `repositories.py` — typed async CRUD

A single generic `Repository[M]` parameterized per object, instantiated once per
table at module scope. One class because the CRUD body is otherwise written eight
times (the CLAUDE.md "rule of three" is met and exceeded).

```python
M = TypeVar("M", bound=DomainModel)

class Repository(Generic[M]):
    def __init__(
        self,
        model: type[M],
        table: str,
        *,
        state_column: str | None = "state",
        state_enum: type[StrEnum] | None = None,
        json_columns: frozenset[str] = frozenset(),
    ) -> None: ...

    async def insert(self, conn: AsyncConnection, obj: M) -> M: ...
    async def get(self, conn: AsyncConnection, obj_id: UUID) -> M | None: ...
    async def update_state(
        self, conn: AsyncConnection, obj_id: UUID, new_state: StrEnum
    ) -> M: ...
```

Module-level instances (the eight durable objects):

| instance | model | table | state column | update_state? |
|----------|-------|-------|--------------|---------------|
| `RESOURCES` | `Resource` | `resources` | `status` (`ResourceStatus`) | yes |
| `ALLOCATIONS` | `Allocation` | `allocations` | `state` (`AllocationState`) | yes |
| `SYSTEMS` | `System` | `systems` | `state` (`SystemState`) | yes |
| `INVESTIGATIONS` | `Investigation` | `investigations` | `state` (`InvestigationState`) | yes |
| `RUNS` | `Run` | `runs` | `state` (`RunState`) | yes |
| `DEBUG_SESSIONS` | `DebugSession` | `debug_sessions` | `state` (`DebugSessionState`) | yes |
| `JOBS` | `Job` | `jobs` | `state` (`JobState`) | yes |
| `ARTIFACTS` | `Artifact` | `artifacts` | `None` | no (write-once) |

**Column mapping.** Column names are `tuple(model.model_fields)`; they already
match the SQL columns one-for-one (verified against `0001_init.sql`). Rows are read
with psycopg's `dict_row` factory and re-validated through `model.model_validate`.

**Insert contract.** `insert` persists the object as given, with one exception:
`created_at` / `updated_at` take their database defaults and are returned via
`RETURNING *`, so the **database is the authority for timestamps**. `id` is
caller-minted (the model already requires it; minting it client-side avoids a
pre-insert round-trip). The model's `created_at` / `updated_at` are therefore
advisory on insert — documented on the method. This keeps a future move to
server-generated-id + `*Create` models purely additive: no code can come to depend
on caller-supplied timestamps. jsonb columns (`json_columns`) are wrapped in
psycopg's `Jsonb` adapter; all other values adapt natively (`UUID`→uuid,
`datetime`→timestamptz, `StrEnum`→text since `StrEnum` is a `str`).

**`update_state`.** Atomic read-check-write inside the method's own transaction:

```
async with conn.transaction():
    row = SELECT <state_column> FROM <table> WHERE id = %s FOR UPDATE
    if row is None: raise ObjectNotFound
    current = state_enum(row[state_column])
    ensure_transition(current, new_state)            # raises IllegalTransition
    return UPDATE <table> SET <state_column> = %s WHERE id = %s RETURNING *
```

`FOR UPDATE` serializes concurrent updaters on the same row, so the guard check and
the write cannot interleave. `conn.transaction()` works whether or not the caller
holds an outer transaction (it opens a real transaction or a nested savepoint), so
the row lock is held across both statements regardless of the connection's
autocommit setting. `update_state` composes beneath an `advisory_xact_lock` (which
serializes the broader operation) but does not require one.

**Errors.** `get` returns `None` on a miss (a lookup may legitimately miss).
`update_state` raises `ObjectNotFound` (a `RuntimeError` subclass) on a missing
row and `IllegalTransition` (already in `domain.state`) on a disallowed edge — both
programming/consistency errors, distinct from `CategorizedError` (reserved for
operational failures a handler turns into a client response).

### `locks.py` — advisory transaction locks

```python
class LockScope(StrEnum):
    ALLOCATION = "allocation"
    SYSTEM = "system"

@asynccontextmanager
async def advisory_xact_lock(
    conn: AsyncConnection, scope: LockScope, key: UUID
) -> AsyncIterator[None]:
    if conn.autocommit:
        raise RuntimeError(
            "advisory_xact_lock requires a non-autocommit connection; a "
            "transaction-scoped lock on an autocommit connection releases "
            "immediately (ADR-0005)"
        )
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope, key),))
    yield
```

**Lock key.** `_lock_key(scope, key)` derives a deterministic signed 64-bit integer:
`blake2b(digest_size=8)` over `scope` bytes, a `0x00` separator, and `str(key)`
bytes, read back with `int.from_bytes(..., "big", signed=True)`. The
**single-bigint** `pg_advisory_xact_lock(bigint)` is a lock space disjoint from
migrate.py's two-int `(class, objid)` migration lock, so application and migration
locks never contend (ADR-0015 already documents this reservation). Hashing folds an
unbounded `(scope, UUID)` key space onto 64 bits; a collision causes two unrelated
keys to over-serialize (safe — never under-serialize). The `0x00` separator removes
`scope`/`key` boundary ambiguity for the NUL-free identifiers used here.

**Release.** `pg_advisory_xact_lock` has no manual unlock; it releases at
transaction end (COMMIT/ROLLBACK). The context manager therefore only acquires —
the surrounding transaction's end is the release. The `autocommit` guard ensures
such a transaction exists; otherwise the lock would be a silent no-op.

### `idempotency.py` — step ledger

```python
JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

async def run_step(
    conn: AsyncConnection,
    run_id: UUID,
    step: str,
    fn: Callable[[], Awaitable[JsonValue]],
) -> JsonValue: ...
```

Logic:

1. `SELECT result FROM run_steps WHERE run_id = %s AND step = %s`. If the **row**
   exists, return its `result` (a stored JSON `null` returns `None`, distinct from
   "no row" — existence is tested on the row, not the value).
2. Else `result = await fn()`.
3. `INSERT INTO run_steps (run_id, step, state, result) VALUES (%s, %s, 'succeeded',
   %s) ON CONFLICT (run_id, step) DO NOTHING RETURNING result`. If a row came back,
   return our `result`.
4. Otherwise a concurrent caller inserted first; re-`SELECT` and return theirs.

**Failure semantics.** If `fn` raises, nothing is inserted, so the step is not
recorded and a later call retries — a failed step never poisons the ledger.
`run_step` records only *successful* step results; the Run's own `failed`
transition is the handler's responsibility, not this function's.

**Concurrency.** Re-runs (sequential) never re-execute `fn` (the acceptance
criterion). A true concurrent *first* call may run `fn` on both racers, but the
unique `(run_id, step)` key makes the first commit win and both callers return that
stored result; the second `fn`'s result is discarded. Single-execution under
concurrency is provided one layer up by the operation's `advisory_xact_lock` and
the job `dedup_key` at admission (per the m0 spec's layered guarantee). `run_steps.state`
is free `text` (no CHECK in the schema) — a ledger-bookkeeping field, set to
`'succeeded'` here.

## Data flow (illustrative, a future handler)

```
handler (later issue), inside one pooled connection + transaction:
  async with conn.transaction():
    async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
      result = await run_step(conn, run_id, "build", do_build)   # idempotent
      await SYSTEMS.update_state(conn, system_id, SystemState.READY)
  # transaction commit releases the advisory lock and persists the row writes
```

## Error handling summary

| Condition | Raised | Kind |
|-----------|--------|------|
| `KDIVE_DATABASE_URL` unset (existing) | `CategorizedError(CONFIGURATION_ERROR)` | operational |
| `update_state` on missing row / lost CAS | `ObjectNotFound(RuntimeError)` | consistency |
| disallowed transition | `IllegalTransition(ValueError)` | programming |
| `advisory_xact_lock` on autocommit conn | `RuntimeError` | programming |
| `fn` raises in `run_step` | propagates; nothing recorded | caller's |

## Testing strategy

Disposable Postgres via the existing `testcontainers` fixtures
(`tests/db/conftest.py`); async code is driven with `asyncio.run(...)` (the
established pattern in `test_pool.py` — no `pytest-asyncio` dependency). A new
`migrated_url` fixture applies migrations to the clean per-test schema and yields
the conninfo for async connections.

- **repositories** — round-trip insert→get for every object (jsonb columns
  included); `get` miss returns `None`; legal `update_state` returns the new state
  with a DB-bumped `updated_at`; illegal transition raises `IllegalTransition`;
  `update_state` on a missing id raises `ObjectNotFound`; concurrent CAS on the same
  row — one wins, the loser raises; timestamps come from the DB, not the input.
- **locks** (the headline acceptance) — two real connections: A holds the lock in a
  transaction; B blocks (proven: B's acquisition task is not `done()` after a wait);
  A commits; B proceeds (`asyncio.wait_for` resolves). Plus: different key does not
  block; different scope does not block; autocommit connection raises;
  `_lock_key` is deterministic and scope-sensitive.
- **idempotency** (the headline acceptance) — `run_step` runs `fn` once across two
  calls (`call count == 1`), both return the same result; `None`/`list`/`dict`
  results round-trip; distinct steps are independent; `fn` raising leaves no row and
  the next call re-executes; concurrent first-call race resolves to one stored
  result.

The env-gated libvirt/gdb/drgn integration tests are untouched and stay gated.

## Files

- Create `src/kdive/db/repositories.py`, `src/kdive/db/locks.py`,
  `src/kdive/db/idempotency.py`.
- Create `tests/db/test_repositories.py`, `tests/db/test_locks.py`,
  `tests/db/test_idempotency.py`; extend `tests/db/conftest.py` with `migrated_url`.
- Create `docs/adr/0016-repository-layer-locks-idempotency.md`; add it to
  `docs/adr/README.md`.
