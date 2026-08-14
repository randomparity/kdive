# 0561 — Serialize stale testcontainer sweeps

## Status

Accepted (2026-08-14)

## Context

Postgres and MinIO xdist fixture processes independently sweep crash-stranded backend containers
before starting their shared testcontainers. The stale-container predicate is safe, but the sweep
itself has no cross-process owner. Two sweepers can select the same stale container and call Docker
removal concurrently. Docker then returns HTTP 409 `removal already in progress` to one caller,
which the best-effort sweep reports as a warning even though removal is succeeding.

Docker's NotFound response is already treated as benign. Treating every removal conflict as benign
would hide unrelated conflicts, while handling only the observed message would leave competing
sweepers and daemon-version wording coupled.

## Decision

The repository's stale-backend sweep uses one per-user filesystem lock in the platform temporary
directory. Its filename contains a fixed KDIVE-owned sweep namespace plus the effective user id; it
never derives identity from a checkout or worktree path. It takes an exclusive `fcntl.flock` before
Docker enumeration and holds it through all removals. Postgres and MinIO fixture processes, xdist
workers, and concurrent test runs from every KDIVE checkout under the same operating-system user
therefore have one effective sweeper. The empty lock file may persist; process exit releases the
kernel lock.

If Docker still reports a removal conflict, the sweeper verifies only the exact container id. It
polls Docker until that id is absent for at most five seconds measured by the process monotonic
clock, per conflicted container. Verified absence is benign and is not reported as a successful
removal by this process. Expiry or any non-conflict failure preserves the existing warning; a later
fixture startup retries the whole sweep.

The lock changes only test cleanup. It adds no dependency and does not alter liveness detection,
container labels, fixture acquisition, or production behavior.

## Consequences

Stale sweeps from every KDIVE checkout under one user are serial, including Docker enumeration time
and removal latency. Fixture startup can wait behind another sweep, and a live stuck sweeper can
delay contenders until that process exits; the lock itself has no timeout. The protected work is
startup-only and bounded to KDIVE-labelled containers, so cross-checkout serialization is accepted.

A Docker actor outside this lock can still race removal. Exact-id absence verification handles the
benign case without suppressing a conflict whose container remains. The five-second verification
limit can still produce a warning for an unusually slow successful removal; that is preferable to
hiding a container that remains conflicted.

## Considered & rejected

- **Handle HTTP 409 without serialization.** This removes the observed warning but leaves multiple
  effective sweepers and makes correctness depend on interpreting every conflict.
- **Use one lock per container.** It permits parallel removals but adds lock naming and lifecycle
  surface for a startup cleanup path with few containers and no measured throughput problem.
- **Serialize only within each fixture.** Postgres and MinIO use separate fixture processes and can
  still race the same repository-wide candidate set.
- **Treat `removal already in progress` text as NotFound.** Docker wording is not the invariant, and
  matching it could suppress an unrelated conflict without proving absence.
- **Do nothing.** Repeated full-suite runs emitted warnings, violating the warning-free test gate.
