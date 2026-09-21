# Overlapping literal secret redaction

## Problem and scope

Issue #2622 requires the union of secret-match spans in the original text to be
masked. Sequential replacement destroys overlapping matches; sorting values by
length cannot fix partial or self-overlap. Debt 0011 records the persistence risk.

The existing `Redactor` remains the sole owner. Its logging, response, transcript,
artifact and external-boot callers require no migration. ADR-0027's redaction
contract and ADR-0327's explicit registry ownership remain in force; this repairs
their implementation rather than introducing an architectural decision.

Exclusions approved by the operator on 2026-09-21 through the #2622 campaign:

- External-boot admission or lifecycle behavior beyond redaction regression proof:
  external-boot maintainers / separate issue.
- New redaction API or dependency: future security design.
- Registry lifetime or snapshot redesign: secret-registry maintainers / future ADR.

## Global constraints

Python 3.14 with uv. No new dependency or public interface. Preserve x86_64 and
ppc64le support. Preserve registry snapshot/lifetime behavior, secret-name masking,
recursive redaction, and diagnostic escaping/bounds. Plans stay uncommitted.

## Design

In `redact_text`, locate each nonempty snapshotted/explicit literal value against
the original input using `str.find`, advancing by one character after a match to
retain overlapping occurrences. Sort the resulting half-open spans and merge
strict overlaps. Render unchanged slices and one existing `[REDACTED]` marker per
merged span; never search inserted markers for literal values. Keep adjacent
non-overlapping matches separate, preserving current marker expansion and the
external-boot 8192-byte rendering bound. Apply the existing secret-name pattern
pass afterwards, with recursive handling and constructors unchanged.

This is a local stdlib implementation. Longest-first replacement still leaks for
`abc`/`bcd` in `abcd`; a new matcher dependency would add unnecessary surface.
For M matches, span storage is O(M) and sorting O(M log M), plus literal searches
and output. No new input limits or truncation policy is introduced.

## Success and validation

- C1–C2 (#2622): exact expected output for permutations of prefix, containment,
  partial overlap, self-overlap, repetition, adjacent matches, and marker-content
  values. Exercise registry-only, explicit-only and mixed sources deterministically.
- C3 (#2622, Debt 0011): the existing real-Postgres authority-failure test uses
  overlapping registered values, checks exact persisted `failure_context` and
  `ToolResponse.from_job` projection, and retains exception-chain checks.
- C4 (#2622): existing secret-key, recursive, snapshot/lifetime, logging-cache and
  diagnostic escape/byte-bound tests remain green.
- C5 (#2622, Debt 0011): run redaction/logging/transcript/response/artifact
  regressions, then append resolution evidence and change only Debt 0011's status.

Focused red tests precede the implementation. Run focused pytest, `just lint`,
`just type`, assembled consumer regressions, staged `prek run`, and `just records
origin/main`. The installed pre-push hook owns the final full `just ci` run.
No VM behavior changes; the existing real database diagnostic path supplies the
end-to-end persistence proof. Reverting the implementation restores the known leak;
there is no schema migration or persisted-data rewrite.

## Failure model

- Actors and deployments: authenticated tenants and worker/provider output across
  the server, worker and reconciler; operator/CI test execution on supported hosts.
- Invariants and assets at stake: no original character covered by a known literal
  secret match reaches rendered output; scoped credentials retain current lifetime;
  external-boot diagnostic storage and response projection retain escaping/bounds.
- Accepted failure classes: transformed or unknown secrets cannot be matched by
  this exact-literal contract; existing callers own input sizing and resource budgets.
- Covered elsewhere: admission/lifecycle policy belongs to external-boot maintainers;
  registry lifetime and registration ordering remain under ADR-0027/ADR-0327.

## Threat model

- Boundary inventory: guest/provider text crosses into logs, returned values,
  transcripts, artifacts and external-boot failure records. No boundary is added
  or widened; literal matching is repaired at the existing shared control.
- Actor model: tenants and guests may influence text containing known credentials;
  process composition owns the registry and supplies explicit values.
- Control per boundary: `Redactor` masks known original literal spans and existing
  secret-name patterns; the diagnostic renderer subsequently escapes and bounds
  text before database persistence and project-scoped response projection.
- Out of scope: encoded/unknown secrets and registration timing redesign are
  outside exact matching; raw artifact access authorization and external-boot
  lifecycle authorization remain their existing owners' responsibilities.
