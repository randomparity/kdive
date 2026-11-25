# Complete local teardown storage enumeration

## Problem

The local storage fence silently accepts os.walk directory failures. Its whole-walk
FileNotFoundError handler also stops after one vanished file, omitting remaining regular files.
Actual temporary-filesystem probes reproduce both failures before destructive admission.

## Scope

Keep enumeration in the existing `_system_storage_paths` owner (C1/C2). Add a nested os.walk
onerror callback: permit FileNotFoundError only when its filename equals the supplied baseline
root; propagate other root/nested/ambiguous errors. Remove the whole-walk absence handler.
Catch individual-file FileNotFoundError around stat and continue with the remaining entries.
Retain missing-overlay handling, regular-file checks, no-follow traversal and the4096limit.

No owner, caller or compatibility migration is needed. Reusing the existing os.walk callback is
smaller than a new walker or ownership service. A precheck alone introduces another observation
without detecting later directory errors; retaining the outer catch could still hide them.
The existing complete fail-closed proof contract needs repair, not a new ADR.

### Failure model

Unreadable or incompletely observed storage must stop the attachment fence. Exact missing root
and individually vanished files are intended absence cases; unknown directory disappearance is
not successful proof. This does not promise atomic enumeration against arbitrary concurrent
writers. Keep connection cleanup, identity/XML proof, lease/session lifetime and all downstream
error mapping with their current owners. No public, permission, concurrency or schema change.

## Success

Root/nested I/O failures raise before the actual provider can destroy, undefine or remove files;
its retained intent survives. One vanished file does not omit other existing files. Absence,
regular-file filtering, no-follow and size-bound contracts remain intact (C1-C3).
Commit locally, resolve the exact finding and continue the native queue (C5).

## Validation

focused-test: extend existing local `test_session.py` with real filesystem and injected scandir
faults, root/nested/ambiguous absence cases, disappearing-file continuation and existing safety
bounds. Extend the current retained-identity fence test in `test_system_authority.py` using the
actual proof callback. Require meaningful RED before source changes and controlled regression
then exact restoration. Run both files plus existing composition and shared/remote attachment
regressions; lint, whole-tree types and hooks. Preserve unrelated AST/signatures and198SQL (C4).

task-test-not-applicable: no tests of design prose or callback call counts. No live VM/hardware
proof is required for this filesystem enumeration repair. Scoped design/scope/code/security
reviews and final enclosing campaign gates remain required under C5.
