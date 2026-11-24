# Typed GET metadata failures

## Problem

GET metadata bypasses the existing typed response reader. Missing or malformed Metadata raises
raw KeyError/TypeError; the console sidecar treats those as corrupt content and resets to ZERO.
The same rejected response leaves its already-open unread body without a receiving owner.

## Scope

Keep parsing in ObjectStore._open_get, shared by buffered and streaming callers (C1).
Use existing _StoreReply for required Metadata dict and sensitivity/retention-class strings.
Retain invalid sensitivity enum classification and its existing diagnostic. Empty retention
strings remain accepted; this repair adds no retention policy. Existing response-reader field
context supplies actionable failures instead of allowing parser exceptions into consumers.

On metadata CategorizedError, attempt to close the rejected response body before rethrowing
that error (C2). An ordinary close failure adds a static operation/type-only note; no raw
cleanup diagnostics. No new helper, caller migration or compatibility path is needed.
Reusing the boundary avoids widening sidecar catches or duplicating GET validation.
No ADR is needed to restore the existing ADR0054/0400 typed failure and ownership contracts.

### Failure model

The configured synchronous S3 client returns its usual response mapping and body. Missing or
wrong-shaped metadata and invalid sensitivity are infrastructure failures, not corrupt sidecar
content. Broken close cannot guarantee resource release; preserve the primary error with a
bounded note. Control BaseExceptions propagate. Top-level nonmapping SDK returns and malformed
Body protocols remain outside this metadata repair; valid-read lifecycle stays with current
callers. No public, persisted, permission, concurrency or deployment behavior change.

## Success

Buffered and streaming reads reject malformed required metadata through CategorizedError.
Actual sidecar reads propagate that failure and preserve valid nonzero rotation state (C1/C3).
Rejected bodies receive close attempts; ordinary close faults retain primary classification
without leaking cleanup values. Genuine stale/corrupt-body ZERO behavior remains intact (C2/C3).
Commit locally, resolve the exact finding and continue the queue (C5).

## Validation

focused-test: extend existing store malformed-response tests with both read modes, missing/
null/list metadata and missing/wrong-type nested strings; verify unread-body close and ordinary/
control close-failure precedence. Extend sidecar tests through actual ObjectStore, including
valid nonzero state. Require meaningful RED before repair and restored controlled faults.
Run existing store/malformed/sidecar suites, including disposable S3 round-trips when available;
lint, whole-tree types, hooks, unrelated AST/signature and198SQL protection (C4).

task-test-not-applicable: no prose/private-helper-count tests or hardware proof. Existing valid
GET request/transport/streaming and sidecar stale/corrupt tests supply preservation evidence.
Scoped design/scope/code/security reviews and final campaign gates retain their owners (C5).
