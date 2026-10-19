# Prepare the Compose persistence image (#3263)

## Authority and status

Operator explicitly approved implementation and the exact aggregate-budget amendment after review.
Amended WORK:SCOPE is issue #3263 comment 6234379164; the original scope/history remains.
The frozen charter is issue #3263, WORK:SCOPE q3263-a2732980, comment 6234142177.
Campaign 2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18 approved the outcome,
criteria and exclusion owners. M250 is fixed; expected implementation is 280–320 changed lines
including focused tests. The original 140–220 forecast undercounted the meaningful failure,
timer-ownership and subprocess-boundary tests. This estimate does not resize the denominator.
ADR-0647 remains accepted and unchanged. No new decision record or dependency is warranted.

## Problem and evidence

At base 0b177a335435d404191ecab13b3374f6c01ca136, SeaweedFS has both image and build
in production Compose. The isolated proof has no separate preparation and allows implicit
builds during its first 360-second startup. Its no-build comments are false.
PR #3261's first startup timed out; the slow suboperation remains unknown. This design fixes
the source-established preparation gap and does not claim that missing images caused that run.
The existing context manager owns project cleanup; extending that owner avoids a second path.

## Scope and selected approach

Modify tests/compose/test_compose_volume_persistence_live.py, focused tests in that module,
and .github/workflows/ci.yml comments plus only the budget amendment below.
No justfile or lifecycle recipe change is needed: the entry point and environment stay intact.
Retain the production Dockerfile/build context and use KDIVE_SEAWEEDFS_IMAGE with a unique
kdive-volume-proof:<full UUID> tag. Never inherit the caller's image override.
Before mutating Docker, inspect the exact tag with image ls --quiet --no-trunc; nonempty
means collision and abort without removal. A successful empty probe establishes ownership;
transport failures never count as absence. Protect build and proof with the existing context.
Compose build --progress plain seaweedfs runs once with timeout600 before yielding.
Inspect its exact tag to capture an immutable image ID; each _up uses --no-build and checks
SeaweedFS container .Image against that ID. Other services retain normal pull behavior.
Only this tag is removed, without force, image-ID removal or prune. Sibling tags/caches stay.

## Approved aggregate budget amendment

Keep per-call preparation600, startup360, readiness180 and down180 seconds.
Add a test-local 2400-second work stop covering absence probe, build, identity checks and
three proof arms; reserve a separate600-second cleanup attempt. These are deliberate policy
caps, not measured runtimes or a promise every individual maximum can be consumed together.
Existing sums already reach3930 seconds before new image operations/setup; readiness's
seven60-second loops are soft, not seven hard call bounds. Installed botocore defaults are
connect_timeout60/read_timeout60; retries and operation count prevent treating this as a
60-second whole-loop bound. psycopg conninfo supplies a default connection timeout, but SQL
operations have no explicit statement deadline in this proof. Do not change those contracts.
Use SIGALRM only inside the existing Linux/main-thread context. Reject non-main threads,
unsupported signals or an already-running real timer before Docker mutation; preserve and
restore the prior handler and idle timer configuration. The handler raises a dedicated local
BaseException subclass so existing broad readiness Exception catches cannot swallow expiry.
Clip _run's subprocess timeout to remaining phase time using a private dictionary deadline
entry removed from child environment. Expiry prevents additional proof work; cleanup gets a
fresh deadline. Catch BaseException solely to retain the primary failure through cleanup.
Cleanup attempts down, each three absence probes and exact-tag inspect/remove/absence checks,
collecting ordinary failures independently. Deadline expiry stops remaining cleanup attempts.
A cleanup-only failure fails the proof; alongside a primary failure it becomes an exception
note, preserving the primary traceback. Restore timer/handler even on cleanup expiry.
Build timeout kills/waits for the CLI through subprocess.run; it does not prove daemon work
ended. On build timeout explicitly report daemon completion unknown, cleanup observations and
exact retained tag if any. No success is claimed from an absence snapshot while build work may
still complete. Do not retry preparation in this invocation.
Approved job timeout-minutes20→70 only for compose-volumes: setup allowance15 + work40 +
cleanup10 + runner/reporting5. Setup15 is a policy allowance, not an observed or enforced
setup bound; a setup overrun reduces later reserve. This amendment does not guarantee cleanup
on hosted hard cancellation. The first actual run must report setup and phase elapsed times.

## Alternatives

- Selected: test-local phase stop and subprocess clipping in existing boundaries. It can
  interrupt readiness without changing SDK/readiness policy; ownership remains in one context.
- Larger job cap alone: cannot stop proof work early enough to reserve cleanup; rejected.
- Propagate deadlines into every SDK/database operation: broadens readiness contracts and
  introduces more policy than this preparation defect requires; rejected by scope judgment.
- New helper, wrapper process or dependency: adds another lifecycle owner; rejected by judgment.

## Success

Preparation precedes first startup, executes once, and records source/tag/ID plus phase time.
The three starts use --no-build and run that prepared image, retaining360/180 bounds.
Build, proof and teardown failures attempt owned cleanup with primary-failure preservation.
No default tag, sibling tag, unrelated project, image-ID deletion, force or prune is touched.
Plain down preserves markers; down --volumes resets; container mounts and Prometheus retain
all existing assertions. Required Docker failures, UUID project isolation and free ports remain.
An actual Docker proof requires a separate operator-approved plan; mocks do not establish it.

## Failure model

1. Actors and deployments: Linux local operator or Ubuntu hosted Compose job, normal pytest
   main thread; Docker daemon shared with concurrent checkouts. Foreign native targets keep
   their declared support; this test is not POWER runtime or multi-architecture qualification.
2. Invariants and assets: operator stack/volumes and sibling image tags survive; only owned
   UUID project/tag may be removed. Proof success requires identity and persistence assertions.
3. Accepted failures: Docker/SDK errors or deliberate aggregate expiry fail with diagnostics.
   Kernel-uninterruptible calls, delayed Python signal delivery, daemon-side build continuation,
   process kill and hosted hard cancellation may prevent complete cleanup; they produce no
   cleanup-success claim. A preexisting alarm or unsupported execution context fails before
   mutation rather than stealing timer ownership. UUID collision fails without tag deletion.
4. Covered elsewhere: reconciliation #3239; capture #3225; publication/history #2446/#2445;
   native POWER and release qualification #2818/#2819. Existing lint/type/CI enforce their gates.

## Validation

Focused mock-boundary tests cover ordering, one build, image override isolation, collision,
prepared/container ID mismatch, no-build startup, exact-tag removal, build/proof/cleanup errors,
primary exception preservation, timer restoration and both aggregate expiries. A sibling tag
sharing the prepared ID must remain, proving cleanup selects a name rather than an image ID.
Controlled faults must turn each new meaningful contract red before its fix is accepted.
Run focused pure tests, lint, whole-tree types, docs and records; mandatory pre-push just ci
later owns full local parity. Actual Docker proof and its findings remain pending separate grant.
