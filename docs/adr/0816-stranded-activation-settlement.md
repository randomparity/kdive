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
  authority states that its own daemon holds no domain for the System and lists its enumerated
  absent store objects. The row captures the System's journal head.
- `record_stranded_activation_worker_ownership`, for `kdive_worker` only: a running, unmarked job
  of an active incarnation states that its own provider session found `systems.domain_name`.
- `settle_stranded_external_boot_activation`, for `kdive_server` only: re-checks the shape, then
  requires both records, each under one hour old, an unmoved journal head, and absent-object
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

- Nothing calls the recorders yet. The operator path in #3551 must obtain one observation from the
  worker and one from the authority; a worker job and an authority operation for this are not in
  this change, so the primitive is unreachable in production until then.
- A settled activation stops restricting the System, but its allocation stays held: the worker
  teardown refusal and the release fence remain until #3551 narrows them.
- With the restriction gone, `runs.boot` is admitted again on a Resource that routes to the
  authority. A new activation is not settled, so the System is back in the fenced shape and the
  same recovery applies.
- Objects the authority still holds refuse settlement. The authority must remove them first, or
  the observation must wait for it.
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
