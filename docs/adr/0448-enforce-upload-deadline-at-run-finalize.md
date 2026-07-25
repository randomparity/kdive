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
host-clock disagreement cannot make finalize and the reaper disagree about one manifest.

`now()` is `transaction_timestamp()`, and the whole request runs inside the one implicit transaction
psycopg opens on the pooled (non-autocommit) connection at the handler's first statement. So this
check is a verdict on the request's **arrival**, not on the instant of the read: a finalize that
arrived inside the window is not then rejected for the seconds it spends HEADing and range-reading a
multi-GiB payload. That is the conservative direction, and it is the same property ADR-0444 named.

The rejection keeps `reason="upload_window_expired"` — the reason this tool already raised on its
chunked path and the one `investigations.complete_rootfs_upload` adopted in ADR-0444, so all three
sites speak one vocabulary — and becomes **self-correcting**: it carries `manifest_deadline`,
`server_time`, and `on_expiry` in the same shape and ISO-8601 UTC rendering
`artifacts.create_run_upload` advertised them with, and puts that tool in
`suggested_next_actions`. The agent is told the wall it hit, the clock the wall is measured on, and
the one call that re-mints.

Recovery is **always a re-mint**; whether it also needs a re-upload is a timing question, and the
honest answer differs from the investigation lane's. There the object key is content-addressed, so
re-minting the same declaration addresses the same object. Here the key is
`owner_prefix("local", "runs", <run_id>) + <entry name>` — *owner*-addressed — which is equally
stable across re-mints of the same Run and artifact name, so a re-mint plus a second finalize can
recover without re-uploading a byte.

It usually will not, and the surface says so rather than promising otherwise. `deadline < now()` is
also the upload reaper's candidate predicate, and the reconciler sweeps every 30 seconds
(`reconciler.loop.DEFAULT_INTERVAL`), deleting a lapsed window's uncommitted objects and its
manifest. So the `upload_window_expired` rejection is reachable only inside roughly one sweep of the
deadline; past that a finalize lands on `no_upload_manifest` with the bytes already gone. Both
rejections therefore point at `artifacts.create_run_upload` (see decision 2), and the agent-facing
wording tells the agent to expect a re-upload unless the retry succeeds.

### 2. The commit re-reads the window under the `RUN` lock and checks its *identity*

Arrival alone is not enough on the **single-PUT** path, and the first draft of this change assumed
it was. Between `_prepare` and the commit sits `_validate_uploads`, which HEADs every artifact and
range-reads up to 128 MiB of the kernel tar off S3 — seconds to tens of seconds holding **no lock**.
The upload reaper runs every 30 seconds, takes the `RUN` advisory lock finalize has not yet
acquired, deletes every object under the Run's prefix that has no `artifacts` row (there is none —
finalize has not committed), and drops the manifest. Finalize would then take the freed lock,
re-read only `runs.state`, and commit `artifacts` rows against just-deleted keys plus a `succeeded`
Run.

