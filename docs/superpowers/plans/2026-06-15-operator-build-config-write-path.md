# Operator build-config write-path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `platform_admin`-gated, audited `buildconfig.set` MCP tool that publishes/replaces a build-config fragment without an image rebuild, with a `source` provenance column so a later `migrate` never clobbers an operator override.

**Architecture:** One additive migration adds `source` (`seed`|`operator`) to `build_config_catalog`. The seed and the new tool both serialize per fragment name on a new `LockScope.BUILD_CONFIG` advisory lock; the seed's upsert is additionally DB-guarded (`WHERE source='seed'`). The tool reuses the existing reserved object key (in-place overwrite, no orphans) and the break-glass `platform_admin` + `platform_audit_log` pattern. `buildconfig.get` surfaces `source` for read-path observability.

**Tech Stack:** Python 3.13, FastMCP, psycopg (async + sync), Postgres advisory locks, S3-compatible object store. Spec: `docs/design/operator-build-config-write-path.md`. ADR: `docs/adr/0119-operator-build-config-write-path.md`.

**Guardrails (run before every commit):** `just lint`, `just type`, and the focused tests named per task. Run `just ci` once at the end. Conventional-commit subjects ≤72 chars, ending with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- Create: `src/kdive/db/schema/0034_build_config_catalog_source.sql` — the `source` column.
- Modify: `src/kdive/config/core_settings.py` — `MAX_BUILD_CONFIG_BYTES` setting + registration tuple.
- Modify: `src/kdive/db/locks.py` — `LockScope.BUILD_CONFIG`.
- Modify: `src/kdive/build_configs/catalog.py` — `BuildConfigEntry.source`, `_SELECT`, `upsert_operator_build_config`, `upsert_seed_build_config`.
- Modify: `src/kdive/build_configs/seed.py` — source-aware seed under the per-name lock.
- Modify: `src/kdive/mcp/tools/catalog/build_configs.py` — `set_build_config` handler + `buildconfig.set` registration; `read_build_config` surfaces `source`.
- Modify: `scripts/m2_portability_gate.py` + `tests/scripts/test_m2_portability_gate.py` — allow `0034_*.sql`.
- Modify: `tests/db/test_migrate.py` — applied-id list gains `0034`.
- Modify: `tests/mcp/core/test_tool_docs.py` — tool→test map gains `buildconfig.set`.
- Modify: generated tool reference (via `just docs`).
- Test: `tests/mcp/catalog/test_build_configs_tool.py` (extend), `tests/build_configs/test_seed.py` (extend/create), `tests/adversarial/test_build_config_concurrency.py` (create).

---

## Task 1: Foundation — migration, config setting, lock scope, wiring guards

**Files:**
- Create: `src/kdive/db/schema/0034_build_config_catalog_source.sql`
- Modify: `src/kdive/config/core_settings.py`
- Modify: `src/kdive/db/locks.py:43-51` (the `LockScope` enum members)
- Modify: `scripts/m2_portability_gate.py:198-200`
- Modify: `tests/scripts/test_m2_portability_gate.py:164-166`
- Modify: `tests/db/test_migrate.py` (both applied-id lists, near lines 128-129 and 565-566)

- [ ] **Step 1: Add the migration file**

`src/kdive/db/schema/0034_build_config_catalog_source.sql`:

```sql
-- Operator write-path for build-config fragments (ADR-0119): provenance for the
-- build_config_catalog row. 'seed' = published by the packaged deploy-time seed (the
-- default and the value existing rows backfill to); 'operator' = published by an admin
-- via buildconfig.set. The seed's source-guarded upsert refuses to overwrite an
-- 'operator' row, so a later migrate never clobbers an operator override.
ALTER TABLE build_config_catalog
    ADD COLUMN source text NOT NULL DEFAULT 'seed'
        CHECK (source IN ('seed', 'operator'));
```

- [ ] **Step 2: Update the applied-migration id lists in `tests/db/test_migrate.py`**

Both lists that end `"0032", "0033",` gain a trailing `"0034",`. Run `rg -n '"0033",' tests/db/test_migrate.py` to find the two sites; add `"0034",` after each.

