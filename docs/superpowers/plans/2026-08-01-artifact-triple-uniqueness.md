# Artifact ownership triple uniqueness implementation plan

**Goal:** Enforce one artifact catalog row per ownership triple and make worker conflict recovery
explicit.

**Architecture:** Migration 0094 adds the database invariant. A focused artifact repository
subclass exposes a conflict-aware claim operation that returns both the persisted row and whether
this attempt inserted it. The three ADR-0519 worker paths adopt a conflict winner and reuse their
existing stat-based repair behavior.

**Guardrail:** `just ci`

## Task 1: Prove the database constraint

**Files:**

- Create: `src/kdive/db/schema/0094_artifact_owner_triple_unique.sql`
- Create: `tests/db/test_artifact_owner_uniqueness.py`

1. Add a failing database test that creates two `Artifact` rows with distinct ids but the same
   `(owner_kind, owner_id, object_key)`, inserts the first, and expects `psycopg.errors.UniqueViolation`
   from the second raw insert.
2. Run `uv run python -m pytest tests/db/test_artifact_owner_uniqueness.py -q` and confirm the test
   fails because both rows are accepted.
3. Add migration 0094:

   ```sql
   CREATE UNIQUE INDEX artifacts_owner_triple_uniq
       ON artifacts (owner_kind, owner_id, object_key);
   ```

4. Re-run the focused test and confirm it passes.
5. Commit with `fix: enforce unique artifact ownership triples`.

## Task 2: Add conflict-aware artifact claiming

**Files:**

- Modify: `src/kdive/db/repositories.py`
- Modify: `tests/db/test_artifact_owner_uniqueness.py`

1. Add a failing disposable-Postgres test for `ARTIFACTS.claim`: the first call returns
   `(new_row, True)`, a duplicate candidate returns `(first_row, False)`, and the persisted winner's
   id and etag remain unchanged.
2. Add a focused unit test around the bounded conflict-then-missing-winner branch, using a scripted
   cursor to prove one retry occurs and repeated disappearance raises `ArtifactClaimConflict`.
3. Implement `ArtifactRepository(Repository[Artifact])` with `claim`. On each of at most two
   attempts it runs the full-column `INSERT ... ON CONFLICT (owner_kind, owner_id, object_key) DO
   NOTHING RETURNING` statement; on no returned row it selects the triple. Return the row and
   insertion marker when either succeeds; otherwise raise an error naming the triple and recovery
   action. Bind `ARTIFACTS` to this subclass.
4. Run `uv run python -m pytest tests/db/test_artifact_owner_uniqueness.py -q` and confirm pass.
5. Commit with `feat: handle artifact claim conflicts`.

## Task 3: Adopt winners in ADR-0519 worker paths

**Files:**

- Modify: `src/kdive/jobs/handlers/control/capture_traffic.py`
- Modify: `src/kdive/jobs/handlers/control/diagnostic_sysrq.py`
- Modify: `src/kdive/jobs/handlers/console/console_rotate.py`
- Modify: `tests/jobs/handlers/control/test_capture_traffic_handler.py`
- Modify: `tests/jobs/handlers/test_diagnostic_sysrq_handler.py`
- Modify: `tests/jobs/handlers/test_console_rotate.py`

1. Add focused failing tests that make the phase-3 probe miss and `ARTIFACTS.claim` return an
   existing row with `inserted=False`. Assert the handler does not emit a raw integrity error,
   returns/adopts the winner as appropriate, and invokes the existing etag-repair path outside the
   lock.
2. Add a repeated-winner-disappearance test that makes `ARTIFACTS.claim` raise
   `ArtifactClaimConflict`, then proves the handler invokes `discard_unregistered_objects` outside
   its transaction before re-raising for worker retry.
3. Replace the three raw `ARTIFACTS.insert` calls with `ARTIFACTS.claim`. Preserve audit only for a
   newly inserted row; route an adopted row into each handler's existing claimed-row collection or
   reconciliation branch.
4. Run the six focused database/handler test files and confirm pass.
5. Commit with `fix: adopt concurrent artifact claims`.

## Task 4: Verify and review

1. Run `just ci` bare and resolve every failure.
2. Run the branch adversarial review with the frozen #1750 charter, disposition every finding, and
   commit accepted fixes separately.
3. Run the simplification pass, then re-run `just ci`.
4. Push, create a PR with `Closes #1750`, post `WORK:REVIEW`, and wait until checks are green and
   GitHub reports the PR mergeable. Do not merge in campaign hand-off mode.
