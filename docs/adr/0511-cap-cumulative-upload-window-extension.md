# 0511 — Cap cumulative upload-window extension against the mint, not the row

## Status

Accepted (2026-07-30)

## Context

`upload_manifest.refresh_deadline` has exactly one caller: the chunked `runs.complete_build`, which
extends the window by a full `KDIVE_UPLOAD_TTL_SECONDS` immediately before server-side reassembly so
the upload reaper cannot delete chunk objects out from under an in-flight multipart copy. ADR-0448
§4 examined that call, kept it, and recorded what it could not bound:

> a finalize that passes the check, refreshes, and then fails in reassembly or validation leaves the
> manifest with a freshly extended deadline. A client that retries such a finalize indefinitely
> holds its uncommitted objects indefinitely.

The extension survives the failure because of where it commits. `_reassemble_chunked_artifacts`
takes the `RUN` lock inside a `conn.transaction()` that is a **savepoint** — the request's
transaction is already open on the pooled connection — and issues the `UPDATE` there, before
`_reassemble_artifacts` runs. Every failure past that point is caught at the MCP tool layer
(`mcp/tools/lifecycle/runs/complete_build.py` maps a `CategorizedError`, a validation rejection, and
a configuration rejection each to a `ToolResponse`), so the handler's
`async with pool.connection() as conn` block exits **cleanly** and psycopg commits. Nothing on any
path unwinds the refresh. A client retrying a failing finalize inside its own still-open window
therefore bought another full TTL on every attempt, without limit, and
`reconciler/cleanup/uploads.py` bounds only on `deadline < now()` — so nothing else capped retention
either.

This is P3 and the issue says why: `artifacts.create_run_upload` re-mints a full fresh window on
demand anyway, so trickling buys an attacker nothing they could not obtain by asking. What it does
mean is that the deadline is a **per-attempt** limit while the surface presents it as the window's
lifetime. `refresh_deadline` also had no test coverage at all.

The design question ADR-0448 deferred is *what the cap is measured from*, and the obvious answer is
wrong. `upload_manifests` already carries `created_at`, and `replace_manifest`'s
`ON CONFLICT DO UPDATE` sets only `prefix`, `manifest`, and `deadline` — so `created_at` survives a
re-mint. Capping against it would bound `artifacts.create_run_upload` too: after enough elapsed time
a re-mint would hand back a window the first refresh immediately clamps, and the re-mint is the
recovery ADR-0448 routes `upload_window_expired`, `no_upload_manifest`, and
`upload_window_replaced` to. The cap would take away a capability instead of bounding a silent one.

## Decision

### 1. A new `window_started_at` column, restamped by the re-mint and only by the re-mint

Migration `0085` adds `upload_manifests.window_started_at timestamptz NOT NULL DEFAULT now()`.
`replace_manifest` sets it from the same statement's `now()` that stamps `deadline`, on both the
INSERT and the ON CONFLICT arms, so `deadline - window_started_at == ttl` exactly on a fresh window.
Nothing else writes it.

That is the whole of the design: one column whose *only* writer is the mint draws the line between
"extension, which is bounded" and "re-minting, which is not". `created_at` cannot draw it because
the upsert deliberately leaves it alone — `created_at` is the row's age, and the row outlives the
windows it carries.

`DEFAULT now()` backfills in-flight windows at migrate time with a fresh budget rather than a
retroactively exhausted one. Backfilling from `created_at` would have been the tidier-looking
choice and is the dangerous one: it can clamp a live multi-GiB reassembly's window on its first
refresh after the upgrade. The default also keeps the column optional for a writer that does not
name it, so a plain `INSERT` still yields a well-formed window.

### 2. The extension is clamped and monotonic

`refresh_deadline` becomes

```sql
UPDATE upload_manifests
   SET deadline = GREATEST(deadline, LEAST(now() + ttl, window_started_at + max_window))
 WHERE owner_kind = %s AND owner_id = %s AND deadline >= now()
RETURNING deadline, deadline < now() + ttl
```

`LEAST` is the cap: no sequence of refreshes carries one minted window past `max_window` from its
mint.

`GREATEST` is not decoration. Without it, a refresh arriving after the budget is spent computes a
deadline **in the past** and writes it — pulling an open window closed and handing the reaper the
chunk objects the refresh exists to protect, inside the request that is about to reassemble them.
That converts a retention bound into data loss, which is the worst failure a bound can have. With
it, a spent budget is a no-op: the deadline stands, the reassembly proceeds under the `RUN` lock it
already holds, and the window simply does not outlive its deadline a second time.

