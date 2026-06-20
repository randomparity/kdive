# Spec: Uniform `idempotency_key` on mutations (#619, AX_REVIEW A1)

Status: accepted · ADR: [0193](../adr/0193-uniform-mutation-idempotency.md) · Issue: #619 (part of #618)

## Goal

Add an optional `idempotency_key: str | None` to every object-creating / job-enqueuing MCP
mutation. A repeated key returns the **identical prior envelope** (replay) instead of acting
again, so a transport reset on the initial enqueue/create cannot double-create a durable
object or double-enqueue a job. Reuse the existing `idempotency_keys` table + reconciler GC
that already back `allocations.{request,renew}` (ADR-0040 §3). No schema change, no migration.

Fold in the two minor findings:
- **M2** — document the replay / GC window in `docs/guide/async-jobs.md`.
- **M3** — lift the mutation-retry idempotency contract into the shared
  `docs/guide/response-envelope.md`, stated once for the whole surface.

## Non-goals

- No schema, table, or migration change — reuse `idempotency_keys.result jsonb` and
  `gc_idempotency_keys` verbatim.
- No change to the allocation idempotency path (`services/allocation/idempotency.py`); it
  keeps re-reading the live Allocation under its own kinds.
- No `idempotency_key` on pure state-transition mutations that act on an existing object by
  id and are naturally idempotent: `runs.cancel`, `runs.bind`, `runs.complete_build`,
  `allocations.release`, `investigations.{close,set,link,unlink}`, `jobs.cancel`, and the
  `debug.*` / `resources.*` / `accounting.*` / `shapes.*` / `images.*` / `ops.*` mutations.
  (These create no second durable object or job; re-applying a transition is a no-op or a
  stale-handle.)
- No change to `control.power`'s default semantics (every call is a distinct job) — the key
  is *opt-in* idempotency.

## In-scope tools

| Tool | Effect | Today's dedup | Change |
|---|---|---|---|
| `runs.create` | creates a **Run** (server UUID) | none | key threaded to insert txn; record envelope |
| `systems.provision` | creates a **System** + enqueues PROVISION | none on the System | key threaded to admission txn; record envelope |
| `systems.define` | creates a **System** (`defined`) | none | key threaded to admission txn; record envelope |
| `investigations.open` | creates an **Investigation** | none | key threaded to insert txn; record envelope |
| `runs.build` | enqueues BUILD | `f"{run_id}:build"` (idempotent) | accept key; record envelope in enqueue txn |
| `runs.install` | enqueues INSTALL | object-derived | accept key; record envelope in enqueue txn |
| `runs.boot` | enqueues BOOT | object-derived | accept key; record envelope in enqueue txn |
| `vmcore.fetch` | enqueues CAPTURE_VMCORE | `f"{system_id}:capture_vmcore:{method}"` | accept key; record envelope in enqueue txn |
| `control.force_crash` | enqueues FORCE_CRASH | `f"{system_id}:force_crash"` | accept key; record envelope in enqueue txn |
| `control.power` | enqueues POWER | `f"{system_id}:power:{action}:{uuid4()}"` | accept key; key replaces the `uuid4()` in dedup key when supplied; record envelope |
| `systems.provision_defined` | enqueues PROVISION on a `defined` System | object-derived | accept key; record envelope in enqueue txn |
| `systems.reprovision` | enqueues REPROVISION | object-derived | accept key; record envelope in enqueue txn |
| `systems.teardown` | enqueues TEARDOWN | object-derived | accept key; record envelope in enqueue txn |

Already done (no change): `allocations.request`, `allocations.renew`.

## Mechanism

### Shared helper — `kdive/services/idempotency/envelope.py`

```python
async def resolve_envelope_replay(
    conn: AsyncConnection, *, principal: str, key: str, kind: str
) -> ToolResponse | None:
    """Return the stored envelope for (principal, key) under `kind`, or None."""

async def record_envelope(
    conn: AsyncConnection, *, principal: str, key: str, project: str,
    kind: str, envelope: ToolResponse
) -> None:
    """Persist `envelope` for (principal, key) in the caller's transaction.

    Raises CategorizedError(CONFLICT) on the (principal, key) PK UniqueViolation.
    """
```

- `result` jsonb shape: `{"envelope": envelope.model_dump(mode="json")}`. Replay rebuilds
  via `ToolResponse.model_validate(row["result"]["envelope"])`.
- `kind` is the **tool name** string (e.g. `"runs.create"`). One named constant per tool,
  colocated with the handler (mirroring `_RENEW_KIND` in `services/allocation/renew.py`).
