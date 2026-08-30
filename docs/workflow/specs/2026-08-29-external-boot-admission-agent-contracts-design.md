# External-boot admission and agent contracts design

## Scope

Issue #2117 implements ADR-0583's server-side admission and agent-contract slice. It applies one
System-locked matrix to Run creation and install, System lifecycle and control, snapshot and capture,
and DebugSession attach/detach. It also exposes the contributor release, project-admin conflict
resolution, and platform-admin orphan-repair contracts with uniform `ToolResponse` envelopes.

Job execution, reconciliation, provider mutation, and provider recovery mechanisms remain owned by
#2118 and provider issues. This slice may persist and enqueue the minimum versioned BOOT-job request
accepted by ADR-0584; it does not register or implement an external recovery handler. Python 3.14,
x86_64, and ppc64le remain supported and no dependency is added.

## Architecture

`services/external_boot/admission.py` is the only operation matrix. Callers hold the existing
`LockScope.SYSTEM` transaction lock before querying the one activation that is not fully cleaned.
The repository supplies that lookup. The decision accepts unrestricted work when no activation is
present or a terminal activation is cleaned. Transitional, conflict, failed, and cleanup-pending
states admit only activation continuation, reconciliation, conflict resolution, and teardown.
`active` additionally admits owning-Run debug attach/detach, traffic capture, force crash, and
vmcore capture; it rejects another Run and generic lifecycle, power, snapshot, or install work.
Allowed provider-mutating active operations return the activation identity and authority binding for
the later execution mechanism; observation remains on existing read-only seams.

Every reverse-admission caller invokes this service inside its existing System lock immediately
before durable admission or enqueue. Run creation adds the guard beside its locked host checks.
Install, control, snapshot, capture, and DebugSession lifecycle move or extend their lock boundary
only as required to make the activation query and enqueue/transition atomic. Teardown passes the
matrix explicitly. A denial is `CONFLICT`, includes only the authorized activation id/state and
owning Run id, and suggests `runs.get`; active also suggests `runs.release_external_boot`, while
conflict/failed states suggest `systems.teardown`.

`services/external_boot/recovery_requests.py` owns release and conflict-resolution admission. Under
the System lock, release requires contributor on the owning Run, an `active` activation, no active
System job, and no attaching/live DebugSession. Conflict resolution requires project admin, literal
`restore-recorded-source`, and the exact current conflict composite-state identity. Each operation
reads database `server_time`, computes one absolute UTC `recovery_readiness_deadline` from configured
seconds, persists the transition request and a versioned ADR-0584 authority marker, and enqueues one
idempotent `JobKind.BOOT` job in the same transaction. Ordinary retry returns that same job and never
extends the deadline. The marker is the executable queue handoff #2118 consumes; this change does not
claim that a worker can finish it before #2118 lands.

`ops.resolve_recovery_orphan` is a platform-admin repair admission contract. Because no durable
quarantine record or executable repair mechanism exists yet, it validates the bounded repair
reference and returns a truthful `configuration_error` with `reason=repair_executor_unavailable`
and recovery guidance; it does not claim success or enqueue an unexecutable job. This keeps the
required agent contract discoverable without inventing persistence owned elsewhere.

MCP wrappers live with the existing runs, systems, and ops registrars. Wrapper docstrings and every
`Field` description state RBAC, admissible state, idempotency, and the full time contract: seconds,
database `server_time`, one recovery attempt, timeout consequence, and recovery action. Successful
release/conflict responses are normal running-job envelopes enriched with activation id/state,
`server_time`, and the absolute deadline. Failure envelopes use the stable taxonomy and literal tool
names only.

## Failure contract

- Foreign or missing objects fail as `configuration_error` without disclosing membership.
- A matrix denial is non-retryable `conflict`; callers follow the returned action for the current
  activation state.
- Active jobs or sessions block release before any transition or enqueue and identify only bounded
  object ids already authorized to the caller.
- Replayed release or conflict requests return the same job, attempt, and deadline.
- A changed conflict identity leaves `recovery_conflict` untouched.
- Queue or transaction failure rolls back the transition and deadline together.
- The orphan-repair wrapper never reports success until its separately owned executor exists.

## Threat model

Authenticated project members cross the MCP boundary with Run/System ids, idempotency keys, and a
conflict observation digest. Existing project membership and `require_role` checks bind them to the
stored project; contributor cannot resolve conflicts, project admin cannot invoke platform repair,
and foreign identifiers reveal no activation. The System lock and database uniqueness prevent two
Runs from both passing admission. Exact digest validation prevents an administrator from approving a
state that changed after observation. Bounded Pydantic fields reject malformed or oversized input.

The worker queue boundary receives only immutable ids, the server-computed operation/deadline, and
the ADR-0584 marker; it receives no credential, provider definition, command, path, or secret. The
authority allocator revalidates the locked job and actor attempt before mutation. Provider-host and
database administrators remain trusted as stated by ADR-0584. Provider execution, journal integrity,
reconciliation, orphan deletion/adoption, and live-provider behavior are explicitly outside this
slice and must not be represented as completed behavior.

## Verification

Table tests cover every activation state and operation, owning versus other Run, and cleaned terminal
states. PostgreSQL tests prove reverse admission is atomic and race another Run's install against
release so exactly one proceeds. Focused MCP tests cover RBAC, redaction, idempotent replay, unchanged
deadlines, conflict CAS, literal next actions, wrapper schemas, and the truthful unavailable repair
response. Existing control, snapshot, vmcore, Run, and DebugSession tests gain negative cases at the
shared service boundary. `just lint`, `just type`, focused `just test-verbose` commands, and `just ci`
are the required gates.
