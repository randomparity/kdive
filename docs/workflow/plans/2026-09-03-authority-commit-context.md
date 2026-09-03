# Authority commit context carries the anchored journal proof

**Goal.** Give `AuthorityMutationAdapter.commit` a service-constructed closed value carrying the
anchored `mutation-started` record's `sequence` and digest, use it to give
`LocalLibvirtExternalBoot.finalize_cleanup_tombstone` a production caller on the local adapter's
`cleanup` commit point, and bind `target_xml` to its own digest on the two durable local recovery
records.

**Architecture.** `protocol.py` gains a closed `AuthorityCommitContextV1` constructible only from a
`mutation-started` `JournalRecordV1`. `service.py` builds it from the record it just anchored,
re-reads the trusted head to confirm the record is still this operation's head, and passes it to
`commit` in place of the bare `commit_point` string. `local_libvirt/external_boot_authority.py`
takes the context, keeps its existing commit-point legality checks, and on `CLEANUP` builds a
`FinalizeCleanupProof` from it and finalizes the tombstone.
`lifecycle/boot/external_boot.py` changes only where those seams meet it.

**Tech stack.** Python 3.14, `uv`, pydantic v2, pytest + pytest-asyncio, `ruff`, `ty`.

Expected implementation size: 380–520 changed lines (M) — derived from the file map below: four
source files totalling roughly 120 changed lines, five test files totalling roughly 300, and no
new module.

Spec: [`docs/workflow/specs/2026-09-03-authority-commit-context-design.md`](../specs/2026-09-03-authority-commit-context-design.md).
Decision: [ADR-0591](../../adr/0591-authority-commit-context-carries-the-anchored-journal-proof.md).

## Global Constraints

- Branch `feat/authority-commit-context-2207`; `BASE_BRANCH` is `main`.
- Guardrails: `just lint`, `just type`, `just test-changed` while iterating; `just ci` bare as the
  pre-push gate. Never pipe a gate recipe; never append `; echo $?`. Capture with
  `just ci > <file> 2>&1 < /dev/null`.
- Run `just format` before committing a Python-only change so the mutating `ruff` hooks do not
  rewrite the tree during `git commit`.
- Line limit 100 characters (`ruff` config). Prefer functions under 100 lines and complexity ≤ 8.
- `just type` is whole-tree (`src` **and** `tests`). Test doubles must type-check.
- `AuthorityMutationRequestV1` must gain no field. It is the client-supplied wire request.
- Every value on the new context is derived from a `JournalRecordV1` the service anchored. None is
  read from protocol input.
- `docs/adr/` records are append-only once merged; ADR-0584 is edited by **appending** a bullet to
  its `## Consequences` section only. ADR-0591 is a new file, and this PR fully implements its
  decision, so it ships at `Accepted (2026-09-03)` — `just adr-status-check` rejects a `Proposed`
  ADR cited from `src/` or `tests/`, and Task 3 cites it from `src/`.
- Do not touch `src/kdive/providers/local_libvirt/lifecycle/boot/session.py` — issue #2211 holds it
  in a concurrent worktree.

## File map

| Path | Answerable for | Change |
|---|---|---|
| `src/kdive/providers/external_boot_authority/protocol.py` | closed authority values | add `AuthorityCommitContextV1`; refresh the `operation_is_permitted` docstring |
| `src/kdive/providers/external_boot_authority/service.py` | lane serialization and journaling | adapter `Protocol` signature; `_require_anchored_head`; build and pass the context |
| `src/kdive/providers/local_libvirt/external_boot_authority.py` | local adapter | accept the context; finalize the tombstone on `CLEANUP`; proven-absence retry branch |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | local records and coordinator | widen `FinalizeCleanupProof.operation_id`; add `target_xml_sha256` to both records; extend `_metadata_extends_intent`; correct the stale comment |
| `docs/adr/0591-...md`, `docs/adr/0584-...md`, `docs/workflow/specs/...`, `docs/workflow/plans/...` | design record | already written |
| `tests/providers/external_boot_authority/test_protocol.py` | context model behaviour | new tests for `for_record` and closure |
| `tests/providers/external_boot_authority/service_support.py` | shared service doubles | update the recording adapter's `commit` signature |
| `tests/providers/external_boot_authority/test_service.py` | service behaviour | context reaches the adapter; head disagreement refused |
| `tests/providers/local_libvirt/test_external_boot_authority.py` | local adapter behaviour | context-shaped commits; cleanup finalization; retry idempotency |
| `tests/providers/local_libvirt/test_external_boot.py` | local records and store | `target_xml_sha256` refusals; projection digest pin; `operation_id` |
| `tests/providers/contract/bindings/local_libvirt.py` | contract binding fixture | supply `target_xml_sha256` |

