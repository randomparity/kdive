# 0816 — Settle a stranded fixed-worker activation on evidence from both owners

## Status

Accepted (2026-11-04)

## Context

A fixed-worker System routed to the external-boot authority (#3138, ADR-0752) leaves an
activation `preparing`. Nothing ends it. The reconciler lanes start at `prepared`, the admission
matrix denies `allocation_release`, and `runs.release_external_boot` needs an `active` activation.
The ADR-0620 note that "lease expiry still ends such an allocation" does not hold for this shape:
`src/kdive/reconciler/repairs/allocations.py` retries behind the same admission guard.

The operator confirmed the recovery contract on 2026-11-04: once the erroneous authority
activation is proven settled, teardown routes to the worker that owns the domain, under
`platform_admin`, with a required reason, audited. This record covers the first step only:
proving the activation settled and ending it.

The database alone cannot prove the shape. The server cannot see which daemon holds a domain, and
the fixture lane's relocation writes no ownership row (ADR-0752), so a worker-provisioned System
with no ownership row may still run on the authority's daemon.

## Decision

The **stranded, worker-owned shape** is an activation in `preparing` whose System has no
`authority_system_ownership` row, is neither `torn_down` nor `tearing_down`, and has no queued or
running authority-marked job.

Migration 0194 adds `external_boot_activation_settlements`, keyed by activation, and three
`SECURITY DEFINER` functions. No role has direct write access to the table.

- `record_stranded_activation_authority_absence`, for `kdive_provider_authority` only: the
  authority states that its own daemon holds no domain named `systems.domain_name` and lists its
  enumerated absent store objects. It passes the journal head it read before observing, which
  must still be current.
- `record_stranded_activation_worker_ownership`, for `kdive_worker` only: a running, unmarked job
  of an active incarnation states that its own provider session found `systems.domain_name`.
- `settle_stranded_external_boot_activation`, for `kdive_server` only: re-checks the shape, then
  requires both records, each under one hour old, one authority instance, an unmoved journal head, and absent-object
  coverage of every object the activation names.

Each owner testifies only about its own daemon, through its own database role, in positive form.
The worker record has no way to state absence, the authority record no way to state ownership, and
neither function reads the other's columns, so one daemon's absence is never evidence for the
other. A missing record refuses settlement.

An `authority_system_ownership` row refuses before any evidence is read. A relocated fixture
fails on evidence: the authority finds its domain and the worker does not.

Settlement ends the activation as `abandoned` with `cleanup_complete`, moves the reservation to a
release row, supersedes open authority rows for the activation, discharges remote-module
obligations, marks the settlement row settled and writes `platform_audit_log`, all in one
transaction. A settled replay is idempotent and writes no second audit row. A refusal returns a
closed reason and changes nothing.

`settle_stranded_activation` in `kdive.services.external_boot.settlement` is the only Python
entry to the function. It requires `platform_admin` and a non-blank reason. #3551 keys routing and
fence changes on a settled row, never on activation state alone.

## Consequences

- Nothing calls the recorders yet, so the primitive is unreachable in production. The recovery
  still needs a worker observation job, which `preparing` admission does not admit today, and an
  authority observation operation. Neither is in this change or in #3551's body.
- This change writes only the success audit row. The #3551 tool must call `audit_platform_denial`
  and return `ToolResponse.denied` for a role denial, and must audit each closed refusal the
  primitive returns.
- A settled activation stops restricting the System. The worker teardown refusal and the
  `allocations.release` fence still hold, but reconciler lease expiry does not: `_expire_one` calls
  only the admission guard, so a settled allocation can expire while the worker domain runs. #3551
  must close that lane before its first call to the primitive.
- `resolve_external_boot_system_teardown_dispatch_binding` still returns a route when a `retired`
  authority row exists, and returns none otherwise. #3551 must check the settlement record before
  that binding; an authority teardown that finalized here would mark the System `torn_down` with the
  worker domain running.
- Migration 0194 grants the authority role a new function, so `check_database_role` accepts it in
  the same change. As with every such grant, the authority host deploys with the migration.
- With the restriction gone, `runs.boot` is admitted again on a Resource that routes to the
  authority. A new activation is not settled, so the System is back in the fenced shape and the
  same recovery applies.
- Objects the authority still holds refuse settlement. The authority must remove them first, or
  the observation must wait for it.
- Settlement accepts a non-terminal journal head, since an interrupted attempt is part of the
  shape. Calling the authority recorder therefore also asserts that the authority's lane for the
  System is quiescent; its producer must prove that first.
- The authority role is not per-instance, so `kdive_provider_authority` is trusted to name its
  instance truthfully; the function checks only that the instance matches the activation's route.

## Considered & rejected

- **Do nothing and leave recovery to lease expiry.** verified: `reconcile` skips the allocation
  with "the external-boot admission guard refused the release; retried next pass"
  (`src/kdive/reconciler/repairs/allocations.py`, main `e7aa79a7d`).
- **Infer the shape from the database, using no ownership row plus an unmarked provision job.**
  verified: ADR-0752 records that the fixture relocation writes no ownership row, so a relocated
  System on the authority's daemon has the same rows.
- **One function taking both evidence documents from the server.** verified: only `kdive_server`
  would write both, and the server reaches neither daemon (ADR-0752), so the evidence would be one
  party's assertion, not independent proof.
- **Reuse the authority teardown as the absence proof.**
  verified: `finalize_external_boot_authority_teardown` sets `systems.state = 'torn_down'`
  (`src/kdive/db/schema/0147_external_boot_system_teardown.sql`), which would hide a running worker
  domain, the #2865 defect.
- **End the activation as `torn_down`.** verified: `ExternalBootTeardownEvidenceV1` carries
  `system_state: Literal["torn_down"]`, and the System is not torn down at settlement.
- **Keep evidence in separate tables.** judgment: complexity. One row per activation holds both
  proofs and the settlement key for #3551.

### Amendment (2026-11-05): the evidence producers (#3574)

This amendment corrects the first Consequence: the producers now exist, and both 0194 recorders
are unchanged. Spec: [stranded activation observation](../workflow/specs/2026-11-05-stranded-activation-observation-design.md).

**Decision.** One platform job, `observe_stranded_activation`, produces both records. It carries
no authority marker, so it never trips settlement's `job_live` refusal, and it runs on the fixed
worker. The worker asks its own daemon for `systems.domain_name` and records ownership. It then
sends the authority a request naming only the activation. The authority reads every other fact
through `read_stranded_activation_target`, added by migration 0195 for its role alone.

The authority proves quiescence under the System lane lock: no failed lane, no active operation,
and a local journal tail equal to the database head, or no journal at all. A non-terminal head
does not refuse. A provider call dies with the single authority process, so an interrupted head is
the shape being recovered. It then observes its own daemon and store, and records absence only when
the domain is absent and `exact_recovery_absence` holds for the activation.

The observation writes no journal record, adds no `AuthorityOperation`, and is a new transport
operation beside `sweep-module-volumes`.

Objects the authority holds refuse the job, naming them. Nothing removes them.
`objects_unproven` needs a domain the authority once held, because
`LocalExternalBootSessionFactory.open` looks the domain up before anything materializes.

Only local-libvirt carries the producers. The runtime port and the authority collaborator are
optional, so any other provider refuses by name.

**Consequences.**

- Upgrade order: migrate 0195 and deploy the authority host together, because the new grant fails
  readiness on an older host; then deploy workers and servers. A newer worker against an older
  authority gets `invalid-request` and the job fails closed. The lifecycle identity does not move:
  `systemd_worker_contract.py` references neither `AuthorityOperation` nor job kinds.
- `request_stranded_observation` in `kdive.services.external_boot.settlement` is the only Python
  entry to the job. It requires `platform_admin`, writes no audit row, and has no MCP tool until
  #3551 calls it. A stranded remote-libvirt System stays stranded.
- `settle_stranded_external_boot_activation` and this read list the same six object paths;
  the end-to-end test covers a materialized activation, so a drift fails it.

**Considered & rejected.**

- **Reuse `observe-authority`.** verified: `_observe_current` raises `superseded` without a current
  bound authority, and the local `observe` opens a session whose `lookupByName` raises on a missing
  domain (`service.py`, `session.py`, main `0ca61d7a3`).
- **Journal the observation under a new `AuthorityOperation`.** judgment: complexity. It needs a
  purpose and allocation path for an activation with no current authority, and the head moves for no
  reader, since the recorder already pins the head it read.
- **Let the worker send the domain and object list.** judgment: fit. A worker credential would then
  choose what the authority testifies about, against this record's rule that each owner states only
  its own facts.
- **Two jobs, one per owner.** judgment: complexity. An authority-marked job trips `job_live`, and
  the worker is the only route to the authority.
- **Remove held objects here.** judgment: cost. A mutation must be journaled, which needs everything
  the journaled observation does plus domain-free store cleanup, for a case the shape does not
  reach. The issue accepts an actionable refusal.
