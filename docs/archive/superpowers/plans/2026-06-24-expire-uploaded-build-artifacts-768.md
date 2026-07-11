# Expire uploaded build artifacts (TTL + clear-on-close) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give run-owned uploaded build artifacts an enforced lifetime via two reconciler sweeps — clear-on-close (grace-gated marker) and a TTL backstop — without touching crash evidence.

**Architecture:** Migration 0048 adds an `investigations.cleanup_pending_at` marker (set on close, backfilled for already-closed rows). Two new `reconciler/cleanup/gc.py` repairs modeled on `gc_report_artifacts` delete run-owned `build`/`kernel-build` artifacts: one gated on the marker past a grace window, one on artifact age past a TTL. Both wire into the reconciler `_repair_plan` under the existing `upload_store` gate.

**Tech Stack:** Python 3.14, psycopg (async), Postgres, boto3 ObjectStore, pytest.

**Spec:** `docs/superpowers/specs/2026-06-24-expire-uploaded-build-artifacts-768.md`
**ADR:** ADR-0234 decision 4.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict, whole-tree (`just type`).
- Absolute imports only (no relative `..`).
- Per-object store failure must be logged and retried next pass, never abort a sweep (mirror `gc_report_artifacts`).
- Deletion scope predicate (both sweeps): `owner_kind = 'runs' AND retention_class = ANY(ARRAY['build','kernel-build'])`. Never console (`'console'`, system-owned), never `build-log` (run-owned evidence), never system-owned uploads.
- Time predicates use Postgres `now()`, never a Python clock.
- Migrations are forward-only, additive (ADR-0015); filename `0048_*.sql`, discovered by glob (no registry to edit).
- Conventional-commit subjects ≤72 chars; trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Doc-style: plain factual prose; never "critical/crucial/essential/significant/comprehensive/robust/elegant"; "Milestone" not "Sprint".
- Adding a config Setting requires regenerating `docs/guide/reference/config.md` (`just config-docs`) and the doc-resource snapshots (`just resources-docs`); CI gates `config-docs-check`, `env-docs-check`, `resources-docs-check`.

## File Structure

- Create `src/kdive/db/schema/0048_investigation_cleanup_marker.sql` — the marker column + backfill.
- Modify `src/kdive/mcp/tools/catalog/investigations.py` `_close_locked` — stamp the marker in the close transaction.
- Modify `src/kdive/reconciler/cleanup/gc.py` — `_BUILD_RETENTION_CLASSES`, `gc_investigation_artifacts`, `gc_expired_build_artifacts`, default-timedelta constants.
- Modify `src/kdive/reconciler/loop.py` — `ReconcileConfig` fields, `_repair_plan` registration, `ALL_REPAIR_KINDS`, `ReconcileReport` counts + aliases.
- Modify `src/kdive/config/core_settings.py` — two `Setting`s + `SETTINGS` list entries.
- Modify `src/kdive/__main__.py` — plumb the two settings into `ReconcileConfig`.
- Create `tests/reconciler/test_gc_investigation_artifacts.py`, `tests/reconciler/test_gc_expired_build_artifacts.py`.
- Modify `tests/mcp/.../test_investigations*.py` (close-marker test) and `tests/reconciler/test_loop.py` (report-style counts) as needed.
- Regenerate `docs/guide/reference/config.md` and any doc-resource snapshot.

---

### Task 1: Migration 0048 — `cleanup_pending_at` marker + backfill

**Files:**
- Create: `src/kdive/db/schema/0048_investigation_cleanup_marker.sql`
- Test: `tests/db/test_migrations.py` (add a case) or a new `tests/db/test_investigation_cleanup_marker.py`

**Interfaces:**
- Produces: `investigations.cleanup_pending_at timestamptz NULL`; already-`closed` rows carry their close-instant `updated_at`.

- [ ] **Step 1: Write the failing test** (new file `tests/db/test_investigation_cleanup_marker.py`)

Use the **migration-replay** pattern (like `tests/db/test_image_catalog_migration.py`): apply every
migration *before* 0048, seed a closed and an open investigation, then apply 0048 and assert the
backfill. `discover_migrations()` returns `Migration` objects sorted by string `version`
("0001".."0048"); `m.sql` is the file text (psycopg3 runs the multi-statement file in one
`execute()` when no params are passed). Use the **sync** `pg_conn` fixture.

