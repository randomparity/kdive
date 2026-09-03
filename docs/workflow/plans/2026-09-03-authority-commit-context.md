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
takes the context and, on `CLEANUP`, builds a `FinalizeCleanupProof` from it and finalizes the
tombstone. `lifecycle/boot/external_boot.py` changes only where those seams meet it.

**Tech stack.** Python 3.14, `uv`, pydantic 2.13.4, pytest + anyio, `ruff`, `ty`.

Expected implementation size: 420–560 changed lines (M) — from the file map below: roughly 130
source lines, roughly 320 test lines including the migration of the 24 existing `commit(...)` call
sites, and no new module. The adapter adds no absence probe, so no `RecoveryMetadataStore` method,
no `LocalExternalBootIO`/`LocalExternalBootOperation` protocol entry and no coordinator method are
in scope — see Task 3.

Spec: [`docs/workflow/specs/2026-09-03-authority-commit-context-design.md`](../specs/2026-09-03-authority-commit-context-design.md).
Decision: [ADR-0592](../../adr/0592-authority-commit-context-carries-the-anchored-journal-proof.md).

## Global Constraints

- Branch `feat/authority-commit-context-2207`; `BASE_BRANCH` is `main`.
- Guardrails: `just lint`, `just type`, `just test-changed` while iterating; `just ci` bare as the
  pre-push gate. Never pipe a gate recipe; never append `; echo $?`. Capture with
  `just ci > <file> 2>&1 < /dev/null`.
- A fresh worktree has no `node_modules`, so `just check-mermaid` dies with
  `ERR_MODULE_NOT_FOUND: jsdom` before checking anything. Run `just install-mermaid-deps` once.
- `ruff format` covers Python code blocks inside Markdown under `docs/workflow/`, so this plan's
  own fenced blocks are formatted. `docs/adr/` is `extend-exclude`d. Run `just format` after
  editing either.
- Line limit 100 characters. Prefer functions under 100 lines and complexity ≤ 8.
- `just type` is whole-tree (`src` **and** `tests`). Test doubles must type-check.
- `AuthorityMutationRequestV1` must gain no field. It is the client-supplied wire request.
- Every value on the new context derives from a `JournalRecordV1` the service anchored. None is
  read from protocol input.
- `docs/adr/` records are append-only once merged; ADR-0584 is edited by **appending** a bullet to
  its `## Consequences`. ADR-0592 is new and ships at `Accepted (2026-09-03)` because this PR fully
  implements its decision — `just adr-status-check` rejects a `Proposed` ADR cited from `src/`, and
  Task 3 cites it from `src/`.
- **Ordering: this change must land before #2212.** Task 4 adds a required field to two durable
  records. `RecoveryMetadataStore._read` validates and then requires byte-exact canonical
  re-encoding (`"recovery intent is not canonical JSON"`), and `_read_pre_stop` does the same, so
  an optional field with a default fails canonicality exactly as a required field fails on
  absence — there is no compatible variant. It is free today only because the feature is dormant:
  `ProviderRuntime.external_boot` is `None` and `RealLocalExternalBootIO` is constructed nowhere in
  `src/`. State that as a property with its lines, not as a hit count, which measures a moving
  tree: `RealLocalExternalBootIO` occurs in `src/` exactly twice and neither is a call — the class
  definition at `lifecycle/boot/external_boot.py:756` and a docstring mention at
  `composition.py:226`. `LocalExternalBootMaterializer` (`external_boot.py:738`) has no
  implementation under `src/`, and `build_external_boot` (`composition.py:220-233`) returns `None`
  without a caller-supplied `io`. #2212 wires that construction; after it, this addition owes a
  real migration.
- Do not touch `src/kdive/providers/local_libvirt/lifecycle/boot/session.py` — #2211 holds it.

## File map

