# Spec — `runs.get` console manifest opt-in (#1067)

- **Status:** Draft
- **Date:** 2026-07-09
- **Issue:** #1067 (`BLACK_BOX_REVIEW.md` pain point P7)
- **ADR:** [0324-runs-get-console-manifest-opt-in](../adr/0324-runs-get-console-manifest-opt-in.md)
- **Branch:** `feat/console-manifest-opt-in-1067` (base `main`)

## Problem

`runs.get` inlines the Run-scoped console manifest under `data.console_artifacts`
unconditionally for every non-`FAILED` Run. A status/provenance read a caller pays
for per token then carries up to `CONSOLE_MANIFEST_MAX = 100` entries of
`{artifact_id, object_key, created_at}` — hundreds of lines — that a `runs.get`
caller rarely wants.

Verified against source:

- `get_run` (`src/kdive/mcp/tools/lifecycle/runs/view.py:66-72`) unconditionally
  calls `list_run_console_artifacts(conn, run.id)` for every non-`FAILED` Run and
  passes the manifest into `envelope_for_run`.
- `_console_manifest_data` (`src/kdive/mcp/tools/lifecycle/runs/common.py:252-269`)
  inlines the full `entries` list under `data.console_artifacts` (plus
  `_total`/`_truncated` when `total > len(entries)`).
- The service cap is `CONSOLE_MANIFEST_MAX = 100`
  (`src/kdive/services/artifacts/listing.py:20`); each entry is
  `{artifact_id, object_key, created_at}`.
- `runs.get` (`registrar.py:180-206`) takes only `run_id` — no opt-out.

The boot-window console snapshot stays separately at `refs.console` with
`data.console_access` describing how to read it, so the inlined manifest is
redundant for a status read.

## Goals

1. Make the console manifest **opt-in** on `runs.get`: add
   `include_console_artifacts: bool = False`. When `False` (default), `get_run`
   skips `list_run_console_artifacts` entirely and the envelope omits
   `data.console_artifacts` / `_total` / `_truncated`.
2. When `True`, behavior is byte-identical to today: the bounded, newest-first
   manifest is inlined with its truncation markers.
3. Keep `refs.console` (boot snapshot) and `data.console_access` unconditional —
   they are cheap and are the primary console-read affordance.
4. Update the `runs.get` wrapper docstring and the generated guide docs so the
   opt-in flag and its default are discoverable at call time.

## Non-goals

- No new `runs.console_manifest` tool (the alternative in the issue). The manifest
  service is already parameterized and reusable; a boolean flag on `runs.get` is
  the smaller surface and keeps the manifest co-located with the Run read.
- No change to `list_run_console_artifacts`, `ConsoleManifest`,
  `_console_manifest_data`, or `envelope_for_run` — all already no-op on a `None`
  manifest, so the opt-out is purely "don't fetch."
- No pagination/cursor on the manifest (out of scope; the existing
  `CONSOLE_MANIFEST_MAX` bound + `_truncated` marker are unchanged when opted in).
- No DB migration; no schema or persistence change.

## Decision (summary; full rationale in ADR-0324)

Add `include_console_artifacts: bool = False` as a keyword-only parameter to
`get_run` (`view.py`) and thread it through from the `runs.get` wrapper
(`registrar.py`). The `list_run_console_artifacts` call becomes conditional:

```
console_manifest = (
    await list_run_console_artifacts(conn, run.id)
    if include_console_artifacts and run.state is not RunState.FAILED
    else None
)
```

`envelope_for_run` and `_console_manifest_data` are unchanged: a `None` manifest
already omits every `console_artifacts*` key. The `runs.get` wrapper gains an
`include_console_artifacts` `Field(default=False)` with an agent-facing
description, and its docstring reframes `data.console_artifacts` as opt-in behind
the flag (default off, `refs.console` remains the always-present boot snapshot).

`include_console_artifacts=False` is the new default; the old always-inline
behavior is reachable with `include_console_artifacts=True`.

## Acceptance criteria

- [ ] `runs.get(run_id)` (default) on a Run with correlated console artifacts
      returns **no** `data.console_artifacts` / `_total` / `_truncated` keys, and
      `list_run_console_artifacts` is not called.
- [ ] `runs.get(run_id, include_console_artifacts=True)` on the same Run inlines
      `data.console_artifacts` (bounded, newest-first) exactly as today, including
      `data.console_artifacts_total` / `_truncated` when truncated.
- [ ] `refs.console` and `data.console_access` are present on a booted Run
      regardless of `include_console_artifacts`.
- [ ] The `runs.get` wrapper exposes `include_console_artifacts` with an
      agent-facing `Field` description naming the default (off), what the flag
      inlines, and that `refs.console` is the always-present boot snapshot.
- [ ] The `runs.get` docstring still names `console_artifacts`, `console_access`,
      and `refs.console` (`test_console_surface_docs.py` guard) — now framed as
      opt-in.
- [ ] `envelope_for_run` unit tests (direct manifest) are unchanged.
- [ ] `just resources-docs` refreshes the packaged/guide doc snapshot and
      `just resources-docs-check` / `docs-check` pass.
- [ ] `just ci` is green.

## Failure modes and edge cases

- **`FAILED` Run.** Already skipped the manifest (its envelope is a failure
  envelope that never renders `console_artifacts`). The `include_console_artifacts`
  guard is `and`-combined with the existing `state is not FAILED` check, so a
  failed Run is unaffected regardless of the flag.
- **Opt-in on a Run with no console.** `list_run_console_artifacts` returns an
  empty manifest; `_console_manifest_data` already omits the key. Same as today.
- **Opt-in truncation.** When `total > 100`, `_total`/`_truncated` render exactly
  as today — the flag gates *whether* to fetch, not *how much*.
- **No extra query when off.** Skipping the call removes one `SELECT`+`count`
  round-trip from the default `runs.get` path (a small latency/token win, not a
  correctness concern).

## Rollback

Pure additive with a behavior-default flip on one envelope key. `refs.console`,
`data.console_access`, and every other `runs.get` field are unchanged. A consumer
that relied on the always-inlined manifest passes `include_console_artifacts=true`
to restore it. No migration, no persisted state. Rollback is reverting the branch.
