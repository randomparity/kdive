# 0011 — Overlapping registered secrets can leave a suffix unredacted

## Status

Open
review-by: 2026-10-04

## Concern

`Redactor` snapshots registered values as an unordered `frozenset` and replaces each value in
sequence (`src/kdive/security/secrets/redaction.py:63-78`). When one registered secret is a prefix
of another, replacing the shorter value first prevents the longer match and can leave its suffix
visible. For example, registered values `west-` and `west-privatevalue` can turn the latter into
`[REDACTED]privatevalue`.

Issue #2175 adds a bounded external-boot command-line mismatch diagnostic. Its authority-failure
renderer correctly invokes `Redactor` before escaping and persists the result
(`src/kdive/jobs/handlers/external_boot/runner.py:304-319`), but it cannot repair a partial
replacement the shared redactor already produced. The surviving suffix can therefore enter the
versioned `failure_context` and its project-scoped job response.

The production external-boot admission lane is not currently wired, so the security review found
no present tenant-to-diagnostic path. The defect is nevertheless a dependency of enabling that
lane because the diagnostic's contract requires registered values to be redacted before durable
persistence.

## Why deferred

#2175 owns the new command-line observation, comparison, and bounded diagnostic carrier. Changing
the process-wide redaction algorithm would alter every log, response, transcript, and artifact
consumer of `Redactor`, not only this diagnostic. That shared security-component change needs its
own regression inventory and review rather than being hidden inside a provider-readiness change.

## Non-regression boundary

- The #2175 diagnostic continues to redact before deterministic escaping and bounding, suppresses
  the raw-byte exception chain, and never persists the raw observation.
- The diagnostic remains unreachable from the production external-boot lane until admission and
  acknowledgement assembly is added. The enabling change must treat this record as a prerequisite
  or prove an equivalent overlap-safe redaction control.
- Tests for #2175 use distinct registered values; they must not claim overlap-safe behavior from
  those fixtures.

## What would resolve it

Make registered-value redaction independent of iteration and replacement order, preserving every
match in the original text even when registered values overlap. Add focused tests for both prefix
orders and a diagnostic persistence test proving that no suffix of the longer registered value
reaches stored `failure_context` or its response projection. Re-run the existing logging,
transcript, response, and artifact redaction suites to establish that the shared change does not
weaken their contracts.

## Provenance

target: src/kdive/jobs/handlers/external_boot/runner.py
target: src/kdive/security/secrets/redaction.py
Found by the issue #2175 `$detect-evil` pass on 2026-09-04
(`run_id: 2175-security-1788556171476-8924d958`, medium finding). Static inspection traced the
partial replacement through the new authority diagnostic into persistence while also confirming
that the production external-boot lane is not yet wired.
