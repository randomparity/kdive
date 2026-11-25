# Shared core-reader failure classification

## Problem

Dmesg helper imports and core opening occur outside categorized conversion. Missing helpers
can be hidden by remote capture's diagnostic fallback; native opening failures escape the
local fallback. Existing policies distinguish missing dependencies from unavailable dmesg.

## Scope

Implement one shared-reader boundary correction, governed by
[ADR-0857](../../adr/0857-classify-shared-core-reader-failures.md).
In `src/kdive/providers/shared/debug_common/core_file.py`, retain lazy imports and signatures.
Classify missing drgn/printk imports as chained `MISSING_DEPENDENCY`. Preserve the existing
`open_core_program` missing-drgn message; use actionable helper-import/opening messages.
Protect Program construction/core opening with `OSError`, `ValueError`, `drgn.BadDataError`
and `drgn.FaultError`, yielding chained `INFRASTRUCTURE_FAILURE`.
For extraction, replace the broad catch with `OSError`, `ValueError`, `LookupError` and
native `BadDataError`, `FaultError`, `MissingDebugInfoError`, `ObjectAbsentError`,
`OutOfBoundsError`, preserving the existing extraction message and cause.
Update only the local fallback docstring to distinguish categorized operational failures.
Add one shared boundary test module; exercise the actual local fallback and constructed
remote `_HostDumpPersister` fallback without duplicating capture fixtures. Run existing
complete local/remote capture regressions unchanged.

### Failure model

- Actors/deployments: configured workers parse guest core files using pinned drgn 0.3.0.
- Assets/invariants: missing dependencies remain fatal; build-id validation stays fatal;
  successful bytes/sentinel, lazy imports and existing provider fallback policies remain.
  Core bytes cross the native parsing boundary; bounded catches preserve error categories
  and causes without adding raw exception text, privileges or persistence behavior.
- Accepted: unexpected `RuntimeError`/`TypeError` and process-control exceptions propagate
  from shared readers; remote diagnostics retain their existing generic placeholder policy.
  Native invalid-file probes do not establish valid-vmcore or hardware capture support.
- Other owners: existing capture code owns size ceilings, temporary files, redaction,
  provenance and publication; campaign workflow owns final CI/live-tier reporting.

## Success

Missing root/helper imports are categorized through both diagnostic fallbacks. Listed
opening/extraction errors preserve causes; expected diagnostic errors yield the sentinel.
Unexpected shared faults preserve identity. Absent BUILD-ID remains `CONFIGURATION_ERROR`,
and build-id failure prevents persistence. Neither provider's existing fallback is weakened.

## Validation

- focused-test: new shared core-reader tests cover root/helper import, construction/opening,
  extraction, bytes, causes, unexpected faults and both actual fallback methods.
- focused-test: build-id success/missing metadata and remote pre-persistence failure tests
  protect provenance; existing local/remote capture files retain generic fallback coverage.
- focused-test: deliberate dependency-classification regression must fail, then restore.
- focused-test: pinned native drgn missing/empty/invalid core probes verify real boundaries;
  run relevant provider regressions, `just lint`, whole-tree `just type`, and commit hooks.
- task-test-not-applicable: no new transport/assembly path, migration, live VM or valid-core
  support; existing fake-boundary captures plus native invalid-input proof bound this change.