| Path | Answerable for | Change |
|---|---|---|
| `src/kdive/providers/external_boot_authority/protocol.py` | closed authority values | add `AuthorityCommitContextV1`; refresh the `operation_is_permitted` docstring |
| `src/kdive/providers/external_boot_authority/service.py` | lane serialization and journaling | adapter `Protocol` signature; `_require_anchored_head`; anchor `never-began` on refusal; build and pass the context |
| `src/kdive/providers/local_libvirt/external_boot_authority.py` | local adapter | accept the context; finalize the tombstone on `CLEANUP`; no absence branch of any kind |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | local records and coordinator | widen `FinalizeCleanupProof.operation_id`; add `target_xml_sha256` to both records; extend `_metadata_extends_intent`; correct the stale comment |
| `tests/providers/external_boot_authority/test_protocol.py` | context model behaviour | `for_record` phase gate, closure, and the wire-request field-set pin |
| `tests/providers/external_boot_authority/service_support.py` | shared service doubles | `_Adapter.commit` signature and context recording; `_Repository` head-override hooks |
| `tests/providers/external_boot_authority/test_service.py` | service behaviour | context reaches the adapter; scoped head disagreement refused; takeover overlap still runs; `never-began` anchored |
| `tests/providers/local_libvirt/test_external_boot_authority.py` | local adapter behaviour | **migrate the 24 existing `commit(request, <str>)` call sites**; cleanup finalization; the four-state unresolvable-point refusal |
| `tests/providers/local_libvirt/test_external_boot.py` | local records and store | `target_xml_sha256` refusals; the non-vacuous `define_xml` reachability test; projection digest pin |
| `tests/providers/contract/bindings/local_libvirt.py` | contract binding fixture | supply `target_xml_sha256` |

Not a write site to change, but named because it inherits an obligation:
`LocalExternalBootMaterializer.inspect_prepare` (`external_boot.py:744`) has no implementation
under `src/`; whoever writes it must populate `target_xml_sha256` on `LocalPreStopIntentV1`.
`_complete_preparation_metadata` (`:1315`) copies the intent's fields, so the metadata side needs
no separate write site.

## Task 1 — the closed commit context

**Where it fits.** The value every later task consumes.

**Files.** `src/kdive/providers/external_boot_authority/protocol.py`; tests in
`tests/providers/external_boot_authority/test_protocol.py`.

**Interfaces produced.**

```python
class AuthorityCommitContextV1(_ClosedValue):
    schema_: Literal["external-boot-authority-v1"]  # alias "schema"
    commit_point: AuthorityOperation
    operation_identity: str  # _bounded_text, max 255
    attempt_id: UUID
    journal_sequence: PositiveBigInt
    journal_digest: Digest
    phase: Literal[JournalPhase.MUTATION_STARTED] = JournalPhase.MUTATION_STARTED

    @classmethod
    def for_record(cls, record: JournalRecordV1) -> AuthorityCommitContextV1: ...
```

**Consumes.** `_ClosedValue`, `Digest`, `PositiveBigInt`, `AuthorityOperation`, `JournalPhase`,
`JournalRecordV1`, `record_digest`, `_bounded_text` — all already in this module. `record_digest`
is at `protocol.py:343`, so the class goes at the end of the file.

`Literal[<StrEnum member>]` with that member as the default is **verified** on pydantic 2.13.4 /
CPython 3.14.7: the default is the member, `model_dump(mode="json")` gives `"mutation-started"`,
`model_validate({"phase": "mutation-started"})` round-trips to the member, and `"observed"` is
refused. No fallback shape is needed.

**Steps.**

1. Write a failing test that `for_record` copies `sequence`, `record_digest(record)`,
   `operation_identity`, `attempt_id` and `operation` off the record, and raises `ValueError`
   matching `mutation-started` for `ADMITTED`, `PROVIDER_RETURNED` and `OBSERVED` records. Build
   records with `JournalRecordV1.model_validate`; the module's existing record helpers are the
   pattern to follow.
2. Run `uv run python -m pytest tests/providers/external_boot_authority/test_protocol.py -q`.
   Expect `ImportError` on `AuthorityCommitContextV1`.
3. Write a second failing test that the model is closed: a `model_dump(mode="json", by_alias=True)`
   round-trips, and `model_validate` raises `ValidationError` for an `extra` key and for
   `phase="observed"`.