The **chunked** path does not have that stretch, and it is worth stating why rather than assuming
symmetry. `_reassemble_chunked_artifacts` takes the `RUN` lock inside a `conn.transaction()` that is
a *savepoint* (the request's transaction is already open on the non-autocommit pooled connection);
`pg_advisory_xact_lock` releases at top-level transaction end, and `RELEASE SAVEPOINT` does not drop
it. Verified against this tree: the lock is still held after the inner block exits. So a chunked
finalize holds the `RUN` lock from reassembly through commit, and neither the reaper nor a re-mint
can interleave.

So `_finalize_external_build` re-reads the manifest inside the transaction it already opens under
`advisory_xact_lock(conn, LockScope.RUN, run.id)`, immediately before the writes. **Presence is not
identity**: a reap followed by a re-mint leaves *a* row, and because the run object keys are
owner-addressed and unchanged across re-mints, nothing downstream would notice the swap — the commit
would register `artifacts` rows carrying the deleted objects' etags, mark the Run `succeeded` with a
dangling `kernel_ref`, and (on the non-chunked path) delete the agent's brand-new window row. The
guard therefore compares the **deadline of the window that was validated**, carried on
`_ExternalBuildFinalization`: `replace_manifest` stamps `deadline = now() + ttl` on every re-mint,
so a different value is a different window. Gone → `no_upload_manifest`; different →
`upload_window_replaced`.

The deadline is *not* re-compared against `now()` there — `now()` is frozen for the transaction, so
the arrival verdict decision 1 reached is the one that stands, and a second comparison would be dead
code.

The `_prepare` check is therefore the fail-fast — it keeps a lapsed window from being read at all,
and it is what produces the self-correcting `upload_window_expired` payload — and the locked
identity re-read is the guard that makes the commit safe.

**A pre-existing residual, recorded not fixed.** Because the chunked path holds the `RUN` lock for
the whole request and `reap_one_owner` is awaited serially inside `repair_abandoned_uploads`, one
slow multi-GiB chunked finalize stalls the entire upload-reaper sweep — for every owner, runs and
investigations alike. That predates this change and is not touched here; it belongs to the
reconciler and wants its own issue.

### 3. Rendering stays in the response layer; the shared helper is reused

`src/kdive/services/**` imports nothing from `kdive.mcp` today, and this change does not invert
that. The service raises a new `CompleteBuildExpiredWindowError` carrying the raw `ManifestStamp`,
and the MCP handler renders the envelope with the **existing** `upload_expiry_contract` — the same
function the mint and the investigation finalize call. One renderer for the wall an agent is told
about and the wall it is later held to; no parallel implementation.

The chunked path's `refresh_deadline`-returned-`False` branch collapses to a single
`no_upload_manifest` raise. It cannot mean "expired": `_require_open_window`'s `SELECT now()` and
`refresh_deadline`'s `deadline >= now()` execute in the same transaction (`conn.transaction()` is a
savepoint when one is already open), and `now()` is `transaction_timestamp()`, so the predicate
cannot flip on time between them. A declined refresh means the row is gone, reaped in between — and
the branch is now covered by a test that deletes the manifest through the object-store factory, the
one seam that runs between the two reads. Emitting an "expired" payload there would be dead code
that also lied.

### 4. The chunked path's refresh is kept, and its residual is recorded not fixed

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

### 5. Nothing else changes

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
- **`no_upload_manifest` gains a recovery pointer, and `upload_window_replaced` joins it.** The
  first is the *more common* post-expiry landing — the reaper's candidate predicate is the same
  `deadline < now()` — so it now carries a `detail` and
  `suggested_next_actions: [artifacts.create_run_upload]`, matching what ADR-0444 did for the
  investigation lane's `_no_manifest_error`. The second is new (decision 2). All three "your window
  is gone" rejections route to one call.
- **`refresh_deadline` returns the stamped deadline instead of a bool.** It has exactly one caller,
  which now needs the value as the chunked path's window identity.
- **A latent bug fixed in passing.** `CompleteBuildConfigurationError` was a `frozen` dataclass, and
  `contextlib` assigns `__traceback__` to an exception it re-raises out of an async context manager,
  so *any* raise of it inside `advisory_xact_lock` died with `FrozenInstanceError` instead of the
  intended rejection. Two such raises already existed, both untested; the new locked re-read is a
  third. The class is now `@dataclass(slots=True, eq=False)` — unfreezing alone would have set
  `__hash__ = None` and given it value equality, which an exception should not have.
- **A recorded, unfixed residual** — unbounded deadline extension by repeated failing chunked
  finalizes (decision 4).

## Considered & rejected

- **Check the deadline only in the MCP handler.** It would need a second `get_manifest` read to see
  the deadline the service has already fetched, racing the service's own read for no benefit. The
  service is where the manifest is in hand.
- **The `_prepare` check alone, with no locked re-read** (this change's first draft). It enforces
  the deadline on arrival and reads well, but it is not atomic with the commit: the reaper can
  collect the window during validation and the Run still commits `succeeded` against deleted keys.
  Decision 2.
- **Move the whole single-PUT finalize inside the `RUN` lock**, as the investigation lane does and
  as the chunked path here already does de facto. It is the simpler shape — one guard instead of
  two, and the identity question dissolves. Rejected because of what the chunked path's version of
  it already costs: an advisory lock held across an object-store read stalls the *serial* upload
  reaper sweep for every owner (see decision 2's residual). Extending that from the chunked path to
  every finalize trades a narrow correctness race for a broad liveness cost, so the one-query
  identity re-read is preferred. If the reaper sweep is ever made concurrent per owner, this
  becomes the better design and should be revisited.
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
  already committed to — the failure the refresh was added to prevent. Decision 4.
- **Render the contract fields in the service layer.** It would make `kdive.services` import
  `kdive.mcp`, inverting a boundary the tree holds today, to save passing a two-field named tuple.
- **Reject with a new `reason`** (e.g. `run_upload_window_expired`). A third spelling for one
  condition; agents would have to match all of them.
