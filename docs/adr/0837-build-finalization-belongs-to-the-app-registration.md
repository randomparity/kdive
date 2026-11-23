# 0837 — Build finalization belongs to the app registration

## Status

Accepted (2026-11-23)

Supersedes the process-wide task namespace in
[ADR-0675](0675-single-flight-external-build-finalization.md).

## Context

The global complete-build map keys only on Run UUID while its task captures a pool,
validator and store factory. Two app contexts can authorize the same UUID in different
databases. A reproduction returned success to both callers while the second database's
Run remained created with no build row and no validator call.

Production registration currently constructs a handler for each request, so moving the
map onto that temporary handler alone would lose retry coalescing.

## Decision

Capture one `CompleteBuildHandlers` when registering `runs.complete_build` and keep the
Run-to-task map on that instance. Calls through that registration join its running work.
Other registrations own independent work, even when their Run UUIDs match.

Retain authorization before lookup, cancellation-resistant `wait`/`task.result`,
task-owned connections and identity-guarded completion cleanup. Recorded-result replay,
the shared validation slot and the database publication fences remain unchanged.

## Consequences

A retry to the same server instance still joins its finalize. A different instance may
validate independently, as another process already can; a shared database still records
one build. Isolated databases cannot answer from each other's tasks.
The normal server remains one event loop per process; this does not redesign the global
validation capacity for multiple loop-hosting threads. Restart recovery stays unchanged.

## Considered & rejected

- **Keep the global map.** verified: `git show 52e1af0:src/kdive/mcp/tools/lifecycle/runs/complete_build.py`
  declares a UUID-only task map and starts the task with the first caller's pool.
- **Add dependency identities to a global map key.** judgment: this adds an identity and
  retention policy where the existing app registration already owns those dependencies.
- **Introduce durable finalize jobs.** judgment: that changes a public workflow and is
  unnecessary for this app-ownership correction; ADR-0675 retains its reopening conditions.