```python
"""Migration 0048 adds investigations.cleanup_pending_at and backfills closed rows."""

from __future__ import annotations

from uuid import uuid4

import psycopg

from kdive.db import migrate


def _apply_before(conn: psycopg.Connection, version: str) -> None:
    for m in migrate.discover_migrations():
        if m.version >= version:
            break
        conn.execute(m.sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _apply_version(conn: psycopg.Connection, version: str) -> None:
    sql = next(m.sql for m in migrate.discover_migrations() if m.version == version)
    conn.execute(sql.encode())  # bytes: a dynamic str fails ty (see migrate.py:135-138)


def _insert_investigation(conn: psycopg.Connection, inv_id, state: str) -> None:
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, %s, %s, %s, %s)",
        (inv_id, "p", "proj", "t", state),
    )


def test_migration_0048_backfills_closed_investigations(pg_conn: psycopg.Connection) -> None:
    _apply_before(pg_conn, "0048")
    open_id, closed_id = uuid4(), uuid4()
    _insert_investigation(pg_conn, open_id, "open")
    _insert_investigation(pg_conn, closed_id, "closed")
    closed_updated = pg_conn.execute(
        "SELECT updated_at FROM investigations WHERE id = %s", (closed_id,)
    ).fetchone()[0]

    _apply_version(pg_conn, "0048")

    assert (
        pg_conn.execute(
            "SELECT cleanup_pending_at FROM investigations WHERE id = %s", (closed_id,)
        ).fetchone()[0]
        == closed_updated
    )
    assert (
        pg_conn.execute(
            "SELECT cleanup_pending_at FROM investigations WHERE id = %s", (open_id,)
        ).fetchone()[0]
        is None
    )
```

> `pg_conn` is the sync, *un-migrated* Postgres connection fixture from `tests/db/conftest.py`
> (`migrated_url` runs every migration; this test needs to stop before 0048). Confirm the fixture
> name by reading `tests/db/conftest.py` — `test_image_catalog_migration.py` uses `pg_conn` the same
> way. The close-path test (Task 2) covers the forward stamping; this covers the historical backfill.

- [ ] **Step 2: Run it — expect failure** (column missing until migration written)

Run: `uv run python -m pytest tests/db/test_investigation_cleanup_marker.py -q`
Expected: error — column `cleanup_pending_at` does not exist (or the migration file absent).

- [ ] **Step 3: Write the migration**

```sql
-- 0048_investigation_cleanup_marker.sql — uploaded-build-artifact cleanup marker (ADR-0234 §4, #768).
-- Additive, forward-only (ADR-0015). `cleanup_pending_at` marks an investigation whose run-owned
-- build artifacts the reconciler `gc_investigation_artifacts` sweep should reclaim after a grace
-- window. `investigations.close` stamps it; already-closed rows are back-marked with `updated_at`
-- (their frozen close instant — `closed` is terminal and link/set/unlink refuse terminal rows) so
-- the close-driven sweep also reclaims historical closed investigations. NULL = not pending.
ALTER TABLE investigations ADD COLUMN cleanup_pending_at timestamptz;

UPDATE investigations SET cleanup_pending_at = updated_at WHERE state = 'closed';
```

- [ ] **Step 4: Run the test — expect pass**

Run: `uv run python -m pytest tests/db/test_investigation_cleanup_marker.py -q`
Expected: PASS.

- [ ] **Step 5: Run the migration suite + ty**

Run: `uv run python -m pytest tests/db -q && just type`
Expected: PASS (no migration-list/count assertion to update — discovery is by glob).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/schema/0048_investigation_cleanup_marker.sql tests/db/test_investigation_cleanup_marker.py
git commit -m "feat(db): add investigations.cleanup_pending_at marker (migration 0048, #768)"
```

---

### Task 2: Stamp the marker on close

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/investigations.py` (`_close_locked`, ~line 315-316)
- Test: the existing investigations close test module (find via `rg -l "investigations.close" tests/`)

**Interfaces:**
- Consumes: migration 0048 column.
- Produces: after `investigations.close`, `cleanup_pending_at` is non-NULL; a re-close (idempotent, already-closed early return) does not move it.