4. Write the wire-request pin. Assert `set(AuthorityMutationRequestV1.model_fields)` equals the
   **literal** seventeen-name set (`schema_`, `authority_id`, `generation`, `system_id`,
   `activation_id`, `run_id`, `plan_identity`, `purpose`, `operation`, `provider_kind`,
   `authority_instance`, `operation_identity`, `operation_digest`, `attempt_id`,
   `expected_source_identity`, `intended_target_identity`, `recovery_objects`) — a comparison
   against `model_fields` itself would pass whatever the model grew. Then assert
   `model_validate` rejects `journal_sequence`, `journal_digest` and `phase`, and that
   `decode_authority_request` rejects canonical bytes carrying `journal_sequence`.
   This test passes before the implementation; it is bite-proved in Task 5 by adding a journal
   field to the model.
5. Implement `AuthorityCommitContextV1` with the shape above. `for_record` raises
   `ValueError("commit context requires an anchored mutation-started record")` unless
   `record.phase is JournalPhase.MUTATION_STARTED`, then copies the five values.
6. Update the `operation_is_permitted` docstring (`protocol.py:92-99`): the seam now carries
   `AuthorityCommitContextV1`, whose `commit_point` is an `AuthorityOperation`, so the model layer
   guarantees the member but still not that it is legal for the request's purpose or equal to the
   request's own operation. Keep the function.
7. `uv run python -m pytest tests/providers/external_boot_authority/test_protocol.py -q` — green.
   Then `just lint && just type`, and commit.

**Acceptance.** `for_record` accepts only `mutation-started`; the model is frozen and
`extra="forbid"`; `AuthorityMutationRequestV1`'s field set is pinned literally.

## Task 2 — the service constructs, verifies, and passes the context

**Files.** `src/kdive/providers/external_boot_authority/service.py`; tests in
`tests/providers/external_boot_authority/test_service.py`; doubles in
`tests/providers/external_boot_authority/service_support.py`.

**Interfaces consumed.** `AuthorityCommitContextV1.for_record` from Task 1. `JournalHead` exposes
`.sequence`, `.digest`, `.phase`, `.operation_identity` — the same fields `_recover` reads at
`service.py:337-344`.

**Interfaces produced.**

```python
class AuthorityMutationAdapter(Protocol):
    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1: ...
    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1: ...
```

**The existing test fixtures, exactly.** `service_support._service(tmp_path)` returns
`(service, repository, adapter, peer, takeover_request)`. The journal the service wrote is
`repository.records`, a `list[JournalRecordV1]`; `test_service.py:41-46` already compares against
it. Build a mutation with `_mutation(takeover)` and set `repository.current = True` before
`execute_mutation`. There is no `_harness()` — do not invent one.

**Steps.**

1. In `service_support.py`, change `_Adapter.commit` to take
   `context: AuthorityCommitContextV1`. **Keep** `self.calls.append(f"commit:{context.commit_point.value}")`
   — `test_service.py` asserts `adapter.calls == ["commit:activate", "observe"]` and that must keep
   passing unchanged in intent. Add `self.commit_contexts: list[AuthorityCommitContextV1] = []` and
   append the context.
2. In `_Repository`, add two head-override hooks used only by tests, both applied in `read_head`:
   - `corrupt_head_after_phase: JournalPhase | None` — once a record of that phase has been
     advanced, return the real head with `sequence + 1` and `digest = "sha256:" + "f" * 64`,
     **keeping** `operation_identity` and `phase`. Keeping the identity is what makes the test
     exercise the scoped arm rather than the takeover arm.
   - `head_operation_identity_override: str | None` — return the real head with that
     `operation_identity`, which is the concurrent-takeover shape.
3. Write the failing test for criterion 1: after `execute_mutation`, the single recorded context's
   `journal_sequence`, `journal_digest`, `attempt_id` and `operation_identity` equal those of the
   last `MUTATION_STARTED` record in `repository.records` for that operation identity, and
   `commit_point is mutation.operation`. Run the module — expect failure on the signature.
