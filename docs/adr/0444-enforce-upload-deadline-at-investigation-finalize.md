# ADR 0444 — Enforce the upload deadline at investigation finalize, then reap on the deadline

- **Status:** Accepted
- **Date:** 2026-07-24
- **Supersedes:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §6's **"Accepted
  residual — uncommitted uploads on a never-closed investigation"** clause and the terminal-state
  gate it justified. ADR-0441 §6's reclaim *policy* (close+grace, the TTL backstop, the
  overlay-absence liveness gate) and its *execution model* as revised by
  [ADR-0442](0442-rootfs-reclaim-worker-job.md) are retained unchanged; only the upload-manifest
  reaper's `investigations` state gate and finalize's deadline-blindness are replaced.
- **Depends on:** [ADR-0394](0394-upload-deadline-contract-fields.md) (the `server_time` /
  `manifest_deadline` / `on_expiry` deadline contract this makes real on the finalize path),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the `upload_manifests` window and its
  reaper), [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) (the investigation-scoped
  upload window and explicit finalize).
- **Spec:** [`../specs/2026-07-24-deadline-governed-upload-reap-1521-design.md`](../specs/2026-07-24-deadline-governed-upload-reap-1521-design.md)

## Context

ADR-0441 §6 named its own hole. The re-scoped upload-manifest reaper reaps a past-deadline
investigation upload window only when the investigation is **terminal**, because
`investigations.complete_rootfs_upload` finalizes on any OPEN/ACTIVE investigation *without
re-checking the manifest deadline*. Reaping an OPEN window would therefore race a
slow-but-legitimate finalize: the reaper deletes the staged object, the agent's finalize then
fails with a confusing `no_upload_manifest`, and the failure looks like the uploader's fault.

The accepted cost was a retention hole in exactly one shape — **PUT, never finalized, investigation
never closed**. Such an upload has no committed `artifacts` row, so the committed-object TTL
backstop (`gc_expired_investigation_rootfs`, which enumerates rows) never sees it, and the manifest
reaper's terminal gate excludes it. The SENSITIVE bytes sit in S3 until the investigation is
eventually closed. #1521 is that residual.