- [ ] **Step 1: Write the failing test** (append to the close test module)

```python
def test_close_stamps_cleanup_pending_at(...):
    # open an investigation, close it, assert cleanup_pending_at is set;
    # close again (idempotent) and assert the value is unchanged.
    # Use the module's existing pool/ctx fixtures and open_investigation/close_investigation.
```

(Concrete form: mirror the module's existing close test — call `close_investigation`, then
`SELECT cleanup_pending_at FROM investigations WHERE id = %s`, assert not None; capture it,
call `close_investigation` again, assert the second read equals the first.)

- [ ] **Step 2: Run it — expect failure** (`cleanup_pending_at` is None)

Run: `uv run python -m pytest <close_test_module> -q -k cleanup_pending`
Expected: FAIL — value is None.

- [ ] **Step 3: Implement — stamp inside the close transaction**

In `_close_locked`, after `updated = await INVESTIGATIONS.update_state(conn, uid, InvestigationState.CLOSED)` and within the same `async with conn.transaction()` block, add:

```python
        await conn.execute(
            "UPDATE investigations SET cleanup_pending_at = now() WHERE id = %s", (uid,)
        )
```

(The already-`CLOSED` early return at the top of `_close_locked` is unchanged, so a re-close
never re-stamps — that path returns before the state flip.)

- [ ] **Step 4: Run the test — expect pass**

Run: `uv run python -m pytest <close_test_module> -q -k cleanup_pending`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/investigations.py tests/...
git commit -m "feat(investigations): mark cleanup_pending_at on close (#768)"
```

---

### Task 3: `gc_investigation_artifacts` sweep (clear-on-close)

**Files:**
- Modify: `src/kdive/reconciler/cleanup/gc.py`
- Test: `tests/reconciler/test_gc_investigation_artifacts.py`

**Interfaces:**
- Consumes: `ArtifactObjectDeleter` (existing Protocol in gc.py), migration 0048.
- Produces: `async def gc_investigation_artifacts(conn, store: ArtifactObjectDeleter, grace: timedelta) -> int`; module constants `_BUILD_RETENTION_CLASSES: tuple[str, ...] = ("build", "kernel-build")` and `DEFAULT_INVESTIGATION_CLEANUP_GRACE = timedelta(days=1)`.

- [ ] **Step 1: Write the failing tests** (`tests/reconciler/test_gc_investigation_artifacts.py`)

> **Seed correctly — do NOT copy `test_gc_report_artifacts.py` verbatim.** That test inserts *bare*
> `artifacts` rows (`owner_id = uuid4()`, no `runs`/`investigations`) because the report sweep is a
> flat query. `gc_investigation_artifacts` **JOINs `runs`** (`r.id = a.owner_id`,
> `r.investigation_id = <inv>`), so a build artifact only matches when a real `runs` row with that id
> points at the closed investigation. A bare-artifact seed yields zero candidates and a vacuously
> passing test. `runs.system_id` is **nullable** (migration 0042), so no systems/allocations chain is
> needed; `runs.build_profile` is `jsonb NOT NULL` and `runs.target_kind` is `NOT NULL`. Concrete
> seed helper (reuse `connect`, `migrated_url`, and a recording `_RecordingStore` like the report
> test):

```python
async def _seed_run_build_artifact(
    conn, *, retention_class: str, owner_kind: str = "runs",
    state: str = "closed", grace_age: timedelta = timedelta(days=2),
) -> tuple[UUID, str]:
    """Insert investigation(closed, cleanup_pending_at past grace) + run + one artifact.

    Returns (artifact_id, object_key). For owner_kind='systems' (console) the run is still made so
    the investigation exists, but the artifact's owner_id is a standalone system uuid (no JOIN).
    """
    inv_id, run_id = uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state, cleanup_pending_at) "
        "VALUES (%s, 'p', 'proj', 't', %s, now() - %s)",
        (inv_id, state, grace_age) if state == "closed" else (inv_id, state, None),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, state, build_profile, target_kind, "
        "principal, project) VALUES (%s, %s, NULL, 'created', '{}'::jsonb, 'local-libvirt', 'p', 'proj')",
        (run_id, inv_id),
    )
    artifact_id = uuid4()
    owner_id = run_id if owner_kind == "runs" else uuid4()
    key = f"local/{owner_kind}/{artifact_id}"
    await conn.execute(
        "INSERT INTO artifacts (id, owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES (%s, %s, %s, %s, 'etag', 'redacted', %s)",
        (artifact_id, owner_kind, owner_id, key, retention_class),
    )
    return artifact_id, key
