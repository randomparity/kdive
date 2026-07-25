# ADR 0448 — Enforce the upload deadline on both `runs.complete_build` paths

- **Status:** Accepted
- **Date:** 2026-07-24
- **Supersedes:** [ADR-0444](0444-enforce-upload-deadline-at-investigation-finalize.md) §3's
  "one asymmetry is named rather than fixed here" clause — the follow-up it called for. ADR-0444's
  decisions 1, 2, and 4 are retained unchanged.
- **Depends on:** [ADR-0394](0394-upload-deadline-contract-fields.md) (the `server_time` /
  `manifest_deadline` / `on_expiry` contract this makes real on the run finalize path),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the `upload_manifests` window and its
  reaper), [ADR-0104](0104-chunked-external-upload-reassembly.md) (the chunked reassembly path that
  already consulted the deadline).
- **Spec:** [`../specs/2026-07-24-run-finalize-deadline-1534-design.md`](../specs/2026-07-24-run-finalize-deadline-1534-design.md)

## Context

ADR-0444 §3 named this defect while declining to fix it: `runs.complete_build` re-checks the upload
manifest deadline only on its **chunked** path, where `_reassemble_chunked_artifacts` calls
`upload_manifest.refresh_deadline` — which updates the row only when `deadline >= now()` and reports
whether it did. A **non-chunked** run finalize never reaches that call. `_prepare` fetches the
manifest, derives the object keys, and hands them to validation; the `deadline` column rides along
on the fetched `UploadManifest` and is then ignored.

