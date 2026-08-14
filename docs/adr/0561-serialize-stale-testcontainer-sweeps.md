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

The repository's stale-backend sweep uses one per-user filesystem lock at the canonical Linux path
`/tmp/kdive-test-backend-sweep-<euid>.lock`. Its fixed KDIVE-owned namespace never depends on an
environment-selected temporary directory, checkout, or worktree path. It takes an exclusive
non-blocking `fcntl.flock` before Docker enumeration and holds it through all removals. A contender
that observes the lock already held skips its optional sweep silently because one effective sweeper
already exists. Postgres and MinIO fixture processes, xdist workers, and concurrent test runs from
every KDIVE checkout under the same effective user therefore never remove concurrently. The empty
lock file may persist; process exit releases the kernel lock. The repository targets Linux, and
`/tmp` is a required host prerequisite.
The sweep uses a dedicated opener with `O_NOFOLLOW`, creates mode 0600, and validates the opened
descriptor is a regular file owned by the effective user before locking. An unsafe existing path
skips the best-effort sweep with a warning; it is never followed, truncated, or removed.

If Docker still reports HTTP 409 whose daemon explanation identifies removal of this exact
container as already in progress, the sweeper verifies only that container id. Both the 409 status
and the concurrent-removal explanation are required; every other conflict preserves the existing
warning. Verification polls Docker until the id is absent for at most five seconds measured by the
process monotonic clock, per conflicted container. Verified absence is benign and is not reported
as a successful removal by this process. Expiry or any other failure preserves the warning; a later
fixture startup retries the whole sweep.

The lock changes only test cleanup. It adds no dependency and does not alter liveness detection,
container labels, fixture acquisition, or production behavior.

## Consequences

Stale sweeps from every KDIVE checkout under one user have at most one active owner. A contending
fixture proceeds without cleanup rather than waiting for Docker enumeration or removal latency.
This can leave a different stale candidate until the next uncontended fixture startup, which is
consistent with best-effort cleanup and avoids making it a fixture-availability dependency.

A Docker actor outside this lock can still race removal. Concurrent-removal classification plus
exact-id absence verification handles the sourced benign case without suppressing another 409 or a
conflict whose container remains. The five-second verification limit can still produce a warning
for an unusually slow successful removal; that is preferable to hiding a container that remains
conflicted.

The predictable name exists in a world-writable directory. Descriptor-relative type and ownership
validation prevents another local user from redirecting or supplying the lock inode. A hostile path
can deny the optional stale sweep and cause a warning, but cannot make the test process mutate the
path or continue without serialization.

## Considered & rejected

- **Handle HTTP 409 without serialization.** This removes the observed warning but leaves multiple
  effective sweepers and makes correctness depend on interpreting every conflict.
- **Use one lock per container.** It permits parallel removals but adds lock naming and lifecycle
  surface for a startup cleanup path with few containers and no measured throughput problem.
- **Serialize only within each fixture.** Postgres and MinIO use separate fixture processes and can
  still race the same repository-wide candidate set.
- **Treat every HTTP 409 as concurrent removal.** An unrelated conflict could be followed by a
  different actor removing the container during verification. Requiring both the sourced daemon
  explanation and exact-id absence keeps that conflict visible.
- **Do nothing.** Repeated full-suite runs emitted warnings, violating the warning-free test gate.