## Task 1 — the closed commit context

**Where it fits.** The value every later task consumes. Nothing else can be written first.

**Files.** Modifies `src/kdive/providers/external_boot_authority/protocol.py`. Tests in
`tests/providers/external_boot_authority/test_protocol.py`.

**Interfaces produced.**

```python
class AuthorityCommitContextV1(_ClosedValue):
    schema_: Literal["external-boot-authority-v1"]
    commit_point: AuthorityOperation
    operation_identity: str
    attempt_id: UUID
    journal_sequence: PositiveBigInt
    journal_digest: Digest
    phase: Literal[JournalPhase.MUTATION_STARTED]

    @classmethod
    def for_record(cls, record: JournalRecordV1) -> AuthorityCommitContextV1: ...
```

**Consumes.** `_ClosedValue`, `Digest`, `PositiveBigInt`, `AuthorityOperation`, `JournalPhase`,
`JournalRecordV1`, `record_digest`, `_bounded_text` — all already defined in this module.
`record_digest` is defined at `protocol.py:343`, so the new class goes at the end of the file.

**Steps.**

1. Add this failing test to `tests/providers/external_boot_authority/test_protocol.py`. Reuse the
   module's existing record helper if one exists; otherwise build a record with
   `JournalRecordV1.model_validate` from an existing test's values.

   ```python
   def test_commit_context_is_built_only_from_an_anchored_mutation_started_record() -> None:
       started = _mutation_record(JournalPhase.MUTATION_STARTED, sequence=4)
       context = AuthorityCommitContextV1.for_record(started)
       assert context.journal_sequence == started.sequence
       assert context.journal_digest == record_digest(started)
       assert context.operation_identity == started.operation_identity
       assert context.attempt_id == started.attempt_id
       assert context.commit_point is started.operation
       assert context.phase is JournalPhase.MUTATION_STARTED
       for refused in (JournalPhase.ADMITTED, JournalPhase.PROVIDER_RETURNED):
           with pytest.raises(ValueError, match="mutation-started"):
               AuthorityCommitContextV1.for_record(_mutation_record(refused, sequence=4))
   ```

2. Run `uv run python -m pytest tests/providers/external_boot_authority/test_protocol.py -q`.
   Expect a failure: `ImportError`/`AttributeError` on `AuthorityCommitContextV1`.
3. Add a second failing test for the `observed` phase and for closure:

   ```python
   def test_commit_context_refuses_observed_records_and_forbids_extra_fields() -> None:
       observed = _mutation_record(
           JournalPhase.OBSERVED, sequence=5, observation=_observation()
       )
       with pytest.raises(ValueError, match="mutation-started"):
           AuthorityCommitContextV1.for_record(observed)
       values = AuthorityCommitContextV1.for_record(
           _mutation_record(JournalPhase.MUTATION_STARTED, sequence=5)
       ).model_dump(mode="json", by_alias=True)
       assert AuthorityCommitContextV1.model_validate(values).journal_sequence == 5
       with pytest.raises(ValidationError):
           AuthorityCommitContextV1.model_validate(values | {"extra": "forbidden"})
       with pytest.raises(ValidationError):
           AuthorityCommitContextV1.model_validate(values | {"phase": "observed"})
   ```

4. Run the same pytest command; expect the same import failure.
5. Add the test that pins the wire request closed, which is the criterion saying a client can
   neither set nor influence a journal value:

   ```python
   def test_the_wire_mutation_request_carries_no_journal_field() -> None:
       assert set(AuthorityMutationRequestV1.model_fields) == {
           "schema_",
           "authority_id",
           "generation",
           "system_id",
           "activation_id",
           "run_id",
           "plan_identity",
           "purpose",
           "operation",
           "provider_kind",
           "authority_instance",
           "operation_identity",
           "operation_digest",
           "attempt_id",
           "expected_source_identity",
           "intended_target_identity",
           "recovery_objects",
       }
       values = _mutation_request().model_dump(mode="json", by_alias=True)
       for smuggled in ("journal_sequence", "journal_digest", "phase"):
           with pytest.raises(ValidationError):
               AuthorityMutationRequestV1.model_validate(values | {smuggled: 1})
       payload = json.dumps(
           values | {"journal_sequence": 1}, sort_keys=True, separators=(",", ":")
       ).encode()
       with pytest.raises(ValueError):
           decode_authority_request(payload)
   ```

   The literal field set is deliberate: a set comparison against
   `AuthorityMutationRequestV1.model_fields` itself would pass no matter what the model grew.
   Reuse the module's existing request builder for `_mutation_request()`.
