# Token-keyed staging drain + a staging-drain reconciler lane (#1559)

- **ADR:** [`../adr/0494-token-keyed-staging-drain.md`](../adr/0494-token-keyed-staging-drain.md)
- **Issue:** #1559 — *No sweep reclaims a staged rootfs base whose artifacts row is already gone*

## Problem

`sweep_investigation_staging_dir` collects a row-less `<token>.qcow2` (ADR-0452 §6), but it is
reached only from a drain tail that first requires **no** rootfs `artifacts` row to remain for the
investigation. Three reachable states never satisfy that precondition, and in each one a SENSITIVE
base of up to the 50 GiB canonical cap is collected by nothing:

| # | State | Why no lane reaches it |
|---|---|---|
| a | Never-closed investigation, all rootfs rows drained | `_CLOSE_DRIVEN_INV_SQL` needs a marker only `investigations.close` sets; `_TTL_ROOTFS_OBJECTS_SQL` is a pure `artifacts` join over zero rows |
| b | Base published between the pass's globs and its `rmdir` | `ENOTEMPTY` with no held partial clears the drain marker, so nothing revisits the directory |
| c | A checksum whose unlink or object delete faults permanently, or a base the liveness gate pins | `_finish_drained_investigation` returns before the sweep while any row remains |

## Behaviour

### Sweep

`sweep_investigation_staging_dir(uploads_dir, investigation_id, *, protected_tokens, drained)`:

1. `*.partial` — unchanged: the ADR-0446/0452 `flock` gate, no token test, and skipped
   entirely unless `drained` (ADR-0442 §7's reach, retained; widening it is #1565).
2. `*.qcow2` — collected only when `path.stem not in protected_tokens`.
3. `*.ready` — collected only when `path.stem not in protected_tokens`.
4. Return `True` (defer the drain) when a rootfs row survives, or when the walk **observed** a live
   writer holding a partial. Never from `protected_tokens` being non-empty.
5. Otherwise `rmdir`; on failure, re-run steps 1–3 **once** and retry, then warn and return `False`.
   A base left behind because a live System pins it is reported and the marker cleared, not
   deferred: a `failed` System's pin never heals, so deferring on it is a never-clearing marker.

`protected_tokens` = tokens owned by a surviving rootfs `artifacts` row ∪ tokens pinned by a live
System (`pinned_rootfs_tokens`, ADR-0441 §6 conditions (a) and (b)).

### Drain tail

`_finish_drained_investigation` no longer returns early on a surviving row. Under the
`INVESTIGATION` lock it reads both sets, runs the sweep, and clears `rootfs_cleanup_pending_at` only
when the sweep did not defer. The deferral WARNING is emitted only when the investigation is
row-drained.

### Reconciler

`sweep_unowned_investigation_rootfs_staging(conn, retention)` — a third lane, gated on its own
`ROOTFS_STAGING_DRAIN_BACKOFF` (6 h) because its worklist is a permanent steady state, whose worklist is
`_UNOWNED_STAGING_INV_SQL`: `open`/`active` investigations with a System older than `retention`
whose `provisioning_profile` names an `upload` rootfs and with no rootfs `artifacts` row. It issues
the existing `reclaim_investigation_rootfs` job with an empty `artifact_ids`. Wired into
`_REPAIR_CATALOG` as `unowned_investigation_rootfs_staging_drains_enqueued`.

## Acceptance

- A staged base with no owning row is removed by a sweep, in each of states (a), (b) and (c).
- The per-investigation staging directory drains to nothing.
- A base a surviving row owns, and its completion marker, are untouched.
- A base a live System pins is left in place, reported, and the drain marker still cleared.
- A non-empty `protected_tokens` with an empty staging directory drains silently.
- The three reconciler worklists remain pairwise disjoint on the shared dedup key.

## Out of scope

#1565 (a live-held staging partial has no retry on the TTL lane) and #1558 (defer a checksum while
a live writer holds its partial).

## Non-changes

No schema, migration, config setting, dependency, job kind, payload, MCP tool, or RBAC change.