The residual rests on one load-bearing premise: that an OPEN/ACTIVE investigation "can still
finalize" past its deadline. That is true only because finalize never reads `manifest_deadline` —
not because the contract says so. The contract says the opposite: `artifacts.create_investigation_upload`
returns `manifest_deadline`, `server_time`, and `on_expiry` to the agent (#1336, ADR-0394), and
finalize silently ignores all three. AGENTS.md's "state a limit's full contract" invariant requires
that a limit handed to an agent be real; on the finalize path it is decorative. So the residual and
a contract defect are the same defect seen from two sides.

## Decision

### 1. Finalize enforces the manifest deadline

`investigations.complete_rootfs_upload` rejects a past-deadline manifest with a
`configuration_error` carrying `reason="upload_window_expired"` — the same reason
`runs.complete_build` already raises for an expired run upload window, so both finalize paths
speak one vocabulary.

The rejection is **self-correcting**: it carries `manifest_deadline`, `server_time`, and
`on_expiry` in the same shape and ISO-8601 UTC rendering `create_investigation_upload` advertised
them with, and puts `artifacts.create_investigation_upload` in `suggested_next_actions`. The agent
is told the wall it hit, the clock that wall is measured on, and the one call that re-mints. The
sibling `no_upload_manifest` rejection — what a finalize arriving *after* a reap now sees — gains
the same `suggested_next_actions`, so every "your window is gone" path routes to the same recovery.

The check reads the deadline and the reference clock from **one statement** on the Postgres clock
that stamped the deadline (`deadline = now() + ttl`) and that the reaper measures against — a new
read-side `upload_manifest.deadline_stamp` returning the existing `ManifestStamp`
(`server_time`, `deadline`). No Python-side `datetime.now()` enters the comparison; a session-TZ
or host-clock disagreement cannot make finalize and the reaper reach opposite verdicts.

`now()` is `transaction_timestamp()`, so the clock is the finalize transaction's *start*, not the
instant the row is read. That is deliberate and conservative in the right direction: a request that
arrived inside the window and then waited on the `INVESTIGATION` advisory lock is judged by its
arrival, never penalized for queueing.

### 2. The reaper reaps a past-deadline uncommitted manifest on the deadline alone

`reconciler/cleanup/uploads.py` drops its `investigations` state gate. The candidate select is
`deadline < now()` over both known owner kinds with no state predicate, and `reap_one_owner`'s
locked re-read keeps only its `deadline < now()` re-check (what declines a manifest renewed since
the candidate select).

This is sound **because of decision 1, not despite it**. Once finalize enforces the deadline, a
finalize arriving after the deadline is rejected anyway — so there is no longer any legitimate
in-flight finalize for a deadline-governed reap to race. The two are serialized by the
`INVESTIGATION` advisory lock both already take, and both orders are safe: a finalize that wins the
lock deletes the manifest and the reaper then finds no row; a reaper that wins deletes the object
and the manifest, and the finalize that follows would have been rejected on the deadline regardless.

`owner_reapable`, `_UPLOAD_REAPABLE_STATES`, and `_OWNER_REAPABLE_QUERIES` become dead with the gate
and are **removed**, not left as shims. (ADR-0441 §6 already superseded the *policy* they last
carried — ADR-0435's `systems`-owner `{defined, failed}` relaxation — by re-scoping the arm to
`investigations`; this removes the machinery itself.) Their one surviving obligation — failing loud
on an unrecognized owner kind rather than locking it under a guessed scope — moves to an explicit
`_LOCK_SCOPES` mapping that raises. `reap_one_owner` correspondingly stops taking a caller-supplied
`LockScope`: the scope is a function of the owner kind, so deriving it removes the chance of a
caller passing a scope that disagrees with the kind.

The per-key **committed-object skip is unchanged** and now carries the whole safety burden for
OPEN/ACTIVE investigations: an object with an `artifacts` row is never deleted, so a *finalized*
rootfs is untouchable by this reaper regardless of investigation state. What the reap now collects
is precisely the uncommitted staged bytes of a window that can no longer be finalized.

### 3. The `runs` branch does not change

It already reaps on the deadline alone, for every Run state (ADR-0104 §7): this change makes the
`investigations` branch symmetric with `runs` rather than introducing a new policy. The reaper's
owner-kind filter is likewise unchanged — legacy `owner_kind='systems'` manifest rows (whose minting
tool ADR-0441 removed) stay outside its scope, as they already were.

One asymmetry is named rather than fixed here: `runs.complete_build` re-checks the deadline only on
its **chunked** path (`refresh_deadline` before reassembly); a non-chunked run finalize is as
deadline-blind as investigation finalize was before this change. That is the same defect in the
runs lane, out of scope for #1521 (which is investigation-scoped), and it is *less* consequential
there because the run reaper's reach never depended on it. It is worth a follow-up, not a widening
of this change.

### 4. No observability signal is added

#1521 offered "and/or a metric on past-deadline investigation upload manifests" as an alternative to
reaping. With the reap in place the metric would observe a hole that no longer exists; the reaper
already logs each reaped owner. Adding one would be speculative surface.

## Consequences

- **An observable behavior change, accepted.** A slow uploader that previously squeaked through
  past the deadline is now rejected and must re-mint via `artifacts.create_investigation_upload`.
  That is the contract agents were already handed — this change makes it true rather than changing
  it. The window is `KDIVE_UPLOAD_TTL_SECONDS`, unchanged.
- **The retention hole closes.** A PUT-but-never-finalized upload on a never-closed investigation is
  collected one reconciler pass after its deadline instead of lingering until close.
- **Two `test_upload_reaper.py` tests are rewritten, deliberately.**
  `test_open_investigation_with_lingering_manifest_is_not_reaped` and
  `test_active_investigation_with_lingering_manifest_is_not_reaped` pinned ADR-0441 §6's terminal
  gate — the behavior this ADR supersedes. They are replaced by tests pinning the inverse (an
  OPEN/ACTIVE past-deadline uncommitted manifest **is** reaped) plus an OPEN-investigation
  committed-object exemption, so the new contract is pinned as tightly as the old one was.
  `test_owner_reapable_rejects_unknown_owner_kind_before_sql` follows `owner_reapable` out and is
  replaced by a fail-loud test over the lock-scope lookup.
- **The finalize error surface grows one reason.** `upload_window_expired` joins
  `no_upload_manifest`, `investigation_not_accepting_upload`, `rootfs_not_uploaded`,
  `rootfs_checksum_missing`, and `rootfs_checksum_mismatch` on the finalize path.
- **The agent-facing wrapper docstring changes.** FastMCP serializes only the `@app.tool` wrapper
  docstring and `Field(...)` text, so the rejection is stated there, not only in the handler.
- **No schema change and no migration.** The check reads columns that already exist.

## Considered & rejected

- **Reap without enforcing at finalize** (the issue's first option, taken literally). This is the
  option ADR-0441 §6 explicitly declined, and declining it was right: it keeps the documented race,
  so a legitimate in-flight finalize can lose its object and surface a late, misattributed error.
  Enforcing first is what turns the reap from "racy" into "cannot race anything legitimate".
- **A metric only** (the issue's second option). It makes the retention observable without closing
  it, which is the actual complaint. Rejected in decision 4.
- **Grace period on the reap** (reap at `deadline + slack` while leaving finalize deadline-blind).
  A softer form of the first rejection: it shrinks the race window without removing it, and it
  introduces a second, undocumented wall the agent was never told about — the opposite of the
  contract invariant this change is honoring.
- **Refresh the deadline at finalize** (the `runs` chunked-reassembly pattern,
  `refresh_deadline`). Right for that path, wrong here: there the refresh protects a long
  *server-side* reassembly the agent has already committed to, whereas an investigation finalize is
  a short verify-and-commit. Refreshing would extend the window on arrival and re-open exactly the
  "past-deadline finalize still wins" behavior this ADR is removing.
- **Compare the deadline in Python against `datetime.now(UTC)`.** Simpler to read, and wrong: it
  puts finalize on a different clock from the reaper's `now()`, so the two can reach opposite
  verdicts on the same manifest under clock skew. The DB clock is the single reference the contract
  already advertises via `server_time`.