6. Run the same pytest command. This test may already pass — it pins existing behaviour rather than
   driving new code. Confirm it passes now, and confirm it *bites* in Task 5 by adding a journal
   field to the model and observing the assertion fail.
7. Append to `protocol.py`, after `record_digest`:

   ```python
   class AuthorityCommitContextV1(_ClosedValue):
       """Service-constructed proof of the anchored ``mutation-started`` record.

       Carried across the ``AuthorityMutationAdapter`` seam so a provider adapter can tie its
       own commit to the exact authority journal record without reading the journal. Every
       field comes from a record the authority anchored; none is reachable from
       ``AuthorityMutationRequestV1``, which is peer-supplied and carries no journal field.
       """

       schema_: Literal["external-boot-authority-v1"] = Field(
           "external-boot-authority-v1", alias="schema"
       )
       commit_point: AuthorityOperation
       operation_identity: str
       attempt_id: UUID
       journal_sequence: PositiveBigInt
       journal_digest: Digest
       phase: Literal[JournalPhase.MUTATION_STARTED] = JournalPhase.MUTATION_STARTED

       @field_validator("operation_identity")
       @classmethod
       def _identity_is_bounded(cls, value: str) -> str:
           return _bounded_text(value)

       @classmethod
       def for_record(cls, record: JournalRecordV1) -> AuthorityCommitContextV1:
           """Build the context for one anchored record, refusing any other phase."""
           if record.phase is not JournalPhase.MUTATION_STARTED:
               raise ValueError("commit context requires an anchored mutation-started record")
           return cls(
               commit_point=record.operation,
               operation_identity=record.operation_identity,
               attempt_id=record.attempt_id,
               journal_sequence=record.sequence,
               journal_digest=record_digest(record),
           )
   ```

8. Update the `operation_is_permitted` docstring (`protocol.py:92-99`): the seam now carries
   `AuthorityCommitContextV1`, whose `commit_point` is an `AuthorityOperation`, so the model layer
   guarantees the *member* but still not that it is legal for the request's purpose or equal to the
   request's own operation. Say exactly that; do not delete the function.
9. Run `uv run python -m pytest tests/providers/external_boot_authority/test_protocol.py -q`.
   Expect all tests to pass.
10. `just lint && just type`, then commit.

**Acceptance.** `for_record` accepts only `mutation-started`; the model is frozen, `extra="forbid"`,
and round-trips through `model_dump(mode="json", by_alias=True)`.

**Note for the implementer.** `Literal[JournalPhase.MUTATION_STARTED]` with an enum default is the
intended shape. If pydantic rejects the enum member as a `Literal` argument on the pinned version,
fall back to `phase: Literal["mutation-started"] = "mutation-started"`, matching
`FinalizeCleanupProof` at `external_boot.py:184`, and keep the `for_record` guard unchanged. Confirm
which shape you used in the commit message.

## Task 2 — the service constructs, verifies, and passes the context

**Where it fits.** Consumes Task 1's model. Produces the seam Task 3 implements against.

**Files.** Modifies `src/kdive/providers/external_boot_authority/service.py`. Tests in
`tests/providers/external_boot_authority/test_service.py`; double updated in
`tests/providers/external_boot_authority/service_support.py`.

**Interfaces consumed.** `AuthorityCommitContextV1.for_record(record) -> AuthorityCommitContextV1`
from Task 1. `JournalHead` (already imported) exposes `.sequence`, `.digest`, `.phase`, and
`.operation_identity` — the same fields `_recover` already reads at `service.py:337-344`.

**Interfaces produced.**

```python
class AuthorityMutationAdapter(Protocol):
    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1: ...
    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1: ...
```

**Steps.**

1. Update the recording adapter in `tests/providers/external_boot_authority/service_support.py:278`
   to the new signature, recording the context rather than only its name:

   ```python
   async def commit(
       self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
   ) -> AuthorityObservationV1:
       self.calls.append(f"commit:{context.commit_point.value}")
       self.commit_contexts.append(context)
       ...
   ```

   Add `self.commit_contexts: list[AuthorityCommitContextV1] = []` to its `__init__`. Keep the
   existing `calls` string format so current assertions in `test_service.py` keep passing
   unchanged in intent.

2. Add this failing test to `tests/providers/external_boot_authority/test_service.py`, driving
   `execute_mutation` — not `adapter.commit` — and comparing against the journal the service wrote:

   ```python
   async def test_commit_receives_the_anchored_mutation_started_sequence_and_digest() -> None:
       harness = _harness()                       # existing helper in this module
       await harness.acknowledge()
       await harness.service.execute_mutation(harness.peer, harness.mutation_request)
       [context] = harness.adapter.commit_contexts
       started = [
           record
           for record in harness.records()
           if record.phase is JournalPhase.MUTATION_STARTED
           and record.operation_identity == harness.mutation_request.operation_identity
       ][-1]
       assert context.journal_sequence == started.sequence
       assert context.journal_digest == record_digest(started)
       assert context.attempt_id == started.attempt_id
       assert context.operation_identity == started.operation_identity
       assert context.commit_point is harness.mutation_request.operation
   ```

   Adapt `_harness()`, `harness.records()` and the peer/request names to whatever the module
   already uses; read the file first and reuse its existing fixtures rather than adding new ones.

