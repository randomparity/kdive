# ADR-0414: a completed teardown steers the agent to allocations.release (#1385)

- Status: Accepted
- Date: 2026-07-21

## Context

`systems.teardown` drives a System to `torn_down`, but its Allocation stays `active` until
`allocations.release` is called separately. Nothing on the teardown surface pointed the agent
at that second step, so the two-step wind-down was easy to forget; a forgotten release holds
budget against the allocation until the reconciler's grace elapses (BLACK_BOX_REVIEW.md §8.2).
This is a missed prompt, not a leak — the reconciler still auto-releases orphaned `active`
allocations — hence the low priority. The doc-encoded golden path already names
teardown → release → close (agent-index.md, guarded by `test_next_actions_graph.py`); the gap
was that the *runtime* `suggested_next_actions` did not match that intent.

Two paths omitted the hint:

1. The idempotent already-`torn_down` short-circuit
   (`mcp/tools/lifecycle/systems/admin.py`) hardcoded `suggested_next_actions=["systems.get"]`.
2. A completed TEARDOWN job. `systems.teardown` enqueues a job and returns a **queued**
   envelope; the agent then polls the job to completion via `jobs.wait` / `jobs.get` /
   `jobs.list`, all of which render the terminal job through the shared
   `jobs._job_response` → `ToolResponse.from_job` with no `extra_next_actions`. A SUCCEEDED job
   therefore surfaced only the generic `_NEXT_ACTIONS[SUCCEEDED] = ["jobs.get"]`.

## Decision tension

The issue flags a real design tension. `ssh_access.py` attaches tool-specific next actions via
`from_job(job, extra_next_actions=…)`, but that seam decorates only the **synchronous** enqueue
envelope a tool returns — not later renders of the same job. The completed teardown job is
**never** returned synchronously by `systems.teardown` (which returns the queued envelope); it is
only ever reached through the generic `jobs.*` readers. So routing teardown's completed job
"through a tool-specific renderer" cannot work — there is no teardown-specific code in the
`jobs.get` / `jobs.wait` / `jobs.list` path, and the queued synchronous envelope must *not* carry
`allocations.release` (nothing is freed until the job succeeds).

## Decision

Make the release steer a durable property of the **completed teardown job**, so every render of
it — `jobs.get`, `jobs.wait`, `jobs.list` — carries the hint consistently.

- Add `_TERMINAL_KIND_ACTIONS: {JobKind.TEARDOWN: ["allocations.release"]}` in `mcp/responses.py`.
  `ToolResponse.from_job` appends the kind's steer **only when `job.state is SUCCEEDED`**, after
  the generic lifecycle set and before any caller-supplied `extra_next_actions`. A queued,
  running, failed, or canceled teardown froze nothing to release, so it is omitted there.
- Fix the idempotent already-`torn_down` short-circuit in `admin.py` to return
  `["allocations.release", "systems.get"]`, so the replay steers identically to a
  freshly-completed teardown job.
- State the two-step wind-down on the `systems.teardown` wrapper docstring (the agent contract
  serialized into the tool schema).

This is option (a) from the issue — kind-keyed dispatch in `from_job` — chosen over (b) because
(b) does not reach the surface the acceptance criterion names.

## Consequences

- Any render of a SUCCEEDED teardown job now lists `allocations.release`. Both acceptance paths
  (the idempotent short-circuit and the completed job) are covered.
- The steer is **not** RBAC-filtered in `from_job`, which has no request context. This matches
  the existing worker-plane behavior: the generic lifecycle set already advertises `jobs.cancel`
  (a contributor/operator gate) to viewers polling a queued job. The next actions are advisory;
  the execution-time gate on `allocations.release` (contributor) remains the boundary. The
  `admin.py` short-circuit needs no filtering either — it is reached only after the ADMIN gate,
  and admin subsumes the contributor `allocations.release` requires.
- Only `JobKind.TEARDOWN` is mapped; every other kind's terminal render is unchanged.

## Rejected alternatives

- **Attach `extra_next_actions` on teardown's synchronous envelope** (the ssh_access seam). The
  synchronous envelope is queued, not terminal, and is not the surface the agent reads after the
  job completes — it would never satisfy the completed-job acceptance path.
- **Thread `RequestContext` into `from_job` to RBAC-filter the steer.** A large change to the
  worker-plane signature for no behavioral gain (the admin/contributor relationship makes the
  filter a no-op for the real callers) and inconsistent with the already-unfiltered lifecycle set.

No schema change, no migration.
