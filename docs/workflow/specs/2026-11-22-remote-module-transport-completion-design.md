# Remote-module transport completion

## Problem

Adjacent preparation and lifecycle dispatch branches repeat success serialization,
service error translation, completion logging and unexpected-error handling.

## Scope and design

Combine the two operation names under one branch in transport._dispatch.
Return provider-not-configured first when the service is absent. Check each operation
against its concrete request class before the try and raise invalid-request on mismatch.
Inside one try, narrow the request explicitly, call the corresponding service method
directly with peer and request, and serialize the result through the existing _success.
Retain one identical copy of the ordered exception handlers:
AuthorityServiceError uses _service_error, including install_refusal; CategorizedError
uses traceback-free _log_module_completion and maps failed-after-mutation to
remote-module-failed, refused-before-mutation to remote-module-refused, otherwise
provider-conflict; unexpected Exception uses the existing exception log and
provider-conflict response. Keep begin-preparation separate. Add no dispatcher abstraction,
mapping, helper, cast or compatibility route.

## Success

1. Preparation and lifecycle invoke only their selected method with the authenticated peer.
2. Success and error frames retain canonical bytes, including install refusal details.
3. Missing service precedes defensive mismatch checks; mismatches never reach a service.
4. Categorized completion logs remain traceback-free and omit credentials.

## Failure model

- Actors: authenticated authority transport clients dispatch either closed request.
- Invariants: operation/type agreement, service availability precedence, bounded wire
  errors, preserved install-refusal evidence and unchanged serialization failure handling.
- Accepted: the internal branch location changes; unsupported operations and begin
  preparation keep existing owners and behavior.
- Covered elsewhere: envelope decoding and authentication remain unchanged; provider
  execution/cancellation and journal state policy remain the service's responsibility.

## Validation

Extend the existing completion matrix with preparation refusal and explicit unknown
completion for both operations. Add shared-route success, absent-service,
AuthorityServiceError/install-refusal, unexpected-error and defensive mismatch cases,
asserting selected method and literal canonical response bytes. Inject opposite request
types at the decoder test boundary because normal envelope decoding already rejects them.
Test serialization failure remains inside translation. Establish these cases green
before consolidation because the requirement preserves current behavior, then compare
the retained handler ASTs and run both transport test files and sender-admission tests.
Controlled routing/error/logging faults must fail relevant tests. Run lint, whole-tree
types, docs and hooks; retain the plan privately and commit locally.