3. Run `uv run python -m pytest tests/providers/external_boot_authority/test_service.py -q`.
   Expect a failure on `commit_contexts` being empty or on the signature.
4. Add the head-disagreement test. It must drive the real service and control the head from the
   repository double, so the compared value is not one the production code also produced:

   ```python
   async def test_a_head_that_disagrees_with_the_anchored_record_refuses_the_commit() -> None:
       harness = _harness()
       await harness.acknowledge()
       harness.repository.corrupt_head_after_phase = JournalPhase.MUTATION_STARTED
       with pytest.raises(AuthorityServiceError) as raised:
           await harness.service.execute_mutation(harness.peer, harness.mutation_request)
       assert raised.value.category == "journal_conflict"
       assert harness.adapter.commit_contexts == []
       assert "commit:cleanup" not in harness.adapter.calls
   ```

   Implement `corrupt_head_after_phase` on the repository double in `service_support.py`: when set,
   `read_head` returns the real head with `sequence` incremented by 1 and `digest` replaced by
   `"sha256:" + "f" * 64`, **keeping** `operation_identity` and `phase`, but only for reads that
   happen after a record of that phase was advanced. Keeping the operation identity is what makes
   the test exercise the scoped arm rather than the takeover arm.

5. Run the same command; expect a failure (no exception raised, adapter was called).
6. Add the takeover-overlap regression test, so the scoping is proved rather than asserted:

   ```python
   async def test_a_head_moved_by_a_concurrent_takeover_still_lets_the_commit_finish() -> None:
       harness = _harness()
       await harness.acknowledge()
       harness.repository.head_operation_identity_override = "takeover-next"
       await harness.service.execute_mutation(harness.peer, harness.mutation_request)
       assert len(harness.adapter.commit_contexts) == 1
   ```

   `head_operation_identity_override` makes `read_head` report a different operation identity,
   which is what a concurrent takeover produces. Implement it beside the previous flag.

7. Run the same command. This test guards against the *over-strict* implementation, so with no
   implementation at all it passes trivially. That is expected and is not a bite proof: Task 5
   proves it by widening `_require_anchored_head` to compare the bare head and observing this test
   fail with `journal_conflict`.
8. In `service.py`, change the `AuthorityMutationAdapter.commit` signature to the block above, and
   import `AuthorityCommitContextV1` in the existing `protocol` import list.
9. Add the head check as a method on `ExternalBootAuthorityService`, placed after `_provider_error`:

   ```python
   async def _require_anchored_head(
       self, binding: AuthorityBinding, context: AuthorityCommitContextV1
   ) -> None:
       """Refuse a commit whose anchored record is no longer this operation's head.

       Scoped to the mutation's own operation identity. ``acknowledge_takeover`` anchors its
       supersession and watermark records under a different identity while an admitted
       mutation is still in flight and then waits on it, so an unscoped comparison would
       reject the overlap ADR-0584 designs for.
       """
       head = await self._repository.read_head(binding)
       if head is None or (
           head.operation_identity == context.operation_identity
           and (
               head.sequence != context.journal_sequence
               or head.digest != context.journal_digest
               or head.phase is not JournalPhase.MUTATION_STARTED
           )
       ):
           raise AuthorityServiceError("journal_conflict")
   ```

10. In `execute_mutation`, inside the `async with lane.lock` block that anchors
    `MUTATION_STARTED` (`service.py:865-871`), build the context from the record just anchored,
    immediately after `active.phase = JournalPhase.MUTATION_STARTED`:

    ```python
    context = AuthorityCommitContextV1.for_record(records[-1])
    ```

11. Replace the commit call (`service.py:880-887`) so the head check runs first and the context is
    passed:

    ```python
    await self._require_anchored_head(binding, context)
    try:
        await self._adapter.commit(request, context)
    except AuthorityServiceError:
        # Already a bounded category; re-classifying it as provider_conflict would
        # lose a superseded verdict the adapter is entitled to reach.
        raise
    except Exception:
        raise self._provider_error(request) from None
    ```

    Leave the `except AuthorityServiceError: raise` arm exactly as #2199 wrote it.
