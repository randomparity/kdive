# Serialize stale testcontainer sweeps design

## Goal

Give concurrent Postgres and MinIO fixture processes one effective owner for stale backend
container removal, while keeping unrelated Docker conflicts and failures visible.

This design implements issue #1963 and is governed by
[ADR-0561](../../adr/0561-serialize-stale-testcontainer-sweeps.md).

## Scope and constraints

- Python 3.14 remains the runtime; use the standard library and existing `fcntl` support.
- The host is `x86_64`; the project targets `x86_64` and `ppc64le` Linux hosts.
- Change only shared test-backend cleanup and its focused tests.
- Preserve backend labels, liveness detection, live-container ownership, and fixture interfaces.
- Preserve warnings for unrelated Docker conflicts and failures.
- Add no dependency, production behavior, or Docker-wide cleanup surface.
- The full repository guardrail is `just ci`; CI gates its constituent recipes individually.

## Evidence and cause

`_start_postgres` and `_start_minio` each call `sweep_stale_backend_containers` before creating a
backend. Under xdist those calls occur in independent processes. Each call enumerates every
container carrying `kdive.test-backend`, applies the ADR-0551 liveness predicate, and removes stale
candidates. Nothing serializes enumeration with removal, so both processes can select one id.

Docker allows only one removal to own that id. The losing caller can receive HTTP 409 while removal
is underway. The current exception handler treats only NotFound as benign, so it warns even when
the exact container is about to disappear.

## Design

`tests/support/xdist_backend.py` adds the canonical Linux lock path
`/tmp/kdive-test-backend-sweep-<euid>.lock`. Its filename uses a fixed KDIVE-owned namespace plus
the effective user id, never an environment-selected temporary directory, checkout, or worktree
path. `sweep_stale_backend_containers` holds the dedicated lock across client construction,
candidate enumeration, liveness inspection, and removal. Acquisition uses `LOCK_EX | LOCK_NB`.
Ordinary contention returns an empty result without warning or Docker construction because the
current owner is already the one effective sweeper. A lock outside per-run pytest roots coordinates
Postgres and MinIO, all workers, and concurrent runs from every KDIVE checkout for the same user.

The canonical path has a dedicated context manager rather than the per-run `_locked` helper. It
opens with `os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW` and mode 0600, then uses `fstat`
on the opened descriptor to require a regular file owned by `os.geteuid()` before taking `flock`.
It never performs a stat-then-open check, follows a symlink, truncates the inode, or removes a path
it did not create. An open, owner, type, or lock failure takes the sweep-skipped warning path.

The removal exception path recognizes Docker `APIError` only when it has HTTP status 409 and
`exc.explanation` contains the semantic phrase
`removal of container <whitespace-delimited id> is already in progress`. A compiled expression
extracts that one non-whitespace id token, and the classifier compares it to the complete
`container.id`. Harmless surrounding API prose may vary; short ids, prefixes, superstrings, other
ids, and unrecognized wording do not match. A small helper then queries
`client.containers.get(container.id)` until the exact id raises NotFound or a five-second monotonic
deadline expires. Polling sleeps briefly between successful lookups. Verified absence is silent
and does not append the id to `reaped`, because this process did not complete the removal. Timeout,
lookup failure other than NotFound, any explanation mismatch, and every other removal error use the
existing per-container warning and continue with later candidates.

The helper accepts optional clock and sleep callables and resolves `time.monotonic` and `time.sleep`
inside the call when they are absent. This keeps production defaults ordinary while allowing both
private and public-path tests to inject deterministic time without waiting five real seconds.

The sourced concurrent-removal shape admits verification; exact-id absence remains the success
signal. Neither condition alone suppresses a warning.

## Failure handling

The sweep remains best-effort. Ordinary lock contention silently returns an empty list. Failure to
open or validate the sweep lock, construct the Docker client, or enumerate candidates emits the
existing sweep-skipped warning and returns an empty list. The sweep never continues unlocked. A
per-container failure emits one warning and does not abandon later candidates. Lock release occurs
through the context manager on normal return and every exception; process termination releases the
kernel lock.

Conflict verification is bounded to five seconds of process monotonic time per container. On
expiry the consequence is the existing warning, scoped to that exact candidate. Recovery is to let
the active Docker removal finish and rerun the failed test or allow the next fixture startup to
retry the sweep.

## Verification

- A deterministic multi-process regression starts two sweepers against one fake stale container,
  pauses the first removal, and proves the second returns without Docker enumeration while the lock
  is held. Exactly one process removes the id and neither warns.
- A focused conflict test makes removal raise the exact concurrent-removal 409, returns the
  container from at least two exact-id lookups, then raises NotFound. An injected monotonic clock and
  sleep callable make retries deterministic; the sweep is silent and does not claim the id in
  `reaped`.
- A deadline test advances the injected monotonic clock while the id remains present and asserts a
  warning at the five-second bound without a real wait.
- Explanation tests cover the exact full id with and without surrounding API prose, another id, a
  short-id prefix, and an id superstring; every identity mismatch remains a warning.
- A verification-lookup failure test makes exact-id lookup raise a non-NotFound Docker error. It
  asserts one warning, no reaped claim for that id, and successful removal of a later stale
  candidate.
- A lock-failure test proves the sweep warns, returns an empty list, and never enumerates Docker
  candidates when opening or acquiring the canonical lock fails.
- Lock-path tests pre-create a symlink and a non-regular path and simulate a wrong-owner descriptor;
  each proves the target is unchanged, the sweep warns, and Docker construction never begins.
- The existing NotFound, unrelated removal failure, uninspectable-container, and Docker-backed
  stale/live-container tests remain green.
- The focused support test passes, then `just ci` passes from a clean tracked tree without stale
  sweep warnings.

## Alternatives

ADR-0561 records the rejected conflict-only, per-container-lock, fixture-local-lock, message-match,
and do-nothing approaches. The repository-wide sweep lock plus exact-id verification is the
smallest design satisfying all issue criteria.

## Provenance

Issue #1963 supplies the outcome, completion criteria, exclusions, and permitted surface. The user
approved the repository-wide per-user lock with exact-id absence verification on 2026-08-14.

## Threat model

The new boundary is the predictable lock pathname in world-writable `/tmp`. A different local user
can create directory entries there but cannot choose the effective uid in the KDIVE process or
control an already-open descriptor. The control is a no-follow, non-truncating open followed by
regular-file and effective-owner validation on that same descriptor. Failure reveals only the path
and operating-system error in the existing local test warning and prevents Docker enumeration.

The design widens no network, Docker, tenant, authentication, or production boundary. A local actor
may deny the optional stale sweep by occupying the pathname with an unsafe inode; preventing local
test denial of service is out of scope. The required non-regression boundary is that such an actor
cannot redirect a write, cause truncation, or make the sweep proceed unlocked.

## Durable workflow context

- Branch: `feat/serialize-stale-sweeps-1963`
- Base branch: `main`
- Guardrails: focused pytest commands during TDD; `just ci` before completion
- Architecture: host `x86_64`; targets `x86_64` and `ppc64le`; relationship `included`