4. Write the failing test for criterion 5: with `corrupt_head_after_phase = MUTATION_STARTED`,
   `execute_mutation` raises `AuthorityServiceError` with category `journal_conflict`,
   `adapter.commit_contexts == []`, and no `commit:` entry is in `adapter.calls`.
5. Write the test for N4a: after that refusal, `repository.records[-1].phase is TERMINAL` with
   `outcome == "never-began"`, and no `PROVIDER_RETURNED` record exists for that operation identity.
6. Write the scoping regression: with `head_operation_identity_override = "takeover-next"`,
   `execute_mutation` completes and exactly one context was recorded. This one guards against the
   *over-strict* implementation, so it passes trivially with no implementation — Task 5 bite-proves
   it by widening `_require_anchored_head` to compare the bare head.
7. Change `AuthorityMutationAdapter.commit`'s signature and import `AuthorityCommitContextV1`.
8. Add `_require_anchored_head(binding, context)` after `_provider_error`. It reads
   `await self._repository.read_head(binding)` and raises
   `AuthorityServiceError("journal_conflict")` when the head is `None`, **or** when
   `head.operation_identity == context.operation_identity` and any of `head.sequence`,
   `head.digest`, `head.phase` disagrees with the context. Its docstring must say why the identity
   scoping is there (`acknowledge_takeover` anchors under a different identity while an admitted
   mutation is in flight, `service.py:628-639` before the wait at `:695-696`) and what the check is
   therefore able to catch — a second authority instance sharing the identity, or a head row that
   changed after `advance` returned. Do not repeat the wrong claim that it closes the
   takeover window.
9. In `execute_mutation`, inside the lock that anchors `MUTATION_STARTED` (`service.py:865-871`)
   and right after `active.phase = JournalPhase.MUTATION_STARTED`, build
   `context = AuthorityCommitContextV1.for_record(records[-1])`.
10. Replace the commit call (`service.py:880-887`) so the head check runs first. On its
    `journal_conflict`, anchor `TERMINAL` with `outcome="never-began"` under `lane.lock` before
    re-raising — the same shape as the `stop_before_start` path at `service.py:852-864` — so the
    lane is left resolved. Keep #2199's `except AuthorityServiceError: raise` arm ahead of the bare
    `except Exception` exactly as it is.
11. `uv run python -m pytest tests/providers/external_boot_authority -q` — every test green,
    including the pre-existing ones. `just lint && just type`, commit.

**Acceptance.** The adapter receives a context matching the journal; a same-identity head
disagreement raises `journal_conflict` before the adapter is called and leaves the lane terminal
with `never-began`; a different-identity head does not refuse.

## Task 3 — the local adapter finalizes the tombstone

**Files.** `src/kdive/providers/local_libvirt/external_boot_authority.py`; tests in
`tests/providers/local_libvirt/test_external_boot_authority.py`.

**Interfaces consumed.**

- `AuthorityCommitContextV1` (Task 1).
- `LocalLibvirtExternalBoot.finalize_cleanup_tombstone(recovery, proof, authority) -> None`
  (`external_boot.py:1530`).
- `LocalLibvirtExternalBoot.point_digest(recovery) -> str`, static (`:1364`).
- `FinalizeCleanupProof(point_digest, binding, operation_id, attempt_id, journal_sequence,
  journal_digest, phase)` (`:177`), with Task 4's widened `operation_id`.

**Three module facts that govern the tests.**

- `_FakeIO.finalize_tombstone` appends the string `"finalize"` (`:154-156`). **Keep that string.**
  `test_unproven_recovery_object_is_quarantined_not_reused_or_deleted` asserts
  `"finalize" not in io.actions` at `:538`, and renaming the action would make that assertion
  vacuous — it is the assertion proving an unproven recovery object is never finalized, which is
  the path this task makes reachable for the first time.
- `io.actions` also collects `open:{ref}`, `reopen`, `observe-state` and `phase:{p}` entries, so
  **never assert list equality on it.** Every existing test in the module uses membership. New
  assertions use `io.actions.count("cleanup")` and `io.actions.count("finalize")`.