12. Run `uv run python -m pytest tests/providers/external_boot_authority -q`. Expect every test to
    pass, including the pre-existing ones.
13. `just lint && just type`, then commit.

**Acceptance.** `execute_mutation` hands the adapter a context whose sequence and digest equal the
`mutation-started` record in the journal; a same-identity head disagreement raises
`journal_conflict` before the adapter is called; a different-identity head does not.

## Task 3 — the local adapter finalizes the tombstone

**Where it fits.** Consumes Task 2's seam and Task 4's widened `operation_id`. Do Task 4 first if
`FinalizeCleanupProof(operation_id=...)` rejects the operation identity; otherwise the order does
not matter.

**Files.** Modifies `src/kdive/providers/local_libvirt/external_boot_authority.py`. Tests in
`tests/providers/local_libvirt/test_external_boot_authority.py`.

**Interfaces consumed.**

- `AuthorityCommitContextV1` with `commit_point`, `operation_identity`, `attempt_id`,
  `journal_sequence`, `journal_digest` (Task 1).
- `LocalLibvirtExternalBoot.finalize_cleanup_tombstone(recovery: RecoveryPoint, proof: FinalizeCleanupProof, authority: OpaqueProviderRef) -> None`
  — confirmed at `external_boot.py:1530-1542`.
- `LocalLibvirtExternalBoot.point_digest(recovery: RecoveryPoint) -> str` — static, at `:1364`.
- `FinalizeCleanupProof(point_digest, binding, operation_id, attempt_id, journal_sequence, journal_digest, phase="mutation-started")`
  — at `:177-184`.

**Steps.**

1. Add this failing test to `tests/providers/local_libvirt/test_external_boot_authority.py`. It must
   drive `ExternalBootAuthorityService.execute_mutation`, not `instance.commit` — a test that calls
   the adapter directly bypasses the service and proves nothing about the wiring:

   ```python
   async def test_a_cleanup_commit_finalizes_the_tombstone_through_the_service() -> None:
       io = _FakeIO()
       harness = _service_harness(_adapter(io))   # wires the real service over this adapter
       await harness.acknowledge()
       await harness.service.execute_mutation(harness.peer, harness.cleanup_request)
       assert io.actions == ["cleanup", "finalize_tombstone"]
       proof = io.finalized_proof
       assert proof is not None
       started = harness.started_record()
       assert proof.journal_sequence == started.sequence
       assert proof.journal_digest == record_digest(started)
       assert proof.operation_id == started.operation_identity
       assert proof.attempt_id == str(started.attempt_id)
       assert proof.phase == "mutation-started"
       assert proof.point_digest == LocalLibvirtExternalBoot.point_digest(io.point)
   ```

   `_FakeIO` at `test_external_boot_authority.py:154` already stubs `finalize_tombstone`; extend it
   to record the proof and to append `"finalize_tombstone"` to its action list. Build
   `_service_harness` from the repository/journal doubles in
   `tests/providers/external_boot_authority/service_support.py`; import them rather than writing new
   ones.

2. Run `uv run python -m pytest tests/providers/local_libvirt/test_external_boot_authority.py -q`.
   Expect a failure: `io.actions == ["cleanup"]`.
3. Add the idempotency test:

   ```python
   async def test_a_retried_cleanup_commit_succeeds_against_the_finalized_absence() -> None:
       io = _FakeIO()
       harness = _service_harness(_adapter(io))
       await harness.acknowledge()
       await harness.service.execute_mutation(harness.peer, harness.cleanup_request)
       io.recovery_point_absent = True            # what finalization leaves behind
       await harness.acknowledge(generation=2)
       await harness.service.execute_mutation(harness.peer, harness.cleanup_request_2)
       assert io.actions == ["cleanup", "finalize_tombstone"]
   ```

   `recovery_point_absent` makes `_FakeIO`'s `reopen_binding` raise `FileNotFoundError`, which is
   what the real store raises once the recovery directory is gone. The assertion is that the action
   list did **not** grow: no second `cleanup` and no second `finalize_tombstone`.

4. Add the negative arm, so the branch cannot be widened silently:

   ```python
   async def test_an_unreadable_recovery_point_is_still_a_conflict_on_cleanup() -> None:
       io = _FakeIO()
       io.recovery_point_error = OSError("device busy")
       instance = _adapter(io)
       with pytest.raises(AuthorityServiceError) as raised:
           await instance.commit(_request(AuthorityOperation.CLEANUP), _context(AuthorityOperation.CLEANUP))
       assert raised.value.category == "provider_conflict"

   async def test_a_teardown_commit_does_not_finalize_the_tombstone() -> None:
       io = _FakeIO()
       io.recovery_point_absent = True
       instance = _adapter(io)
       with pytest.raises(AuthorityServiceError) as raised:
           await instance.commit(_request(AuthorityOperation.TEARDOWN), _context(AuthorityOperation.TEARDOWN))
       assert raised.value.category == "provider_conflict"
   ```

   These two may call `instance.commit` directly: they assert the adapter's own bounding, and the
   service cannot construct the states they need.

