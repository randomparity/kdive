# Accepted object-stream cleanup

## Problem

The stream context manager closes its body in an unguarded finally. A close fault replaces a
consumer or categorized read failure; a standalone close fault escapes the store taxonomy.
Actual current ObjectStore probes reproduce both failures (C1).

## Scope

Keep accepted-body ownership in get_artifact_stream. Track its own primary BaseException
explicitly, rethrow it, and always attempt close (C1/C2). Ordinary close failures append only
static phase/type context to an active primary. With no primary, map close through existing
_infrastructure_error using phase "get_object body close" and fromNone to suppress raw cleanup
context. Widen the private helper's exception annotation honestly; existing SDK mappings stay
unchanged. The primary's identity, category, cause, terminal flag and existing notes survive.

Reuse current owners; no caller migration or compatibility shim. ADR0860 refines ADR0400's
close-on-exit policy. Buffered reads and rejected-metadata cleanup retain their existing paths.

### Failure model

The configured SDK returns its usual mapping/body. Ordinary read/consumer failures and control
BaseExceptions remain primary under ordinary close failure. A control exception from close
propagates unchanged. A failed close cannot guarantee resource release; no retrying close loop.
Standalone close still fails the operation, now categorized. Outer handled exceptions are not
stream failures: sys.exception can inherit unrelated context, so tracking is explicit.
No new response/Body validator, buffering, limits, public/schema, permission or concurrency policy.
Libvirt's different best-effort capture cleanup remains governed by ADR0859.

## Success

Combined consumer/read and ordinary close failures preserve the initiating exception; a lone
ordinary close error is infrastructure failure without raw close diagnostics (C1).
Normal close/streaming/request/metadata behavior and control propagation remain intact (C2).
Commit locally, attribute the exact finding and continue the originating I/O batch (C4).

## Validation

focused-test: extend tests/store/test_objectstore.py for read/consumer/control primaries with
close faults, standalone close classification, bounded notes/tracebacks, successful close and
an outer handled exception during otherwise successful consumption. Require baseline RED and
controlled regression/exact restoration. Run Dockerrequired just test-verbose for that file,
tests/store/test_objectstore_malformed_response.py and tests/artifacts/console/test_sidecar.py,
including actual S3 round-trips. Run lint, whole-tree types/hooks and protect unrelated AST,
public signatures and198SQL (C3).

task-test-not-applicable: no prose or callback-count snapshots. Existing valid-stream/request
and rejected-metadata tests supply preservation evidence. No VM/hardware claim. Scoped reviews,
remaining I/O outcome and final campaign gates retain their existing owners (C4).
