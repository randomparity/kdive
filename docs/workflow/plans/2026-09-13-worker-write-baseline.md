# Plan: record the worker-handler write baseline

## Goal

Implement #2345's analysis-only baseline: a versioned test-owned inventory that maps every active
worker job kind to its reachable database writes and grant-path verdict. Runtime worker behavior,
database roles, schemas, and migrations remain unchanged.

## Architecture

`tests/jobs/worker_write_baseline.json` is the durable inventory. Its focused test is the only
consumer in this change and validates the file against `JobKind` plus the cited repository source.
`docs/guide/worker-write-baseline.md` explains how the inventory was produced and refreshed.
ADR-0649 owns the choice of test-owned data over runtime code.

## Tech stack

Python 3.14, pytest, standard-library `json` and `pathlib`, and Markdown/JSON repository docs.

## Global Constraints

- Keep the change analysis-only: no `src/kdive/` runtime edits and no schema migration edits.
- Record only paths reachable from registered worker handlers; server and reconciler paths remain
  excluded.
- Use the existing grant matrix (0107, 0114, 0117, 0118, 0121, 0126, 0138, 0152) as evidence;
  do not infer authorization from an owner-privileged test connection.
- Preserve deterministic JSON ordering and repository-relative `file:line` source references.

Expected implementation size: 350–600 changed lines (M) — manifest rows for active handlers and
writes, structural assertions, and a short provenance guide.

## Task 1: Publish the inventory

**Files:** create `tests/jobs/worker_write_baseline.json`.

**Interfaces:** The file is a JSON object with `format_version: 1` and a sorted `handlers` list.
Each handler has `job_kind` and `writes`; each write has `id`, `table`, `operation`,
`handler_source`, `write_source`, `role`, `route`, `authority_source`, `grant_source`, and
`verdict`. Every evidence source carries a `path`, `line`, and expected `text`. The top level has
`confirmed_leaks`, sorted and exactly equal to the `id` values of `LEAK` rows. Later test code
consumes this exact shape. Every write row has exactly `role: kdive_worker`; server and reconciler
rows are invalid.

**Verification inventory:**

- Contract: every active handler has one explicit record. Mode: focused-test. Expected red:
  remove a handler record and the coverage assertion names its job kind. Green:
  `uv run python -m pytest tests/jobs/test_worker_write_baseline.py -q` passes.
- Contract: each write states handler, write, authority, and grant evidence. Mode: focused-test.
  Expected red: alter one evidence fragment or route to an unsupported value and validation fails.
  Green: the same focused command passes.
- Contract: confirmed leaks are explicit. Mode: focused-test. Expected red: remove or add an id in
  `confirmed_leaks`; green: the same focused command passes.
- Contract: the baseline is worker-only. Mode: focused-test. Expected red: mutate a row's role to
  `kdive_server`; green: the same focused command passes.

**Steps:**

1. Enumerate handler registrations from `src/kdive/jobs/assembly.py` and registrar modules.
2. Trace each registered handler into direct handler SQL and repository calls; classify all writes
   with the grant matrix and `SECURITY DEFINER` function bodies.
3. Write sorted JSON records, retaining empty `writes` lists for handlers with no database write
   and a `confirmed_leaks` list equal to the sorted `LEAK` identities.
4. Run the focused test after Task 2 adds it; expect one passing committed-manifest case.

**Acceptance criteria:** no active job kind is absent; no record leaves table, operation, source,
role, route, or verdict implicit.

## Task 2: Enforce the data contract

**Files:** create `tests/jobs/test_worker_write_baseline.py`; consume
`tests/jobs/worker_write_baseline.json` from Task 1.

**Interfaces:** `_load_baseline(path: Path) -> dict[str, object]` reads committed JSON; validation
returns deterministic violation strings for tests. No production import is introduced.

**Verification inventory:**

- Contract: shape, stable ordering, and uniqueness. Mode: focused-test. Expected red: duplicate a
  handler or write identity in a copied manifest; green: focused module passes committed data.
- Contract: complete active job-kind coverage. Mode: focused-test. Expected red: remove one entry;
  green: focused module passes committed data.
- Contract: cited evidence remains meaningful. Mode: focused-test. Expected red: replace a copied
  handler, write, authority, or grant fragment; green: focused module passes committed data.

**Steps:**

1. Add isolated mutation tests that prove each validator arm fails.
2. Validate committed JSON against `JobKind`, reconcile `confirmed_leaks`, and check every cited
   handler, write, authority, and grant file/line contains its recorded fragment. Reject every
   role other than `kdive_worker`.
3. Run `uv run python -m pytest tests/jobs/test_worker_write_baseline.py -q`; expect all focused
   cases to pass.

**Acceptance criteria:** the test can fail independently for coverage, identity, vocabulary, and
source drift without contacting a database.

## Task 3: Document provenance and run guardrails

**Files:** create `docs/guide/worker-write-baseline.md`; update `docs/README.md` to link it for
contributors.

**Interfaces:** The guide names the baseline path, schema version, scope, grant migrations, and
refresh workflow; it makes no runtime promise.

**Verification inventory:**

- Contract: the baseline is discoverable and its evidence is reviewable. Mode: focused-test. The
  docs-path/link checks fail when the new link is invalid; `just docs-links` passes after the link
  and guide are added.

**Steps:**

1. Describe the handler-only boundary, classifications, grant-matrix source migrations, and how
   to refresh source lines after an intentional change.
2. Link the guide from the contributor section in `docs/README.md`.
3. Keep ADR-0649 Proposed until the implementation PR merges, when the merge ratifies it. Run
   `just docs-links`, `just lint`, `just type`, the focused test, and finally
   `just ci > /tmp/kdive-2345-ci.log 2>&1 < /dev/null`; expect exit 0 from each.

**Acceptance criteria:** a reviewer can reproduce the classification evidence without treating
the guide as authorization to change a grant.

## Rollback

Reverting the manifest, test, guide, and ADR restores the prior absence of an audit baseline;
there is no database, role, or external-state cleanup.
