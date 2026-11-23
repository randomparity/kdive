# Authority mutation adoption policy

## Problem

The mutation execution method combines two pure adoption selections with journal
coordination, provider execution, observation and settlement, obscuring the phase order.

## Scope

Extract two synchronous private methods on ExternalBootAuthorityService:
_preparation_predecessor(request, records) returns the existing predecessor request
and receipt identity pair; _adopted_release_phase(request, records) returns the
existing observation or None. Reuse existing operation-matching and prior-terminal
predicates. Keep the same reverse search, reconstruction fields, outcome checks and
AuthorityServiceError categories/reasons. Return (None, None) when no predecessor
exists. Invoke the methods at the current selection position, before unresolved recovery.
Keep preparation type narrowing at the caller; release eligibility remains in its method.

The methods own only the current synchronous policy blocks. Provider dispatch,
in_provider, teardown snapshots, proof checkpoints, locks, admission, recovery,
refusal translation, observation and finalization stay inline and unchanged.
Add no context container, dispatch helper, generic reconstruction or compatibility path.

### Failure model

- Actors: the authority service receives requests and reads its validated journal records.
- Invariants: selection order, selected identities and rejection reasons remain unchanged;
  no provider or journal side effect occurs inside the extracted methods.
- Accepted: private method names and stack frames change.
- Covered elsewhere: existing authority/provider tests retain concurrency, recovery,
  takeover, proof and cancellation coverage; no operational policy changes here.

## Success

1. The two selections have explicit typed inputs and outputs and one caller each.
2. The remaining mutation orchestration preserves its AST after replacing the two
   original policy blocks with their calls, including provider dispatch and error handling.
3. Existing adoption, replay, refusal, checkpoint, teardown and connected release tests pass.

## Validation

- Mode: task-test-not-applicable: pure extraction adds no decision or behavior.
  Compare extracted policy ASTs with the original blocks, allowing return statements
  and method-local renames only; compare unchanged orchestration around the call sites.
- Run existing external-boot authority service, teardown, running-observation and
  connected release/conflict tests with database prerequisites required.
  Inject temporary record-selection and mismatch-validation faults and confirm existing
  tests detect them; restore the source and rerun the affected cases.
- Run lint, whole-tree types, documentation checks and commit hooks. This unit
  does not claim new live-hardware qualification or alter public contracts.
