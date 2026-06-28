# Issue 895 Composite Failure Details Design

## Problem

`runs.build_install_boot` wraps build, install, and boot failures in
`CompositePhaseError`. The wrapper keeps the failure category and message but replaces
the original `CategorizedError.details` with only `{"failed_phase": phase}`. Job failure
context therefore loses structured diagnostics such as dropped config symbols or artifact
references that the phase handler already produced.

## Contract

When a composite phase raises `CategorizedError`, the wrapper must preserve the cause's
structured details and add `failed_phase`. If the cause is not categorized, the wrapper
continues to use `infrastructure_failure` with only `failed_phase`.

If a cause already includes a `failed_phase` detail, the wrapper's actual phase wins. The
outer wrapper owns that key because it describes where the composite sequence stopped.

## Implementation

Change `CompositePhaseError.__init__` to build details from the categorized cause details
when present, then set `failed_phase`. Keep the existing public behavior:

- message remains `"{phase} phase failed: {cause}"`;
- category remains the cause category for `CategorizedError`;
- non-categorized causes remain infrastructure failures.

## Testing

Add a unit regression test in `tests/jobs/handlers/runs/test_composite.py` that constructs
a categorized phase error with structured details and verifies the composite wrapper
contains both the original detail keys and `failed_phase`. Keep the existing test that
proves the phase marker is present.