```

> The `cleanup_pending_at` insert above branches on `state`: for an open investigation pass
> `state='open'` and set the column NULL (params tuple differs — write two small INSERT variants or a
> conditional rather than the compressed ternary if it reads cleaner). Confirm `runs` columns against
> `src/kdive/db/schema/0001_init.sql` + `0042_decouple_run_system_binding.sql` (`target_kind` arrived
> with 0042). Cases to cover:
- A closed investigation whose `cleanup_pending_at` is past grace: its run-owned `build` and
  `kernel-build` artifacts are deleted (object + row); `cleanup_pending_at` cleared after full drain.
- Under grace: nothing deleted, marker retained.
- An *open* investigation (marker NULL): untouched.
- A System-owned `console` artifact under the same investigation's System: untouched.
- A run-owned `build-log` artifact: untouched.
- Per-object failure: failed row kept, marker retained; the other row reaped.
- Re-run after full drain: deletes 0 (marker cleared).

Helper to seed: insert an `investigations` row (`state='closed'`, `cleanup_pending_at = now() - age`),
a `runs` row with `investigation_id`, then `artifacts` rows with the right `owner_kind`/`retention_class`.
(Check `runs` NOT NULL columns via `\d runs` / the 0001 schema when writing the INSERT.)

- [ ] **Step 2: Run — expect failure** (function undefined)

Run: `uv run python -m pytest tests/reconciler/test_gc_investigation_artifacts.py -q`
Expected: FAIL — `cannot import name 'gc_investigation_artifacts'`.

- [ ] **Step 3: Implement** in `gc.py`

```python
DEFAULT_INVESTIGATION_CLEANUP_GRACE = timedelta(days=1)
_BUILD_RETENTION_CLASSES: tuple[str, ...] = ("build", "kernel-build")


