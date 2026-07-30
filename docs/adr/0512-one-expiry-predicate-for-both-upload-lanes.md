# 0512 — One expiry predicate for both upload lanes; the envelope stays per-lane

## Status

Accepted (2026-07-30)

## Context

Two finalize paths enforce the same upload-window deadline. Both fetch a manifest, pair its
`deadline` with the Postgres reference clock via `deadline_stamp`
(`src/kdive/artifacts/upload_manifest.py`), reject a past-deadline finalize, and render the
self-correcting envelope through the shared `upload_expiry_contract` renderer that PR #1542
already hoisted.

What #1542 did **not** hoist is the comparison itself. Each lane wrote its own, and the two are
spelled as exact logical negations:

```text
runs            if stamp.deadline <  stamp.server_time:  → raise CompleteBuildExpiredWindowError
investigations  if stamp.deadline >= stamp.server_time:  → return None (window open)
```

They agree today, including at the boundary: both treat `deadline == server_time` as **open**.
So this is not a live defect. It is drift surface, and it is the same shape of drift surface that
produced the #1523 / #1534 class of defect — two staging codecs and two `complete_build` paths
each drifted because one rule lived in more than one place. Any future change to the rule — a
grace period, strictness at equality, a skew allowance — lands on whichever spelling the author
was reading, and the two lanes silently disagree about when a window closes. Negated spellings
make that worse than duplication: a reviewer comparing them must mentally invert one before the
divergence is even visible.

The reason code duplicated the same way. `UPLOAD_WINDOW_EXPIRED = "upload_window_expired"` was
defined in `services/runs/complete_build.py` and imported by the runs MCP handler, while the
investigations tool carried the bare literal `"upload_window_expired"`. The investigations lane
could not import the constant without reaching into a *runs service* module for a string that is
neither runs-specific nor service-specific.

The structural obstacle to a naive fix is that the two lanes enforce at **different layers**.
Runs enforces in the service layer and signals by raising `CompleteBuildExpiredWindowError`,
which the MCP handler catches and maps. Investigations enforces in the MCP tool layer and signals
by returning a `ToolResponse`. A shared helper that returned either lane's signalling type would
force one lane to import the other's, so the shared thing has to be a **neutral verdict**.

## Decision

Hoist exactly two things into `kdive.artifacts.upload_manifest`, beside `ManifestStamp` and
`deadline_stamp` that both lanes already import:

1. **`ManifestStamp.expired`**, a property returning `self.deadline < self.server_time`. A bare
   `bool` — the narrowest neutral verdict — so the service lane can raise on it and the tool lane
   can return on it, and neither imports the other's signalling type. It sits on `ManifestStamp`
   rather than beside it as a free function because the two fields the comparison reads are the
   whole of that type, and the stamp is already the object both lanes hold at the decision point.

2. **`UPLOAD_WINDOW_EXPIRED`**, moved out of `services/runs/complete_build.py` with its value
   unchanged. Both lanes now name it. The old definition is deleted rather than re-exported.

Equality stays **open**: `deadline == server_time` is not expired. That is today's behaviour in
both lanes, and preserving it is the point of the change, so it is pinned by a test rather than
left to the reader of a `<`.

**The rejection envelope is deliberately not unified.** Each lane keeps its own `detail` wording
(`"the build upload window has expired…"` vs `"the rootfs upload window has expired…"`) and its
own `suggested_next_actions` (`artifacts.create_run_upload` vs
`artifacts.create_investigation_upload`). Those strings are agent-visible and asserted in
behavior tests; collapsing them into one shared wording would be a schema change wearing a
refactor's clothes. The envelope is already as shared as it should be — `upload_expiry_contract`
renders the three fields that must agree, and the two fields that must differ stay at the call
sites.

A guard test fails any new hand-rolled comparison of a stamp's two fields outside the module that
owns the predicate, so the spelling cannot silently return.

## Consequences

- A change to the expiry rule — grace period, strictness at equality, skew allowance — has one
  edit site and reaches both lanes, or fails the guard test.
- The wire is unchanged. The reason literal, both `detail` strings, both
  `suggested_next_actions` lists, and the three `upload_expiry_contract` fields are byte-identical
  before and after; the behavior tests that assert them were not modified.
- `services/runs/complete_build.py` no longer exports `UPLOAD_WINDOW_EXPIRED`. An importer of
  the old path fails at import, loudly, rather than reading a stale copy.
- `kdive.artifacts.upload_manifest` now holds one agent-facing reason string. That is a small
  widening of a storage module's remit, consistent with the module's existing role as the place
  where facts several mechanisms must agree on live (`UPLOAD_TENANT`, `_LOCK_SCOPES`), and it is
  the only layer both an `artifacts`-layer service and an `mcp`-layer tool can import without one
  reaching into the other.
- The boundary case is now stated once and tested, instead of being implied by two opposite
  operators.

## Considered & rejected

- **Leave it; the two spellings agree today.** True, and it is exactly the argument that was
  available before #1523 and #1534. The cost of the fix is one property and one moved constant;
  the cost of the drift is a lane-specific expiry rule nobody notices until an agent's finalize is
  rejected on one lane and accepted on the other.
- **Hoist a shared function that returns the rejection.** The obvious "share more" move, and the
  one the layer split forbids: the runs lane needs an exception the service can raise without
  importing `kdive.mcp`, the investigations lane needs a `ToolResponse`. Any single return type
  drags one lane's dependencies into the other's, or invents a third envelope type both must
  translate through.
- **Unify the `detail` string and the `suggested_next_actions` too.** Superficially the tidier
  end state, and a real wire change: the recovery tool genuinely differs per lane, and the detail
  names the artifact the agent was uploading. Merging them would tell an agent finalizing a rootfs
  to re-mint a build upload. Rejected as a schema change disguised as deduplication.
- **Put the predicate on `UploadManifest` instead of `ManifestStamp`.** `UploadManifest` carries
  a deadline but no reference clock, so the property would have to take the clock as an argument —
  and a caller free to pass any clock is free to pass `datetime.now()`, which is the Python-side
  comparison both lanes exist to avoid. `ManifestStamp` already binds the deadline to the
  Postgres clock it must be measured against.
- **Keep `UPLOAD_WINDOW_EXPIRED` in the runs service and import it from the investigations
  tool.** Zero-diff for the runs lane, but it makes an investigations MCP tool depend on a runs
  service module for a string owned by neither. The next reader would reasonably conclude the
  investigations lane is part of the runs finalize.
- **Re-export the constant from its old location for compatibility.** There is exactly one
  importer in-tree and no external consumer of a private service module, so a shim would buy
  nothing and leave two names for one string — the condition this record exists to remove.
