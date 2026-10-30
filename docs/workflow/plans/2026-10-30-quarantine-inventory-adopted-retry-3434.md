# Quarantine inventory for an adopted release cleanup retry (#3434): implementation plan

**Goal:** a remote release CLEANUP retried after a `before-finalize` abort publishes, or
confirms, one quarantine row naming the provider receipt's own operation identity. This holds
however many generations were interrupted.

**Architecture:** the service derives one proof context, the adopted `mutation-started` record,
and passes it to `finalize` and to the quarantine inventory. The remote inventory compares the
receipt with that context. The repository forwards each object's own identity. Migration 0186
replaces the publish SQL function so it stores that identity and admits a foreign identity only
when it is an earlier release authority's derived cleanup identity for the same activation.

**Tech stack:** Python 3 (asyncio, pydantic), PostgreSQL PL/pgSQL, pytest.

Spec: [`docs/workflow/specs/2026-10-30-quarantine-inventory-adopted-retry-3434-design.md`](../specs/2026-10-30-quarantine-inventory-adopted-retry-3434-design.md).
Decision: ADR-0787 amendment (2026-10-30, #3434).

Expected implementation size: 350–550 changed lines (L) — the file map below: about 60 source
lines across three Python modules, a roughly 130-line migration copied from 0144, and about
250 test lines.

## Global constraints

- Migration number `0186` is assigned by the campaign orchestrator. Do not renumber it.
  Migrations 0183–0185 are in flight on other branches; the orchestrator resolves the window-test
  conflicts at landing.
- The publish function keeps its exact signature
  `(text, uuid, bigint, text, bigint, text, jsonb) RETURNS integer`, `SECURITY DEFINER`,
  `SET search_path = ''`, so the grants and the host allow-list
  (`external_boot_authority/host.py`) stay valid.
- The local-libvirt adapter changes signature only (operator exclusion).
- Guardrails: `just lint`, `just type`, `just test-changed`. The pre-push hook runs `just ci`.
- Doc wording: no "critical/robust/comprehensive/elegant".

## File map

| File | Change | Criterion |
|---|---|---|
| `src/kdive/providers/external_boot_authority/service.py` | Extract `_proven_cleanup_context`. Inventory protocol takes `context`. Publication passes it and takes `records`. | 1, 2 |
| `src/kdive/providers/remote_libvirt/external_boot_authority.py` | Inventory takes `context` and compares identity and attempt with it. | 1 |
| `src/kdive/providers/local_libvirt/external_boot_authority.py` | Signature only. | 1 (protocol) |
| `src/kdive/providers/external_boot_authority/repository.py` | Per-object `operation_identity` in the payload. Release-only relaxation of the Python identity check. | 2, 3 |
| `src/kdive/db/schema/0186_quarantine_publication_adopted_identity.sql` | Replace the publish function. | 2, 3 |
| `tests/providers/external_boot_authority/test_service.py` | Fake inventory signature, plus a twice-aborted retry test. | 4 |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | Inventory call sites take `context`, plus an adopted-retry test. | 1, 4 |
| `tests/providers/external_boot_authority/test_quarantine_publication.py` | Adopted identity admitted, confirmed and stored; foreign identity refused. | 2, 3 |
| `tests/jobs/handlers/external_boot/test_orphan_resolution.py` | Inventory call site takes `context`. | protocol |
| `tests/db/test_migrate.py`, `tests/db/test_migration_0115_capture_reap_state.py`, `tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0091_system_object_sweep_cursors.py` | 0186 window row, K+1, and the applied-version lists. | migration |

## Task 1: service and adapters use the adopted proof context for the inventory

**Verification**

- Contract: the service passes the oldest adopted `mutation-started` context to the inventory.
  Mode: focused-test. A new `test_adopted_release_retry_publishes_the_receipt_it_ran_with` in
  `test_service.py`, parametrized over the first interruption (`abort`, `shutdown`; the second
  is `ProofBarrierShutdown`, which publishes nothing). Red under the controlled fault: the
  service passes `AuthorityCommitContextV1.for_record` of the request's own `mutation-started`
  record. Green:
  `uv run pytest tests/providers/external_boot_authority/test_service.py -k "adopted_release" -q`.
- Contract: the remote inventory accepts the receipt's proof context on a retry request and
  refuses a differing one, and the observation's binding is disposed of. Mode: focused-test. A new
  `test_remote_cleanup_inventory_follows_the_adopted_proof_on_a_retry` in
  `test_external_boot_authority.py`. Red with the strict request comparison restored. Green:
  `uv run pytest tests/providers/remote_libvirt/test_external_boot_authority.py -k "inventory" -q`.

**Interfaces produced**

- `ExternalBootAuthorityService._proven_cleanup_context(request: AuthorityMutationRequestV1,
  records: list[JournalRecordV1]) -> AuthorityCommitContextV1`, a static method.
- `AuthorityCleanupQuarantineInventory.cleanup_quarantine_inventory(self, request:
  AuthorityMutationRequestV1, context: AuthorityCommitContextV1) ->
  tuple[RecoveryObjectObservation, ...]`.

**Steps**

1. In `service.py`, replace the derivation inside `_finalize_adapter` with a static method:

   ```python
   @staticmethod
   def _proven_cleanup_context(
       request: AuthorityMutationRequestV1, records: list[JournalRecordV1]
   ) -> AuthorityCommitContextV1:
       """Return the proof a release phase's provider receipt holds (ADR-0787).

       A release adopted from an earlier generation ran no commit of its own, so its receipt
       holds the proof of the oldest adopted operation, the one that ran: a retry that adopted
       it and was itself interrupted leaves a later terminal (#3416, #3434).
       """
       terminals = (
           ExternalBootAuthorityService._prior_release_terminals(records, request)
           if request.purpose == "release"
           and request.operation in {AuthorityOperation.RECOVER, AuthorityOperation.CLEANUP}
           else []
       )
       proven_identity = terminals[0].operation_identity if terminals else request.operation_identity
       started = next(
           (
               record
               for record in reversed(records)
               if record.operation_identity == proven_identity
               and record.phase is JournalPhase.MUTATION_STARTED
           ),
           None,
       )
       if started is None:
           raise AuthorityServiceError("journal_conflict", reason="mutation_started_missing")
       return AuthorityCommitContextV1.for_record(started)
   ```

   `_finalize_adapter` then calls `context = self._proven_cleanup_context(request, records)` at
   the point where `started` was computed. It keeps the checkpoint, and calls
   `self._adapter.finalize(request, context)`.
2. Change the `AuthorityCleanupQuarantineInventory` protocol to the two-argument signature
   above. Give `_publish_cleanup_quarantine` a trailing `records: list[JournalRecordV1]`
   parameter and call
   `self._adapter.cleanup_quarantine_inventory(request, self._proven_cleanup_context(request,
   records))`. Pass `records` at the three call sites (conflict refusal, terminal replay, normal
   terminal).
3. Remote adapter: in `cleanup_quarantine_inventory(self, request, context)`, compare
   `receipt.operation_identity` and `receipt.attempt_id` with `context.operation_identity` and
   `context.attempt_id` instead of the request's. The raise stays
   `AuthorityServiceError("provider_conflict")`. `finalize` is unchanged.
4. Local adapter: add `context: AuthorityCommitContextV1` to the signature. Keep the body.
   Extend the docstring: "The comparison stays with the request; an adopted release retry is a
   follow-up (#3434)."
5. Tests: update the fake `_FinalizingAdapter.cleanup_quarantine_inventory(self, request,
   context)` in `test_service.py`, every remote call site (pass the `context` from
   `_cleaned_release` or `_native_context(request)`). The existing attempt-mismatch assertion in
   `test_remote_cleanup_inventory_is_the_exact_unmanaged_recovery_record` becomes
   `cleanup_quarantine_inventory(request, context.model_copy(update={"attempt_id": uuid4()}))`
   raising `AuthorityServiceError`. In `test_orphan_resolution.py` `_publish_local_quarantine`,
   build `AuthorityCommitContextV1(commit_point=AuthorityOperation.CLEANUP,
   operation_identity=cleanup_identity, attempt_id=attempt_id, journal_sequence=8,
   journal_digest=mutation_digest)` from the helper's own values and pass it. Add the two
   focused tests:
   - Service: reuse the setup of `test_adopted_release_cleanup_finalizes_against_the_proof_that_ran`
     with two interrupted generations. The first is parametrized as `ProofBarrierAbort` or
     `ProofBarrierShutdown`, and the second is always `ProofBarrierAbort`. A recording adapter
     stores each inventory `context`. Assert that every recorded context equals
     `AuthorityCommitContextV1.for_record(started)` of the first cleanup. Assert that
     `repository.published_cleanup_quarantines` holds one entry per abort (two for abort/abort,
     one for shutdown/abort), all with the same receipt.
   - Remote: `_cleaned_release` gives `request` and `context`. Build `retry =
     request.model_copy(update={"generation": request.generation + 1, "attempt_id": uuid4(),
     "operation_identity": "sha256:" + "9" * 64})`. Then
     `cleanup_quarantine_inventory(retry, context)` returns one observation whose
     `binding.operation_identity == request.operation_identity`. Calling it with
     `_native_context(retry)` raises `AuthorityServiceError`. `delete_recovery_object(
     observed.binding, authority_reference(retry), observed.observed_digest)` returns
     `present=False`.
6. Run the two focused commands, then `just lint` and `just type`. Commit
   `fix(external-boot): inventory a retried release cleanup under its adopted proof`.

## Task 2: the publish function stores and admits the receipt's own identity

**Verification**

- Contract: an earlier acknowledged release generation's derived cleanup identity is admitted,
  stored on the row, and confirmed on re-publication. A foreign identity is refused. Mode:
  focused-test. A new `test_publication_admits_an_earlier_release_cleanup_identity` in
  `test_quarantine_publication.py`. Red before 0186: `ValueError` from the repository, or
  `psycopg.Error` from the 0144 function. Green:
  `uv run pytest tests/providers/external_boot_authority/test_quarantine_publication.py -q`.
- Contract: a non-release binding still requires identity equality in the repository. Mode:
  focused-test, the same file. Calling `publish_cleanup_quarantine` with the binding's
  `purpose="cleanup"` and an object identity that differs raises `ValueError` before any SQL.
- Contract: the migration window includes 0186. Mode: focused-test, the four window tests. Red
  until the rows are added. Green:
  `uv run pytest tests/db/test_migrate.py tests/db/test_migration_0115_capture_reap_state.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0091_system_object_sweep_cursors.py -q`.

**Interfaces consumed:** none from Task 1. **Interfaces produced:** the payload object key
`operation_identity` (text).

**Steps**

1. `repository.py` `publish_cleanup_quarantine`: replace the identity line of the guard with

   ```python
                or (
                    object_binding.operation_identity != binding.operation_identity
                    and binding.purpose != "release"
                )
   ```

   and add `"operation_identity": object_binding.operation_identity,` to each payload object.
2. Create `src/kdive/db/schema/0186_quarantine_publication_adopted_identity.sql`. Copy the 0144
   function body exactly, as `CREATE OR REPLACE FUNCTION
   public.publish_external_boot_recovery_quarantine_authority(...)` with the same parameters.
   Then change it:
   - Add `operation_identity text` to both `jsonb_to_recordset` column lists.
   - After the resource lookup, add the check below. Every `item.operation_identity` read in
     this function is `COALESCE(item.operation_identity, p_operation_identity)`, so a payload
     from a binary older than 0186 behaves exactly as under 0144:

     ```sql
     IF EXISTS (
         SELECT 1 FROM jsonb_to_recordset(p_objects) AS item(operation_identity text)
         WHERE COALESCE(item.operation_identity, p_operation_identity) IS DISTINCT FROM p_operation_identity
           AND NOT (
               v_authority.purpose = 'release' AND v_authority.operation = 'release'
               AND EXISTS (
                   SELECT 1
                   FROM public.external_boot_authorities AS earlier
                   CROSS JOIN LATERAL public.derive_external_boot_release_phase_binding(
                       jsonb_build_object(
                           'authority_id', earlier.id, 'generation', earlier.generation,
                           'system_id', earlier.system_id, 'activation_id', earlier.activation_id,
                           'run_id', earlier.run_id, 'plan_identity', earlier.plan_identity,
                           'provider_kind', earlier.provider_kind,
                           'authority_instance', earlier.authority_instance,
                           'worker_incarnation', earlier.worker_incarnation,
                           'root_operation_identity', earlier.operation_identity,
                           'root_operation_digest', earlier.operation_digest
                       ), 'cleanup'
                   ) AS derived
                   WHERE earlier.system_id = v_authority.system_id
                     AND earlier.activation_id = v_authority.activation_id
                     AND earlier.run_id = v_authority.run_id
                     AND earlier.plan_identity = v_authority.plan_identity
                     AND earlier.provider_kind = v_authority.provider_kind
                     AND earlier.authority_instance = v_authority.authority_instance
                     AND earlier.purpose = 'release' AND earlier.operation = 'release'
                     AND earlier.generation < v_authority.generation
                     AND earlier.acknowledged_at IS NOT NULL
                     AND derived.operation_identity = COALESCE(item.operation_identity, p_operation_identity)
               )
           )
     ) THEN
         RAISE EXCEPTION 'cleanup quarantine object operation is not admissible'
             USING ERRCODE = 'P0001';
     END IF;
     ```

   - The insert selects `COALESCE(objects.operation_identity, p_operation_identity)` in place of `p_operation_identity`. The
     conflict check compares `q.operation_identity IS DISTINCT FROM COALESCE(objects.operation_identity, p_operation_identity)`.
   - Add a header comment naming ADR-0787 and #3434, in the style of 0182's header.
3. Add the window rows: `("0186", "0186_quarantine_publication_adopted_identity.sql")` and K+1
   in the four `migrations[-K:]` assertions, and `"0186"` after `"0182"` in the three
   applied-version lists in `test_migrate.py`.
4. Add the focused DB test. Seed a release case as the existing test does. Insert generation 1
   as `state='superseded', acknowledged_at=now(), superseded_at=now()`, and generation 2 as
   `current`. Each has its own id and root identity and the same activation, run, plan,
   provider and instance. Derive cleanup identity `A` from generation 1 and `B` from
   generation 2 with `derive_external_boot_release_phase_binding`. Set the journal head to
   generation 2's terminal under `B`. Then:
   - publishing an object with identity `A` under the generation-2 binding succeeds, twice;
   - `SELECT operation_identity FROM external_boot_recovery_quarantine` returns `A`, count 1;
   - an object with identity `"sha256:" + "7" * 64` raises `psycopg.Error`;
   - a payload object without the `operation_identity` key, sent straight to the SQL function as
     the `kdive_provider_authority` role, is stored under `p_operation_identity` (0144 behaviour);
   - building `orphan._Selection` from the stored row's columns and calling `.binding()` returns
     the published observation's `binding`;
   - the non-release `ValueError` case from the Verification list.
5. Run the focused commands, `just lint` and `just type`. Commit
   `fix(db): publish a quarantine row under the receipt's own operation identity`.

## Task 3: ADR amendment and design record

**Verification:** Contract: the ADR-0787 amendment. Mode: task-test-not-applicable. It is
prose, and no executable consumer reads it. `just records` validates the record's structure.

Steps: the amendment and this design set are committed with the design. After rebasing, run
`git fetch origin main && just records`.

## Rollback

Each task is one commit. Reverting the 0186 migration needs a forward migration that restores
the 0144 body. Rows written by 0186 carry valid receipt identities and need no data repair.
