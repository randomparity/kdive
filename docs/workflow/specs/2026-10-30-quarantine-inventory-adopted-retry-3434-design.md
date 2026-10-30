# Quarantine inventory for a release cleanup retried after an abort (#3434)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended
2026-10-30 (second amendment that day) by this change.

## Problem

A remote release CLEANUP leaves a private cleanup quarantine receipt. When the operator aborts
`finalize` at the `before-finalize` proof checkpoint, the authority publishes that receipt as an
`external_boot_recovery_quarantine` row (ADR-0787), so `ops.resolve_recovery_orphan` can dispose
of it.

A retried release generation adopts the earlier generation's terminal cleanup (#3416). It runs no
commit, so the receipt keeps the first cleanup's operation identity `A`, attempt and journal
position. The retry's own CLEANUP identity is `B`, because the release-phase identity hashes the
authority row and generation (migration 0143). `finalize` already proves against `A` (#3417).
Publication still uses `B` at three layers:

1. Remote `cleanup_quarantine_inventory` requires `receipt.operation_identity` and
   `receipt.attempt_id` to equal the request's. On the retry they differ, so it raises
   `provider_conflict`.
2. `DatabaseAuthorityRepository.publish_cleanup_quarantine` raises `ValueError` unless each
   object's operation identity equals the binding's (`B`).
3. SQL `publish_external_boot_recovery_quarantine_authority` (0144) stores
   `p_operation_identity` (`B`) as the row's identity. It also refuses an existing row whose
   identity differs from `B`.

The resolver rebuilds the provider binding from the row's `operation_identity`
(`orphan.py` `binding()`). The remote object port requires that binding to equal the receipt's.
So even a row stored under `B` could never be resolved.

Two cases reach this:

- **Abort, then abort.** Generation 1 aborts and publishes row `A`. Generation 2 adopts it, aborts
  again, and its publication conflicts.
- **Shutdown, then abort.** Generation 1's `before-finalize` sees a shutdown. ADR-0787 publishes
  nothing in that case. Generation 2 adopts and aborts. No row exists, and generation 2 cannot
  publish one.

## Scope

- **Service** (`external_boot_authority/service.py`). Extract the proof-context derivation from
  `_finalize_adapter` into `_proven_cleanup_context(request, records) ->
  AuthorityCommitContextV1`. That is the `mutation-started` record of the oldest adopted earlier
  release terminal, or of the request itself. `_finalize_adapter` and `_publish_cleanup_quarantine`
  both call it. The inventory protocol becomes
  `cleanup_quarantine_inventory(request, context)`, and publication passes the derived context.
  `_publish_cleanup_quarantine` gains a `records` argument. Its three call sites already hold
  `records`.
- **Remote adapter.** The inventory compares the standing unmanaged receipt's operation identity,
  attempt, journal sequence and journal digest with `context`. These are the same four fields
  `finalize` compares, through one shared predicate. A mismatch is still `provider_conflict`. The
  returned observation is unchanged: it names the receipt's own identity `A`.
- **Local adapter.** It gets the new signature only. Its comparison with the request stays
  unchanged (operator exclusion; follow-up candidate below).
- **Repository.** Each published object carries its own `operation_identity` in the JSON
  payload. The Python check accepts an object identity different from the binding's only when the
  binding's purpose is `release`. The database decides whether that identity is admissible.
- **Migration 0186** (`0186_quarantine_publication_adopted_identity.sql`). `CREATE OR REPLACE`
  the publish function with the same signature, so grants and the host function allow-list
  are unchanged. The function is still `SECURITY DEFINER` with `search_path = ''`, and its
  authority, worker, job and head fences are unchanged. Changes:
  - Each object's `operation_identity` must be one of two values. It may be `p_operation_identity`.
    Or the current authority is a `release`/`release` row and the identity equals the 0143
    `cleanup` derivation of an **earlier acknowledged** `release`/`release` authority row. That row
    must share `system_id`, `activation_id`, `run_id`, `plan_identity`, `provider_kind` and
    `authority_instance`, and have a lower `generation`. Anything else raises
    `cleanup quarantine object operation is not admissible` (SQLSTATE `P0001`).
  - The insert stores the object's own identity, and the existing-row conflict check compares
    against it.
- **ADR-0787 amendment** records the rule.
- **Window tests.** Add the 0186 row and `K+1` to the four `migrations[-K:]` tests.

No ownership transition: each layer keeps its owner and only its identity source changes.

Deferrals: none. Follow-up candidate: the local-libvirt inventory has the same gap. The local
adapter records its quarantine only inside `commit` (`local_libvirt/external_boot_authority.py`
`_commit`). An adopted retry runs no commit, and the local inventory compares against
`request.operation_identity`. Operator exclusion; owner is the campaign orchestrator.

## Failure model

1. **Actors and deployments**
   - Provider authority service on a remote-libvirt host, running a release job's CLEANUP across
     authority generations.
   - Provider authority database role (`kdive_provider_authority`) calling the publish function.
   - Platform administrator resolving the row through `ops.resolve_recovery_orphan`.
2. **Invariants and assets at stake**
   - The published row's identity, attempt and journal fields equal the provider receipt's, so
     the resolver's binding equals the object port's.
   - The publish function admits only the current authority's identity, or a cleanup identity
     that an earlier acknowledged release authority of the same activation derives. An authority
     cannot name another activation's or another operation's identity.
   - Non-release publication keeps its exact identity equality.
   - The inventory still refuses a receipt whose proof differs from the derived context.
3. **Accepted failure classes**
   - An earlier release authority's derived identity is admissible even if that generation never
     ran a CLEANUP. Accepted: the caller already passed every current-authority fence, and the
     identity names only this activation's own release lineage.
   - A local-libvirt release retry still conflicts at the inventory. Accepted: operator
     exclusion, follow-up candidate.
   - A `journal_conflict` from a missing `mutation-started` record is raised again by publication
     after `finalize` raised it. Accepted: same category, nothing is published.
4. **Covered elsewhere**
   - Adopted (`managed`) receipt at finalize: #3435 (merged).
   - Finalize hardening: #3419.

### Threat model

- **Boundary inventory.** This widens one existing boundary, the publish function, which runs
  `SECURITY DEFINER` for `kdive_provider_authority` and now takes a per-object operation identity
  from the caller. It adds no boundary.
- **Actor model.** The untrusted party is a provider authority worker holding the authority role
  and a current authority. It is trusted only for its own current authority (ADR-0584 fences).
- **Control per boundary.** The new identity is admitted only by equality with
  `p_operation_identity`, or with a database-side derivation from an earlier acknowledged release
  row of the same activation. The failure message leaks no stored value.
- **Out of scope.** A compromised authority worker publishing a false row for its own current
  activation. The existing fences already admit that, and it is unchanged.

## Success

1. Abort, then abort: the second abort publishes nothing new. The inventory reports one
   observation equal to generation 1's, and the database row is confirmed, not duplicated.
2. Shutdown, then abort: the retry publishes one row whose `operation_identity`, `attempt_id`
   and journal fields equal the receipt's.
3. The resolver's binding built from that row equals the remote object port's observation, and
   `delete_recovery_object` finalizes it.
4. The publish function refuses an object identity that matches neither the current identity nor
   an earlier release authority's cleanup derivation for the same activation. It also refuses a
   foreign identity on a non-release authority.
5. Restoring the strict request comparison in the remote inventory turns the service-level and
   adapter tests red.

## Validation

- `focused-test`: service passes the oldest adopted context to the inventory on a twice-aborted
  retry. `tests/providers/external_boot_authority/test_service.py`. Red on main: the context
  argument is absent and the retry raises before publishing.
- `focused-test`: remote inventory accepts the adopted context and refuses a differing one.
  `tests/providers/remote_libvirt/test_external_boot_authority.py`.
- `focused-test`: the SQL function admits an earlier generation's derived identity, confirms an
  existing row, stores the object's identity, and refuses foreign identities.
  `tests/providers/external_boot_authority/test_quarantine_publication.py`.
- `focused-test`: migration window lists include 0186. The four `migrations[-K:]` tests.
- `task-test-not-applicable`: ADR-0787 amendment. Prose with no executable consumer.
