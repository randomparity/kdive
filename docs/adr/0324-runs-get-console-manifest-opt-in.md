# ADR 0324 — `runs.get` console manifest opt-in

- **Status:** Accepted
- **Date:** 2026-07-09
- **Deciders:** kdive maintainers
- **Spec:** [`../specs/2026-07-09-runs-get-console-manifest-opt-in-1067.md`](../specs/2026-07-09-runs-get-console-manifest-opt-in-1067.md)
- **Follows:** [ADR-0279](0279-console-run-correlation.md) — the Run-scoped
  console manifest (`list_run_console_artifacts`, `data.console_artifacts`) this
  change makes opt-in.

## Context

`runs.get` inlines the Run-scoped console manifest under `data.console_artifacts`
for every non-`FAILED` Run (#1067, `BLACK_BOX_REVIEW.md` P7). `get_run`
(`view.py`) unconditionally calls `list_run_console_artifacts(conn, run.id)` and
`_console_manifest_data` (`common.py`) emits the full `entries` list — up to
`CONSOLE_MANIFEST_MAX = 100` `{artifact_id, object_key, created_at}` rows, plus
`_total`/`_truncated` when overflowed. `runs.get` takes only `run_id`; there is no
opt-out.

A `runs.get` call is a status/provenance read an agent pays for per token. The
boot-window console snapshot is already reachable at `refs.console`, with
`data.console_access` naming how to read it — so the inlined manifest is hundreds
of lines of rarely-wanted rotating-part keys on the common path.

## Decision

Make the manifest opt-in behind a new keyword-only flag.

**Flag.** Add `include_console_artifacts: bool = False` to `get_run` (`view.py`)
and to the `runs.get` wrapper (`registrar.py`) as a `Field(default=False)` with an
agent-facing description.

**Conditional fetch.** The `list_run_console_artifacts` call is gated on the flag,
`and`-combined with the existing non-`FAILED` guard:

```
console_manifest = (
    await list_run_console_artifacts(conn, run.id)
    if include_console_artifacts and run.state is not RunState.FAILED
    else None
)
```

When the flag is `False` (default) the manifest is never fetched, so no
`SELECT`+`count` runs and the envelope omits `data.console_artifacts` and its
`_total`/`_truncated` siblings. When `True`, behavior is byte-identical to today.

**No downstream change.** `envelope_for_run` and `_console_manifest_data` already
return `{}` for a `None`/empty manifest, so the opt-out is purely "don't fetch" —
no rendering branch is added. `refs.console` and `data.console_access` stay
unconditional (cheap, and the primary console-read affordance).

**No migration.** No schema, persistence, or service-layer change. The manifest
service (`list_run_console_artifacts`, `ConsoleManifest`) is untouched.

## Consequences

- The default `runs.get` envelope shrinks: no console manifest, and one fewer DB
  round-trip on the common path. `refs.console`/`data.console_access` are
  unchanged, so an agent still discovers and reads the boot console.
- A consumer that wants the full manifest passes
  `include_console_artifacts=true`, getting today's exact output (bound,
  newest-first, with truncation markers).
- This is a **default-behavior flip on one envelope key**: any consumer that relied
  on `data.console_artifacts` always being present must now opt in. The key was
  already conditional (absent when the Run had no console, or when `FAILED`), so
  consumers already had to tolerate its absence.
- The `runs.get` wrapper docstring reframes `data.console_artifacts` as opt-in; the
  generated guide docs regenerate from it.

## Considered & rejected

- **A separate `runs.console_manifest` tool** (the issue's alternative). Adds a
  new tool, its own auth/validation/pagination surface, and a second place to
  document the manifest — for a read that is naturally a facet of the Run. A
  boolean on the existing read is the smaller surface and keeps the manifest
  co-located. Rejected.
- **Keep it always-inline but paginate/shrink the cap.** Still spends tokens and a
  query on every status read for data most `runs.get` callers ignore, and the
  boot snapshot is already at `refs.console`. Rejected — opt-in is the token win.
- **Default the flag `True`** (opt-out instead of opt-in). Preserves today's
  behavior but does not fix the reported cost — the point is that the common path
  should be cheap. Rejected.
- **Render `data.console_artifacts: []` when off** (empty list rather than absent
  key). `_console_manifest_data` already omits the key when empty; emitting an
  empty list would diverge from that convention and mislead a caller into thinking
  the Run has no console when it simply was not requested. Rejected — omit the key.
