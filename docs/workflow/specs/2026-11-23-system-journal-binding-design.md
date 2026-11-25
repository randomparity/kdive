# Share the System journal operation binding

## Problem and outcome

Replay and append each spell out operation identity fields in the per-System authority journal.
Append spells them twice, once for the prior record and once for its candidate. Share the exact
projection so these safety checks cannot drift when maintained, preserving current behavior.

## Scope

Add `_operation_binding(record: AuthoritySystemJournalRecordV1) -> tuple[object, ...]` beside
`_ownership_binding` in `providers/system_authority/journal.py`. Return these existing fields in
order: `authority_id`, `generation`, `attempt_id`, `operation`, `operation_identity`,
`operation_digest`, `bootstrap_identity`.

Replay assigns `binding = _operation_binding(record)` at its existing location. Append compares
`_operation_binding(record) != _operation_binding(last)` inside its existing continuation guard,
removing only the two temporary tuples. Keep all ownership, phase, generation, sequence, digest,
canonical-form and size checks in order, with identical errors and file operations. The record
projection is pure over normalized frozen models; evaluating the candidate before the prior
record changes no business validation. Keep bootstrap in both projections: ownership checks must
still reject its change first.

Use only the existing test module. Add seven parameter cases changing exactly one binding field
between valid ADMITTED and MUTATION_STARTED records, with a valid canonical payload and digest
chain. Assert append refuses before changing file bytes, then write the candidate after the valid
prefix independently and assert replay refuses. Bootstrap expects the ownership error in both
paths; the six other fields expect their operation-binding errors. Keep the existing success case
and all other tests unchanged.

The queue suggests validating a JSON-shaped dictionary through `model_validate`. Source and a
read-only baseline experiment confirm strict Python validation rejects its UUID/enum strings
before reaching journal logic. Rebuild `canonical_record` from `model_dump(mode="json",
by_alias=True, exclude={"canonical_record"})`, then use the existing strict JSON boundary,
`model_validate_json(json.dumps(fields), strict=True)`. Do not relax validation. This test setup
correction is recorded in the official plan note and frozen charter.

### Failure model and threat model

- Actors: authority append callers and replay of durable, potentially interrupted or altered
  records. Assets: exact operation identity, stable ownership, canonical bytes/digest chain,
  validated phase/generation transitions and bounded private journal files.
- Trust crossings: a validated candidate enters the durable append lane; existing bytes enter
  replay. Both must retain the same seven-field projection and earlier ownership rejection.
  Canonicalization, strict record schema, file identity/modes/no-follow, bounds, fsync, torn-tail
  and abandoned-evidence behavior remain unchanged.
- New tests must construct schema-valid candidates so a UUID/coercion error cannot masquerade as
  identity enforcement. Temp files live under pytest-managed roots; no real journal is touched.
- Accepted: bootstrap mutation is refused by ownership first, so it cannot independently exercise
  the later operation projection. Static comparison proves all seven projected fields are retained.
- No new public contract, stored format, schema, migration, permission, dependency or deployment
  policy. [ADR-0623](../../adr/0623-authority-owned-system-provisioning.md) remains unchanged; no
  new ADR is required. Other queue work stays separate.

## Validation and completion

Run the seven new behavioral cases on the original implementation: expected green preservation
proof, not a fabricated behavior RED. Refactor, then run the journal, protocol, service and
transport test files. Inject independent append and replay binding-check faults against the
operation-digest case; each must fail for the intended missing refusal. Restore exact bytes.

Compare the helper's seven fields to all three original tuples, and normalize only the declared
projection extraction to prove all other method AST, errors and write paths unchanged. Preserve
old test AST, sibling modules and SQL bytes. Build/install an actual wheel, compare bytes/origins
and run the filesystem journal and related behavior through installed code. Run lint, whole-tree
types, documentation checks and hooks; fresh bounded code/security reviews; local conventional
commit, exact health attribution and next.

## Global Constraints

Python 3.14, managed with `uv`. Declared targets remain Linux x86_64 and ppc64le; host tests run on
Linux x86_64 and claim no live hardware proof. Preserve public contracts and persisted bytes.
Use existing dependencies and project guardrails. Keep all changes and commits local.