- [ ] **Step 3: Run the migrate test to verify it passes**

Run: `uv run python -m pytest tests/db/test_migrate.py -q`
Expected: PASS (the schema applies and the id list matches). If Docker is absent it SKIPS — that is acceptable locally; CI runs it.

- [ ] **Step 4: Add the `MAX_BUILD_CONFIG_BYTES` setting**

In `src/kdive/config/core_settings.py`, after the `MAX_UPLOAD_BYTES` block (~line 157-170), add:

```python
MAX_BUILD_CONFIG_BYTES = Setting(
    name="KDIVE_MAX_BUILD_CONFIG_BYTES",
    parse=_int,
    default=str(256 * 1024),
    group="upload",
    processes=_SERVER,
    help=(
        "Maximum accepted build-config fragment size in bytes for buildconfig.set "
        "(ADR-0119). Kernel-config fragments are a few KiB; the cap bounds a hostile "
        "or accidental large upload."
    ),
    suggest="an integer number of bytes, e.g. 262144 (256 KiB)",
)
```

Then add `MAX_BUILD_CONFIG_BYTES,` to the registration tuple that lists `MAX_UPLOAD_BYTES,` (~line 415).

- [ ] **Step 5: Verify the env-docs coverage guard passes**

Run: `just env-docs-check`
Expected: passes (every `KDIVE_*` token documented). The `help`/`suggest` above satisfy it.

- [ ] **Step 6: Add `LockScope.BUILD_CONFIG`**

In `src/kdive/db/locks.py`, add `BUILD_CONFIG = "build_config"` to the `LockScope` enum (after `INVENTORY`). Update the enum docstring's total-order sentence is NOT required — `buildconfig.set` and the seed hold this scope alone (no co-hold), so it has no ordering constraint; add a one-line note to the class docstring: ``BUILD_CONFIG`` is keyed by the fragment **name** string and is always held alone (build-config set/seed serialization), so it sits outside the co-hold total order.

- [ ] **Step 7: Allowlist `0034` in the portability gate + meta-test**

In `scripts/m2_portability_gate.py`, in the `ALLOWED_FILES` set, after `"src/kdive/db/schema/0025_build_config_catalog.sql",` add `"src/kdive/db/schema/0034_build_config_catalog_source.sql",`. Make the identical addition in `tests/scripts/test_m2_portability_gate.py`'s expected frozenset (after the same `0025` line).

- [ ] **Step 8: Run the portability-gate meta-test + lint/type**

Run: `uv run python -m pytest tests/scripts/test_m2_portability_gate.py -q && just lint && just type`
Expected: PASS / clean.

- [ ] **Step 9: Commit**

```bash
git add src/kdive/db/schema/0034_build_config_catalog_source.sql src/kdive/config/core_settings.py src/kdive/db/locks.py scripts/m2_portability_gate.py tests/scripts/test_m2_portability_gate.py tests/db/test_migrate.py
git commit -m "feat(db): add build_config_catalog source column + lock scope (#438)"
```

---

## Task 2: Catalog repository — `source` field + two upsert writers

**Files:**
- Modify: `src/kdive/build_configs/catalog.py`
- Test: **Extend the existing** `tests/build_configs/test_seed_db.py` (the DB-backed home; it already does `from tests.db.conftest import migrated_url, pg_conn, postgres_url`). Do **not** add DB-backed cases to `tests/build_configs/test_catalog.py`/`test_seed.py` unless you also add the explicit fixture imports — the local `tests/build_configs/conftest.py` provides only `fake_conn`/`fake_store`, not `migrated_url`/`minio_store`.

> **Harness note (read before writing any test in this plan):** `tests/build_configs/` already contains `test_catalog.py`, `test_seed.py` (fake-double unit tests), and `test_seed_db.py` (DB-backed). The disposable-Postgres `migrated_url`/`pg_conn`/`postgres_url` fixtures live in `tests/db/conftest.py` and the `minio_store` fixture in `tests/store/conftest.py`; they are **only** in scope where explicitly imported. Every DB-backed test in this plan must include, at module top:
> ```python
> from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
> from tests.store.conftest import minio_store  # noqa: F401
> ```
> and re-export them via `__all__` (mirror `test_seed_db.py`). Omitting these yields a `fixture 'migrated_url' not found` collection error.

