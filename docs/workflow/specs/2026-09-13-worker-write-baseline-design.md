# Worker handler write baseline

Issue: [#2345](https://github.com/randomparity/kdive/issues/2345). Decision:
[ADR-0649](../../adr/0649-worker-handler-write-baseline.md). Plan:
[2026-09-13-worker-write-baseline.md](../plans/2026-09-13-worker-write-baseline.md).

## Problem

The worker role can execute handler paths that an owner-privileged test connection cannot prove.
The grant migrations name permitted tables, but no durable artifact maps registered handlers to
their reachable writes and the authority path that performs each write.

## Scope

Add a versioned JSON baseline under `tests/jobs/`, one record per active `JobKind`, and a focused
structural test. A write record has a stable `id`, `table`, `operation`, `handler_source`,
`write_source`, `role`, `route`, `authority_source`, `grant_source`, and `verdict`. Every source
is a repository-relative `file:line` plus an expected text fragment. `handler_source` identifies
the registered handler path; `write_source` identifies the table statement; `authority_source`
identifies either its direct table path or `SECURITY DEFINER` function; and `grant_source`
identifies the table or function grant. `route` is `direct` or `security-definer`; `verdict` is
`covered`, `definer-mediated`, or `LEAK`. A no-write handler has an explicit empty `writes` list.
The top-level sorted `confirmed_leaks` list must equal the identities of `LEAK` rows, so `[]` is a
durable zero-leak result. Every write row has `role: kdive_worker`; a non-worker role is invalid
because server and reconciler paths are excluded. The guide cites the worker-grant migrations and
names the audit's handler-only boundary.

The implementation is analysis-only. It changes no runtime source, database schema, role grant,
handler behavior, database connection, or migration. A discovered `LEAK` is recorded and handed
to its remediation owner; this issue does not repair it.

## Failure model

- A newly active handler is omitted: the test compares manifest job kinds to the active job-kind
  set and fails.
- A baseline row is duplicated, unsorted, malformed, or points at moved handler, write, authority,
  or grant evidence: the test fails with the offending record.
- A direct write is misclassified as grant-covered or definer-mediated: review resolves the
  handler call path, direct grant or `SECURITY DEFINER` function, and grant-matrix evidence before
  it enters the manifest.
- A source change adds a new write within an already-classified handler: this baseline preserves
  a reviewable starting point; subsequent structural grant work owns broader call-graph drift
  detection.

## Security model

**Boundaries added or widened.** None. The manifest is repository test data, read only by tests;
it is not loaded by a worker or database connection. Existing worker-role and `SECURITY DEFINER`
boundaries remain unchanged.

**Actors and controls.** A contributor can alter the manifest or handler source in a pull request.
The focused test binds each row to a source location and requires every active handler to be
classified. Code review resolves the role and grant verdict against migrations 0107, 0114, 0117,
0118, 0121, 0126, 0138, and 0152. Invalid JSON or an invalid source reference fails locally and
in CI without executing SQL.

**Out of scope.** This does not prove a live database's grants, audit server or reconciler paths,
or alter a `LEAK`. Those require their owning work items and role-bound integration tests.

## Success

1. Every active worker handler has exactly one baseline entry, including explicit no-write cases.
2. Every handler-reachable write in the recorded sweep has its identity, table, operation,
   handler, write, authority, and grant evidence, role, route, and verdict recorded.
3. The baseline is valid JSON with a declared format version, stable ordering, and no duplicate
   handler or write identity.
4. The focused test rejects malformed data, missing handler coverage, duplicate identities,
   non-worker roles, mismatched leak lists, and any handler, write, authority, or grant evidence
   that no longer carries its recorded fragment.
5. The guide identifies the grant-matrix evidence, `SECURITY DEFINER` interpretation, scope, and
   refresh procedure.
6. No production code or migrations change.

## Validation

The focused baseline module first proves each structural rejection against an in-memory mutated
copy of the committed manifest, then proves the committed file. `just lint`, `just type`, and the
focused module check formatting, whole-tree types, and the contract; `just ci` is the full gate.