async def gc_investigation_artifacts(
    conn: AsyncConnection, store: ArtifactObjectDeleter, grace: timedelta
) -> int:
    """Reclaim run-owned build artifacts of closed investigations past ``grace`` (ADR-0234 §4).

    Deletes object + row for ``owner_kind='runs'`` artifacts whose ``retention_class`` is a build
    class (never console/build-log/system-owned), linked via ``runs.investigation_id`` to an
    investigation whose ``cleanup_pending_at`` is older than ``grace``. The marker is cleared once an
    investigation's build artifacts are fully drained, so a fully-reclaimed investigation drops out
    of the worklist. A per-object store failure is logged and retried next pass, leaving the marker
    set; it never aborts the sweep (mirrors :func:`gc_report_artifacts`).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM investigations "
            "WHERE cleanup_pending_at IS NOT NULL AND cleanup_pending_at < now() - %s",
            (grace,),
        )
        investigation_ids = [row[0] for row in await cur.fetchall()]
    deleted = 0
    for investigation_id in investigation_ids:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT a.id, a.object_key FROM artifacts a "
                "JOIN runs r ON r.id = a.owner_id "
                "WHERE a.owner_kind = 'runs' AND a.retention_class = ANY(%s) "
                "AND r.investigation_id = %s",
                (list(_BUILD_RETENTION_CLASSES), investigation_id),
            )
            candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
        all_ok = True
        for artifact_id, object_key in candidates:
            try:
                await asyncio.to_thread(store.delete, object_key)
            except Exception:  # noqa: BLE001 - one object failure must not starve the rest
                _log.warning(
                    "reconciler: deleting investigation artifact object %s failed; retry next pass",
                    object_key,
                    exc_info=True,
                )
                all_ok = False
                continue
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
            deleted += 1
        if all_ok:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "UPDATE investigations SET cleanup_pending_at = NULL WHERE id = %s",
                    (investigation_id,),
                )
    if deleted:
        _log.info("reconciler: GC'd %d closed-investigation build artifact(s)", deleted)
    return deleted
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run python -m pytest tests/reconciler/test_gc_investigation_artifacts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/reconciler/cleanup/gc.py tests/reconciler/test_gc_investigation_artifacts.py
git commit -m "feat(reconciler): gc_investigation_artifacts clears closed-investigation builds (#768)"
```

---

### Task 4: `gc_expired_build_artifacts` sweep (TTL backstop)

**Files:**
- Modify: `src/kdive/reconciler/cleanup/gc.py`
- Test: `tests/reconciler/test_gc_expired_build_artifacts.py`

**Interfaces:**
- Produces: `async def gc_expired_build_artifacts(conn, store: ArtifactObjectDeleter, retention: timedelta) -> int`; constant `DEFAULT_BUILD_ARTIFACT_RETENTION = timedelta(days=30)`.

- [ ] **Step 1: Write the failing tests** (`tests/reconciler/test_gc_expired_build_artifacts.py`)

> Reuse the same `_seed_run_build_artifact` helper shape from Task 3 (a run row is required because
> the exclusion cases need realistic owner kinds; the TTL query itself does not JOIN runs, but a
> run-owned artifact still needs `owner_kind='runs'`). Add a `created_at` override to the artifact
> INSERT (`created_at = now() - %s`) so the test can place a row past/under the TTL; `cleanup_pending_at`
> and investigation state are irrelevant to this sweep, so seed the investigation `open`. Cases:

- Run-owned `build`/`kernel-build` artifacts older than the TTL are deleted regardless of
  investigation state (open or closed).
- Fresh ones (under TTL): untouched.
- System-owned `console`, run-owned `build-log`, and a System-owned `build` (operator upload):
  untouched.
- Per-object failure isolation (failed row kept, other reaped).

- [ ] **Step 2: Run — expect failure**

Run: `uv run python -m pytest tests/reconciler/test_gc_expired_build_artifacts.py -q`
Expected: FAIL — function undefined.

- [ ] **Step 3: Implement** in `gc.py`

```python
DEFAULT_BUILD_ARTIFACT_RETENTION = timedelta(days=30)


async def gc_expired_build_artifacts(
    conn: AsyncConnection, store: ArtifactObjectDeleter, retention: timedelta
) -> int:
    """Reclaim run-owned build artifacts older than ``retention`` regardless of close (ADR-0234 §4).

    The TTL backstop for never-closed investigations. Same row scope as
    :func:`gc_investigation_artifacts` (``owner_kind='runs'`` and a build ``retention_class``), gated
    on ``artifacts.created_at`` rather than the close marker. Per-object failure isolation mirrors
    :func:`gc_report_artifacts`.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, object_key FROM artifacts "
            "WHERE owner_kind = 'runs' AND retention_class = ANY(%s) "
            "AND created_at < now() - %s",
            (list(_BUILD_RETENTION_CLASSES), retention),
        )
        candidates = [(row[0], str(row[1])) for row in await cur.fetchall()]
    deleted = 0
    for artifact_id, object_key in candidates:
        try:
            await asyncio.to_thread(store.delete, object_key)
        except Exception:  # noqa: BLE001 - one object failure must not starve the rest
            _log.warning(
                "reconciler: deleting expired build artifact object %s failed; retry next pass",
                object_key,
                exc_info=True,
            )
            continue
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
        deleted += 1
    if deleted:
        _log.info("reconciler: GC'd %d build artifact(s) past TTL", deleted)
    return deleted
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run python -m pytest tests/reconciler/test_gc_expired_build_artifacts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/reconciler/cleanup/gc.py tests/reconciler/test_gc_expired_build_artifacts.py
git commit -m "feat(reconciler): gc_expired_build_artifacts TTL backstop (#768)"
```

---

### Task 5: Config settings + regenerated docs

**Files:**
- Modify: `src/kdive/config/core_settings.py`
- Modify (generated): `docs/guide/reference/config.md` and any doc-resource snapshot.

**Interfaces:**
- Produces: `INVESTIGATION_CLEANUP_GRACE_DAYS`, `BUILD_ARTIFACT_RETENTION_DAYS` Settings (both `parse=_int`, `processes=_STORE_USERS`), present in `SETTINGS`.

- [ ] **Step 1: Add the two Settings** (after `REPORT_ARTIFACT_RETENTION_DAYS`)

```python
INVESTIGATION_CLEANUP_GRACE_DAYS = Setting(
    name="KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS",
    parse=_int,
    default="1",
    group="reports",
    processes=_STORE_USERS,
    help=(
        "Grace window in days between an investigation closing and the reconciler "
        "`gc_investigation_artifacts` sweep reclaiming its run-owned uploaded build artifacts "
        "(kernel/vmlinux/initrd; never console or crash evidence). ADR-0234 §4."
    ),
    suggest="an integer number of days, e.g. 1",
)
BUILD_ARTIFACT_RETENTION_DAYS = Setting(
    name="KDIVE_BUILD_ARTIFACT_RETENTION_DAYS",
    parse=_int,
    default="30",
    group="reports",
    processes=_STORE_USERS,
    help=(
        "Age in days after which the reconciler `gc_expired_build_artifacts` sweep deletes a "
        "run-owned uploaded build artifact regardless of investigation close — the backstop for "
        "investigations that never close. ADR-0234 §4."
    ),
    suggest="an integer number of days, e.g. 30",
)
```

- [ ] **Step 2: Register both in `SETTINGS`** (after `REPORT_ARTIFACT_RETENTION_DAYS,`)

```python
    REPORT_ARTIFACT_RETENTION_DAYS,
    INVESTIGATION_CLEANUP_GRACE_DAYS,
    BUILD_ARTIFACT_RETENTION_DAYS,