`GREATEST` does not weaken the bound *provided `KDIVE_UPLOAD_TTL_SECONDS` has been stable since the
mint*. Its other argument is the clamped grant, which is `≤ window_started_at + max_window` by
construction — both read from whatever is live at refresh time. The only other value `GREATEST`
can select is the standing deadline, which the mint stamped at `window_started_at + ttl` using
*its own* `ttl`, not the refresh's. When the two agree, `ttl ≤ max_window` because the multiple is
at least 1, so `deadline ≤ window_started_at + max_window` holds after every refresh, not merely on
average. When they do not — an operator lowers `KDIVE_UPLOAD_TTL_SECONDS` after the mint, or a row
migration `0085`'s backfill lands on was minted under a since-changed value — the standing deadline
was computed against the *old*, larger `ttl` and can exceed `window_started_at` plus the *new*,
smaller `max_window`. `GREATEST` then keeps that already-too-large deadline standing indefinitely,
because every later refresh's clamped grant is smaller still and never gets selected.

That is a bounded leak, not an exposure: the window decays out at the old bound rather than
snapping to the new one, no refresh ever moves it later than the mint already placed it, and a
second refresh after the budget is spent moves nothing. Nothing on this path writes a deadline that
exposes an in-flight upload to the reaper — the failure mode `GREATEST` exists to prevent is
unchanged. It does mean retention after a TTL decrease is governed by whatever `ttl` was live at
each row's mint, not by the operator's newly configured value, until that row's window is next
re-minted.

Every comparison is Postgres's `now()`. The reaper measures `deadline` against the same clock, and
DB `now()` is session-TZ dependent, so a Python-side comparison here would be subtly wrong for the
reason ADR-0444 and ADR-0448 both already rejected it.

The `WHERE` clause is unchanged, and so is the meaning of `None`: no row, or a window already past
its deadline. **A spent budget never returns `None`** — it returns the unchanged deadline with
`capped` set. That matters because the sole caller maps `None` to `no_upload_manifest`; folding an
exhausted budget into that return would have told the agent its window was reaped when it is open,
and pointed it at a re-mint for the wrong reason.

### 3. The cap is `KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE × KDIVE_UPLOAD_TTL_SECONDS`, default 3

A **multiple** of the TTL, not an absolute number of seconds. An absolute cap can be configured
below the TTL it bounds, and the failure is silent: `LEAST` would clamp every refresh to the
deadline the mint already stamped, disabling the reassembly protection entirely while the
configuration looks deliberate. A multiple cannot be smaller than one window by construction, and
the parser rejects 0 and negatives, which would put the cap at or before the mint.

The default is **3**, chosen from what each value costs:

- **1** forbids extension outright — the mint's deadline is the whole budget — so a finalize
  arriving late in its window reassembles with whatever time is left. Defensible as an operator
  choice, and documented as such, but not a safe default: it silently removes the protection
  ADR-0448 §4 kept.
- **2** allows exactly one full extension. It bounds retention tightly, and it fails the first time
  an agent legitimately retries after a transient store error — a case the surface does not
  otherwise punish.
- **3** tolerates one legitimate retry and still bounds one mint's uncommitted retention at three
  advertised windows (72 h at the default TTL).

The cap is deliberately *not* sized to a reassembly. A server-side multipart copy of the 50 GiB
`KDIVE_MAX_UPLOAD_BYTES` ceiling is minutes; a full 24 h TTL is already orders of magnitude more
slack than one attempt needs. What is being sized is how many *failing* attempts may roll the window
forward before the agent has to say so out loud — and saying so is one call to
`artifacts.create_run_upload`, which starts a new window with a fresh budget.

### 4. A capped refresh is logged; nothing agent-facing changes

`refresh_deadline` returns a `WindowRefresh(deadline, capped)` named tuple. The finalize logs a
warning when `capped` is set, naming the run, the cap, the standing deadline, and the re-mint tool.
That is the only signal a Run retrying in a loop is running out of budget rather than out of luck.

No new rejection reason, no envelope change, no `suggested_next_actions` change. A capped refresh
does not fail the finalize — the attempt proceeds exactly as before, and if it fails again it fails
with the reason it would have failed with anyway. Once the window finally lapses, the existing
`upload_window_expired` payload already carries `manifest_deadline`, `server_time`, `on_expiry`, and
the re-mint pointer, which is precisely the correct guidance for an exhausted budget. Inventing a
second vocabulary for it would give agents two strings to match for one condition.

## Consequences

- **A behavior change on a failing retry loop, which is the point.** The first failing chunked
  finalize still buys a full TTL, unchanged. Past `max_window` from the mint, further failing
  retries buy nothing, and the window lapses on schedule for the reaper to collect.
