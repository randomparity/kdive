# Derived authority completion routing

## Problem

Derived release and teardown complete their root receipt transaction through
an early hook in the ordinary operation runner. Three required provider/result
callbacks are unreachable assertions, and release casts through the mismatch.
An unused recovery factory describes a path the registrar never invokes.
Initial local recovery timing also reparses the same request for two fields.

## Scope and design

Follow ADR-0832. Keep the authority runner module as the admission owner.
Extract the existing timing/client assembly into _invocation_ports, returning
its effective ports and local timing snapshot. Retain the parsed local
RecoveryRequestV1 for both local_timing and continuing readiness_deadline.
Preserve non-local behavior, default timeouts, activation's committed deadline
plus 30 seconds, fresh recovery budgets and nonnegative client deadlines.

Extract _prepare_operation: resolve binding and activation; configure invocation
ports; run preconditions; check required preparation/teardown executors; allocate
and acknowledge authority; build OperationContext; debit/materialize preparing
state; recheck required evidence. Return context and its effective ports.
Keep the existing awaits, database functions and exception boundaries in order.
Refusals before acknowledgement still propagate without an invented bound result.

Ordinary run_operation uses that context and retains its optional early hook,
provider call and result builder with their existing COMMIT/PROVIDER_CALL phases.
Add run_derived_operation with the same admission inputs and exactly one async
complete(context) callback. Translate its ordinary exceptions as COMMIT failures
using effective invocation ports; propagate cancellation unchanged. Return the
exact completion or conflict subtype, including existing derived subclasses.

Migrate release and teardown directly to this entrypoint, preserving their
completion bodies and preconditions. Remove the three assertion functions,
release cast and unreferenced _recovering_handler. Update stale module/runner
prose to distinguish worker-committed results from already-committed completions.
No typed producer/model or serialization change belongs to this subtask; those
are the next required commit under the same cluster.

Global constraints: Python 3.14 managed by uv; x86_64 and ppc64le targets;
public tool contracts preserved; local changes and commits only.

## Failure model

- Actors and deployments: marked worker jobs and the configured local/remote
  authority host in local, Compose and Kubernetes deployments; authenticated
  tenant requests reach existing MCP admission before this internal runner.
- Invariants and assets: current claimed authority, receipt ownership, one-time
  release credit, System cleanup and exact result subtype; preparation and
  readiness semantics; no provider call before required admission/acknowledgement.
- Accepted failure classes: an authority refusal before acknowledgement cannot
  create a bound failure, as before; process death during receipt workflows is
  handled by durable re-entry rather than this control-flow extraction.
- Covered elsewhere: SQL current-holder/receipt fences, worker carrier binding
  and finalization checks, provider-side mutation journal and lifecycle repairs.

## Threat model

- Boundaries added/widened: none. Existing worker-to-authority client, credential
  and database boundaries move as unchanged calls, not new permissions.
- Actors/trust: authenticated tenants cannot supply runner callbacks; worker
  assembly owns callback/client selection; the host verifies marked operations.
- Controls: retain preconditions, worker credential, exact authority allocation,
  acknowledgement and context-bound factory client. Existing SQL and worker
  binding checks remain mandatory for terminal writes and credit.
- Out of scope: changing those authorization/protocol controls or deployment
  trust; their existing owners remain responsible. No user-visible error detail
  or secret-handling expansion is introduced.

## Success and validation

1. Both derived handlers use a complete-only path after common admission;
   ordinary deadline/attempt/provider paths retain their current sequencing.
2. Completion errors retain commit attribution, current bound fields and recovery
   deadline; cancellation and pre-acknowledgement refusals keep their handling.
3. Real receipt/replay suites preserve credit, cleanup and worker result subtype.
4. Initial timing resolves one parsed recovery request, with the same timeout
   choices and invocation-bound client in the named local cases.

Use the existing real-role runner/lifecycle/release/recovery suites (baseline
137 pass19.99s). Strengthen derived failure-phase and subtype assertions; add
factory deadline cases for committed activation, continuing/renewed recovery and
expired-deadline clamp. Behavior-preserving cases may already pass before the
refactor; do not invent behavioral reds for relocating existing control flow.
Controlled faults must catch wrong derived failure phase, lost factory binding,
wrong continuing/fresh budget, and routing a derived callback to ordinary work.
Run focused tests, lint, whole-tree types, doc/record gates and commit hooks.