5. Run `uv run python -m pytest tests/providers/local_libvirt/test_external_boot_authority.py -q`.
   Expect failures on all four new tests.
6. Change the adapter's public signature:

   ```python
   async def commit(
       self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
   ) -> AuthorityObservationV1:
       """Apply one named commit point, then report the resulting observation."""
       operation = self._require_permitted_commit_point(request, context)
       self._require_admissible_generation(request)
       return await asyncio.to_thread(self._commit, request, operation, context)
   ```

7. Rewrite `_require_permitted_commit_point` to take the context. Drop the
   `AuthorityOperation(commit_point)` parse — the model already guarantees a member — and keep both
   remaining checks with #2199's reasoning intact:

   ```python
   @staticmethod
   def _require_permitted_commit_point(
       request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
   ) -> AuthorityOperation:
       """Refuse an illegal commit point before any provider call.

       The context's ``commit_point`` is an ``AuthorityOperation``, so the member itself is
       model-guaranteed. Two things still are not: that the operation is legal for the
       request's purpose, and that it is the same operation the request carries. Without the
       second, a request could journal one operation while driving the provider through
       another, and the journal record — the evidence ADR-0584 makes authoritative for what
       mutation may have happened — would name the wrong one.
       """
       operation = context.commit_point
       if not operation_is_permitted(request.purpose, operation):
           raise AuthorityServiceError("provider_conflict")
       if operation is not request.operation:
           raise AuthorityServiceError("provider_conflict")
       return operation
   ```

8. Add the proven-absence probe beside `_resolve_point`, leaving `_resolve_point` itself unchanged
   so `_observe` keeps classifying an unresolvable point as `unreadable`:

   ```python
   def _recovery_record_is_absent(
       self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
   ) -> bool:
       """Prove the durable recovery record is gone rather than merely unreadable.

       Only ``FileNotFoundError`` from the owner-derived path counts. A read that failed for
       any other reason is not absence and must not be treated as a completed cleanup.
       """
       try:
           self._ports.recovery_point(binding, authority)
       except FileNotFoundError:
           return True
       except Exception:  # noqa: BLE001 - an unreadable point is not a proven absence
           logger.exception("external-boot recovery point is unresolvable")
       return False
   ```

9. Add the cleanup branch to `_commit`, and thread the context into `_apply`:

   ```python
   def _commit(
       self,
       request: AuthorityMutationRequestV1,
       operation: AuthorityOperation,
       context: AuthorityCommitContextV1,
   ) -> AuthorityObservationV1:
       binding = _activation_binding(request)
       authority = _authority_ref(request)
       point = self._resolve_point(binding, authority)
       if (
           point is None
           and operation is AuthorityOperation.CLEANUP
           and self._recovery_record_is_absent(binding, authority)
       ):
           # Cleanup destroys the record it commits against: publish_tombstone unlinks the
           # metadata and finalization removes the directory. A retried cleanup therefore
           # finds a proven absence, which is this operation already accounted for. No
           # provider mutation runs, so no deletion authority is exercised here.
           return self._observation(request, binding, authority, None)
       matched = self._require_matching_identities(request, point)
       if not _ownership_is_proven(
           request, matched, require_named=operation in _DELETING_OPERATIONS
       ):
           # Quarantine: the objects are neither reused for a mutation nor deleted, and
           # the state is reported as the conflict ADR-0584 calls an unowned observation.
           raise AuthorityServiceError("provider_conflict")
       if operation in _MUTATING_OPERATIONS:
           self._apply(operation, matched, authority, context)
       return self._observation(request, binding, authority, matched)
   ```

10. In `_apply`, split the `{CLEANUP, TEARDOWN}` arm and finalize on `CLEANUP` only:

    ```python
    elif operation is AuthorityOperation.CLEANUP:
        self._ports.cleanup(point, authority)
        self._ports.finalize_cleanup_tombstone(point, _cleanup_proof(context, point), authority)
    elif operation is AuthorityOperation.TEARDOWN:
        # Teardown publishes a tombstone through the same primitive and has no finalizer
        # yet; that gap belongs to the local adapter, not to this seam (ADR-0591).
        self._ports.cleanup(point, authority)
    ```

    Add `context: AuthorityCommitContextV1` as `_apply`'s fourth parameter. Leave the surrounding
    `except AuthorityServiceError: raise` / `except Exception:` wrapper unchanged, so a
    finalization `ValueError` becomes a bounded `provider_conflict`.