- [ ] **Step 1: Write the failing test (append to `tests/build_configs/test_seed_db.py`)**

```python
def test_operator_upsert_then_seed_guard_preserves_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await upsert_operator_build_config(conn, "kdump", "k/op", "shaop", "op desc")
            # seed-guarded upsert must NOT overwrite an operator row:
            await upsert_seed_build_config(conn, "kdump", "k/seed", "shaseed", "seed desc")
            entry = await get_build_config(conn, "kdump")
        assert entry is not None
        assert entry.source == "operator"
        assert entry.object_key == "k/op"
        assert entry.sha256 == "shaop"
        assert entry.description == "op desc"
    asyncio.run(_run())


def test_operator_upsert_empty_description_preserves_prior(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await upsert_seed_build_config(conn, "kdump", "k", "sha1", "kept desc")
            await upsert_operator_build_config(conn, "kdump", "k", "sha2", "")
            entry = await get_build_config(conn, "kdump")
        assert entry is not None
        assert entry.source == "operator"
        assert entry.description == "kept desc"
    asyncio.run(_run())
```

`test_seed_db.py` already imports `asyncio`, `psycopg`, and the db fixtures. Add `from kdive.build_configs.catalog import (get_build_config, upsert_operator_build_config, upsert_seed_build_config)` to its imports.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/build_configs/test_seed_db.py -q`
Expected: FAIL with ImportError (the two upsert functions do not exist yet). Skips if Docker is absent — run it where Docker is available, or rely on CI.

- [ ] **Step 3: Implement in `src/kdive/build_configs/catalog.py`**

Add `source: str` to `BuildConfigEntry` (after `description`). Update `_SELECT` to `... SELECT name, object_key, sha256, description, source FROM ...`. Update `parse_build_config_row` to set `source=_required_str(row, "source")`. Add:

```python
async def upsert_operator_build_config(
    conn: AsyncConnection, name: str, object_key: str, sha256: str, description: str
) -> None:
    """Upsert an operator-published fragment row (source='operator'), unconditionally.

    An empty ``description`` preserves the row's prior description instead of blanking it,
    so re-publishing bytes without a description keeps the seed's text (ADR-0119).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description, source) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s, 'operator') "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = COALESCE(NULLIF(EXCLUDED.description, ''), "
            "build_config_catalog.description, ''), "
            "source = 'operator', updated_at = now()",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": description},
        )


async def upsert_seed_build_config(
    conn: AsyncConnection, name: str, object_key: str, sha256: str, description: str
) -> None:
    """Upsert a seed-published fragment row (source='seed'), guarded against operator rows.

    The ``WHERE build_config_catalog.source = 'seed'`` conflict guard makes the database
    refuse to overwrite an operator override, so a later migrate never clobbers it (ADR-0119).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description, source) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s, 'seed') "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = EXCLUDED.description, source = 'seed', updated_at = now() "
            "WHERE build_config_catalog.source = 'seed'",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": description},
        )
