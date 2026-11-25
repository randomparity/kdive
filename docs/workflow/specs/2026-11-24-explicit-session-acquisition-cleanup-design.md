# Make acquisition cleanup explicit

## Problem

Local session acquisition hides three ordered cleanup attempts behind a callback
tuple, a conditional lambda and a callback-presence loop. The direct sequence is
shorter and makes acquired-resource conditions and independent failures visible.
The outer operation context cannot replace it because factory acquisition precedes
the context's yielded-operation cleanup.

## Scope

Change only the acquisition-failure block in `LocalExternalBootSessionFactory.open`:
close an acquired overlay, close an acquired connection, then close the pin. Each
attempt catches `Exception` independently into the existing error list. Keep the
final ordered note loop, bare re-raise and outer `BaseException` handling. Preserve
existing cleanup `BaseException` interruption rather than adding a new policy.
Keep session-close, mismatch, pinned and teardown paths, artifact callbacks, caller
interfaces and persistence unchanged. No helper or cleanup framework is added.
Existing ADR-0587 owns resource lifetime; no accepted decision changes.

## Success

The direct sequence removes the callback machinery. Failure before connection,
after connection and after overlay acquisition closes only acquired resources,
once in overlay/connection/pin order. Ordinary cleanup failures retain independent
attempts and ordered notes on the original primary exception. Existing abort
interruption and all session callers retain their behavior.

## Validation

- `focused-test`: extend existing `test_session.py` acquisition cases for the three
  stages, ordinary close failures, primary identity (including `_CloseAbort`) and
  cleanup abort interruption. These are preservation controls: require original
  source GREEN rather than inventing a behavior change. Controlled omission of an
  acquired-resource close or replacement of the primary exception must fail, then
  restore exact source and rerun GREEN.
- Run `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py
  tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py
  tests/providers/local_libvirt/test_external_boot.py` and `just lint`, whole-tree
  `just type`, docs checks and hooks. Compare source AST outside the approved block,
  protected callers and 198 applied migrations; run a fresh independent code review.
- Human review assesses the clarity of the direct sequence; no prose or code-shape
  snapshot test substitutes for behavioral evidence. Local Linux x86_64 doubles
  exercise contracts; this does not claim real guest or ppc64le hardware proof.
