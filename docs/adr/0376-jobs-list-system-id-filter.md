# ADR 0376 — `jobs.list` `system_id` filter for system-scoped jobs

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** kdive maintainers

## Context

`jobs.list` (ADR-0197) accepts `status`, `kind`, and `investigation_id` filters. The
`investigation_id` filter joins `runs` on `jobs.payload->>'run_id'`, so it matches only
run-bearing kinds (`build`/`install`/`boot`). System-scoped kinds — `authorize_ssh_key`
and `check_ssh_reachable` — extend `SystemPayload`, which carries a `system_id` but **no**
`run_id`. They are therefore structurally excluded from every investigation-filtered
listing, and no `system_id` filter exists to reach them by. During an investigation an
agent that just ran the SSH-access jobs for a System could only find them by scanning
unfiltered pages (#1249).

The DB layer already resolves a job by its payload `system_id`:
`latest_succeeded_job_for_system` (`queue.py`) matches `payload->>'system_id'` for the
`runs.get` liveness read (ADR-0373). The gap is only that the list path never exposed the
same predicate.

## Decision

**Add a `system_id` filter to `jobs.list`.** Thread a new optional `system_id: str | None`
field through the public `_JobsListPayload` and the internal `JobsListRequest` mirror
(`mcp/tools/jobs.py`), and a `system_id: UUID | None` parameter through the DB-layer
`recent_jobs` (`jobs/queue.py`). The predicate is a direct equality on the payload column,
`j.payload->>'system_id' = %s`, mirroring `latest_succeeded_job_for_system` — no join,
because `system_id` lives on the job payload itself. A malformed `system_id` is an
`invalid_uuid` `configuration_error`, matching the `investigation_id` validation. The
filter composes with `status`/`kind`/`investigation_id` and the keyset cursor, applied
before the seek so the cursor stays a pure boundary.

**Document the investigation-filter exclusion.** The `investigation_id` `Field`
description now states that system-scoped jobs (`authorize_ssh_key`, `check_ssh_reachable`)
are excluded because they carry no `run_id`, pointing the agent at the new `system_id`
filter as the way to reach them.

**No migration.** The `jobs.payload` JSON column already carries `system_id` for the
system-scoped kinds; the filter is a pure query over an existing field.

## Consequences

- An agent can list a System's `authorize_ssh_key`/`check_ssh_reachable` jobs directly
  (`jobs.list(system_id=…)`) instead of paging the unfiltered history.
- `recent_jobs` gains one optional equality predicate; the project predicate still gates
  every row, so a System in an unreadable project yields no rows (no existence leak).
- The agent-facing schema (wrapper `Field` text + the generated tool reference) documents
  both the new filter and the investigation-filter exclusion.

## Alternatives considered

- **Documentation only (issue's minimum).** Rejected: documenting the exclusion without a
  `system_id` filter leaves the system-scoped jobs unreachable by any filter — the actual
  gap.
- **Join `runs` and widen the investigation filter to system-scoped jobs.** Rejected:
  those jobs have no run/investigation linkage at all; a `system_id` filter is the correct
  key and needs no join.
- **A denormalized `system_id` column on `jobs`.** Rejected: a migration and write-path
  invariant for a predicate the existing `payload->>'system_id'` answers directly, exactly
  as `latest_succeeded_job_for_system` already does.