```

Also update `get_build_config_sync`'s `parse_build_config_row` use (it already calls the same parser, so adding `source` to `_SELECT` + parser is enough).

Also update `get_build_config_sync`'s parser use (it shares `parse_build_config_row`, so adding `source` to `_SELECT` + the parser covers it). Check `tests/build_configs/test_catalog.py`: its existing fake-double / parser tests build a row dict for `parse_build_config_row` and may assert `BuildConfigEntry` fields — add `source` to any such row literal there so they still pass.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/build_configs/test_seed_db.py tests/build_configs/test_catalog.py -q`
Expected: PASS (or SKIP for the Docker-gated cases).

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/build_configs/catalog.py tests/build_configs/test_seed_db.py tests/build_configs/test_catalog.py
git commit -m "feat(build-configs): source field + operator/seed upsert writers (#438)"
```

---

## Task 3: Seed — source-aware under the per-name lock

**Files:**
- Modify: `src/kdive/build_configs/seed.py`
- **Retire** the fake-double seed tests: `tests/build_configs/test_seed.py` drives `seed_build_configs` through the `FakeConn`/`_FakeCursor` doubles in `tests/build_configs/conftest.py`. The new seed body uses `conn.transaction()` + `advisory_xact_lock` (a real `pg_advisory_xact_lock` call) and reads a `(sha256, source)` row, neither of which the fake doubles support (`FakeConn` has no `transaction()`; `_FakeCursor.fetchone` returns `{"sha256": ...}` with no `source`). The advisory lock is only meaningful against a real connection, so the fake-double seed path cannot be salvaged.
  - Delete the now-unrunnable `seed_build_configs` cases from `tests/build_configs/test_seed.py`. If that leaves the file with no remaining tests, delete the file. If the `fake_conn`/`fake_store` fixtures in `conftest.py` then have no users (`rg -n "fake_conn|fake_store" tests/`), delete them too.
  - Move/re-add the seed's behavioral coverage **DB-backed** in `tests/build_configs/test_seed_db.py` (Step 1 below).
- Test: append to `tests/build_configs/test_seed_db.py` (already DB-backed, already imports the db fixtures; add `from tests.store.conftest import minio_store  # noqa: F401` to its imports and `"minio_store"` to `__all__`).

- [ ] **Step 1: Write the failing test (append to `tests/build_configs/test_seed_db.py`)**

```python
def test_seed_skips_operator_override(migrated_url: str, minio_store: ObjectStore) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            # First seed publishes the packaged kdump fragment.
            assert await seed_build_configs(conn, minio_store) == 1
            # Operator overrides it.
            await upsert_operator_build_config(
                conn, "kdump", "system/build-configs/kdump/kdump.config", "operatorsha", "op"
            )
            # A later seed must skip (return 0) and leave the operator row + source intact.
            assert await seed_build_configs(conn, minio_store) == 0
            entry = await get_build_config(conn, "kdump")
        assert entry is not None
        assert entry.source == "operator"
        assert entry.sha256 == "operatorsha"
    asyncio.run(_run())
```

Imports (add to `test_seed_db.py`): `upsert_operator_build_config, get_build_config` from `kdive.build_configs.catalog` (seed already imported); the `minio_store` fixture import noted above.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/build_configs/test_seed_db.py -q`
Expected: FAIL — the current seed re-publishes on sha mismatch and overwrites the operator row. (Skips without Docker; run where available or via CI.)

- [ ] **Step 3: Implement the source-aware, locked seed in `src/kdive/build_configs/seed.py`**

Replace `_stored_sha` with a `(sha256, source)` read, drop the inline `_upsert` (use `upsert_seed_build_config`), and wrap the per-fragment publish in `advisory_xact_lock(BUILD_CONFIG, name)` inside an explicit transaction (the migrate connection is autocommit). New body for `seed_build_configs`:

```python
async def _stored_row(conn: AsyncConnection, name: str) -> tuple[str, str] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sha256, source FROM build_config_catalog WHERE name = %(name)s",
            {"name": name},
        )
        row = await cur.fetchone()
    return (row["sha256"], row["source"]) if row is not None else None


async def seed_build_configs(conn: AsyncConnection, store: ObjectStore) -> int:
    """Publish the packaged kdump fragment + upsert its row, source-aware. Returns 0 or 1.

    Serialized per fragment name on ``LockScope.BUILD_CONFIG`` (the same lock
    ``buildconfig.set`` takes), inside an explicit transaction since the migrate connection is
    autocommit, so a concurrent operator ``set`` cannot interleave with the read/PUT/upsert and
    the seed never PUTs over an operator override (ADR-0119). Idempotent: an unchanged seed-owned
    fragment writes nothing; an operator-owned row is skipped.
    """
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.BUILD_CONFIG, _KDUMP_NAME):
        stored = await _stored_row(conn, _KDUMP_NAME)
        if stored is not None and (stored == (sha256, "seed") or stored[1] == "operator"):
            return 0
        written = store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=_KDUMP_NAME,
                name="kdump.config",
                data=data,
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION_CLASS,
            )
        )
        await upsert_seed_build_config(conn, _KDUMP_NAME, written.key, sha256, _KDUMP_DESCRIPTION)
    return 1
