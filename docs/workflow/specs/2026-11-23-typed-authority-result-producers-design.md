# Typed authority result producers

## Problem

Result dictionaries hide required operation fields from the type checker. The
existing variants already validate them at runtime, after assembly. Derived release
also serializes a successful carrier merely to obtain its completion subtype.

## Scope and design

Follow ADR-0834. Rename only these existing private types at their jobs-level owner:
_ActivateResult, _RecoverResult, _ReleaseResult, _CleanupResult, _TeardownResult,
_DeadlineResult, _RecoveryAttemptResult, _ConflictResult lose the leading underscore;
_ReleaseEvidence becomes ReleaseResultEvidence and _CleanupEvidence becomes
CleanupResultEvidence. Keep all fields, validators, discriminator behavior and order.
Other helpers/private failure variants remain private. Update actual imports in
lifecycle and reentry tests. No re-export or compatibility alias is retained.

Add ExternalBootSuccessPayload as the seven non-conflict/non-failure variants.
Do not restructure ExternalBootResultPayload through that alias: preserve its
validation graph apart from renamed internal definition labels. authority_result
accepts only the success union. Every lifecycle payload uses its concrete constructor,
with datetime and UUID values directly rather than stringify/parse round trips.

terminal_evidence returns ExternalBootTerminalEvidenceV1 with its bounded outcome
literal, canonical OpaqueProviderRef tuple and acknowledgement digest. Conflict
construction uses ExternalBootConflictEvidenceV1 and ConflictResult. Cleanup uses
the existing closed CleanupResultEvidence, preserving ordinary/teardown constraints.

Rename the existing _bound helper to authority_carrier_data. Its input is the
existing typed result union; output is dict[str, object] containing binding facts
and that model. Closed carrier model_validate retains its carrier-level runtime checks.
Inputs here are freshly validated, unmodified instances: Pydantic does not revalidate
every nested field after internal mutation. Untrusted serialized inputs still receive
the existing field validation; no mutation path is introduced between construction
and wrapping.
Derived release validates ExternalBootDerivedReleaseCompletion directly from this
mapping and ReleaseResult. Keep the canonical domain release evidence and SQL JSON
bytes/identity; bridge its serialized value through ReleaseResultEvidence validation.
Derived teardown constructs TeardownResult with its already-typed response and
shares the mapping, then replaces journal_sequence and journal_digest with the
response values. No intermediate successful carrier or response dump is needed.

Changing terminal_evidence to a model requires its one direct SQL caller to serialize
with model_dump_json(by_alias=True). Keep SQL argument values equivalent. The lifecycle
clock helper returns UTC datetime; its raw cleanup SQL dictionary still serializes
that timestamp to the existing UTC-Z string. Do not change SQL calls/order, receipt
checks, callback phases, worker dispatch, deadlines or readiness budgets.

Global constraints: Python 3.14 managed by uv; x86_64 and ppc64le targets;
public tool contracts preserved; local changes and commits only.

## Failure model

- Actors/deployments: ordinary and derived authority workers in all current Python
  deployments, with persisted queue/database JSON and provider authority responses.
- Protected invariants: exact operation and evidence fields; binding ownership;
  domain release identity; actual teardown journal head; concrete completion subtype
  and one-time receipt consumption; existing error phase/category behavior.
- Accepted classes: unsupported private Python variant names are replaced together;
  JSON-schema internal labels follow the new names. No external wire field changes.
- Existing controls retained: runtime closed carrier validation, SQL holder/receipt
  fences, provider authentication and worker incarnation checks. No bypass constructor.

## Threat model

No permission or executable boundary is added. Data from stored rows still enters
existing evidence/carrier validation. Typed inputs improve internal construction but
do not substitute for runtime ownership, digest, size or allowed-operation checks.
Model construction failures remain in the existing handler error-phase boundaries.

## Success and validation

1. Actual producers use concrete variants, and the success helper rejects unrelated
   dictionaries or conflict/failure payloads under whole-tree type checking.
2. Existing schemas match the captured baseline after only the ten declared internal
   name/title/ref renames; serialized-input acceptance and alias JSON stay unchanged
   for validated, unmodified constructed instances.
3. Terminal evidence is typed at creation; its acknowledgement digest, owner IDs,
   reference canonicalization and UTC timestamp survive serialization unchanged.
4. Release retains its identity and exact completion; teardown retains its response
   journal sequence/digest, proof and exact completion. No duplicate commit occurs.

Use existing real-role lifecycle, reentry, release, teardown and worker suites plus
focused model/evidence tests. The evidence test first requires the typed return and
fails on the current dictionary. Add fixed alias JSON checks where existing tests
only inspect Python fields. Controlled producer field misspellings must fail ty;
using the acknowledgement journal for teardown must fail its receipt/binding proof.
Prove a valid typed deadline payload still receives carrier operation/purpose checks.
Restore and rerun affected tests and gates before a conventional local code commit.