11. Add the module-level proof builder beside `_ownership_is_proven`:

    ```python
    def _cleanup_proof(
        context: AuthorityCommitContextV1, point: RecoveryPoint
    ) -> FinalizeCleanupProof:
        """Tie this cleanup to the exact authority record the service anchored for it.

        Every field is either the recovery point this adapter resolved or a value the
        authority read out of its own journal. Nothing here is defaulted or derived from
        protocol input.
        """
        return FinalizeCleanupProof(
            point_digest=LocalLibvirtExternalBoot.point_digest(point),
            binding=point.binding,
            operation_id=context.operation_identity,
            attempt_id=str(context.attempt_id),
            journal_sequence=context.journal_sequence,
            journal_digest=context.journal_digest,
        )
    ```

    Import `FinalizeCleanupProof` from
    `kdive.providers.local_libvirt.lifecycle.boot.external_boot` in the existing import block.

12. Run `uv run python -m pytest tests/providers/local_libvirt/test_external_boot_authority.py -q`.
    Expect every test to pass.
13. `just lint && just type`, then commit.

**Acceptance.** A `cleanup` commit driven through the service calls `finalize_cleanup_tombstone`
with a proof matching the anchored record; a retried cleanup performs no second provider mutation;
an unreadable point and a teardown commit both stay `provider_conflict`.

**Rollback.** No durable state is written by this task's tests; `_FakeIO` holds everything in
memory.

## Task 4 — bind `target_xml` to its own digest

**Where it fits.** Independent of Tasks 1–3 except that Task 3's proof needs the widened
`operation_id`. Can be done first.

**Files.** Modifies `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`. Tests in
`tests/providers/local_libvirt/test_external_boot.py`; fixture updated in
`tests/providers/contract/bindings/local_libvirt.py`.

**Interfaces consumed.** `LocalRecoveryMetadataV1` (`:101`), `LocalPreStopIntentV1` (`:141`),
`_metadata_extends_intent` (`:1977`), `TargetProjectionV1.digest` (`:226`),
`FinalizeCleanupProof` (`:177`). All confirmed present at those lines.

**Steps.**

1. Add these failing tests to `tests/providers/local_libvirt/test_external_boot.py`, one per record.
   `_metadata()` at `:565` is the existing metadata builder; add a matching pre-stop builder if the
   module does not already have one.

   ```python
   def test_metadata_refuses_a_substituted_target_xml() -> None:
       metadata = _metadata()
       values = metadata.model_dump(mode="json", by_alias=True)
       with pytest.raises(ValidationError, match="target domain XML digest"):
           LocalRecoveryMetadataV1.model_validate(values | {"target_xml": _SOURCE_XML + " "})

   def test_pre_stop_intent_refuses_a_substituted_target_xml() -> None:
       intent = _pre_stop_intent()
       values = intent.model_dump(mode="json", by_alias=True)
       with pytest.raises(ValidationError, match="target domain XML digest"):
           LocalPreStopIntentV1.model_validate(values | {"target_xml": _SOURCE_XML + " "})
   ```

2. Add the on-disk substitution test, which is the one that covers the reachable path. It writes a
   record through the store, rewrites `target_xml` in the file, and asserts the reopen is refused:

   ```python
   def test_a_target_xml_substituted_on_disk_is_refused_at_reopen(tmp_path: Path) -> None:
       root = tmp_path / "recovery"
       root.mkdir(mode=0o700)
       metadata = _metadata("recovered")
       with RecoveryMetadataStore(root) as store:
           reference = store.publish(metadata)
           name = recovery_directory_name(reference, metadata.binding)
           record = root / name / "intent.json"
           payload = json.loads(record.read_text())
           payload["target_xml"] = payload["target_xml"] + " "
           record.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
           with pytest.raises(ValueError):
               store.reopen(reference, metadata.binding)
   ```

   If `store.publish` is not the right entry point for a `"recovered"` record in this module, use
   whichever publish/complete helper the surrounding tests already use for the same phase; read
   them first.

3. Add the reachability test the criterion names — the recovery path never reaches `define_xml`
   with substituted bytes. Drive `define_target` through the existing session double and assert the
   substituted record never gets that far:

   ```python
   def test_define_target_never_defines_a_substituted_target_xml() -> None:
       metadata = _metadata("module-restored")
       substituted = metadata.model_dump(mode="json", by_alias=True) | {
           "target_xml": metadata.target_xml + " "
       }
       with pytest.raises(ValidationError):
           LocalRecoveryMetadataV1.model_validate(substituted)
       # The bytes never reach a session: validation is the only door into _host_state and
       # define_target, both of which take a LocalRecoveryMetadataV1.
       session = _RecordingSession()
       ports = _io_for(session, _metadata("module-restored"))
       ports.define_target(_metadata("module-restored"))
       assert metadata.target_xml + " " not in session.defined
   ```

   Use whichever recording session double the module already provides; do not add a new one.