```

Add imports: `from psycopg.rows import dict_row`, `from kdive.db.locks import LockScope, advisory_xact_lock`, `from kdive.build_configs.catalog import upsert_seed_build_config`. Remove the now-dead `_stored_sha` and `_upsert`.

Note: `store.put_artifact` is synchronous and the lock holds the transaction open across it; the seed runs in `migrate()`'s dedicated `asyncio.run`, so blocking the loop here is harmless and the lock is per-name.

- [ ] **Step 4: Run the whole build_configs suite + the tool test to verify nothing is left red**

Run: `uv run python -m pytest tests/build_configs/ tests/mcp/catalog/test_build_configs_tool.py -q`
Expected: PASS/SKIP — the new seed test passes, the retired fake-double tests are gone (not erroring), and the existing get tests still pass. A leftover fake-double seed test would surface here as an AttributeError — that is the signal the retirement in this task's Files section was incomplete.

- [ ] **Step 5: Lint/type + commit**

```bash
just lint && just type
git add src/kdive/build_configs/seed.py tests/build_configs/test_seed_db.py tests/build_configs/test_seed.py tests/build_configs/conftest.py
git commit -m "feat(build-configs): source-aware seed under per-name lock (#438)"
```

---

## Task 4: `buildconfig.set` tool + `buildconfig.get` surfaces `source`

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/build_configs.py`
- Modify: `tests/mcp/core/test_tool_docs.py:56` (tool→test map)
- Modify: `tests/mcp/catalog/test_build_configs_tool.py` (extend)
- Test: `tests/adversarial/test_build_config_concurrency.py` (create)
- Regenerate: the committed tool reference via `just docs`

- [ ] **Step 1: Write the failing tests (handler-level, injected pool + store + ctx)**

Extend `tests/mcp/catalog/test_build_configs_tool.py` (it lives under `tests/mcp/`, whose `conftest.py` already re-exports `migrated_url`/`minio_store` — so no extra fixture import is needed there, unlike `tests/build_configs/`). Add these module-level context builders (the literal construction from `tests/mcp/ops/test_diagnostics.py` / `test_breakglass.py`) and import `from kdive.security.authz.context import RequestContext`, `from kdive.security.authz.rbac import PlatformRole`, `from kdive.domain.errors import ErrorCategory`:

```python
_PLATFORM_ADMIN = RequestContext(
    principal="op-1", agent_session="sess-1", projects=(), roles={},
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)
_PLATFORM_OPERATOR = RequestContext(
    principal="op-1", agent_session="sess-1", projects=(), roles={},
    platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
)
```

Tests:

```python
def test_set_publishes_and_get_reports_operator_source(migrated_url, minio_store):
    async def _run():
        async with _pool(migrated_url) as pool:
            resp = await set_build_config(pool, minio_store, _PLATFORM_ADMIN, name="kdump",
                                          content="CONFIG_X=y\n", description="d")
            assert resp.status == "published"
            assert resp.data["source"] == "operator"
            async with pool.connection() as conn:
                got = await read_build_config(conn, minio_store, name="kdump")
        assert got.data["content"] == "CONFIG_X=y\n"
        assert got.data["source"] == "operator"
    asyncio.run(_run())


def test_set_requires_platform_admin(migrated_url, minio_store):
    async def _run():
        async with _pool(migrated_url) as pool:
            resp = await set_build_config(pool, minio_store, _PLATFORM_OPERATOR, name="kdump",
                                          content="x\n", description="")
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
    asyncio.run(_run())


def test_set_rejects_bad_name_and_empty_content(migrated_url, minio_store):
    async def _run():
        async with _pool(migrated_url) as pool:
            bad = await set_build_config(pool, minio_store, _PLATFORM_ADMIN, name="../etc",
                                         content="x\n", description="")
            empty = await set_build_config(pool, minio_store, _PLATFORM_ADMIN, name="kdump",
                                           content="", description="")
        assert bad.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert empty.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    asyncio.run(_run())
```