- **`artifacts.create_run_upload` is unaffected.** It restamps `window_started_at`, so a re-mint
  grants a full fresh window with a full fresh budget however many times it is called. A test
  asserts this directly, because it is the property the whole column exists to preserve.
- **`refresh_deadline` gains test coverage, from none.** Extension, decline on an absent row,
  decline on a lapsed window, the cap binding partially, the cap spent, monotonicity, the re-mint
  reset, and a row that predates the column. Each was mutation-verified: removing `LEAST` reddens
  four tests, removing `GREATEST` reddens two, dropping the re-mint restamp reddens one, and
  hard-coding the multiple instead of reading it reddens one — the last because a cap test run at
  the built-in default cannot tell a wired knob from an ignored one.
- **The return type changed** from `datetime | None` to `WindowRefresh | None`. One caller.
- **A table rewrite on upgrade.** `ADD COLUMN ... NOT NULL DEFAULT now()` is a volatile default, so
  Postgres rewrites `upload_manifests`. The table holds one row per *in-flight* upload window and is
  emptied by finalize and by the reaper, so it is small by construction.
- **A pre-existing residual stays open, narrowed.** ADR-0448 §2's partially-completed reap — S3
  deletes committed, the manifest row restored with a byte-identical deadline — is a reconciler
  ordering problem this does not touch. The cap does bound how long the window it happens to be
  racing can be kept alive.

## Considered & rejected

- **Cap against `created_at`.** The column already exists, so this looks free. It is the trap the
  issue's fix direction walks into: `replace_manifest`'s `ON CONFLICT DO UPDATE` does not reset
  `created_at`, so the cap would bound the re-mint as well — and the re-mint is the documented
  recovery from every "your window is gone" rejection ADR-0448 emits. Bounding it would remove a
  capability agents are told to use.
- **Reset `created_at` on the upsert so it can serve as the mint instant.** Zero new columns, and it
  silently redefines a column whose name promises the row's birth. Anything reading it for row age —
  an operator query, a future retention report — would quietly start measuring something else.
- **A `refresh_count` integer instead of a timestamp.** It bounds the number of extensions but not
  the time they buy, so the same cap means an hour or a week depending on the TTL, and it cannot
  express a partial grant: a refresh either consumes a whole unit or is refused. The timestamp
  answers "how long may this window live", which is the question retention actually asks.
- **An absolute `KDIVE_UPLOAD_WINDOW_MAX_SECONDS`.** More legible than a multiple, and it can be set
  below `KDIVE_UPLOAD_TTL_SECONDS` — which silently disables the reassembly protection instead of
  bounding it. Cross-validating two settings against each other adds machinery to recover a
  property the multiple has for free.
- **Refresh by a reassembly-sized slack instead of a full TTL** (ADR-0448 §4's other candidate).
  It attacks the same retention from the other end and is a strictly larger change: nothing in the
  tree estimates a reassembly's duration, so the slack would be a second unjustified constant, and
  under-estimating it hands the reaper a live reassembly's chunk objects — the failure the refresh
  exists to prevent. The cap composes with it if it is ever wanted.
- **Refuse the refresh once the budget is spent** (return `None`, or a new rejection). The caller
  maps `None` to `no_upload_manifest`, so this would tell an agent its window was reaped while it is
  demonstrably open. A distinct rejection avoids the lie but fails a finalize that would otherwise
  have succeeded, turning a retention bound into an availability cut for the one attempt still
  holding the `RUN` lock.
- **Let the clamp shorten the deadline** (`LEAST` without `GREATEST`). Simpler to read and it makes
  the cap exact rather than eventual. It also writes a past deadline onto an open window mid-request
  and hands the reaper the chunk objects the caller is about to reassemble. Decision 2.
- **Unwind the refresh on failure instead of capping it.** The honest fix for the root cause, and
  out of reach here: the refresh is committed by a savepoint deliberately taken so the `RUN` lock
  outlives it (ADR-0448 §2), and the swallowing happens two layers up in the MCP handler. Undoing it
  means either releasing that lock — reopening the reap-mid-reassembly race — or threading a
  compensating write through every failure path in the tool layer. A cap needs neither and bounds
  the retention regardless of which path failed.
- **Bound the reaper instead** (`reconciler/cleanup/uploads.py`). The reaper's predicate is
  `deadline < now()`, and the defect is that the deadline moves. A second bound there would
  duplicate this one in the process that must not be taught to ignore a live window, and #1554 has
  just reworked that lane.
