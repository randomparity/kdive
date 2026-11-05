# Stranded activation observation design

Issue #3574, part of #3520. Decision record: the 2026-11-05 amendment to
[ADR-0816](../../adr/0816-stranded-activation-settlement.md).

## Problem

`settle_stranded_external_boot_activation` needs one record from each owner, and nothing calls
either recorder. Every settlement returns `authority_evidence_missing`, so #3551 has nothing to
call. The recorder signatures are adequate: neither changes.

## Components

**Migration `0195_stranded_activation_observation.sql`**

- Adds `observe_stranded_activation` to `jobs_kind_check`.
- Adds `read_stranded_activation_target(activation, instance)`, `SECURITY DEFINER`, role
  `kdive_provider_authority` only. It returns one row of `system_id`, `run_id`, `domain_name`,
  `object_refs`, `journal_sequence`, `journal_digest`, or no row when the activation is missing or
  not `preparing`, the System has an ownership row or a NULL `domain_name`, or the instance does not
  match the route (the predicate `record_stranded_activation_authority_absence` uses). `object_refs`
  is the settlement function's six paths, non-NULL only. The journal columns are NULL with no head.

**Job.** `JobKind.OBSERVE_STRANDED_ACTIVATION` joins `PLATFORM_INTERNAL_JOB_KINDS`. Its payload
is `system_id` and `activation_id`, derived from `_PayloadBase` so no marker key exists in the
stored row.

**Admission.** `ExternalBootOperation.OBSERVE_STRANDED_ACTIVATION` is admitted in `preparing` only.

**Service.** `request_stranded_observation(conn, ctx, *, activation_id) -> Job` in
`kdive.services.external_boot.settlement`, in one transaction under the System lock. Refusals, each
terminal with `details["reason"]`, checked in this order:

1. `not_found`: no such activation or System.
2. `authority_owned`: `authority_system_binding` returns a binding.
3. `not_preparing`: the activation is not `preparing`.
4. `external_boot_restricted` from the admission guard, kept so the matrix stays the one policy.

The dedup key is `stranded-observation:<activation>`, recycling terminal rows, so a failed or aged
observation can run again.

**Runtime port.** `ProviderRuntime.domain_presence: DomainPresence | None`, with one method
`is_defined(domain_name) -> bool`. Local-libvirt sets it over the worker's `libvirt.open` connection;
`VIR_ERR_NO_DOMAIN` is `False` and any other libvirt fault raises.

**Handler** `observe_stranded_activation_handler`:

1. Refuse terminally with `stranded_observation_unsupported` when the runtime has no
   `domain_presence`, or with the existing route reasons when it has no sender.
2. Read the System. A missing domain name, or `is_defined` false, refuses terminally with
   `worker_domain_absent` and records nothing.
3. Call the worker recorder. Anything but `applied` refuses terminally with
   `worker_evidence_superseded`.
4. Send `observe-stranded-activation` with a 60 s deadline.
5. Map the response: `recorded` succeeds; `domain_present` refuses terminally with
   `authority_domain_present`; `objects_present` refuses terminally with
   `authority_objects_present`, naming each object in the message.

**Transport.** Closed `AuthorityStrandedObservationRequestV1` carrying only `activation_id`, and
`AuthorityStrandedObservationResponseV1` carrying `disposition` and `objects`, which is non-empty
exactly for `objects_present`.

**Authority.** `providers/external_boot_authority/stranded.py` holds `StrandedObservationRepository`
(built with the connection factory and instance) and the composite digest.
`ExternalBootAuthorityService.observe_stranded_activation`:

1. Refuse `superseded` when closing or when the target read returns no row; refuse
   `provider_conflict` with no peer, no repository, or an adapter without
   `observe_stranded_absence`.
2. Under the System lane lock: refuse `journal_conflict` on a failed lane and `superseded` on an
   active operation.
3. Re-read the target, then refuse `journal_conflict` unless the lane's last record equals the
   database head, or both are absent.
4. Ask the adapter for `StrandedAuthorityFacts(domain_absent, store_absent, recovery_ref)`.
5. Domain present returns `domain_present`. Store present returns `objects_present` naming
   `object_refs`, or `recovery_ref` when that list is empty.
