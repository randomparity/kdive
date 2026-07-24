# Deadline-governed reap of uncommitted investigation uploads (#1521)

- **Issue:** [#1521](https://github.com/randomparity/kdive/issues/1521)
- **ADR:** [ADR-0444](../adr/0444-enforce-upload-deadline-at-investigation-finalize.md)
- **Status:** implemented

## Problem

ADR-0441 §6 shipped an explicitly accepted residual. The upload-manifest reaper reaps a
past-deadline investigation upload window only when the investigation is **terminal**
(`closed`/`abandoned`), because `investigations.complete_rootfs_upload` finalizes on any
OPEN/ACTIVE investigation *without re-checking the manifest deadline* — so reaping an OPEN
window would race a slow-but-legitimate finalize and delete the object out from under it.

The cost is a retention hole. An agent that PUTs the rootfs and then crashes or abandons
before calling finalize, on an investigation that is also never closed, leaves a SENSITIVE
uncommitted object in S3 with no backstop: the manifest reaper's terminal gate excludes it, and
the committed-object TTL sweep (`gc_expired_investigation_rootfs`) enumerates `artifacts` rows,
which an unfinalized upload does not have.

There is a second, independent defect. `artifacts.create_investigation_upload` already returns
`manifest_deadline`, `server_time`, and `on_expiry` to the agent (#1336 / ADR-0394), and finalize
silently ignores all three. AGENTS.md's "state a limit's full contract" invariant requires that a
limit handed to an agent be real; today it is decorative on the finalize path.

## Requirements

1. `investigations.complete_rootfs_upload` rejects a manifest whose deadline has passed, with a
   `configuration_error` that names the deadline, the reference clock, and the recovery tool.
2. A finalize inside the deadline is unaffected.
3. The reaper reaps a past-deadline **uncommitted** investigation manifest on the deadline alone,
   regardless of investigation state.
4. The committed-object skip still exempts any object with an `artifacts` row.
5. The deadline comparison uses the Postgres clock the deadline was stamped from and the reaper
   measures against — never a Python-side `datetime.now()`.
6. The agent-facing `@app.tool` wrapper docstring states the new rejection (FastMCP serializes
   only the wrapper, not the handler).

## Design

### Measure the deadline against the DB reference clock

`artifacts/upload_manifest.py` gains `deadline_stamp(conn, manifest)`, pairing an already-fetched
manifest's `deadline` with the transaction's `now()` in the existing `ManifestStamp`. It is the
read-side twin of `replace_manifest`'s write-side stamp, so both halves of the agent-facing
deadline contract are rendered from the same pair of fields. It takes the fetched manifest rather
than re-reading the row: finalize already holds it, and there is no state in which the row could
have vanished between the two reads (every `investigations` manifest mutator takes the same
`INVESTIGATION` advisory lock finalize holds), so a second read would only add an unreachable
absent-row branch.

`now()` is `transaction_timestamp()`, so the clock is the finalize transaction's *start*. That is
the conservative direction: a request that arrived before the deadline and then waited on the
investigation lock is judged by its arrival, not by how long it queued.

### Enforce at finalize

`_finalize_locked` checks the stamp after resolving the manifest entry and before the object
HEAD, inside the same transaction and `INVESTIGATION` advisory lock:

```text
stamp.deadline < stamp.server_time  →  configuration_error, reason="upload_window_expired"
```

The error data carries `manifest_deadline`, `server_time`, and `on_expiry` in the same shape and
ISO-8601 UTC rendering `create_investigation_upload` used to advertise them, and
`suggested_next_actions` points at `artifacts.create_investigation_upload`. The `reason` value
reuses the one `runs.complete_build` already raises for an expired run upload window, so both
finalize paths speak one vocabulary.

The `no_upload_manifest` rejection — which is what a finalize arriving *after* a reap now sees —
gains the same `suggested_next_actions`, so every "your window is gone" path is self-correcting.

### Reap on deadline

`reconciler/cleanup/uploads.py` drops the investigation-state gate:

- the candidate select becomes `WHERE deadline < now() AND owner_kind = ANY(%s)` over the two
  known owner kinds, with no `investigations` state predicate;
- `reap_one_owner`'s locked re-read keeps its `deadline < now()` re-check (which is what declines
  a manifest renewed since the candidate select) and loses the state re-check;
- `owner_reapable`, `_UPLOAD_REAPABLE_STATES`, and `_OWNER_REAPABLE_QUERIES` become dead and are
  removed. The lock scope is resolved through an explicit `_LOCK_SCOPES` mapping that raises on an
  unknown owner kind, keeping the fail-loud property `owner_reapable` carried.

The per-key committed-object skip is untouched: it is the invariant that keeps a finalized rootfs
safe, and it now carries the whole safety burden for OPEN/ACTIVE investigations.

The `runs` branch does not change. It already reaps on the deadline alone for every Run state.

## Acceptance criteria

- AC-1 Finalize on a past-deadline manifest returns `configuration_error` with
  `reason="upload_window_expired"`, `data.manifest_deadline`, `data.server_time`,
  `data.on_expiry.tool`, and `artifacts.create_investigation_upload` in
  `suggested_next_actions`; the manifest and the object are left alone (no committed row).
- AC-2 Finalize inside the deadline still writes the row and returns the checksum handle.
- AC-3 The reaper reaps a past-deadline uncommitted manifest on an `open` investigation and on an
  `active` one — object deleted, manifest row gone, count 1.
- AC-4 A committed object (with an `artifacts` row) is exempt on an OPEN investigation, exactly as
  on a terminal one; the manifest is still reaped.
- AC-5 A manifest whose deadline is in the future is not reaped (unchanged).
- AC-6 An unknown owner kind fails loud rather than being silently locked under the wrong scope.
- AC-7 The `investigations.complete_rootfs_upload` wrapper docstring states the expiry rejection
  and names the re-mint tool.