```

- [ ] **Step 3: Regenerate docs**

Run: `just config-docs && just resources-docs`
Then verify: `just config-docs-check && just env-docs-check && just resources-docs-check`
Expected: all PASS; `git status` shows `docs/guide/reference/config.md` (and possibly a snapshot) changed.

- [ ] **Step 4: Commit**

```bash
git add src/kdive/config/core_settings.py docs/guide/reference/config.md docs/...snapshots
git commit -m "feat(config): add cleanup-grace + build-artifact-retention settings (#768)"
```

---

### Task 6: Wire both sweeps into the reconciler loop + assembly

**Files:**
- Modify: `src/kdive/reconciler/loop.py`
- Modify: `src/kdive/__main__.py`
- Test: `tests/reconciler/test_loop.py`

**Interfaces:**
- Consumes: `gc_investigation_artifacts`, `gc_expired_build_artifacts`, the two default constants, the two Settings.
- Produces: `ReconcileReport.investigation_artifacts_gc_count`, `ReconcileReport.expired_build_artifacts_gc_count`; `ReconcileConfig.investigation_cleanup_grace`, `ReconcileConfig.build_artifact_retention`; two new `ALL_REPAIR_KINDS` entries `"investigation_artifacts_gc_count"`, `"expired_build_artifacts_gc_count"`.

- [ ] **Step 1: Update the pinning + report tests first** (`tests/reconciler/test_loop.py`)

The pinning test `test_all_repair_kinds_matches_a_fully_populated_plan` already builds a fully
populated plan; it passes automatically once the plan and `ALL_REPAIR_KINDS` both gain the two names.
Add an explicit assertion that the two new kinds are present:

```python
def test_build_artifact_repairs_are_in_all_repair_kinds() -> None:
    assert "investigation_artifacts_gc_count" in loop.ALL_REPAIR_KINDS
    assert "expired_build_artifacts_gc_count" in loop.ALL_REPAIR_KINDS
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run python -m pytest tests/reconciler/test_loop.py -q -k build_artifact_repairs`
Expected: FAIL — names absent.

- [ ] **Step 3: Implement loop wiring** (`loop.py`)

1. Module aliases + defaults near the existing ones:

```python
DEFAULT_INVESTIGATION_CLEANUP_GRACE = gc_repairs.DEFAULT_INVESTIGATION_CLEANUP_GRACE
DEFAULT_BUILD_ARTIFACT_RETENTION = gc_repairs.DEFAULT_BUILD_ARTIFACT_RETENTION
_gc_investigation_artifacts = gc_repairs.gc_investigation_artifacts
_gc_expired_build_artifacts = gc_repairs.gc_expired_build_artifacts
```

Add `_gc_investigation_artifacts`, `_gc_expired_build_artifacts` to `__all__`.

2. `ReconcileConfig` fields (next to `report_artifact_retention`):

```python
    investigation_cleanup_grace: timedelta = DEFAULT_INVESTIGATION_CLEANUP_GRACE
    build_artifact_retention: timedelta = DEFAULT_BUILD_ARTIFACT_RETENTION