6. Record absence with the head from step 3. `applied` returns `recorded`; anything else refuses
   `superseded`.

The local adapter reads the daemon without a session, through a new
`LocalExternalBootSessionFactory.domain_is_defined`, and the store through
`RecoveryMetadataStore.exact_recovery_absence`. The remote branch of `_build_mutation_service`
passes no repository.

`check_database_role` adds the read function to `accepted_function`.

## Upgrade order

Migrate 0195 and deploy the authority host together, then workers and servers. The lifecycle
identity does not move.

## Failure model

1. **Actors and deployments**
   - `platform_admin` through #3551's tool, on a deployment with a local-libvirt authority route.
   - The fixed worker and the single authority process, each under its own database role.
2. **Invariants and assets at stake**
   - A genuine authority-owned System never gets a job or a record: the service and the read refuse it.
   - The authority records absence only for facts it read itself, with the lane quiescent.
   - One daemon's absence never produces the other's record.
   - Settlement's refusals in ADR-0816 stay unchanged; 0194 is untouched.
3. **Accepted failure classes**
   - An authority with no mutation service answers `provider-not-configured`; the job fails naming
     it, because that host has no daemon session to observe.
   - A failed lane stays refused until the authority restarts, because a failed lane has an unknown
     head.
   - A worker record with no authority record: settlement refuses until it ages out, by design.
   - A domain created on the authority after the read and within the hour: ADR-0816's accepted class.
   - The job fails after its worker record; a rerun replaces the record.
4. **Covered elsewhere**
   - Teardown routing, the release fence, lease expiry, the tool, audit: #3551.
   - Runbook and live proof: #3552. No live tier runs here.
   - Removing held objects, and producers for remote-libvirt: reported to the campaign as
     follow-ups.

## Threat model

1. **Boundary inventory.** Added: authenticated peer to `observe-stranded-activation`;
   `kdive_provider_authority` to the read function; `platform_admin` to the enqueue. Widened: the
   authority role's accepted function list, and a platform job reaching the worker.
2. **Actor model.** A project user is untrusted and cannot reach the service. A compromised worker
   credential is untrusted for the authority's evidence. Trust sits in the database roles and in
   the peer credential, as ADR-0584 sets it.
3. **Control per boundary.** Service: `require_platform_role(PLATFORM_ADMIN)`. Transport: peer
   authentication, closed request model, canonical decode. Read function: `session_user` role check,
   `REVOKE ALL`, one `GRANT`, `SET search_path = ''`. Authority: the request names only an
   activation, so a peer cannot choose the domain, run, or object list. Refusals name object
   references the operator already owns and no provider detail.
4. **Explicitly out of scope.** A compromised authority credential, trusted by ADR-0816 to name its
   instance. Denial auditing, which #3551's tool owns.

## Success

- A request on the stranded shape enqueues one job; its handler, a real authority service, the real
  recorders and `settle_stranded_activation` end with `applied` (real Postgres).
- The enumerated refusals each hold: `not_found`, `authority_owned`, `not_preparing`, each worker
  refusal, each authority disposition, a failed lane, an active lane, a head mismatch.
- After each refusal the settlement row holds no record for the refusing owner.
- A System with an ownership row is refused by the service, returns no row from the read, and
  returns `authority_owned` from settlement.
- `read_stranded_activation_target` is denied to `kdive_server`, `kdive_worker` and
  `kdive_reconciler`.
- A materialized activation refuses `authority_objects_present` while held; once the adapter reports
  the store absent, it settles, so the read's list covers settlement's.
- `preparing` admits the new operation; every other state in `_ADMITTED` denies it.

## Validation

Real-Postgres tests in `tests/services/external_boot/test_stranded_observation.py` and
`tests/db/test_migration_0195_stranded_activation_observation.py`; unit tests for the transport,
service, handler and local adapter; the four migration window lists; `tests/domain/test_models.py`,
`tests/jobs/worker_write_baseline.json`, the host readiness inventory; regenerated job-kind docs and
CLI verbs; then `just lint`, `just type`, `just records` and the pre-push gate.
