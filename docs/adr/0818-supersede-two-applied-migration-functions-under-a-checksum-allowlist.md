# 0818 — Supersede two applied migration functions under an exact checksum allowlist

## Status

Accepted (2026-11-16)

Amends [ADR-0015](0015-sql-migration-runner.md) (byte-immutability of applied migrations, enforced
by its runner checksum and authoring-time guard).

## Context

PostgreSQL 17.12 refuses `ALTER TABLE … ADD COLUMN … DEFAULT` on a table whose row type is used by
a new-style SQL function body. Migration 0112 defines two such functions over
`capture_operations` and `worker_incarnations`, and 0113 adds a defaulted column to
`capture_operations`. So a fresh install on 17.12 stops at 0113 (#3612). ADR-0015 makes 0112
byte-immutable, and no new migration can run between 0112 and 0113.

## Decision

- 0112 is edited once. Its two functions change from `RETURN <expr>` to
  `AS $fn$ SELECT <expr> $fn$`. The expression, signature, volatility and `search_path` are
  unchanged.
- `src/kdive/db/supersessions.py` (stdlib only) lists each permitted edit as
  `(version, filename, superseded_sha256, current_sha256, decision)`. It holds one entry, for
  0112.
  - The runner accepts a recorded checksum only when it equals the file's, or when the pair
    equals that version's exact `(superseded, current)` entry. It leaves the recorded row
    untouched.
  - The immutability guard allows a modification only when the base blob and the working file
    hash to that exact pair.
- Migration 0199 runs `CREATE OR REPLACE` on both functions with the string bodies, so
  databases that applied the original 0112 lose the row-type dependency too. Grants are
  preserved.
- A DB test asserts that, after the full chain, no non-system new-style SQL function has a table
  row type among its arguments (including `OUT`), its return type, or their array elements.
  The DB-test Postgres image is pinned at 17.12+, so the fresh chain runs on the strict
  version.

Each future supersession needs its own ADR and entry. The allowlist never admits a version
without an exact byte pair.

## Consequences

- Fresh installs work on 17.12+. Existing databases keep their recorded 0112 checksum forever,
  so the entry can never be removed.
- 0112 now has two valid byte histories. Reading the file shows the current one; the old one
  survives only as its SHA-256 and in git history.
- The runner and the guard share one stdlib module, and the guard imports it from `src/`
  without a venv.
- A database that applied 0112 but not 0113 still cannot migrate on 17.12+. No release ships
  that state.
- New-style SQL bodies remain allowed where they use no table row type.

## Considered & rejected

- **Re-baseline the chain for fresh installs.** judgment: complexity. An equivalence proof
  over 190 files, plus fresh-versus-existing detection in the runner, to fix two functions.
  Rejected by the operator in favour of this route.
- **Runner hook around 0113.** judgment: fit. The runner would carry migration-specific DDL and
  grants, a second schema source outside `schema/`.
- **Insert a sub-version migration between 0112 and 0113.** judgment: fit. It needs a new
  version grammar in the runner, `schema_migrations` and the ordering guard, and it applies an
  old-numbered file late on existing databases.
- **Keep the 17.10 pin (do nothing).** verified: https://www.postgresql.org/docs/release/17.12/
  lists the tightened composite-type rule. Applying `src/kdive/db/schema/*.sql` one file at a
  time to `postgres:17@sha256:0d938e97…` (17.12-1.pgdg13+1, at commit d0f52ad33) fails at 0113
  with `cannot alter table "capture_operations" because function
  capture_launch_abort_evidence_valid(capture_operations,jsonb) uses its row type`. So any
  operator-run 17.12+ server cannot install.
- **Rewrite the record's checksum on upgrade.** judgment: fit. It would mutate history in
  `schema_migrations` to save one permanent allowlist row.