```

3. `_repair_plan`, inside the `if config.upload_store is not None:` block (after the report-GC spec),
   capturing locals for the closures:

```python
        cleanup_grace = config.investigation_cleanup_grace
        build_retention = config.build_artifact_retention
        repairs.append(
            _RepairSpec(
                "investigation_artifacts_gc_count",
                lambda conn: _gc_investigation_artifacts(conn, upload_store, cleanup_grace),
            )
        )
        repairs.append(
            _RepairSpec(
                "expired_build_artifacts_gc_count",
                lambda conn: _gc_expired_build_artifacts(conn, upload_store, build_retention),
            )
        )
```

4. Add both names to `ALL_REPAIR_KINDS` (after `"report_artifacts_gc_count",`).

5. `ReconcileReport`: add two `int = 0` fields and populate them in `reconcile_once`'s return via
   `counts.get(...)`:

```python
    investigation_artifacts_gc_count: int = 0
    expired_build_artifacts_gc_count: int = 0
```
```python
        investigation_artifacts_gc_count=counts.get("investigation_artifacts_gc_count", 0),
        expired_build_artifacts_gc_count=counts.get("expired_build_artifacts_gc_count", 0),
```

- [ ] **Step 4: Plumb settings in `__main__.py`** (inside the `ReconcileConfig(...)` near `report_artifact_retention=`)

```python
                    investigation_cleanup_grace=timedelta(
                        days=config.require(INVESTIGATION_CLEANUP_GRACE_DAYS)
                    ),
                    build_artifact_retention=timedelta(
                        days=config.require(BUILD_ARTIFACT_RETENTION_DAYS)
                    ),
```

Add `INVESTIGATION_CLEANUP_GRACE_DAYS, BUILD_ARTIFACT_RETENTION_DAYS` to the `core_settings` import
block in `__main__.py` (next to `REPORT_ARTIFACT_RETENTION_DAYS`).

- [ ] **Step 5: Run the full reconciler + loop tests + ty**

Run: `uv run python -m pytest tests/reconciler -q && just type`
Expected: PASS, including the pinning test.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/reconciler/loop.py src/kdive/__main__.py tests/reconciler/test_loop.py
git commit -m "feat(reconciler): wire build-artifact GC sweeps into the loop (#768)"
```

---

### Task 7: Full gate + docs

- [ ] **Step 1: Run the full CI gate locally**

Run: `just ci`
Expected: all green (lint, type, doc-checks incl. config-docs-check/env-docs-check/resources-docs-check, test).

- [ ] **Step 2: Fix anything red, fold fixups into the owning commit, re-run.**

---

## Self-Review

- **Spec coverage:** migration 0048 + backfill (Task 1) ✓; close marker (Task 2) ✓; clear-on-close sweep w/ grace + marker-clear + per-object isolation (Task 3) ✓; TTL backstop (Task 4) ✓; console/build-log/system-owned exclusion (Tasks 3–4 tests) ✓; config settings + regenerated docs (Task 5) ✓; loop wiring + ALL_REPAIR_KINDS + report counts + assembly (Task 6) ✓; full gate (Task 7) ✓.
- **Type consistency:** `gc_investigation_artifacts(conn, store, grace)` and `gc_expired_build_artifacts(conn, store, retention)`, constants `DEFAULT_INVESTIGATION_CLEANUP_GRACE`/`DEFAULT_BUILD_ARTIFACT_RETENTION`, repair-kind names `investigation_artifacts_gc_count`/`expired_build_artifacts_gc_count`, config `INVESTIGATION_CLEANUP_GRACE_DAYS`/`BUILD_ARTIFACT_RETENTION_DAYS` — used identically across Tasks 3–6.
- **Placeholders:** Task 2's test and Task 3/4 seed helpers are described rather than fully coded because they depend on the close test module's fixtures and the `runs` NOT-NULL columns; the implementer reads those at the named paths. All production code is shown in full.