4. Add the projection-digest pin:

   ```python
   def test_target_projection_digest_still_measures_projection_inputs() -> None:
       projection = _projection()
       assert projection.digest == "sha256:" + hashlib.sha256(projection.canonical_bytes()).hexdigest()
       assert "<domain" not in projection.canonical_bytes().decode()
       changed = projection.model_copy(update={"cmdline": projection.cmdline + " quiet"})
       assert changed.digest != projection.digest
   ```

5. Run `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -q`. Expect
   failures on the two digest tests and the on-disk test (no `target_xml_sha256` field yet, so
   validation accepts the substitution).
6. Add `target_xml_sha256: Digest` to `LocalRecoveryMetadataV1` immediately after
   `target_projection_sha256` (`:119`) and to `LocalPreStopIntentV1` after `:159`.
7. Extend both validators. In each, after the existing `source_xml` check, add:

   ```python
   target_digest = "sha256:" + hashlib.sha256(self.target_xml.encode()).hexdigest()
   if target_digest != self.target_xml_sha256:
       raise ValueError("target domain XML digest does not match bytes")
   ```

   Rename each validator from `_source_xml_matches_digest` to `_domain_xml_matches_digests`, since
   it now binds both.
8. Add `"target_xml_sha256"` to `_metadata_extends_intent`'s `shared` tuple (`:1977-1998`),
   immediately after `"target_projection_sha256"`.
9. Widen `FinalizeCleanupProof.operation_id` (`:180`) from the UUID pattern to the shared
   contract's bounded-text rule, and say why in one line:

   ```python
   # The authority's operation identifier is `_AuthorityBinding.operation_identity`, bounded
   # text (ADR-0584), not a UUID. Deriving one here would make this field synthesized.
   operation_id: Annotated[str, Field(min_length=1, max_length=255)]
   ```

   Leave `attempt_id`'s UUID pattern alone: the journal's `attempt_id` really is a `UUID`.
10. Replace the stale comment at `:1538-1541` in `finalize_cleanup_tombstone`:

    ```python
    # The authority supplies the anchored mutation-started proof as
    # AuthorityCommitContextV1 (ADR-0591). The local seam deliberately does not decode it;
    # it compares the closed owner/point fields and handles present or post-delete absence
    # idempotently.
    ```

11. Update every construction site to supply `target_xml_sha256`:
    `tests/providers/contract/bindings/local_libvirt.py:93`,
    `tests/providers/local_libvirt/test_external_boot_authority.py:96`, and
    `tests/providers/local_libvirt/test_external_boot.py:565`, each already computing
    `source_xml_sha256` the same way one line above.
12. Run `uv run python -m pytest tests/providers/local_libvirt tests/providers/contract -q`. Expect
    every test to pass.
13. `just lint && just type`, then commit.

**Acceptance.** Both records refuse a substituted `target_xml`; `_metadata_extends_intent` compares
the new field; `TargetProjectionV1.digest` is unchanged; `operation_id` carries a real operation
identity.

## Task 5 — bite proofs and the full gate

**Where it fits.** Last. Nothing depends on it; everything depends on it being honest.

**Steps.**

1. For each new test added in Tasks 1–4, with the implementation already committed: inject one
   controlled fault into the source it covers (for example, drop the `phase` guard in `for_record`;
   drop the `head.digest` arm in `_require_anchored_head`; drop the
   `finalize_cleanup_tombstone` call in `_apply`; drop the `target_xml_sha256` comparison).
2. Run that test alone and require a **clean assertion failure** naming the asserted value — not a
   collection error, not an import error. A collection error means the test does not bite; fix the
   test, not the fault.
3. `git checkout -- <file>` to revert, and verify byte-identity with `sha256sum <file>` against the
   value recorded before the injection.
4. Record each fault, the observed failure line, and the two matching hashes in the commit message
   or the PR body.
5. Run the full gate bare, capturing output without displacing the exit code:

   ```sh
   just ci > /tmp/ci-2207.log 2>&1 < /dev/null
   ```

   Expect exit code 0. Read the log for the recipe list; do not pipe the recipe.

**Acceptance.** Every new test has an observed clean assertion failure under a controlled fault and
a byte-identical revert. `just ci` exits 0.

## Deferrals carried into this plan

None yet. Any deferral a `$trial-loop` run on this branch disposes of is appended here with its
owning record path or tracker issue before the branch ships.
