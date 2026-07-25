# Enforce the upload deadline at `runs.complete_build` — design (#1534)

- **Issue:** [#1534](https://github.com/randomparity/kdive/issues/1534)
- **ADR:** [ADR-0448](../adr/0448-enforce-upload-deadline-at-run-finalize.md)
- **Depends on:** [ADR-0444](../adr/0444-enforce-upload-deadline-at-investigation-finalize.md) (the
  finalize-enforcement shape this reuses), [ADR-0394](../adr/0394-upload-deadline-contract-fields.md)
  (the `server_time` / `manifest_deadline` / `on_expiry` contract), [ADR-0048](../adr/0048-external-build-artifact-ingestion.md)
  (the `upload_manifests` window and its reaper).

## Problem

`runs.complete_build` reads the upload-manifest deadline on exactly one path: chunked reassembly.
`_reassemble_chunked_artifacts` calls `upload_manifest.refresh_deadline`, which updates the row only
when `deadline >= now()` and reports whether it did — so a chunked finalize past the window is
rejected with `reason="upload_window_expired"`.

A **non-chunked** run finalize never reaches that call. `_prepare` fetches the manifest, computes the
object keys, and hands them straight to validation. The `deadline` column is read (it rides on
`UploadManifest`) and then ignored. A finalize arriving an hour past the window commits the Run.

`artifacts.create_run_upload` hands the agent `manifest_deadline`, `server_time`, and `on_expiry`
(#1336 / ADR-0394). On the single-PUT path all three are decorative. AGENTS.md's "state a limit's
full contract" invariant says a limit handed to an agent must be real. This is the same defect
#1521 fixed in the investigation lane, named but deferred in ADR-0444 §3.

## Requirements

1. A non-chunked `runs.complete_build` whose manifest deadline has passed is rejected; the Run stays
   `created`, no `artifacts` rows are written, and the manifest is left in place for the reaper.
2. The rejection is self-correcting: it names the deadline it missed, the reference clock that
   deadline is measured on, and the one call that re-opens a window.
3. The comparison uses the **Postgres** clock — the same `now()` that stamped the deadline and that
   the reaper measures against. No Python-side `datetime.now()`.
4. Both run finalize paths (chunked and non-chunked) emit one rejection shape for one condition.
5. The chunked path's in-window behavior — refresh the deadline, then reassemble — is unchanged.
6. The agent-facing wrapper docstring states the new rejection (FastMCP serializes only the
   `@app.tool` wrapper docstring).

## Design

### The check

`CompleteBuildFinalizer._prepare` already holds the fetched `UploadManifest`. Immediately after the
`no_upload_manifest` guard, it pairs that manifest's `deadline` with the transaction's `now()` via
the ADR-0444 read-side helper `upload_manifest.deadline_stamp` and raises when
`deadline < server_time`.

Placing the check in `_prepare` — before the chunked branch — makes it cover both paths from one
site, so the two lanes cannot drift again. `now()` is `transaction_timestamp()`, so a request that
arrived inside the window is judged by its arrival, not by time spent queueing behind the `RUN`
advisory lock.

### The error

The service layer does not import `kdive.mcp` (no `src/kdive/services/**` file does today), so it
cannot render the contract fields itself. It raises a new

```
CompleteBuildExpiredWindowError(stamp: upload_manifest.ManifestStamp)
```

carrying the raw clock pair, and `CompleteBuildHandlers._complete_authorized_build` renders the
envelope with the **existing shared helper** `upload_expiry_contract` — the same function the mint
and the investigation finalize use:

```
data = {"reason": "upload_window_expired",
        **upload_expiry_contract(stamp, remint_tool=CREATE_RUN_UPLOAD_TOOL)}
suggested_next_actions = [CREATE_RUN_UPLOAD_TOOL]
```

No parallel rendering, and the layering stays one-directional.

### The chunked path

`_reassemble_chunked_artifacts`'s `refresh_deadline`-returned-`False` branch stays as the
narrow-race backstop it now is (the deadline can still lapse between `_prepare`'s check and the
refresh). Its bare `CompleteBuildConfigurationError({"reason": "upload_window_expired"})` is
replaced by a re-read + the same `CompleteBuildExpiredWindowError`, so one condition has one payload
on every path. The `no_upload_manifest` sibling branch is unchanged.

The refresh itself is **not** changed. See ADR-0448 §3 for why, and for the follow-up it recommends.

### Recovery: re-mint, not re-upload

The investigation lane's phrasing carries over, for a different reason. There the key is
content-addressed, so a re-mint of the same declaration addresses the same object. Here the key is
`owner_prefix("local", "runs", <run_id>) + <entry name>` — **owner**-addressed. It is equally stable
across re-mints of the same Run and artifact name, so a re-mint followed by a second finalize
recovers without re-uploading a byte, provided (a) the reaper has not yet collected the object and
(b) the re-mint declares the same name with the same `sha256` (a changed checksum makes the
still-present object fail validation, which is a re-upload).

## Tests

New, all against a real Postgres (`migrated_url`):

- non-chunked manifest with `ttl=-1` → `CompleteBuildExpiredWindowError`; Run stays `created`; the
  validator is never called; the manifest row survives (service level).
- the same through the MCP handler → `configuration_error`, `data.reason == "upload_window_expired"`,
  `data.manifest_deadline` / `data.server_time` / `data.on_expiry` present and self-consistent
  (`manifest_deadline < server_time`, `on_expiry.tool == "artifacts.create_run_upload"`),
  `suggested_next_actions == ["artifacts.create_run_upload"]`.
- a non-chunked manifest still inside its window finalizes (regression pin that the check is a
  wall, not a floor).
- an in-window chunked finalize still refreshes the deadline before reassembly (pins requirement 5).

Rewritten: the two existing expired-chunk tests assert the enriched payload rather than the bare
`{"reason": ...}` dict.

## Not doing

- No schema change and no migration — the check reads `upload_manifests.deadline`, which exists.
- No change to the reaper. The `runs` branch already reaps on the deadline alone for every Run
  state (ADR-0104 §7); nothing about its reach depended on finalize's blindness.
- No metric. The reaper logs each reaped owner; a counter here would be speculative surface.
- No change to `refresh_deadline`'s semantics (ADR-0448 §3).
