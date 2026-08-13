# Active capture guard design (#1945)

## Scope

Fix the ADR-0094 host-dump live-holder guard so a running Run-addressed `capture_vmcore` job
protects its System's volume, while a permanently queued job cannot retain an orphan forever.
Traffic-capture reclamation, queue retry policy, provider behavior, and MCP contracts remain
unchanged. [ADR-0557](../../adr/0557-running-only-host-dump-live-holder.md) records the state
bound.

## Design

`has_active_capture_job(conn, system_id)` remains the single guard used by
`reap_orphaned_dump_volumes`. Its query joins `jobs` to `runs` by parsing the job payload's
`run_id` with the non-throwing text comparison
`runs.id::text = jobs.payload->>'run_id'`, filters `jobs.kind = 'capture_vmcore'`, filters
`jobs.state = 'running'`, and compares `runs.system_id` with the requested System. Parameters
remain bound query values.

The query does not treat `queued` as active. Queue admission is intent to run, not evidence of
a provider operation, and a queued row has no terminal age bound. Once claimed, the existing
queue transition to `running` precedes handler execution; at that point the live-holder guard
protects the volume. The existing 30-minute, database-clock-referenced mtime grace continues to
cover fresh volumes independently.

No public interface changes. The function's docstring will say `running` rather than
`non-terminal`, keeping its internal contract aligned with the query.

## Failure behavior

Missing or malformed payload Run identities, missing Runs, and Runs bound to another System do
not match. Terminal and queued jobs do not match. Other database errors continue to fail the
reconciliation pass through the existing repair runner; this change does not add error
suppression.

## Verification

Database-backed regression tests will prove:

1. `has_active_capture_job` returns true for a running `capture_vmcore` job whose payload names
   a Run bound to the requested System. This fails against the shipped payload lookup.
2. The dump-volume sweep skips an old volume for that running job.
3. The sweep deletes an old volume when the matching job remains queued, proving the guard has
   no permanent queued-row pin.
4. A running capture row with a malformed or absent payload `run_id` neither matches nor aborts
   the dump-volume sweep.
5. Existing terminal, grace-window, iteration, and per-volume failure-isolation tests remain
   green, followed by `just ci`.

The tests exercise the Postgres JSON/UUID join rather than mocking SQL results. The change has
no architecture-sensitive generation; the retained context is host `x86_64`, declared targets
`x86_64` and `ppc64le`, relationship `included`.

## Repository context

- Branch: `feat/fix-active-capture-guard-1945`
- Base branch: `main`
- PR guardrail: `just ci`
- CI also gates the `just` sub-recipes individually and runs the separate decision-record
  workflow. ADRs have no hand-maintained index, so record/index coupling is not applicable.
