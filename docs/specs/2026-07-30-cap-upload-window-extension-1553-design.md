# Design — cap cumulative upload-window extension (#1553)

- **Issue:** [#1553](https://github.com/randomparity/kdive/issues/1553)
- **ADR:** [0511](../adr/0511-cap-cumulative-upload-window-extension.md)
- **Date:** 2026-07-30

## Requirement

`upload_manifest.refresh_deadline` sets `deadline = now() + ttl` on any still-open window, with no
cap, no counter, and no reference to when the window was minted. Its one caller — the chunked
`runs.complete_build` — commits that `UPDATE` in a savepoint *before* reassembly runs, and every
failure that follows is caught at the MCP tool layer and returned as a `ToolResponse`, so the
pooled connection exits its `async with` cleanly and psycopg commits. Nothing unwinds the
extension. A client retrying a failing finalize inside its own still-open window therefore buys
another full TTL every attempt, indefinitely; `reconciler/cleanup/uploads.py` bounds only on
`deadline < now()`, so nothing else caps retention.

ADR-0448 §4 recorded this as a disclosed residual and deferred it: "capping cumulative extension is
a separate change with its own contract question, filed as a follow-up rather than smuggled in
here."

Acceptance criteria:

1. Total extension of one minted window is bounded.
2. `artifacts.create_run_upload` still grants a full fresh window on demand — the cap must bound
   extension, not re-minting, because the re-mint is the recovery ADR-0448 points every "your
   window is gone" rejection at.
3. Every comparison uses Postgres `now()`; DB `now()` is session-TZ dependent, so a Python-side
   clock is subtly wrong.
4. The bound is configurable, with a justified default.
5. `refresh_deadline` currently has zero test coverage; it gains real tests.

## Mechanism

Migration `0085` adds `upload_manifests.window_started_at timestamptz NOT NULL DEFAULT now()`.
`replace_manifest` restamps it from the same statement's `now()` that stamps `deadline`, on both
upsert arms. Nothing else writes it, so it is the mint instant and a re-mint is its only reset.

`refresh_deadline` clamps:

```sql
SET deadline = GREATEST(deadline, LEAST(now() + ttl, window_started_at + max_window))
```

`LEAST` caps the window's total life at `max_window` from its mint. `GREATEST` keeps a spent budget
from *shortening* an open window — a past deadline written mid-request would hand the reaper the
chunk objects the caller is about to reassemble.

`max_window = KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE × KDIVE_UPLOAD_TTL_SECONDS`, computed by the
caller so the storage module stays config-free. A multiple rather than an absolute number of
seconds: an absolute cap can be set below the TTL, which silently disables the extension instead of
bounding it. Default 3 — see ADR-0511 decision 3 for what 1 and 2 each cost.

`refresh_deadline` returns `WindowRefresh(deadline, capped)`. `capped` is computed in SQL
(`deadline < now() + ttl`) so no Python clock enters the comparison; the finalize logs a warning on
it. `None` keeps its existing meaning — no row, or already lapsed — and a spent budget is
deliberately *not* folded into it, because the caller maps `None` to `no_upload_manifest` and that
would report a reaped window while the window is open.

Nothing agent-facing changes: no new rejection reason, no envelope change. A capped refresh does not
fail the finalize; when the window finally lapses, the existing `upload_window_expired` payload
already carries the deadline, the clock, and the re-mint pointer.

## Boundaries

- `reconciler/**` is untouched. #1554 has just reworked the reap lane and its predicate
  (`deadline < now()`) is correct; the defect was that the deadline moved.
- `mcp/tools/catalog/artifacts/uploads.py` is untouched — the mint's behavior is preserved, not
  changed.
- The investigations finalize lane never calls `refresh_deadline`; it is unaffected.

## Verification

Storage-level, against a real Postgres (`migrated_url`): extension within budget, decline on an
absent row, decline on a lapsed window, the cap binding *partially* (deadline lands exactly on
`window_started_at + max_window`), the cap fully spent across five refreshes, monotonicity, the
re-mint reset, and a row inserted without the column.

Service-level, through `CompleteBuildFinalizer` with the MCP handler's swallow-inside-the-connection
shape: a first failing chunked finalize still commits its extension; after the budget is spent a
second failing finalize commits nothing, and the capped warning is logged.

Mutation-verified on the real tree: removing `LEAST` reddens four tests, removing `GREATEST`
reddens two, dropping the `window_started_at` restamp from the upsert reddens the re-mint test. The
restored tree is green.
