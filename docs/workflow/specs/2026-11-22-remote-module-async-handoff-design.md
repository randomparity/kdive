# Remote-module asynchronous handoff

## Problem

Production preparation uses the asynchronous authority-host consumer, but its verifier
also accepts an unused synchronous mode and constructs a device identity collaborator
that the active consumer discards. The worker forwards these through an Any wrapper.

## Scope and design

Require one operation callback accepting the verified ModuleAttempt and deadline
checker, returning Awaitable[ResultT]. Remove the two consumer protocols, optional
mode selection, executor/authority arguments, identity construction and sync branch from
prepare_verified_remote_module_attempt. Schedule the callback with asyncio.ensure_future
to accept the declared Awaitable contract; a production coroutine still runs in its
own task. Preserve the completion event loop, repeated cancellation draining, count
restoration, scheduling-edge cancellation check, clock and verifier call unchanged.

The authority-host preparation service accepts existing ModuleAuthority, drops its
executor argument and unused callback identity argument, and invokes the narrowed helper.
Remove the unused combined sender protocol and PreparationExecutor protocol only.
Keep concrete provider executors and device-identity ports and their direct callers.

Add an optional module_authority field to ExternalBootHandlerPorts. Bind it to the same
factory-created ExternalBootAuthorityClient as the existing operation executors.
Require it and pool at the existing module-preparation check. Use it as sender, with a
direct typed local service import; remove the Any forwarding wrapper and sender cast.
Keep separately injectable lifecycle/preparation executors narrow. The provider runtime
capability still selects whether modules are needed, but does not bypass the validated
authority client. Preserve that client's request validation and fixed deadline.

## Success

1. The active chain has one typed async consumer and no discarded identity/executor.
2. The helper waits for underlying completion before releasing verification, preserves
   repeated cancellation messages/counts, and restores cancellation only after settlement.
3. Receipt/plan validation, opening budget min(900, floor(remaining monotonic seconds)),
   terminal replay, evidence rollback and System-lock contention retain existing behavior.
4. Factory wiring supplies the exact client through module_authority; explicit
   lifecycle-only injected executors do not acquire new required methods.

## Failure model

- Actors: worker jobs invoke an authenticated authority client and durable verifier;
  cancellation or transport ambiguity may arrive while the provider still runs.
- Invariants: verification and System lock outlive completion, evidence commits remain
  fenced, request binding and client deadline remain validated, provider is not bypassed.
- Accepted: obsolete internal synchronous helper signatures and identity test fixture
  disappear; concrete provider thread-executor contracts remain unchanged.
- Covered elsewhere: the unchanged verifier and client own transaction, receipt and
  authentication policy; existing provider executor tests retain their own concurrency.

## Validation

Adapt the existing helper tests to async operations before implementation and establish
the narrowed-call failure. Preserve cancellation/count/deadline assertions and add
scheduling-edge and Awaitable coverage if absent. Update service call sites and runner
injection tests to assert exact sender identity. Retain concrete executor tests.
Run the four queue-named provider/runner test files, service verifier tests and authority
client tests with Docker required. Confirm real lock contention, terminal replay and
rollback arms run. Temporary completion-drain and wiring faults must be caught.
Compare verifier/client and cancellation-loop ASTs; run lint, whole-tree type, docs and
hooks. Keep the implementation plan transient; no new live qualification is claimed.