- There are **24** existing `await ....commit(request, AuthorityOperation.X.value)` call sites in
  this module. They all have to move to the context form; step 1 does that before anything else.

**Steps.**

1. **Migrate first.** Add a `_context(operation, *, sequence=4)` helper to the module that builds a
   `JournalRecordV1` in `MUTATION_STARTED` phase from the same values `_request()` uses and returns
   `AuthorityCommitContextV1.for_record(record)`. Then rewrite all 24 `commit(request, <str>)` call
   sites as `commit(request, _context(<operation>))`. Run the module: it must fail only on the new
   signature, not on anything else.
2. Dispose of `test_commit_refuses_a_commit_point_that_is_not_an_operation_at_all` (`:337`). It
   exists because the seam took a bare string, and a closed model cannot carry a non-member, so the
   case is now unreachable at this layer. Delete it and replace it with a `ValidationError` test on
   `AuthorityCommitContextV1` asserting a non-member `commit_point` is refused at construction —
   the same ground, at the layer that now owns it. Record the swap in the commit message.
3. Write the failing test for criteria 3 and 7, driving
   `ExternalBootAuthorityService.execute_mutation` over the real adapter — not `instance.commit`,
   which bypasses the service and proves nothing about the wiring. Import the repository and
   journal doubles from `tests/providers/external_boot_authority/service_support.py`; do not write
   new ones. Assert `io.actions.count("cleanup") == 1` and `io.actions.count("finalize") == 1`, and
   that the recorded proof's `journal_sequence`, `journal_digest`, `operation_id`, `attempt_id`,
   `phase` and `point_digest` equal the anchored record's values and `point_digest(io.point)`.
   Extend `_FakeIO.finalize_tombstone` to store the proof.
4. Write the four-state refusal test, which is what replaces an absence branch. Parametrise
   `_FakeIO` over the four ways `recovery_point` fails — tombstone live, fully finalized, never
   prepared, prepare interrupted — plus a merely-unreadable point (`OSError`), and assert every one
   raises `provider_conflict` and that `io.actions` gained no `"cleanup"` and no `"finalize"`.
   All five raise from the same place, so model them on the double as `recovery_point` raising
   `FileNotFoundError` or `OSError`; the point of the parametrisation is that no state is special.
   **The fault this catches** is adding back any absence-derived success branch: with one present,
   whichever state it keys on returns an observation instead of raising. Task 5 proves that.
5. Write the positive-evidence idempotency test at the primitive, where the evidence exists: drive
   a cleanup commit whose tombstone is already published for this point, and assert
   `cleanup_complete` short-circuits it — `io.actions` gains no second `"cleanup"` — and that the
   commit still finalizes. Also assert a `TEARDOWN` commit never appends `"finalize"`.
6. Run the module — expect the new tests to fail.
7. Change `commit` to take `context: AuthorityCommitContextV1`, and pass it through to `_commit`
   and `_apply`.
8. Rewrite `_require_permitted_commit_point` to take the context. Drop the
   `AuthorityOperation(commit_point)` parse — the model guarantees the member — and keep both
   remaining checks with #2199's reasoning intact: the operation must be legal for the request's
   purpose, and it must equal `request.operation`, or a request could journal one operation while
   driving the provider through another.
9. **Add nothing here.** `_resolve_point` stays exactly as it is, and `_commit` keeps handing its
   result straight to `_require_matching_identities`, so an unresolvable point stays
   `provider_conflict` for every operation. Do not add an absence probe, a
   `RecoveryMetadataStore` method, a `LocalExternalBootIO`/`LocalExternalBootOperation` protocol
   entry, a coordinator method, or in-process bookkeeping. Two review rounds established that no
   discriminator at this seam separates the four states in step 4, and that any success answer
   derived from absence skips `_require_matching_identities` and
   `_ownership_is_proven(require_named=True)` on a peer-chosen binding. This step exists so the
   omission is deliberate and reviewable rather than looking like something forgotten.
10. Leave `_commit`'s structure alone apart from threading `context` through. The only behavioural
    change in this task is step 11.