- `record_envelope` uses the same `INSERT … VALUES (key, principal, project, kind, result)`
  as `services/allocation/idempotency.record_key`, differing only in the jsonb payload and
  the `CONFLICT` category (the allocation helper uses `CONFIGURATION_ERROR`; for a generic
  mutation `CONFLICT` is the more specific "key already in use under a different in-flight or
  completed operation").

### Handler integration pattern

Every in-scope handler follows the same shape:

```python
async def handler(pool, ctx, ..., idempotency_key: str | None = None) -> ToolResponse:
    # ... validate inputs (cheap, no DB) ...
    async with pool.connection() as conn:
        if idempotency_key is not None:
            replay = await resolve_envelope_replay(
                conn, principal=ctx.principal, key=idempotency_key, kind=_KIND)
            if replay is not None:
                return replay
        # ... existing authz + work, opening conn.transaction() to commit the object/job ...
        # inside that SAME transaction, after building `envelope`:
        if idempotency_key is not None:
            await record_envelope(
                conn, principal=ctx.principal, key=idempotency_key,
                project=project, kind=_KIND, envelope=envelope)
        return envelope
```

Two integration points per handler:
1. **Up-front replay** — a read on the pooled connection before any lock/work. On a hit,
   short-circuit; nothing is locked, no job enqueued, no row inserted.
2. **In-transaction record** — `record_envelope` is called inside the existing
   `conn.transaction()` that commits the durable object / enqueues the job, so the key and
   the effect are atomic. The success envelope must be *constructed before* the transaction
   commits (it already is — the handlers build the envelope from the inserted row / enqueued
   job before returning).

For the **object-creating** services (`create_run`, `SystemAdmission.create_for_allocation`,
`open_investigation`), `idempotency_key` and `ctx.principal` thread from the registrar →
handler → the function owning the insert transaction. The replay check stays at the handler
boundary (one connection, opened once).

For the **job-enqueuing** handlers, the enqueue already happens inside a `conn.transaction()`
(e.g. `_build_locked`); `record_envelope` is added in that block after the job is enqueued
and the envelope built.

`control.power`: when `idempotency_key` is supplied, the job dedup key becomes
`f"{system_id}:power:{action}:{idempotency_key}"` (replacing the `uuid4()`); the envelope is
recorded under `kind="control.power"`. Absent, the `uuid4()` path is unchanged and nothing
is recorded.

### Registrar changes

Each in-scope registrar adds the parameter, exactly as `allocations.request` already has it:

```python
idempotency_key: Annotated[
    str | None,
    Field(description="Replay-safe key; a repeated key returns the prior envelope."),
] = None,
```

and forwards it to the handler. No annotation change (`_docmeta.mutating()` unchanged); no
exposure/RBAC change (the tools stay in their current `_TOOL_SCOPES` entries); the new
parameter is additive and optional so the `outputSchema` / tool-doc drift guards see only an
added input field.

## Replay contract (the invariants tests must pin)

1. **No second action on replay.** A keyed call, then a repeat with the same
   `(principal, key)` for the same tool ⇒ exactly one durable object / one job, and the
   second call returns the byte-identical envelope (compare `model_dump`).
2. **Atomicity.** If the recording transaction rolls back, neither the object nor the key
   persists (simulate by forcing `record_envelope` to raise inside the txn → assert no Run/
   System row and no `idempotency_keys` row).
3. **Failure not cached.** A keyed call that returns `status="error"` records **no** key; a
   subsequent corrected call with the same key proceeds and succeeds.
4. **Principal scope.** Principal A's key never resolves principal B's envelope (same key
   string, different principal ⇒ miss, then a fresh record).
5. **Concurrent duplicate.** Two same-key calls that both miss the up-front read ⇒ one
   commits, the other's `record_envelope` raises `CONFLICT` and rolls back (no second
   object); a third call with that key now replays the winner's envelope.
6. **Cross-tool key reuse** ⇒ the second tool's record raises `CONFLICT` (PK is
   `(principal, key)`, not `(principal, key, kind)`).
7. **Unkeyed unchanged.** `idempotency_key=None` ⇒ today's behavior exactly (no read, no
   record); two unkeyed calls create two objects.
8. **GC window.** A key older than the retention window is GC'd; a repeat after GC is a fresh
   request (covered by the existing `gc_idempotency_keys` test extended for a non-allocation
   `kind`).

## Acceptance-test outline

A worker/server-less integration test against the testcontainer Postgres:

- `test_runs_create_replays_on_keyed_retry` — create a Run with key K; simulate a transport
  drop (discard the first envelope); call `runs.create` again with K; assert exactly one
  `runs` row and an identical envelope. The canonical acceptance test from the issue.
- mirror for `systems.provision` (one System row, one PROVISION job), `vmcore.fetch` (one
  CAPTURE_VMCORE job), and `investigations.open`.
- the invariants 2–8 above as focused unit tests on the helper + one representative handler.

## Docs

- **M3** `docs/guide/response-envelope.md`: a new top-level "Idempotent retries" section
  (additive, not in the pagination region #620 edits) stating: every object-creating /
  job-enqueuing mutation accepts `idempotency_key`; a repeated key within the retention
  window replays the prior envelope; keys are principal-scoped; a key is recorded only on
  success; reuse one key per logical operation (cross-operation reuse fails closed).
- **M2** `docs/guide/async-jobs.md`: document the replay/GC window — a keyed enqueue replays
  the same job envelope within the reconciler retention window (default 7 days); after GC a
  repeat is a fresh enqueue (still idempotent at the job layer via the object-derived dedup
  key for the job-enqueue tools).

## Risk / rollback

- The change is additive and opt-in: every handler's behavior with `idempotency_key=None` is
  byte-identical to today, so the blast radius is the new keyed path only.
- Rollback = revert the branch; no migration to undo, no data shape change to live rows
  (existing allocation rows are untouched; new-kind rows are GC'd within the window).
