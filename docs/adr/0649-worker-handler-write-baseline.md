# 0649 — Test-owned baseline for worker-handler database writes

## Status

Accepted (2026-09-13)

- **Issue:** #2345

## Context

`kdive_worker` has deliberately narrower table privileges than the owner connection used by
most handler tests. A handler-reachable repository write can therefore appear healthy until it
runs on the real worker role. The current grant migrations document privileges, but do not
record the handler paths that consume them.

#2345 is an analysis slice. It must make the current sweep reviewable without changing a role,
schema, runtime handler, or remediation path. The baseline must include handlers with no writes,
so a newly registered handler cannot be silently outside the audit.

## Decision

Store a versioned JSON manifest under `tests/jobs/` and validate it with a structural test. Each
registered job kind has one entry. Every reachable database write records a stable identity, the
table, operation, handler-call source, write source, connection role, direct or `SECURITY
DEFINER` authority source, grant source, and grant verdict: `covered`, `definer-mediated`, or
`LEAK`. The top-level confirmed-leak list must equal the sorted identities of all `LEAK` rows;
an empty list is the explicit zero-leak result. Every write row's connection role is exactly
`kdive_worker`; other roles are outside this baseline.

The structural test validates the manifest shape, unique and sorted entries, complete active
handler-kind coverage, leak-list reconciliation, and each cited evidence location. A contributor
guide explains the sweep boundary, grant-matrix sources, and how to refresh a record after an
intentional handler-write change.

## Consequences

- The baseline is executable test data, not a runtime registry or source of authorization.
- A moved or altered handler, write, authority, or grant evidence location makes the focused test
  fail until its evidence is refreshed.
- A new active job kind makes the focused test fail until it is explicitly classified, including
  a no-write entry.
- The first baseline may report no additional `LEAK`; remediation remains owned by a separate
  issue when one is found.
- This ADR becomes Accepted only with the manifest, test, and guide in the implementation PR.

## Considered & rejected

- **Markdown-only inventory.** verified: the #2345 completion criteria require a durable
  baseline later structural work can assert; prose cannot check that every active handler was
  classified or that a cited source still contains its write.
- **Runtime registry beside the handlers.** judgment: adding a production artifact to describe
  an analysis result would widen the runtime surface without changing worker authorization.
- **Change grants while recording the audit.** verified: #2345 explicitly excludes production
  code and migrations; a `LEAK` is evidence for its remediation issue, not authority to widen a
  role here.
