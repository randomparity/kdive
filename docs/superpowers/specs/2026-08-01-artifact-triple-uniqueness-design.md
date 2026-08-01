# Artifact ownership triple uniqueness

## Scope

Issue #1750 requires a database backstop for artifact ownership identity after worker PUTs
moved outside advisory-lock spans. Migration 0094 adds the constraint, worker registration
handles a losing insert deliberately, a database test proves the constraint is effective, and
ADR-0519 points to ADR-0528 as the resolution. Object reconciliation and unrelated artifact
ownership changes remain out of scope.

## Decision

An artifact catalog claim is identified by `(owner_kind, owner_id, object_key)`. A forward-only
unique index applies to every owner kind rather than enumerating today's worker kinds. The wider
scope matches the row-driven object lifecycle invariant: two rows with the same owner and key are
duplicate claims regardless of which producer wrote them.

Worker phase-3 registration uses a focused `insert_artifact_if_absent` helper. It issues `INSERT
... ON CONFLICT (owner_kind, owner_id, object_key) DO NOTHING RETURNING ...` and returns `None`
when a concurrent claim won. Existing phase-3 handlers then load the winning row and follow their
current claimed-object behavior, including stat-based etag repair outside the advisory lock. The
probe remains as a fast path; the insert conflict is the authoritative backstop.

The alternative of allowing the integrity error to abort and retry the whole job was rejected:
the object PUT already happened, so an undifferentiated retry loses the handler's established
compensation and etag-repair path. Replacing the probe with an upsert was also rejected because
the losing attempt must not overwrite the winning row's etag based on lock acquisition order.

## Data flow and failure handling

1. The worker writes the object without holding its allocation/system advisory lock.
2. Phase 3 acquires the existing lock and probes for an already registered triple.
3. If absent, it attempts the conflict-aware insert.
4. A successful insert proceeds normally. A conflict loads the committed winner and treats it
   exactly like a positive phase-3 probe.
5. Any required etag reconciliation stats the object after releasing the lock; it never assumes
   the losing attempt's etag is authoritative.

Migration deployment fails if historical duplicate triples already exist. That is deliberate:
silently deleting or choosing among conflicting durable claims would be a destructive data repair
outside this issue's authority. Operators must inspect and resolve such data before retrying the
forward migration.

## Testing

- A disposable-Postgres test inserts two rows with the same ownership triple and different ids,
  and asserts PostgreSQL raises `UniqueViolation` on the second insert.
- Focused worker tests force the insert helper's no-op result and prove each phase-3 path adopts
  the winning row rather than surfacing an integrity error or deleting a claimed object.
- The repository guardrail remains `just ci`.
