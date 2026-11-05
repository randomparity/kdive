# Stranded activation settlement design

Issue #3550, part of #3520. Decision record: [ADR-0816](../../adr/0816-stranded-activation-settlement.md).

## Problem

A fixed-worker System stranded by #3138 holds an activation in `preparing` that no path can end.
Later recovery (#3551) needs a proven starting point: the authority holds nothing for this System
and the worker holds its domain. The primitive provides that and nothing else.

## Components

**Migration `0194_stranded_activation_settlement.sql`**

- Table `external_boot_activation_settlements`: one row per activation with authority columns,
  worker columns and settlement columns. `kdive_server`, `kdive_worker` and `kdive_reconciler` may
  read it, as with the other activation tables; nobody may write it directly.
- `record_stranded_activation_authority_absence(activation, instance, domain_name,
  composite_state, objects)` returns `applied`, `superseded` or `not_found`. Role
  `kdive_provider_authority`. `domain_name` must equal `systems.domain_name`. `composite_state` is
  the authority's digest of what it observed as absent; it becomes the terminal evidence's
  `composite_state`. At most 32 objects, each in the ASCII subset of `OpaqueProviderRef`, else
  `22023`.
- `record_stranded_activation_worker_ownership(credential_hash, job, attempt, activation,
  domain_name)` returns the same set. Role `kdive_worker`. The job must be `running`, leased by the
  active fence-protocol-4 incarnation, carry the System's id and no authority marker.
- `settle_stranded_external_boot_activation(activation, principal, agent_session, actor, reason)`
  returns one closed status. Role `kdive_server`.

**Repository.** `ExternalBootActivationRepository` gains `settle_stranded` and `is_settled`, plus
`SettlementStatus`. The recorders get no Python wrapper until a producer exists.

**Host readiness.** `check_database_role` in `providers/external_boot_authority/host.py` adds the
authority recorder to `accepted_function`, or the grant fails authority readiness.

**Service.** `kdive.services.external_boot.settlement.settle_stranded_activation` enforces
`platform_admin` and a non-blank reason, and turns a refusal into a typed `CategorizedError`
carrying `details["reason"]`.

## Settlement order

Under the System advisory lock, with the activation, System and settlement rows locked:

0. No such activation: `not_found`.
1. Settled already: return `replayed`.
2. Ownership row exists: `authority_owned`.
3. Activation not `preparing`: `not_preparing`.
4. System `torn_down` or `tearing_down`: `system_ended`.
5. Queued or running job marked `external_boot_authority_v1` for the System: `job_live`.
6. No authority record: `authority_evidence_missing`.
7. No worker record: `worker_evidence_missing`.
8. Either record older than one hour, or naming a domain other than `systems.domain_name`:
   `evidence_stale`.
9. Journal head differs from the captured head: `authority_journal_moved`.
10. No reservation row: `reservation_missing`.
11. An object named by `materialization`, `recovery_point` or `pre_recovery_evidence` is not in the
    absent list: `objects_unproven`.
12. Apply: write the release row from the absent list and delete the reservation; set `abandoned`
    with terminal evidence and `ordinary` cleanup evidence; supersede open authority rows; discharge
    remote-module obligations; mark settled; insert `platform_audit_log`.

Open or retired authority rows do not refuse: failed jobs retire theirs, and an interrupted attempt
leaves one open. Step 5 and step 9 fence authority activity that moves the journal after the
observation, and superseding the open rows fences a later journal write. Neither stops a provider
call already running, so the authority must prove its lane quiescent before it records absence; a
non-terminal head does not refuse, because an interrupted attempt is the shape being recovered. Retired rows stay; the ADR states what that requires of #3551.

A blank or oversize reason or a blank principal raises `22023`; those are caller defects, not
refusals. Reason is 1 to 1024 bytes after trimming.

## Failure model

1. **Actors and deployments**
   - `platform_admin` operator through the #3551 tool, on any deployment with an authority route.
   - The fixed worker and the authority process, each under its own database role.
2. **Invariants and assets at stake**
   - A genuine authority-owned System never settles (ownership row, or a domain on the authority).
   - One owner's record is never accepted in place of the other's.
   - Settlement and its audit row commit together or not at all.
   - Capacity: the reservation ends exactly once, through a release row.
3. **Accepted failure classes**
   - An `OpaqueProviderRef` with non-ASCII characters or `"`: the recorder refuses it with `22023`,
     since SQL ordering must equal the Python canonical ordering, and such a System stays stranded.
   - An untruthful observer: the recorders trust their database role, the same trust the journal
     functions place in it (ADR-0584).
   - A domain created or relocated after an observation and within the hour with no authority
     journal movement: an operator act outside the recovery, the same trust ADR-0752 records.
4. **Covered elsewhere**
   - Teardown routing, the lease-expiry lane, fence changes, the audited tool and refusal auditing:
     #3551.
   - Producing the observations and removing objects the authority still holds: no owner yet;
     reported to the campaign as a follow-up.
   - Stranded-shape end-to-end test and live proof: #3552.
   - General `preparing` reconciliation: excluded by the operator.

## Threat model

1. **Boundary inventory.** Added: `platform_admin` to the service; server to the settlement
   function; worker and authority to their recorders. Widened: the authority role's accepted
   function list gains the recorder. Reason text enters from the
   operator and is stored trimmed. `actor` enters from the caller; the #3551 tool must pass
   `actor_for(ctx)`, the server-resolved value (ADR-0089).
2. **Actor model.** A project user or a platform role below admin is untrusted. A compromised worker
   or authority credential is untrusted for the other owner's evidence. The trust is in database role
   separation and in the operator holding `platform_admin`.
3. **Control per boundary.** Service: `require_platform_role(PLATFORM_ADMIN)` and non-blank reason.
   Functions: `REVOKE ALL` from every role, `GRANT EXECUTE` to the one role, a `session_user` role
   check, `SET search_path = ''`. Worker recorder: credential hash, job lease and System binding.
   Table: no direct write grant. Refusals return a closed reason and leak no evidence.
4. **Explicitly out of scope.** A compromised `kdive_server`, which could call the function with any
   principal; it can already write the audit table. Denial auditing, which the #3551 tool owns through
   `audit_platform_denial`.

## Success

- Each refusal in the settlement order changes no row (test per status, against real Postgres).
- `is_settled` is false before settlement and true after, for `kdive_server`, `kdive_worker` and
  `kdive_reconciler`.
- An object outside the `OpaqueProviderRef` subset is refused with `22023`, leaving no record.
- A System with an ownership row and both records returns `authority_owned`.
- Worker record alone returns `authority_evidence_missing`; authority record alone returns
  `worker_evidence_missing`.
- `kdive_server` cannot run either recorder; `kdive_worker` cannot run the authority recorder or the
  settlement; `kdive_provider_authority` cannot run the worker recorder or the settlement.
- The applied path leaves a row that validates as `ExternalBootActivation` and a release that
  validates as `ExternalBootReservationRelease`, `get_restricting_for_system` returns `None`, and one
  `platform_audit_log` row exists; a replay adds none.
- The service refuses a caller without `platform_admin` before touching the database.

## Validation

Real-Postgres tests in `tests/db/test_migration_0194_stranded_activation_settlement.py`, service
tests in `tests/services/external_boot/test_settlement.py`, the four migration window-list tests,
the table inventories in `tests/db/test_worker_fence_authority.py` and
`tests/db/test_external_boot_activation_migration.py`, the authority readiness tests,
then `just lint`, `just type`, `just records` and the full gate through the pre-push hook. No live
tier: nothing in production reaches the primitive until #3551, and #3552 owns the live proof.
