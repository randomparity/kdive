# PostgreSQL 17.12 fresh-install migration chain (#3612) — design

## Problem

PostgreSQL 17.12 (2026-11-12) rejects `ALTER TABLE … ADD COLUMN … DEFAULT` on a table whose row
type is used by a new-style SQL function body (`RETURN …` or `BEGIN ATOMIC`). Migration 0112
creates two such functions:

- `capture_launch_abort_evidence_valid(capture_operations, jsonb)`
- `capture_recovery_authorized(worker_incarnations, worker_incarnations)`

Migration 0113 then adds a defaulted column to `capture_operations`. On 17.12 the fresh chain
stops there with `cannot alter table "capture_operations" because function
capture_launch_abort_evidence_valid(capture_operations,jsonb) uses its row type`. #3610 pinned
the Postgres image to 17.10 to defer the break. Operators running their own 17.12+ server cannot
install, and existing databases cannot safely gain a defaulted column on either table.

ADR-0015 makes applied migrations byte-immutable, through the runner's checksum check and
`scripts/guards/schema_immutable_guard.py`. A forward migration alone cannot help a fresh
install, because 0113 runs before any new file.

Probe evidence (PostgreSQL 17.12, `postgres:17@sha256:0d938e97…`):

- The unmodified chain fails at 0113 and nowhere else.
- With the two 0112 functions rewritten as string bodies (`AS $fn$ SELECT … $fn$`), all 190 files
  apply.
- `CREATE OR REPLACE` of a new-style function as a string body clears the dependency and keeps
  its ACL.
- A row-type return type, an array of a row type (parameter or return), an `OUT` row-type
  parameter, and a `BEGIN ATOMIC` body all trigger the same rejection. A row-type parameter the
  body never reads does not, and neither does a body that only queries the table.
