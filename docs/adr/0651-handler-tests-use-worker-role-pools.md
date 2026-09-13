# ADR-0651: Handler tests use worker-role pools

## Status

Proposed

## Context

Job handlers run with the `kdive_worker` database role, but handler tests have commonly passed a
pool connected by `migrated_url`, which is the migration owner. That owner bypasses grants, so a
handler write that production rejects can pass in the test suite.

Issue #2347 requires handler-test setup to retain owner access while each handler invocation uses
the real worker LOGIN fixture introduced by #2346. The fixture creates cluster-global roles, so
the conversion must avoid per-test principal creation.

## Decision

Use the shared `kdive_worker_pool` fixture for the act phase of converted handler tests. Keep
`migrated_url` connections for setup and post-condition observation only. Test helpers that need
both phases accept distinct owner and worker connections/pools rather than hiding role selection.

Replace owner-backed handler pools in the affected handler-test files; do not retain an
owner-execution compatibility path for their handler calls.

## Consequences

Converted tests exercise the same database grant boundary as the worker process. Existing seed and
inspection helpers can continue to use the migration owner. Test signatures and helpers become
explicit about the owner/worker boundary, and a missing worker grant fails the relevant test.

## Considered & rejected

- **Keep owner-backed handler pools and add a separate privilege test.** verified: issue #2347
  documents that the production failure passed owner-backed handler tests; a separate test cannot
  make each handler's normal act path observe its own grants.
- **Create a worker LOGIN in every handler test.** verified: issue #2347 identifies the
  cluster-global role lock and its xdist queue; #2346 already supplies a shared fixture.
- **Add an owner-role compatibility switch to each handler helper.** judgment: it preserves the
  blind execution path the conversion is intended to remove.