11. In `_apply`, split the `{CLEANUP, TEARDOWN}` arm. `CLEANUP` calls `self._ports.cleanup(...)`
    then `self._ports.finalize_cleanup_tombstone(point, proof, authority)`. `TEARDOWN` calls
    `cleanup` only, with a comment that its missing finalizer is a known residual recorded in
    ADR-0592 and not this seam's to close. Leave the surrounding
    `except AuthorityServiceError: raise` / `except Exception:` wrapper alone, so a finalization
    `ValueError` becomes a bounded `provider_conflict`.
12. Add the module-level proof builder. Every field comes from the context or the resolved point,
    and `phase` is passed as `context.phase` rather than left to the model default — otherwise the
    proof's phase assertion observes the default and would pass even if the context had no `phase`
    field at all.
13. Run the module — green. `just lint && just type`, commit.

**Acceptance.** A cleanup commit driven through the service finalizes the tombstone with a proof
matching the anchored record; a retried cleanup against a gone directory performs no second
provider mutation; a live tombstone, an unreadable point and a teardown commit all stay as they
are today.

## Task 4 — bind `target_xml` to its own digest

**Files.** `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`; tests in
`tests/providers/local_libvirt/test_external_boot.py`; fixture in
`tests/providers/contract/bindings/local_libvirt.py`.

**Interfaces consumed.** `LocalRecoveryMetadataV1` (`:101`), `LocalPreStopIntentV1` (`:141`),
`_metadata_extends_intent` (`:1977`), `TargetProjectionV1.digest` (`:226`), `FinalizeCleanupProof`
(`:177`). Test helpers that exist and must be reused: `_metadata(phase)` (`:565`),
`_pre_stop(metadata)` (`:685`, derives the intent from the metadata dump so it inherits the new
field automatically), `_real_io(root, preparation)` (`:3094`, returns
`(RealLocalExternalBootIO, _RealSession)`), `_real_prepare(io, materialization)` (`:3109`).
`_RealSession.define_xml` appends `f"define:{xml}"` to `preparation.actions` (`:1693-1694`).
There is no `_RecordingSession` and no `_io_for` — do not reference them.

**Steps.**

1. Write two failing validator tests, one per record: dump the model, substitute `target_xml`, and
   assert `ValidationError` matching `target domain XML digest`.
2. Write the failing on-disk test: publish a record through `RecoveryMetadataStore`, rewrite
   `target_xml` inside `intent.json`, and assert `store.reopen(...)` refuses.
3. Write the criterion-10 reachability test, and make it non-vacuous. **The fault it must catch is
   deleting the `target_xml_sha256` comparison from the validator**; with the comparison gone the
   substituted record validates, `activate` proceeds, and a `define:` action is recorded. So the
   test must hand the substituted record to the real path, not an unsubstituted one:
   build the store and `_real_io` harness, write the metadata, substitute `target_xml` on disk,
   call `LocalLibvirtExternalBoot(io).activate(point, authority)`, and assert it raises **and**
   that no entry in `preparation.actions` starts with `"define:"`. Confirm in Task 5 that removing
   the comparison turns this red on its own assertion.
4. Write the projection pin: `TargetProjectionV1.digest` equals `sha256` over `canonical_bytes()`,
   the canonical bytes contain no XML, and the digest changes when a projection input changes.
5. Run `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py -q` — expect
   the first three to fail.
6. Add `target_xml_sha256: Digest` to `LocalRecoveryMetadataV1` after `target_projection_sha256`
   (`:119`) and to `LocalPreStopIntentV1` after `:159`.
7. Extend both validators with the `target_xml` digest comparison beside the existing `source_xml`
   one, raising `ValueError("target domain XML digest does not match bytes")`. Rename each
   validator from `_source_xml_matches_digest` to `_domain_xml_matches_digests`.
8. Add `"target_xml_sha256"` to `_metadata_extends_intent`'s `shared` tuple, after
   `"target_projection_sha256"`. Both records must be bound: the function compares them
   field-for-field, so a consistently tampered pair would otherwise pass.
