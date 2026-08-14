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

`tests/support/xdist_backend.py` adds a sweep lock path beneath `tempfile.gettempdir()`. Its filename
uses a fixed KDIVE-owned namespace plus the effective user id, never a checkout or worktree path.
The existing `_locked` context manager takes an exclusive `fcntl.flock` on it.
`sweep_stale_backend_containers` holds that lock across client creation, candidate enumeration,
liveness inspection, and removal. A lock outside per-run pytest roots coordinates Postgres and
MinIO, all workers, and concurrent runs from every KDIVE checkout for the same user.

The removal exception path distinguishes Docker `APIError` with HTTP status 409 from NotFound and
other failures. For a 409, a small helper queries `client.containers.get(container.id)` until the
exact id raises NotFound or a five-second monotonic deadline expires. Polling sleeps briefly between
successful lookups. Verified absence is silent and does not append the id to `reaped`, because this
process did not complete the removal. Timeout, lookup failure other than NotFound, and non-409
removal errors use the existing per-container warning and continue with later candidates.

No message matching decides whether a conflict is benign. Exact-id absence is the only success
signal after a conflict.

## Failure handling

The sweep remains best-effort. Failure to construct the Docker client or enumerate candidates emits
the existing sweep-skipped warning and returns an empty list. A per-container failure emits one
warning and does not abandon later candidates. Lock release occurs through the context manager on
normal return and every exception; process termination releases the kernel lock.

Conflict verification is bounded to five seconds of process monotonic time per container. On
expiry the consequence is the existing warning, scoped to that exact candidate. Recovery is to let
the active Docker removal finish and rerun the failed test or allow the next fixture startup to
retry the sweep.

## Verification

- A deterministic multi-process regression starts two sweepers against one fake stale container,
  pauses the first removal, and proves the second cannot enumerate until the first releases the
  sweep lock. Exactly one process removes the id and neither warns.
- A focused conflict test makes removal raise Docker 409, then makes exact-id lookup raise NotFound;
  the sweep is silent and does not claim the id in `reaped`.
- A retained-conflict test keeps the exact id present through the bounded verifier and asserts the
  existing warning remains.
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

## Durable workflow context

- Branch: `feat/serialize-stale-sweeps-1963`
- Base branch: `main`
- Guardrails: focused pytest commands during TDD; `just ci` before completion
- Architecture: host `x86_64`; targets `x86_64` and `ppc64le`; relationship `included`