Note `ToolResponse.error_category` stores `category.value` (a string), so compare against `ErrorCategory.X.value`. Also add a `test_set_oversize_content_rejected` that `monkeypatch.setenv("KDIVE_MAX_BUILD_CONFIG_BYTES", "8")` and sends 9+ bytes, asserting `CONFIGURATION_ERROR.value`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/mcp/catalog/test_build_configs_tool.py -q`
Expected: FAIL — `set_build_config` does not exist; `read_build_config` does not yet return `source`.

- [ ] **Step 3: Implement in `src/kdive/mcp/tools/catalog/build_configs.py`**

Add `source` to `read_build_config`'s success `data` (`"source": entry.source`). Add the name regex, the cap read, and the `set_build_config` handler + `buildconfig.set` registration. Key code:

```python
import re
from kdive.build_configs.catalog import get_build_config, upsert_operator_build_config
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.config import require as _config_require  # or: import kdive.config as config
from kdive.config.core_settings import MAX_BUILD_CONFIG_BYTES
from kdive.domain.models import Sensitivity
from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles

_SET_TOOL = "buildconfig.set"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_DESCRIPTION_BYTES = 1024


async def set_build_config(
    pool: AsyncConnectionPool, store: ObjectStore, ctx: RequestContext,
    *, name: str, content: str, description: str,
) -> ToolResponse:
    """Publish/replace a build-config fragment (platform_admin; ADR-0119)."""
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(pool, ctx, tool=_SET_TOOL, scope=f"denied:{name}",
                                    args={"name": name})
        return ToolResponse.failure(name, ErrorCategory.AUTHORIZATION_DENIED,
                                    suggested_next_actions=[_SET_TOOL])
    if not _NAME_RE.match(name):
        return ToolResponse.failure(name, ErrorCategory.CONFIGURATION_ERROR,
                                    suggested_next_actions=[_SET_TOOL], data={"field": "name"})
    data = content.encode("utf-8")
    cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
    if not data or len(data) > cap:
        return ToolResponse.failure(name, ErrorCategory.CONFIGURATION_ERROR,
                                    suggested_next_actions=[_SET_TOOL],
                                    data={"field": "content", "limit": cap, "actual": len(data)})
    if len(description.encode("utf-8")) > _MAX_DESCRIPTION_BYTES:
        return ToolResponse.failure(name, ErrorCategory.CONFIGURATION_ERROR,
                                    suggested_next_actions=[_SET_TOOL], data={"field": "description"})
    sha256 = hashlib.sha256(data).hexdigest()
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.transaction(), \
                advisory_xact_lock(conn, LockScope.BUILD_CONFIG, name):
            written = await asyncio.to_thread(store.put_artifact, ArtifactWriteRequest(
                tenant="system", owner_kind="build-configs", owner_id=name,
                name=f"{name}.config", data=data, sensitivity=Sensitivity.REDACTED,
                retention_class="build-config"))
            await upsert_operator_build_config(conn, name, written.key, sha256, description)
            await audit.record_platform(conn, principal=ctx.principal,
                agent_session=ctx.agent_session, event=audit.PlatformAuditEvent(
                    tool=_SET_TOOL, scope=name,
                    args={"name": name, "sha256": sha256, "bytes": len(data)},
                    platform_role=held_platform_roles(ctx), actor=actor_for(ctx)))
    return ToolResponse.success(name, "published",
        suggested_next_actions=["buildconfig.get"],
        data={"name": name, "sha256": sha256, "bytes": len(data), "source": "operator"})
