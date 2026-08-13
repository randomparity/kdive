# 0557 — Treat only running host-dump captures as live holders

## Status

Accepted (2026-08-12)

## Context

ADR-0094 requires the orphaned host-dump volume sweep to skip a volume while its System has
an active `capture_vmcore` job. The shipped query reads `system_id` from the job payload, but
`CaptureVmcorePayload` is Run-addressed and contains only `run_id` plus the capture method. The
query therefore never matches.

Resolving the System through `runs` makes the guard effective, but the existing active-state
set includes `queued`. A queued job on an unserved lane has no lease or age transition that
forces it terminal, so treating it as a live holder can retain a stale dump volume forever.
The guard needs a bound that reflects provider activity rather than queue intent.

## Decision

The host-dump live-holder query resolves ownership through `jobs.payload.run_id → runs.id` with
the non-throwing text comparison `runs.id::text = jobs.payload->>'run_id'`, then matches the
requested `runs.system_id`. It treats only a `running` `capture_vmcore` job as a live holder.
A missing or malformed payload identity does not match and cannot abort the whole sweep.

A queued job is not a live holder because no worker has begun its provider operation. When it
is claimed, its state becomes `running` before provider capture proceeds, so a sweep that samples
the job after that transition skips deletion. The state check and provider deletion are not one
atomic operation, however: a claim can occur between them. #1955 owns the shared coordination
needed to close that pre-existing residual race. The existing ADR-0094 mtime grace remains the
independent guard for a newly changed volume.

## Consequences

- A running Run-addressed capture prevents the reconciler from deleting its System's dump
  volume.
- A permanently queued capture cannot pin an old orphaned volume.
- Correctness depends on the existing queue contract that a worker records `running` before it
  performs provider work. This change adds no new job state or clock.
- The guard remains a state sample rather than an exclusion boundary. It fixes the shipped
  always-false predicate and protects captures already running when sampled, but it does not make
  a queued-to-running transition atomic with deletion; #1955 tracks that coordination.
- The query adds a join to `runs` on its primary key for each old volume considered by the
  sweep.
- The text comparison does not use the UUID index for the join, but the query first narrows to
  the small set of running `capture_vmcore` rows and stops after its first match.

## Considered & rejected

- **Keep queued and running, with a job-age cutoff.** This introduces an arbitrary clock that
  does not prove provider activity. Before the cutoff it retains a known orphan for a job that
  may never run; after the cutoff it would ignore a genuinely running job unless state and age
  rules became more complex.
- **Keep the current payload lookup.** `CaptureVmcorePayload` forbids and never stores
  `system_id`, so this leaves the live-holder guard vacuous.
- **Change `CaptureVmcorePayload` to carry `system_id`.** The handler and ownership model are
  intentionally Run-addressed. Duplicating System identity creates a consistency question and
  broadens a reconciler fix into a job-contract change.