9. Widen `FinalizeCleanupProof.operation_id` (`:180`) from `^[0-9a-f-]{36}$` to bounded text
   (`min_length=1, max_length=255`), with a one-line comment that the authority's operation
   identifier is `_AuthorityBinding.operation_identity` — bounded text per ADR-0584, not a UUID —
   and that deriving one would make the field synthesized. Leave `attempt_id`'s UUID pattern alone;
   the journal's `attempt_id` really is a `UUID`. In the same edit, delete
   `FinalizeCleanupProof.phase`'s `= "mutation-started"` default (`:184`) so the field is required.
   With a default it carried no information — `AuthorityCommitContextV1.phase` pins the same single
   literal, so passing it and defaulting it are the same value and no assertion can tell them
   apart. Required, omitting it raises `ValidationError`, which is a fault Task 5 can observe. The
   model is pre-release with no production caller, so this is a replacement, not a migration.
10. Replace the stale comment at `:1538-1541` in `finalize_cleanup_tombstone`: the authority now
    supplies the anchored `mutation-started` proof as `AuthorityCommitContextV1` (ADR-0592); the
    local seam still does not decode it and still compares the closed owner/point fields.
11. Add `target_xml_sha256` at the three construction sites:
    `tests/providers/contract/bindings/local_libvirt.py:93`,
    `tests/providers/local_libvirt/test_external_boot_authority.py:96`, and
    `tests/providers/local_libvirt/test_external_boot.py:565`. Each already computes
    `source_xml_sha256` one line above. `_pre_stop` needs no change — it validates from the
    metadata dump.
12. `uv run python -m pytest tests/providers/local_libvirt tests/providers/contract -q` — green.
    `just lint && just type`, commit.

**Acceptance.** Both records refuse a substituted `target_xml`; the recovery path never reaches
`define_xml` with substituted bytes, proved by a test that goes red when the comparison is removed;
`_metadata_extends_intent` compares the new field; `TargetProjectionV1.digest` is unchanged.

**Rollback.** Reverting this task leaves any record written by the new build unreadable by the old
one, for the canonicality reason in Global Constraints. That is acceptable only while the feature
is dormant, which is the same window the ordering constraint names.

## Task 5 — bite proofs and the full gate

**Steps.**

1. With the implementation committed, record `sha256sum` for each source file you are about to
   perturb.
2. Inject one controlled fault per new test and run that test alone. At minimum:
   drop the `phase` guard in `for_record`; drop the `head.digest` arm in `_require_anchored_head`;
   widen `_require_anchored_head` to compare the bare head (must turn the takeover-overlap test
   red); drop the `never-began` anchor; drop the `finalize_cleanup_tombstone` call in `_apply`;
   add an absence-derived success branch keyed on the recovery directory (must turn the
   never-prepared and prepare-interrupted arms of the four-state test red); omit `phase` in the
   proof builder (must turn it red with a `ValidationError`, which is only possible because the
   model default was removed); delete the `target_xml_sha256` comparison (must turn both validator tests and
   the `define_xml` reachability test red); add a `journal_sequence` field to
   `AuthorityMutationRequestV1` (must turn the field-set pin red).
3. Each must produce a **clean assertion failure naming the asserted value** — not a collection or
   import error. A collection error means the test does not bite; fix the test, not the fault.
4. `git checkout -- <file>` and confirm the `sha256sum` matches the pre-injection value.
5. Record every fault, its observed failure line, and the matching hashes in the PR body.
6. Run the gate bare: `just ci > /tmp/ci-2207.log 2>&1 < /dev/null`. Expect exit 0. A `failed=1`
   inside `test-ansible`'s output is an intended negative case in
   `run-remote-module-appliance.sh` and is not a real red.

**Acceptance.** Every new test has an observed clean assertion failure under a controlled fault and
a byte-identical revert. `just ci` exits 0.

## Deferrals carried into this plan

None. The design review's one unclosable concern — a crash between `cleanup()` and its
finalization strands a tombstone with no recovery path — is recorded as a consequence with its
non-regression boundary in ADR-0592 rather than deferred, because the fix needs a
`FinalizeCleanupProof`/`CleanupTombstoneV1` shape change that belongs to ADR-0586.