So `artifacts.create_run_upload` hands the agent `manifest_deadline`, `server_time`, and `on_expiry`
(#1336, ADR-0394), and on the single-PUT path finalize honors none of them. AGENTS.md's "state a
limit's full contract" invariant requires that a limit an agent is handed be real; here it is
decorative, which is precisely the distrust #1336 was filed about.

This is *less* consequential than the investigation-lane version #1521 fixed. There, the reaper had
to gate itself out of OPEN/ACTIVE investigations *because* a legitimate finalize might still arrive,
and that gate was a SENSITIVE-bytes retention hole. The runs reaper never had such a gate — it
already reaps on the deadline alone for every Run state (ADR-0104 §7) — so nothing here is blocking
a reap. What remains is a contract defect and an unexplained asymmetry between two finalize paths,
which is the kind of divergence that gets rediscovered as a surprise.

## Decision

### 1. `_prepare` enforces the deadline for both paths

The check goes in `CompleteBuildFinalizer._prepare`, immediately after the `no_upload_manifest`
guard and **before** the chunked branch, so one site covers both lanes and they cannot drift apart
again. It pairs the already-fetched manifest's `deadline` with the transaction's `now()` through
ADR-0444's read-side `upload_manifest.deadline_stamp` and rejects when `deadline < server_time`.

The comparison is against the **Postgres** clock — the one that stamped `deadline = now() + ttl` and
the one the reaper measures against. No Python-side `datetime.now()` enters it, so a session-TZ or
host-clock disagreement cannot make finalize and the reaper reach opposite verdicts on one manifest.
(`now()` is `transaction_timestamp()`: the finalize transaction's start, not the instant the row is
read, so a request that arrived inside the window is never penalized for time spent queueing behind
the `RUN` advisory lock.)

The rejection keeps `reason="upload_window_expired"` — the reason this tool already raised on its
chunked path and the one `investigations.complete_rootfs_upload` adopted in ADR-0444, so all three
sites speak one vocabulary — and becomes **self-correcting**: it carries `manifest_deadline`,
`server_time`, and `on_expiry` in the same shape and ISO-8601 UTC rendering
`artifacts.create_run_upload` advertised them with, and puts that tool in
`suggested_next_actions`. The agent is told the wall it hit, the clock the wall is measured on, and
the one call that re-mints.

Recovery is **re-mint, not re-upload**, and holds for a different reason than in the investigation
lane. There the object key is content-addressed, so re-minting the same declaration addresses the
same object. Here the key is `owner_prefix("local", "runs", <run_id>) + <entry name>` —
*owner*-addressed — which is equally stable across re-mints of the same Run and artifact name. A
re-mint plus a second finalize therefore recovers without re-uploading a byte, provided the reaper
has not already collected the object and the re-mint declares the same name with the same `sha256`
(a changed checksum leaves the stale object in place and fails validation, which is a re-upload).

### 2. Rendering stays in the response layer; the shared helper is reused

`src/kdive/services/**` imports nothing from `kdive.mcp` today, and this change does not invert
that. The service raises a new `CompleteBuildExpiredWindowError` carrying the raw `ManifestStamp`,
and the MCP handler renders the envelope with the **existing** `upload_expiry_contract` — the same
function the mint and the investigation finalize call. One renderer for the wall an agent is told
about and the wall it is later held to; no parallel implementation.

The chunked path's `refresh_deadline`-returned-`False` branch keeps its `no_upload_manifest` /
`upload_window_expired` split but raises the same enriched error, so one condition has one payload
regardless of which path observed it. That branch is now a narrow-race backstop rather than the
enforcement point — the deadline can still lapse between `_prepare`'s check and the refresh — and is
retained for exactly that.

### 3. The chunked path's refresh is kept, and its residual is recorded not fixed

`refresh_deadline` is called from **one** place in the codebase: this finalize, once, before
server-side reassembly. It is not a per-chunk refresh — chunk PUTs go straight to S3 through
presigned URLs and never touch the database — so the issue's "extends the deadline on each chunk"
framing does not describe the code. It extends the window by a full `KDIVE_UPLOAD_TTL_SECONDS` for
the duration of a reassembly the agent has already committed to, under the same `RUN` advisory lock
the reaper takes. That is what stops the reaper from deleting chunk objects out from under an
in-flight multipart copy, and ADR-0444 already recorded it as right for this path and wrong for a
short verify-and-commit finalize. It stays.

It is worth being precise about what decision 1 changes for it: the refresh is no longer the
enforcement gate. A finalize now has to pass an explicit deadline check *before* reaching the
refresh, so the refresh only ever extends a window that was open on arrival.

One residual is recorded rather than designed around: a finalize that passes the check, refreshes,
and then fails in reassembly or validation leaves the manifest with a freshly extended deadline. A
client that retries such a finalize indefinitely holds its uncommitted objects indefinitely. This is
not an escalation — `artifacts.create_run_upload` re-mints a full window on demand anyway, so the
same retention is available to any agent that simply asks for it, and the objects stay within the
Run's own prefix — but it does mean the window is not a hard total bound on uncommitted run-upload
retention. Bounding it (refreshing by a reassembly-sized slack instead of a full TTL, or capping
cumulative extension) is a separate change with its own contract question, filed as a follow-up
rather than smuggled in here.

### 4. Nothing else changes

No schema change and no migration: the check reads `upload_manifests.deadline`, which exists. No
reaper change: the `runs` branch already reaps on the deadline alone for every Run state, and its
reach never depended on finalize's blindness. No metric, for the same reason ADR-0444 §4 gave.

## Consequences

- **An observable behavior change, accepted.** A single-PUT uploader that previously squeaked
  through past its deadline is now rejected and must re-mint via `artifacts.create_run_upload`. That
  is the contract agents were already handed — this makes it true rather than changing it. The
  window is `KDIVE_UPLOAD_TTL_SECONDS`, unchanged.
- **The two run finalize paths converge.** Both reject a lapsed window with the same reason and the
  same self-correcting payload, and the check lives at one site rather than two.
- **The run and investigation lanes converge.** All three finalize/mint sites now render the
  deadline contract through `upload_expiry_contract`.
- **A richer payload on an existing rejection.** The chunked path's `upload_window_expired` grows
  `manifest_deadline` / `server_time` / `on_expiry` and a `suggested_next_actions` pointer. Two
  existing tests that asserted the bare `{"reason": ...}` dict are updated.
- **The agent-facing wrapper docstring changes.** FastMCP serializes only the `@app.tool` wrapper
  docstring and `Field(...)` text, so the rejection is stated there, not only in the handler.
- **A recorded, unfixed residual** — unbounded deadline extension by repeated failing chunked
  finalizes (decision 3).

## Considered & rejected

- **Check the deadline only in the MCP handler.** It would need a second `get_manifest` read to see
  the deadline the service has already fetched, racing the service's own read for no benefit. The
  service is where the manifest is in hand.
- **Compare in Python against `datetime.now(UTC)`.** Simpler to read, and wrong for the same reason
  ADR-0444 rejected it: it puts finalize on a different clock from the reaper's `now()`, so the two
  can disagree on one manifest under clock skew. The DB clock is the single reference the contract
  already advertises through `server_time`.
- **Reuse `refresh_deadline` on the non-chunked path too** (check-and-extend instead of check). A
  single-PUT finalize is a short verify-and-commit with nothing to protect from the reaper; the
  extension would buy nothing and would re-open "a past-deadline finalize still wins" on the path
  this ADR exists to close. This is ADR-0444's own rejection of the pattern for short finalizes.
- **Remove the chunked refresh in favor of the plain check** (the issue's open question). It would
  hand the reaper a live window to reap during a multi-GiB server-side reassembly the agent has
  already committed to — the failure the refresh was added to prevent. Decision 3.
- **Render the contract fields in the service layer.** It would make `kdive.services` import
  `kdive.mcp`, inverting a boundary the tree holds today, to save passing a two-field named tuple.
- **Reject with a new `reason`** (e.g. `run_upload_window_expired`). A third spelling for one
  condition; agents would have to match all of them.
