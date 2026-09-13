# 0651 — Handler tests use worker-role pools

## Status

Accepted (2026-09-13)

## Context

Job handlers run with the `kdive_worker` database role, but handler tests have commonly passed a
pool connected by `migrated_url`, which is the migration owner. That owner bypasses grants, so a
handler write that production rejects can pass in the test suite. The handler tree contains 34
test modules; its conversion needs an explicit classification, because a text match cannot tell a
seed connection from a handler act connection.

Issue #2347 requires handler-test setup to retain owner access while each handler invocation uses
the real worker LOGIN fixture introduced by #2346. LOGIN roles are cluster-global, so creating a
principal per converted test would queue behind the global role lock under xdist.

## Decision

Create one uniquely named LOGIN set per xdist worker session after that worker's migrated schema
exists. Acquire the existing maintenance-database cluster-global role lock around both CREATE ROLE
and DROP ROLE, because another xdist worker can migrate while this worker creates its principal.
Build a function-scoped DSN adapter from the current `migrated_url`, and open a function-scoped
`kdive_worker_pool` from it for the act phase. This keeps each test database in its DSN while
limiting cluster-global CREATE/DROP ROLE work to once per worker session.

Keep `migrated_url` connections for setup and post-condition observation only. Tests either pass
the role DSN explicitly to their act helper or bind it in a module-local fixture used only by the
act helpers; neither form retains an owner-execution fallback. A checked 34-module inventory
records each test module's worker act paths or the reason it is owner-only.

Replace owner-backed handler pools in the affected handler-test files; do not retain an
owner-execution compatibility path for their handler calls.

## Consequences

Converted tests exercise the same database grant boundary as the worker process. Existing seed and
inspection helpers can continue to use the migration owner. Test signatures and helpers become
explicit about the owner/worker boundary, and a missing worker grant fails the relevant test. A
focused lifecycle test proves role creation and teardown take the existing cluster-global lock.
If conversion finds an unrecorded missing grant, this branch is parked with its failing evidence;
reporting the leak does not make a red handler test mergeable.

## Considered & rejected

- **Keep owner-backed handler pools and add a separate privilege test.** verified: issue #2347
  documents that the production failure passed owner-backed handler tests; a separate test cannot
  make each handler's normal act path observe its own grants.
- **Create a worker LOGIN in every handler test.** verified: issue #2347 identifies the
  cluster-global role lock and its xdist queue; one login set per xdist worker avoids that queue.
- **Add an owner-role compatibility switch to each handler helper.** judgment: it preserves the
  blind execution path the conversion is intended to remove.