- Source: PostgreSQL 17.12 release notes, https://www.postgresql.org/docs/release/17.12/
  ("Tighten the rules about whether `ALTER COLUMN TYPE` is allowed when dependencies exist on the
  composite type").
  The `ADD COLUMN … DEFAULT` case is not in the notes; it was reproduced with
  `CREATE TABLE a (id int, s text); CREATE FUNCTION fa(p a) RETURNS boolean LANGUAGE sql RETURN
  p.s IS NOT NULL; ALTER TABLE a ADD COLUMN x text DEFAULT (1)::text;`.

## Design

Decision record: [ADR-0818](../../adr/0818-supersede-two-applied-migration-functions-under-a-checksum-allowlist.md),
which amends ADR-0015.

1. **0112 edit.** Only the two functions change: `RETURN <expr>;` becomes
   `AS $fn$ SELECT <expr> $fn$;`. The expression text, parameters, return type, `IMMUTABLE`,
   `SET search_path = ''`, and every other byte of the file stay unchanged. Callers
   (plpgsql at 0112/0113) resolve at run time and see identical results.
2. **Supersession allowlist.** A new stdlib-only module, `src/kdive/db/supersessions.py`, holds
   `SUPERSESSIONS`. That is a tuple of frozen `Supersession(version, filename, superseded_sha256,
   current_sha256, decision)`, with exactly one entry, for 0112.
   - `apply_migrations` accepts a recorded checksum when it equals the file's, or when
     `(recorded, file)` equals the entry's `(superseded, current)` pair for that version.
     Anything else raises the existing `checksum changed` error. The recorded row is never
     rewritten.
   - `schema_immutable_guard.py` adds `<root>/src` to `sys.path` and imports the module.
     It permits an `M` on a schema file only when the base blob's SHA-256 equals
     `superseded_sha256` and the working file's SHA-256 equals `current_sha256`. Every other
     change keeps its current verdict. An `M` on a file that has an entry but does not match it
     (a branch cut before the supersession merged, now behind the new base) gets the existing
     "merge the base in" hint. A failed import of the module is a `GuardError` (exit 1 with a
     message), not a traceback.
3. **0199 forward migration** `0199_capture_predicates_string_bodies.sql` runs
   `CREATE OR REPLACE` on both functions with the same string bodies. On a fresh install this is
   a byte-equivalent redefinition. On an existing database it removes the row-type dependency
   while keeping grants.
4. **Hazard guard.** A DB test asserts that after the full chain, no function outside
   `pg_catalog` and `information_schema` has a new-style body (`prosqlbody IS NOT NULL`) whose
   argument types (including `OUT`/`TABLE` columns, `proallargtypes`), return type, or the
   element type of any of those arrays is an ordinary or partitioned table's row type. A second
   DB test proves the query flags the array and `OUT` shapes. The DB fixture moves to 17.12
   in the same change, so any future fresh-chain break also fails every DB test.
5. **Digest move.** The `postgres:17` digest becomes
   `sha256:0d938e975177bef9f1f61f3f9dbcf2e0aa0a139568f66d3415ed8892eb97f6ac` (the OCI index;
   every platform carries `PG_VERSION=17.12-1.pgdg13+1`). It changes in `docker-compose.yml`
   (both services), `tests/compose/fixtures/worker-lifecycle-live.yml` (both services) and
   `tests/db/conftest.py`, `scripts/pull-test-images.sh` and `deploy/helm/kdive/values.yaml`
   (`demo.postgres.image`). Those last two are required consequences:
   `tests/guards/test_prepull_images_match_fixtures.py` and
   `tests/guards/test_fixture_image_pins.py` fail on any drift from the fixture pin. The move
   lands in the same commit as the 0112 edit, because the fresh chain on 17.12 fails without it.
6. **ADR-0015** gains one header line: `Amended by ADR-0818`.

## Failure model

1. **Actors and deployments.**
   - Operator-run migrator: a startup daemon, `apply-migrations.sh`, or the compose
     `migrate` one-shot, against PostgreSQL 14+ (ADR-0015 floor).
   - CI DB tests on the pinned image.
   - Developers authoring migrations.
2. **Invariants and assets at stake.**
   - Every database that applied the original 0112 keeps migrating. Breaking that is a fleet
     outage.
   - The checksum check still rejects every unreviewed edit.
   - The two functions return the same values for the same inputs.
   - Grants on the two functions survive.
3. **Accepted failure classes.**
   - A database that recorded 0112 but not 0113, on 17.12+: the fix cannot apply, because 0113
     runs before 0199. No release tag ships 0112 without 0113 (v0.4.0 ends at 0075, v0.5.0 at
     0196), and the runner applies the whole chain in one transaction. Only a dev database
     migrated at main between 2026-08-13 and 08-14 is exposed. Remedy: recreate it.
   - A new-style function that depends on a row type through something the query does not
     inspect (for example, a domain over a composite type). PostgreSQL 17.12 is the reference
     behaviour, and the fresh-chain run on the 17.12 fixture catches any such break.
   - A contributor who adds a supersession entry together with its matching edit passes both
     checks. That is the mechanism: each entry is reviewed code, and ADR-0818 requires its own
     ADR, which is the same control every migration already has.
   - A test backend older than 17.12 (`KDIVE_TEST_PG_URL`) cannot show the rejection. The
     upgrade test asserts the ALTER rejection only when `server_version >= 170012`, and asserts
     the catalog state on every version.
4. **Covered elsewhere.**
   - PostgreSQL 18: excluded by the operator.
   - Other image pins: excluded.
   - The runtime backstop for out-of-band file edits stays the checksum check (ADR-0015).

### Threat model

- **Boundaries.**
  - Widened: the Postgres image pulled from Docker Hub by tag-plus-digest.
  - Added: the guard imports repository code (`src/kdive/db/supersessions.py`) from its own
    checkout.
- **Actors.**
  - An upstream registry publisher. The digest pin stops a re-tag reaching CI.
  - A contributor adding a supersession entry. This is reviewed code. An entry only admits
    one exact byte pair for one version, so it cannot blanket-admit edits.
- **Controls.**
  - Digest pinning (ADR-0505 shape) is unchanged.
  - The allowlist matches whole-file SHA-256 on both sides.
- **Out of scope.** Signing or provenance of upstream images, which is already outside
  ADR-0505.

## Success

1. On PostgreSQL 17.12, a fresh database applies 0001→0199.
2. A database whose `schema_migrations` row for 0112 holds the superseded checksum, with
   0113→0198 applied and the two functions in new-style form, migrates to 0199 without a
   checksum error. Afterwards, `ALTER TABLE capture_operations ADD COLUMN … DEFAULT` and the
   same statement on `worker_incarnations` succeed.
3. Any other checksum mismatch, including 0112 with any third byte content, still raises
   `checksum changed`.
4. The guard passes on this branch. It still rejects an edit to 0112 (or any other schema
   file) whose bytes are not the allowlisted pair.
5. The catalog hazard test passes on the full chain and fails if either function is new-style.
6. Every pin of the Postgres image runs on the 17.12 digest: compose, the compose fixture,
   the DB tests, the pre-pull script and the Helm demo value. `rg 7958605b` finds no hit outside
   design history.
7. Live: on a restored flexo lab guest, demo-up brings a fresh stack up on the 17.12 image, with
   all migrations applied and the server `/readyz` ready.

## Validation

- `focused-test`: a runner unit test with synthetic supersessions covers the accept pair,
  reject-other-pair and reject-third-content cases (`tests/db/test_migration_supersessions.py`).
- `focused-test`: the existing-database upgrade, success criterion 2
  (`tests/db/test_migration_supersessions.py`).
- `focused-test`: the catalog hazard check, including the array and `OUT` shapes
  (`tests/db/test_migration_supersessions.py`).
- `focused-test`: the guard's stale-branch hint on an unmatched `M` of a listed file, and the
  import-failure `GuardError` (`tests/scripts/test_schema_immutable_guard.py`).
- `focused-test`: the `SUPERSESSIONS` literal is well-formed: 4-digit version, 64-hex digests,
  filename prefix matches version, unique versions, and `current_sha256` equals the file on
  disk.
- `focused-test`: the guard's exact-pair allow and wrong-content reject
  (`tests/scripts/test_schema_immutable_guard.py`).
- Fresh chain on 17.12: every DB test (the fixture image) plus the window-list tests.
- Live: flexo demo-up proof.
