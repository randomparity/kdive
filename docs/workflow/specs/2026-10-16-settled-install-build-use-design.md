# Settled canceled-install build-use release (#3241)

## Problem and authority

Actual `jobs.cancel` moves a running install job to canceled while preserving its
worker/attempt. Migration0105 permits release only for running jobs, stranding the
pin after the provider settles. Issue #3241 requires cooperative release and a
cancel-while-running test. Operator-approved scope/exclusions are recorded in
WORK:SCOPE6223128552, campaign 2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18,
SHA2565ce7042937eaa47a8e1cfb10ba07265e720dac9be3d037e655b927ac17b29b50.
The exact owner-release design was approved under
SHA256d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418.
ADR0784 records the decision. Assessment M250/full-spec/iterating is fixed.

## Design

Add migration0172 replacing `release_investigation_build_use(uuid,bytea) -> boolean`.
Permit the job state to be running or canceled, preserving credential validation,
active incarnation, advisory/row locks, exact holder/current attempt and exact
pin deletion. Preserve SECURITY DEFINER and empty search_path; CREATE OR REPLACE
retains existing worker-only EXECUTE. Add no grants or dependency changes.

Keep `release_build_use(...) -> bool` and install cleanup ownership unchanged.
The install step drains its provider thread through cancellation before calling
release. No early release follows from terminal job state. A denied release returns
false and keeps the pin; stale/reclaimed/inactive-worker pins require existing
operator-confirmed worker-death recovery. Existing callers and owners remain in
place; no migration of callers or obsolete path is needed.

## Failure model

- **Actors and deployments:** existing Linux worker/server/Postgres roles. A disposable
  real-LOGIN-role integration test proves this seam without a live guest; x86_64 host
  proof does not qualify ppc64le deployment.
- **Invariants and assets:** retain artifact bytes until the provider thread settles,
  including repeated cancellation and provider fault. Preserve credential, active
  incarnation, exact holder/current attempt, locks and deletion fences. Denial retains
  the pin; failed/succeeded states remain denied.
- **Accepted failure classes:** trust process-local provider-thread settlement for
  cooperative release. External-descendant termination is an unprovided guarantee;
  no deferred descendant defect is claimed. Release database failures retain the pin,
  and existing cancellation/error precedence can mask the release error.
- **Covered elsewhere:** existing operator-confirmed worker-death recovery handles
  stranded pins. #2681 owns external-build finalization across replicas/restarts;
  unrelated job-kind cancellation redesign belongs to existing lifecycle owners.
  General durable-finalization/cross-replica guarantees remain excluded here.

## Threat model

- **Boundary inventory:** no new entry point. The existing worker-to-SECURITY-DEFINER
  release boundary admits canceled jobs as well as running jobs; the server cancel
  boundary and provider-thread-to-install-cleanup settlement boundary remain unchanged.
- **Actors:** authenticated tenants request cancellation through the server; server,
  worker and lifecycle-witness LOGIN roles remain distinct. Trust the credential-owning
  worker to invoke release only after its provider thread settles.
- **Controls:** the existing server authorizes project/role before cancellation. The
  release function requires worker membership, non-null UUID and 32-byte credential
  hash, active incarnation, exact holder/current attempt, advisory/row locks and exact
  deletion. Inputs remain SQL parameters; empty search_path and existing worker-only
  EXECUTE remain. False denial retains the pin; no credential is added to responses.
- **Out of scope:** external-descendant termination and general durable/cross-replica
  finalization are unprovided guarantees under the existing process-local boundary.
  No new role grant, bulk pin release or unrelated cancellation redesign is proposed.

## Success and validation

Exercise the real install step, real acquire/release service and SQL through a worker
LOGIN; invoke actual cancel_job through a server LOGIN while a provider thread blocks.
Observe exact canceled holder/attempt and a live pin until the provider returns or raises;
repeated cancel returns the existing terminal error while repeated task cancellation
still drains. Then cancellation propagates and the pin is absent. Test changed
attempt/holder/credential/inactive incarnation and failed/succeeded-state refusal with
pins retained. Test worker-only EXECUTE and SECURITY DEFINER/search_path preservation.
New positive regression must fail on the old state predicate; controlled fence removal
must fail a refusal case. Run focused authority/install tests, lint, whole-tree type,
staged hooks, bounded reviews and one managed prepush full gate, then CI.

## Operations and rollback

Existing canceled pins are not bulk-released. An owner still executing cleanup may
release only after its provider settles; stranded owners retain operator death recovery.
No deployed host mutation or live resource allocation is authorized. Reverting an applied
function requires a new forward migration and would restore the cancellation leak;
never edit historical migration0105. No data rewrite or backfill is needed.
