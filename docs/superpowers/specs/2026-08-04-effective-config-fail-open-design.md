# Effective-config fail-open boundary design

## Goal

Make kernel-config advisories honor their existing total, fail-open contracts. Ordinary dependency
or implementation faults must not turn install, vmcore, debug, or complete-build operations into
failures. Task cancellation and process-level control exceptions must still propagate.

## Root cause

`load_effective_config` promises that unreadable or untrusted config returns `None` and never fails
its caller, but its exception boundary names only `CategorizedError`, `psycopg.Error`, and
`OSError`. The injected store factory and `ConfigStore.get_artifact` protocol do not restrict their
exceptions to that list. The parser also runs outside the boundary. A reproducible `RuntimeError`
from the store factory therefore escapes every fail-open consumer.

The sibling `missing_effective_config_nudge` directly reads artifact presence even though it is an
advisory on a successful build. An unexpected lookup error can likewise replace that success with a
failure. When presence cannot be established, the safe result is no nudge, not a claim that the
artifact is absent.

## Design

Wrap the complete effective-config read in one `except Exception` boundary: artifact-key lookup,
store construction, object fetch, parsing, and the degenerate decision. On an ordinary exception,
log a warning with the Run id and traceback, then return `None`. Keep the existing distinct warning
for a successfully parsed but degenerate config. Remove exception-type imports made obsolete by the
total boundary.

Wrap the nudge's presence lookup in its own advisory `except Exception` boundary. Log the Run id and
traceback, then return `None`. Preserve the existing behavior for a successful lookup: return no
nudge when a key exists and the current remediation payload only when absence is proven.

Do not catch `BaseException` or use a bare `except`. On Python 3.14, `asyncio.CancelledError`
inherits from `BaseException`; catching `Exception` therefore preserves cancellation as well as
`KeyboardInterrupt` and `SystemExit`.

This broad catch is intentionally confined to two advisory reads. It does not change errors in
state transitions, writes, or required operations. It follows the existing never-raise patterns in
artifact etag repair and advisory artifact discard.

## Tests

Add RED regressions showing unexpected store-factory and `get_artifact` exceptions currently
escape the loader. Assert the fixed path returns `None`, emits one warning naming the Run, and logs
traceback information. Cover an unexpected key-lookup failure to prove the DB half of the same
boundary. Characterize that cancellation still propagates without a fail-open warning.

Add a nudge regression where the key lookup raises unexpectedly: it must return `None` and log the
fault. Add its cancellation characterization. Keep existing no-row, valid config, typed store
error, degenerate config, and nudge payload behavior unchanged.
