# Fault-inject inventory reads and orphan flags take the lock (#3748)

Builds on ADR-0072 (in-process inventory) and #3715, which locked the draw path only.

## Problem

`FaultInjectInventory` guards `_domains`, `_orphaned` and `_draws` in `record`, `forget` and
`next_draw`, but `owned_domains` iterates `_domains` and `flag_orphan` mutates `_orphaned`
without the lock. Provider ports run in worker threads and the reaper's `list_owned` runs on the
event loop, both on one shared instance. A listing that overlaps a `record` or `forget` raises
`RuntimeError: dictionary changed size during iteration`; a cancel flag landing after `forget`
stores a flag for a domain with no entry.

## Scope

In: `inventory.py` and its tests. Out: the draw-ordinal path, a durable or cross-process
inventory, and whether reconciler and worker share a process (the fix holds either way).

## Contract

- Every access to `_domains`, `_orphaned` and `_draws` happens under `self._lock`:
  `owned_domains` returns a consistent snapshot; `is_orphaned` and `system_id_of` read under it.
- `flag_orphan` stores the name only while it is in `_domains`; membership check and add are one
  critical section, so a flag after `forget` is dropped and a later domain of the same name does
  not read as orphaned.

## Failure model

- Listing during `record`/`forget`: the writer waits for the listing to finish; the listing sees
  the map before or after the write, never mid-write.
- Cancel flag after teardown: dropped, nothing for the reaper to reap or report.
- Lock scope is a few dict operations; no call out while held, so no deadlock or contention risk.

## Tests

- Mid-listing mutation, driven deterministically: a domain map whose `items()` starts a `record`
  on another thread after its first entry. Unlocked, the iteration raises; locked, the writer
  blocks until the listing returns.
- Each of `owned_domains`, `is_orphaned`, `system_id_of`, `flag_orphan` blocks while the lock is
  held.
- `flag_orphan` after `forget` leaves `is_orphaned` false, including after the name is reused.
- Controlled faults: dropping the lock in `owned_domains`, or the membership check, turns the
  matching tests red.