```

Register inside `register()`:

```python
@app.tool(name=_SET_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
async def buildconfig_set_tool(
    name: Annotated[str, Field(description="Fragment name (lowercase a-z0-9_-, e.g. kdump).")],
    content: Annotated[str, Field(description="The full kernel-config fragment text (UTF-8).")],
    description: Annotated[str, Field(description="Optional human label for the fragment.")] = "",
) -> ToolResponse:
    """Publish/replace a build-config fragment. Requires platform_admin; audited."""
    nonlocal _store
    if _store is None:
        _store = _resolve_store()
    return await set_build_config(pool, _store, current_context(), name=name,
                                  content=content, description=description)
```

Add `from kdive.log import bind_context` and `import kdive.config as config` and `import hashlib`. Confirm `record_platform` runs on the same `conn`/transaction as the upsert (atomic row+audit).

- [ ] **Step 4: Add the tool→test map entry**

In `tests/mcp/core/test_tool_docs.py`, after the `"buildconfig.get"` line (~56) add:
`    "buildconfig.set": ("tests/mcp/catalog/test_build_configs_tool.py",),`

- [ ] **Step 5: Run the tool tests + tool-docs test**

Run: `uv run python -m pytest tests/mcp/catalog/test_build_configs_tool.py tests/mcp/core/test_tool_docs.py -q`
Expected: PASS. (`test_active_tools_have_a_covering_test` needs the map entry; `read_build_config` now returns `source`.)

- [ ] **Step 6: Write the adversarial concurrency test**

`tests/adversarial/test_build_config_concurrency.py` (new file). `tests/adversarial/conftest.py` does **not** re-export the db/store fixtures, so add at module top:
```python
from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
from tests.store.conftest import minio_store  # noqa: F401
```
and re-export via `__all__`. Build the `_PLATFORM_ADMIN` `RequestContext` literal as in Step 1. Drive two concurrent `set_build_config` calls for the same name with different content via `asyncio.gather` over a pool (each `set` opens its own pool connection + lock); after both settle, read the row and fetch the object, asserting `entry.sha256 == hashlib.sha256(object_bytes).hexdigest()` (the per-name lock keeps row and object in agreement). Add a seed/set interleave: `asyncio.gather(seed_build_configs(conn_a, store), set_build_config(pool, store, _PLATFORM_ADMIN, name="kdump", ...))` on the same name and assert the same row/object agreement and that the surviving row's `source` is consistent with its bytes.

Run: `uv run python -m pytest tests/adversarial/test_build_config_concurrency.py -q`
Expected: PASS (skips if Docker/MinIO absent).

- [ ] **Step 7: Regenerate the tool reference doc**

Run: `just docs` (mutating), then `just docs-check`.
Expected: `docs-check` passes after regeneration. Stage the regenerated reference file.

- [ ] **Step 8: Full local gate + commit**

```bash
just ci
git add src/kdive/mcp/tools/catalog/build_configs.py tests/mcp/catalog/test_build_configs_tool.py tests/mcp/core/test_tool_docs.py tests/adversarial/test_build_config_concurrency.py docs/  # the regenerated reference
git commit -m "feat(mcp): add buildconfig.set write tool + get source (#438)"
```

---

## Self-Review checklist (run after implementation, before the branch review loop)

- Spec coverage: AC#1 (Task 4 tool) · AC#2 (Task 4 platform_admin + record_platform) · AC#3 (Task 1 column + Task 3 source-guarded locked seed + Task 2 guarded writer). Read-path provenance (Task 4 get). All failure-table rows have a test (Tasks 2-4).
- Type consistency: `upsert_operator_build_config` / `upsert_seed_build_config` / `set_build_config` / `LockScope.BUILD_CONFIG` / `MAX_BUILD_CONFIG_BYTES` names used identically across tasks.
- Guards: migration in `test_migrate` (T1), portability gate + meta-test (T1), tool-docs map (T4), generated reference (T4), env-docs (T1).

## Rollback / cleanup

Each task is an independent commit; revert a task's commit to back it out. The migration is additive (`ADD COLUMN ... DEFAULT 'seed'`) — backing out the feature leaves the column harmlessly (forward-only migration runner; do not delete an applied migration file). No object-store cleanup needed (in-place reserved key, no orphans).
