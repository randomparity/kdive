# Local-libvirt staged-path catalog source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an operator-staged local-libvirt rootfs a first-class `image_catalog` entry discoverable through the existing catalog surface and provisionable via the (newly-wired) `catalog` rootfs lane.

**Architecture:** Add a `staged-path` inventory image source that seeds an `image_catalog` row carrying a host `path` (new column, no S3 object). Wire the previously-unwired local-libvirt catalog rootfs materialization lane with a synchronous, public-scope, arch-matched fetch (mirroring `build_config_fetch_from_env`) that branches: `path` → validate against `allowed_roots`; `object_key` → S3 fetch + digest + cache. Discovery (`fixtures.list`/`images.list`/`profile_examples`) needs no schema change — the row surfaces by `(provider, name, arch)` and the agent provisions `{kind:catalog, provider, name}`.

**Tech Stack:** Python 3.14, `uv`, `psycopg` (sync + async), pydantic v2, Postgres, FastMCP. See ADR-0228 and `docs/design/2026-06-23-local-staged-path-catalog-source.md`.

## Global Constraints

- Python 3.14; deps via `uv`; lint/format `ruff` (line length 100, set `E,F,I,UP,B,SIM`); types `ty` (whole tree, strict).
- Guardrail commands: `just lint`, `just type`, `just test`; full gate `just ci` (lint type lock-check lint-shell lint-ansible test-ansible lint-workflows check-mermaid docs-links docs-paths adr-status-check docs-check config-docs-check config-guard env-docs-check resources-docs-check chart-version-check test). Run a single test: `uv run python -m pytest <path>::<name> -q`.
- Doc-style: use "Milestone" not "Sprint"; avoid "critical/robust/comprehensive/elegant". ADRs cited in `src/` must be `Accepted`.
- Error taxonomy: return `CategorizedError` with the most specific existing `ErrorCategory`; never invent strings. Config problems → `CONFIGURATION_ERROR`; infra/IO → `INFRASTRUCTURE_FAILURE`.
- No-leak (ADR-0123): the absolute `path` must never appear in an MCP response.
- ADR/migration numbers are assigned: **ADR-0228**, **migration 0047**.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- `src/kdive/inventory/model.py` — add `StagedPathSource`; extend `ImageSource`; reject `staged-path` + private.
- `src/kdive/db/schema/0047_image_catalog_staged_path.sql` — new migration (path column + 3-way CHECK).
- `src/kdive/domain/catalog/images.py` — `ImageCatalogEntry.path`.
- `src/kdive/inventory/reconcile_images.py` — `_realize` returns `path`; INSERT/UPDATE carry `path`.
- `src/kdive/images/catalog.py` — `resolve_public_rootfs_sync(conn, provider, name, arch)`.
- `src/kdive/images/fetch.py` — `fetch_registered_rootfs_sync(...)` (staged-path + s3 branch).
- `src/kdive/providers/local_libvirt/lifecycle/rootfs_catalog_fetch.py` — `rootfs_catalog_fetch_from_env(allowed_roots)`.
- `src/kdive/providers/local_libvirt/lifecycle/materialize.py` — `CatalogFetch` gains `arch`; context gains `arch`.
- `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` — wire `catalog_fetch`, thread `arch`.
- `src/kdive/inventory/serialize.py` — export `staged-path` round-trip.
- `examples/local-libvirt/README.md`, `systems.toml.example` — declare a staged-path image; drop the host `ls`.

---

### Task 1: Inventory `StagedPathSource` model + validation

**Files:**
- Modify: `src/kdive/inventory/model.py` (after `StagedSource`, ~line 54-62; `ImageEntry` ~65-80)
- Test: `tests/inventory/test_model.py` (or the existing image-model test file; check `rg -l "ImageEntry" tests/inventory`)

**Interfaces:**
- Produces: `StagedPathSource(kind: Literal["staged-path"], path: str)` (absolute); `ImageSource` union now includes it.

- [ ] **Step 1: Write failing tests** — parse a staged-path image; reject relative path; reject `staged-path` + `visibility="private"`.

```python
import pytest
from pydantic import ValidationError
from kdive.inventory.model import ImageEntry, StagedPathSource

def _entry(**over):
    base = dict(provider="local-libvirt", name="fedora", arch="x86_64", format="qcow2",
                root_device="/dev/vda", visibility="public",
                source={"kind": "staged-path", "path": "/var/lib/kdive/rootfs/fedora.qcow2"})
    base.update(over)
    return ImageEntry.model_validate(base)

def test_staged_path_source_parses():
    e = _entry()
    assert isinstance(e.source, StagedPathSource)
    assert e.source.path == "/var/lib/kdive/rootfs/fedora.qcow2"

def test_staged_path_rejects_relative_path():
    with pytest.raises(ValidationError):
        _entry(source={"kind": "staged-path", "path": "rootfs/fedora.qcow2"})

def test_staged_path_rejects_private_visibility():
    with pytest.raises(ValidationError):
        _entry(visibility="private")  # staged-path is public-only by contract
```

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/inventory/test_model.py -k staged_path -q` → FAIL (StagedPathSource missing / no validator).

- [ ] **Step 3: Implement** in `model.py`:

```python
class StagedPathSource(BaseModel):
    """An image backed by an operator-staged rootfs file under a local-libvirt provider root.

    The local analog of ``StagedSource`` (which names a libvirt storage-pool volume): ``path`` is
    an absolute host path validated against the provider's ``allowed_roots`` at provision time.
    No S3 object, no digest (ADR-0228).
    """

    kind: Literal["staged-path"]
    path: str

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("staged-path source path must be absolute")
        return value


ImageSource = Annotated[
    S3Source | BuildSource | StagedSource | StagedPathSource, Field(discriminator="kind")
]
```

Add a model validator on `ImageEntry` (after the `identity` property) rejecting private staged-path:

```python
    @model_validator(mode="after")
    def _staged_path_is_public(self) -> ImageEntry:
        if isinstance(self.source, StagedPathSource) and self.visibility != ImageVisibility.PUBLIC:
            raise ValueError(
                "a staged-path image must be public; project-private local staged-path is not "
                "supported (it would be discoverable via images.list but unresolvable)"
            )
        return self
```

Ensure `field_validator` and `model_validator` are imported from `pydantic` at the top of `model.py`.

- [ ] **Step 4: Run** — `uv run python -m pytest tests/inventory/test_model.py -k staged_path -q` → PASS. Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/inventory/model.py tests/inventory/test_model.py
git commit -m "feat(inventory): add public-only staged-path image source

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Migration 0047 — `image_catalog.path` column + 3-way CHECK + domain field

**Files:**
- Create: `src/kdive/db/schema/0047_image_catalog_staged_path.sql`
- Modify: `src/kdive/domain/catalog/images.py` (`ImageCatalogEntry`, add `path`)
- Test: `tests/db/test_image_catalog_migration.py`

**Interfaces:**
- Produces: `image_catalog.path text` (nullable); non-`defined` rows carry exactly one of `{object_key, volume, path}`. `ImageCatalogEntry.path: str | None = None`.

- [ ] **Step 1: Write failing migration tests** in `tests/db/test_image_catalog_migration.py` (mirror the existing `_insert_image` helper — read it first; it likely needs a `path` kwarg):

```python
def test_staged_path_row_accepts_path_only(pg_conn):
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="registered", object_key=None, volume=None, path="/var/lib/kdive/rootfs/x.img", digest=None)

def test_registered_row_rejects_two_of_three_sources(pg_conn):
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="registered", object_key="images/x", volume=None, path="/var/lib/kdive/rootfs/x.img", digest="sha256:" + "0"*64)

def test_registered_row_rejects_no_source(pg_conn):
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="registered", object_key=None, volume=None, path=None, digest=None)

def test_defined_row_rejects_path(pg_conn):
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="defined", object_key=None, volume=None, path="/var/lib/kdive/rootfs/x.img", digest=None)
```

Update `_insert_image` to accept and bind `path` (column list + value).

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/db/test_image_catalog_migration.py -k "staged_path or two_of_three or no_source or rejects_path" -q` → FAIL (no `path` column).

- [ ] **Step 3: Implement migration** `0047_image_catalog_staged_path.sql`:

```sql
-- 0047_image_catalog_staged_path.sql — local-libvirt staged-path image source (ADR-0228, #732).
-- Additive, forward-only (ADR-0015). A registered local-libvirt rootfs may be an operator-staged
-- host file under the provider allowed_roots, carried as `path` instead of an S3 `object_key` or a
-- storage-pool `volume`. Reworks image_object_present from the 2-way object_key/volume exactly-one
-- (migration 0030) to a 3-way object_key/volume/path exactly-one for non-'defined' rows; a
-- 'defined' row still carries none of the three. Existing rows satisfy the new CHECK unchanged
-- (path defaults NULL): a registered s3 row has object_key only, a registered staged row has
-- volume only, a defined row has none.

ALTER TABLE image_catalog ADD COLUMN path text;

ALTER TABLE image_catalog DROP CONSTRAINT image_object_present;
ALTER TABLE image_catalog ADD CONSTRAINT image_object_present CHECK (
    (state = 'defined' AND object_key IS NULL AND volume IS NULL AND path IS NULL)
    OR (
        state <> 'defined'
        AND (
            (object_key IS NOT NULL)::int + (volume IS NOT NULL)::int + (path IS NOT NULL)::int = 1
        )
    )
);
```

Add `path` to `ImageCatalogEntry` (`domain/catalog/images.py`) next to `volume`:

```python
    volume: str | None = None
    path: str | None = None
```

- [ ] **Step 4: Run** — `uv run python -m pytest tests/db/test_image_catalog_migration.py -q` → PASS (and existing migration tests stay green). Then `just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/schema/0047_image_catalog_staged_path.sql src/kdive/domain/catalog/images.py tests/db/test_image_catalog_migration.py
git commit -m "feat(db): add image_catalog.path with 3-way source CHECK (migration 0047)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Seeding — `_realize` staged-path branch + INSERT/UPDATE carry `path`

**Files:**
- Modify: `src/kdive/inventory/reconcile_images.py` (`_realize` ~271-287; `_create_entry` ~198-227; `_update_entry` ~229-268)
- Test: `tests/integration/test_reconcile_inventory.py`

**Interfaces:**
- Consumes: `StagedPathSource` (Task 1), `image_catalog.path` (Task 2).
- Produces: `_realize` returns `(state, object_key, volume, path, digest, warning)` (6-tuple). A staged-path entry → `("registered", None, None, source.path, None, None)`.

- [ ] **Step 1: Write failing test** — reconcile a staged-path `[[image]]` and assert the row:

```python
async def test_reconcile_seeds_staged_path_registered(pg_async_conn, ...):
    doc = _doc_with_image(provider="local-libvirt", name="fed", arch="x86_64",
                          source={"kind": "staged-path", "path": "/var/lib/kdive/rootfs/fed.img"})
    await reconcile_images(pg_async_conn, doc, store=_no_head_store())
    row = await _fetch_image(pg_async_conn, "local-libvirt", "fed", "x86_64")
    assert row["state"] == "registered"
    assert row["path"] == "/var/lib/kdive/rootfs/fed.img"
    assert row["object_key"] is None and row["volume"] is None and row["digest"] is None
```

Use the existing test helpers in `test_reconcile_inventory.py` (read `_insert_config_staged_row` ~152 and the doc builders; mirror their shapes).

- [ ] **Step 2: Run to verify it fails** — `uv run python -m pytest tests/integration/test_reconcile_inventory.py -k staged_path -q` → FAIL (path not seeded; `_realize` arity).

- [ ] **Step 3: Implement.** In `_realize`, change the return type to a 6-tuple and add the branch (place it before `StagedSource`):

```python
def _realize(
    entry: ImageEntry, row: dict[str, object] | None, head: _S3Head
) -> tuple[str, str | None, str | None, str | None, str | None, str | None]:
    """Compute ``(state, object_key, volume, path, digest, warning)`` for an entry."""
    source = entry.source
    if isinstance(source, StagedPathSource):
        return (_REGISTERED, None, None, source.path, None, None)
    if isinstance(source, StagedSource):
        return (_REGISTERED, None, source.volume, None, None, None)
    if isinstance(source, BuildSource):
        state, object_key, volume, digest, warning = _realize_build(entry, row)
        return (state, object_key, volume, None, digest, warning)
    if isinstance(source, S3Source):
        state, object_key, volume, digest, warning = _realize_s3(entry, row, source, head)
        return (state, object_key, volume, None, digest, warning)
    raise AssertionError(f"unhandled image source kind: {source!r}")  # pragma: no cover
```

Update `_create_entry` to unpack 6 and add `path` to the column list + values:

```python
    state, object_key, volume, path, digest, warning = _realize(entry, None, head)
    await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, visibility, capabilities, "
        " object_key, volume, path, digest, state, managed_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (entry.provider, entry.name, entry.arch, entry.format, entry.root_device,
         entry.visibility.value, entry.capabilities, object_key, volume, path, digest,
         state, CONFIG_MANAGED_BY),
    )
```

Update `_update_entry`: unpack 6, add `path` to the `realized` change-detection dict and the UPDATE SET list:

```python
    state, object_key, volume, path, digest, warning = _realize(entry, row, head)
    realized = {"object_key": object_key, "volume": volume, "path": path, "digest": digest, "state": state}
    ...
        "UPDATE image_catalog SET format = %s, root_device = %s, visibility = %s, "
        "capabilities = %s, object_key = %s, volume = %s, path = %s, digest = %s, state = %s "
        "WHERE id = %s",
        (..., object_key, volume, path, digest, state, row["id"]),
```

Import `StagedPathSource` in `reconcile_images.py`.

- [ ] **Step 4: Run** — `uv run python -m pytest tests/integration/test_reconcile_inventory.py -q` → PASS. `just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/inventory/reconcile_images.py tests/integration/test_reconcile_inventory.py
git commit -m "feat(inventory): seed staged-path images into image_catalog

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Sync, arch-matched, public-scope resolver

**Files:**
- Modify: `src/kdive/images/catalog.py` (add `resolve_public_rootfs_sync`)
- Test: `tests/images/test_catalog_resolver.py`

**Interfaces:**
- Produces: `resolve_public_rootfs_sync(conn: psycopg.Connection, provider: str, name: str, arch: str) -> ImageCatalogEntry | None` — returns the one registered, public, arch-matched row, else None.

- [ ] **Step 1: Write failing tests** — matches arch; misses on wrong arch; ignores private.

```python
def test_resolve_public_sync_matches_arch(pg_conn):
    _insert_registered(pg_conn, provider="local-libvirt", name="fed", arch="x86_64", path="/r/x.img")
    _insert_registered(pg_conn, provider="local-libvirt", name="fed", arch="aarch64", path="/r/a.img")
    row = resolve_public_rootfs_sync(pg_conn, "local-libvirt", "fed", "x86_64")
    assert row is not None and row.path == "/r/x.img"

def test_resolve_public_sync_misses_unknown_arch(pg_conn):
    _insert_registered(pg_conn, provider="local-libvirt", name="fed", arch="x86_64", path="/r/x.img")
    assert resolve_public_rootfs_sync(pg_conn, "local-libvirt", "fed", "riscv64") is None

def test_resolve_public_sync_ignores_private(pg_conn):
    _insert_registered(pg_conn, provider="local-libvirt", name="fed", arch="x86_64", path="/r/x.img", visibility="private", owner="proj")
    assert resolve_public_rootfs_sync(pg_conn, "local-libvirt", "fed", "x86_64") is None
```

- [ ] **Step 2: Run to verify fail** — `uv run python -m pytest tests/images/test_catalog_resolver.py -k public_sync -q` → FAIL.

- [ ] **Step 3: Implement** in `catalog.py`:

```python
import psycopg  # add to imports

_RESOLVE_PUBLIC_SYNC_SQL = """
    SELECT *
    FROM image_catalog
    WHERE provider = %(provider)s
      AND name = %(name)s
      AND arch = %(arch)s
      AND state = %(registered)s
      AND visibility = %(public)s
    LIMIT 1
"""


def resolve_public_rootfs_sync(
    conn: psycopg.Connection, provider: str, name: str, arch: str
) -> ImageCatalogEntry | None:
    """Resolve the one registered, public, arch-matched rootfs image (sync, public-scope).

    The local-libvirt catalog rootfs lane resolves public images only (ADR-0228); ``arch`` makes
    the match deterministic via the ``(provider, name, arch)`` unique index. Returns ``None`` when
    no registered public image of that arch is declared.
    """
    params = {
        "provider": provider, "name": name, "arch": arch,
        "registered": ImageState.REGISTERED.value, "public": ImageVisibility.PUBLIC.value,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_RESOLVE_PUBLIC_SYNC_SQL, params)
        row = cur.fetchone()
    return None if row is None else ImageCatalogEntry.model_validate(row)
```

(`dict_row` is already imported in `catalog.py`.)

- [ ] **Step 4: Run** — `uv run python -m pytest tests/images/test_catalog_resolver.py -q` → PASS. `just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/catalog.py tests/images/test_catalog_resolver.py
git commit -m "feat(images): add sync arch-matched public rootfs resolver

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Sync catalog fetch lane (staged-path + s3 branch)

**Files:**
- Modify: `src/kdive/images/fetch.py` (add `fetch_registered_rootfs_sync`; factor the cache write so async + sync share it, or duplicate minimally)
- Test: `tests/images/test_fetch.py`

**Interfaces:**
- Consumes: `resolve_public_rootfs_sync` (Task 4), `validate_local_component_path` (`kdive.components.local_paths`).
- Produces: `fetch_registered_rootfs_sync(conn, store_factory, *, allowed_roots: list[Path], provider, name, arch, cache_dir: Path) -> Path`. `store_factory: Callable[[], RootfsObjectStore]` is called **only** on the s3 branch — staged-path resolution must never require an object store (the no-S3 lane, ADR-0228).

- [ ] **Step 1: Write failing tests** — staged-path returns the validated path *and never invokes the store factory*; a path outside roots raises CONFIGURATION_ERROR; s3 fetches+caches+digest-checks.

```python
def _exploding_factory():
    def _f():
        raise AssertionError("store factory must not be called for staged-path")
    return _f

def test_sync_fetch_staged_path_returns_validated_path_without_store(pg_conn, tmp_path):
    f = tmp_path / "x.img"; f.write_bytes(b"data")
    _insert_registered(pg_conn, provider="local-libvirt", name="fed", arch="x86_64", path=str(f))
    out = fetch_registered_rootfs_sync(pg_conn, _exploding_factory(),
        allowed_roots=[tmp_path], provider="local-libvirt", name="fed", arch="x86_64", cache_dir=tmp_path / ".cache")
    assert out == f.resolve()  # factory never called -> no AssertionError

def test_sync_fetch_staged_path_outside_roots_rejected(pg_conn, tmp_path):
    outside = tmp_path / "x.img"; outside.write_bytes(b"d")
    _insert_registered(pg_conn, provider="local-libvirt", name="fed", arch="x86_64", path=str(outside))
    with pytest.raises(CategorizedError) as ei:
        fetch_registered_rootfs_sync(pg_conn, _exploding_factory(), allowed_roots=[tmp_path / "roots"],
            provider="local-libvirt", name="fed", arch="x86_64", cache_dir=tmp_path / ".cache")
    assert ei.value.category is ErrorCategory.CONFIGURATION_ERROR

def test_sync_fetch_s3_downloads_and_caches(pg_conn, tmp_path):
    data = b"qcow"; digest = "sha256:" + hashlib.sha256(data).hexdigest()
    _insert_registered(pg_conn, provider="local-libvirt", name="img", arch="x86_64", object_key="images/img", digest=digest)
    out = fetch_registered_rootfs_sync(pg_conn, lambda: _store_returning(data), allowed_roots=[tmp_path],
        provider="local-libvirt", name="img", arch="x86_64", cache_dir=tmp_path / ".cache")
    assert out.read_bytes() == data
```

- [ ] **Step 2: Run to verify fail** — `uv run python -m pytest tests/images/test_fetch.py -k sync -q` → FAIL.

- [ ] **Step 3: Implement** `fetch_registered_rootfs_sync` in `fetch.py`. Reuse `_cache_path`, `_cache_io_error`, `_unlink_tmp_cache`. The store's `get_artifact` is synchronous; call it directly (no `to_thread`). **The store is built lazily via `store_factory`, only on the s3 branch**, so staged-path never touches object storage:

```python
from collections.abc import Callable
from pathlib import Path
from kdive.components.local_paths import validate_local_component_path
from kdive.images.catalog import resolve_public_rootfs_sync
import psycopg


def fetch_registered_rootfs_sync(
    conn: psycopg.Connection,
    store_factory: Callable[[], RootfsObjectStore],
    *,
    allowed_roots: list[Path],
    provider: str,
    name: str,
    arch: str,
    cache_dir: Path,
) -> Path:
    """Resolve a registered public rootfs and return a provider-readable local path (sync).

    A staged-path row resolves to its ``path`` validated against ``allowed_roots`` (no object
    store, no cache, no digest) — ``store_factory`` is never called. An s3 row builds the store
    via ``store_factory``, downloads ``object_key``, verifies sha256 against ``digest``, and
    caches it under a digest-keyed file in ``cache_dir``.
    """
    row = resolve_public_rootfs_sync(conn, provider, name, arch)
    if row is None:
        raise CategorizedError(
            "unknown registered rootfs catalog entry",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name, "arch": arch},
        )
    if row.path is not None:
        return validate_local_component_path(row.path, allowed_roots=allowed_roots)
    store = store_factory()
    object_key, digest = row.object_key, row.digest
    if object_key is None or digest is None:
        raise CategorizedError(
            "registered rootfs row is missing its object_key or digest",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"provider": provider, "name": name},
        )
    cached = _cache_path(cache_dir, digest)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if cached.is_file():
            return cached
    except OSError as err:
        raise _cache_io_error(provider=provider, name=name, object_key=object_key, cache_path=cached, err=err) from err
    fetched = store.get_artifact(object_key, None)
    actual = "sha256:" + hashlib.sha256(fetched.data).hexdigest()
    if actual != digest:
        raise CategorizedError(
            "fetched rootfs object digest does not match the catalog row",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"provider": provider, "name": name, "object_key": object_key},
        )
    tmp = cached.with_suffix(".qcow2.partial")
    try:
        tmp.write_bytes(fetched.data)
        tmp.replace(cached)
    except OSError as err:
        _unlink_tmp_cache(tmp)
        raise _cache_io_error(provider=provider, name=name, object_key=object_key, cache_path=cached, err=err) from err
    return cached
```

- [ ] **Step 4: Run** — `uv run python -m pytest tests/images/test_fetch.py -q` → PASS. `just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/fetch.py tests/images/test_fetch.py
git commit -m "feat(images): sync rootfs fetch branching staged-path vs s3

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Wire the lane — `CatalogFetch` arch arg, `from_env` fetch, provisioner threading

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/materialize.py` (`CatalogFetch` gains `arch`; context gains `arch`; `_materialize_catalog_rootfs` passes it)
- Create: `src/kdive/providers/local_libvirt/lifecycle/rootfs_catalog_fetch.py` (`rootfs_catalog_fetch_from_env`)
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (`MaterializeRootfs` gains `arch`; `from_env` wires `catalog_fetch`; `provision` passes `profile.arch`; `_materialize_rootfs_base` builds context with `arch` + `catalog_fetch`)
- Test: `tests/providers/local_libvirt/test_materialize.py`, `tests/providers/local_libvirt/test_provisioning.py`

**Interfaces:**
- Consumes: `fetch_registered_rootfs_sync` (Task 5).
- Produces: `CatalogFetch = Callable[[CatalogComponentRef, str], Path]`; `RootfsMaterializationContext.arch: str`; `rootfs_catalog_fetch_from_env(allowed_roots: list[Path]) -> CatalogFetch`; `MaterializeRootfs = Callable[[RootfsSource, UUID, str], str]`.

- [ ] **Step 1: Write failing tests.**
  - `test_materialize.py`: a staged-path catalog ref resolves through an injected `catalog_fetch` to the validated path; the fetch receives the arch.
  - `test_provisioning.py`: `provision` with a `{kind:catalog}` rootfs threads `profile.arch` into the fetch (use a recording fake `catalog_fetch`).

```python
# test_materialize.py
def test_catalog_staged_path_materializes_via_fetch(tmp_path):
    f = tmp_path / "x.img"; f.write_bytes(b"d")
    seen = {}
    def _fetch(ref, arch):
        seen["arch"] = arch; return f
    ref = CatalogComponentRef(kind="catalog", provider="local-libvirt", name="fed")
    out = materialize_rootfs_base(ref, context=RootfsMaterializationContext(
        allowed_roots=[tmp_path], arch="x86_64", catalog_fetch=_fetch))
    assert out == f and seen["arch"] == "x86_64"
```

- [ ] **Step 2: Run to verify fail** — `uv run python -m pytest tests/providers/local_libvirt/test_materialize.py -k staged_path -q` → FAIL (context has no `arch`; fetch arity).

- [ ] **Step 3: Implement.**
  - `materialize.py`: `type CatalogFetch = Callable[[CatalogComponentRef, str], Path]`; add `arch: str` to `RootfsMaterializationContext` (place before the defaulted fields, or give it no default and update call sites); in `_materialize_catalog_rootfs` return `context.catalog_fetch(ref, context.arch)`.
  - New `rootfs_catalog_fetch.py`:

```python
"""Synchronous rootfs catalog fetch for the local-libvirt provision lane (ADR-0228)."""
from __future__ import annotations

from pathlib import Path

import psycopg

import kdive.config as config
from kdive.components.references import CatalogComponentRef
from kdive.config.core_settings import DATABASE_URL
from kdive.images.fetch import fetch_registered_rootfs_sync
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import CatalogFetch
from kdive.providers.local_libvirt.lifecycle.storage import ROOTFS_DIR
from kdive.store.objectstore import object_store_from_env

# The s3-fetch cache lives OUTSIDE allowed_roots (which default to [ROOTFS_DIR]) so a cached image
# is never reachable as a staged-path candidate, keeping the spec's isolation invariant true.
_CACHE_DIR = Path(ROOTFS_DIR).parent / "rootfs-cache"


def rootfs_catalog_fetch_from_env(allowed_roots: list[Path]) -> CatalogFetch:
    """A sync ``(ref, arch) -> Path`` rootfs catalog fetch (mirrors build_config_fetch_from_env).

    Opens a short-lived sync ``psycopg`` connection per call (the provision seam runs in a thread
    and owns no async pool). Resolves the registered **public** image of ``arch`` and branches: a
    staged-path row validates its host path against ``allowed_roots`` (**no object store touched**);
    an s3 row builds the object store lazily, downloads + digest-verifies + caches under
    ``_CACHE_DIR`` (outside ``allowed_roots``). Passing ``object_store_from_env`` as a factory keeps
    staged-path provisioning working when no object storage is configured (the no-S3 lane).
    """

    def _fetch(ref: CatalogComponentRef, arch: str) -> Path:
        with psycopg.connect(config.require(DATABASE_URL)) as conn:
            return fetch_registered_rootfs_sync(
                conn, object_store_from_env,
                allowed_roots=allowed_roots, provider=ref.provider, name=ref.name, arch=arch,
                cache_dir=_CACHE_DIR,
            )

    return _fetch
```

  - `provisioning.py`:
    - `type MaterializeRootfs = Callable[[RootfsSource, UUID, str], str]`.
    - `__init__`: add `catalog_fetch: CatalogFetch | None = None`; store `self._catalog_fetch = catalog_fetch`.
    - `from_env`: `return cls(connect=lambda: libvirt.open(host_uri), catalog_fetch=rootfs_catalog_fetch_from_env([Path(ROOTFS_DIR)]))` (import `rootfs_catalog_fetch_from_env`).
    - `provision`: line 185 `base = self._materialize_rootfs(section.rootfs, system_id, profile.arch)`.
    - `_materialize_rootfs_base(self, rootfs, system_id, arch)`: pass `arch=arch, catalog_fetch=self._catalog_fetch` into `RootfsMaterializationContext`.
    - the validate-only call at line 309 (`self._materialize_rootfs_base(rootfs, UUID(int=0))`) → pass a placeholder arch, e.g. the profile arch if available there, else `"x86_64"`; check the caller (`validate_rootfs_ref`) for the arch and thread it if present.

- [ ] **Step 4: Run** — `uv run python -m pytest tests/providers/local_libvirt/test_materialize.py tests/providers/local_libvirt/test_provisioning.py -q` → PASS. `just type`. (Update the existing test doubles whose `materialize_rootfs=lambda rootfs, system_id: ...` now need a third `arch` param — grep them.)

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/ tests/providers/local_libvirt/
git commit -m "feat(local-libvirt): wire catalog rootfs lane (staged-path + s3)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: No-leak — `path` never reaches an MCP response

**Files:**
- Test: `tests/mcp/catalog/test_images_list.py`, `tests/mcp/catalog/test_fixtures_list.py`
- Modify only if a leak is found: `src/kdive/mcp/tools/catalog/images.py`, `fixtures.py`

**Interfaces:** none new — this locks the existing allowlist projection.

- [ ] **Step 1: Write tests** asserting a staged-path row surfaces by `(provider, name, arch)` but no response field carries its path:

```python
async def test_images_list_omits_staged_path(pool):
    await _insert_registered(pool, provider="local-libvirt", name="fed", arch="x86_64", path="/var/lib/kdive/rootfs/secret.img", visibility="public")
    resp = await list_images(pool, _ctx())
    blob = json.dumps(resp.model_dump())
    assert "/var/lib/kdive/rootfs/secret.img" not in blob
    assert any(i.data["name"] == "fed" for i in resp.items)

async def test_fixtures_list_omits_staged_path(pool):
    await _insert_registered(pool, provider="local-libvirt", name="fed", arch="x86_64", path="/var/lib/kdive/rootfs/secret.img", visibility="public")
    resp = await list_fixtures(pool)
    assert "/var/lib/kdive/rootfs/secret.img" not in json.dumps(resp.model_dump())
```

- [ ] **Step 2: Run** — `uv run python -m pytest tests/mcp/catalog/test_images_list.py tests/mcp/catalog/test_fixtures_list.py -k staged_path -q`. Expected PASS already (the envelopes are allowlists). If FAIL, the row envelope leaked `path` — fix `_row_envelope`/`_public_rows` to keep their explicit allowlist (do not add `path`).

- [ ] **Step 3:** No implementation expected; only act on a real leak.

- [ ] **Step 4: Run** — confirm PASS. `just type`.

- [ ] **Step 5: Commit**

```bash
git add tests/mcp/catalog/test_images_list.py tests/mcp/catalog/test_fixtures_list.py
git commit -m "test(catalog): lock no-leak of staged-path path in listings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Export round-trip — serialize a staged-path row back to `[image.source]`

**Files:**
- Modify: `src/kdive/inventory/serialize.py` (`ImageRow` add `path`; `_emit_image_source` staged-path branch; the export query that builds `ImageRow` — grep `ImageRow(` / `SELECT ... image_catalog`)
- Test: `tests/inventory/test_serialize.py`

**Interfaces:**
- Consumes: `image_catalog.path` (Task 2).
- Produces: `_emit_image_source` emits `kind = "staged-path"` + `path` when `image.path is not None`.

- [ ] **Step 1: Write failing test:**

```python
def test_emit_staged_path_source():
    row = ImageRow(provider="local-libvirt", name="fed", arch="x86_64", format="qcow2",
                   root_device="/dev/vda", visibility="public", capabilities=[],
                   object_key=None, volume=None, path="/var/lib/kdive/rootfs/fed.img", digest=None, state="registered")
    out = "\n".join(_emit_image_source(row))
    assert 'kind = "staged-path"' in out and 'path = "/var/lib/kdive/rootfs/fed.img"' in out
```

- [ ] **Step 2: Run to verify fail** — `uv run python -m pytest tests/inventory/test_serialize.py -k staged_path -q` → FAIL (`ImageRow` has no `path` / no branch).

- [ ] **Step 3: Implement.** Add `path: str | None` to `ImageRow` (find its dataclass/model). In `_emit_image_source`, before the `volume` branch:

```python
    if image.path is not None:
        return ["[image.source]", 'kind = "staged-path"', f"path = {_toml_str(image.path)}"]
```

Update the export query/row constructor to select and pass `path`.

- [ ] **Step 4: Run** — `uv run python -m pytest tests/inventory/test_serialize.py -q` → PASS. `just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/inventory/serialize.py tests/inventory/test_serialize.py
git commit -m "feat(inventory): serialize staged-path image source on export

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Walkthrough + example declare a staged-path image (drop the host `ls`)

**Files:**
- Modify: `systems.toml.example`, `examples/local-libvirt/README.md` (and the minimal example walkthrough that previously instructed a host `ls` — grep `ls /var/lib/kdive/rootfs`)
- Test: `tests/inventory/test_image_catalog_contract.py` if it parses the example; otherwise the example is covered by `docs-*` guards + a parse assertion.

**Interfaces:** none — documentation + example inventory.

- [ ] **Step 1:** Add to `systems.toml.example` a public staged-path `[[image]]` for local-libvirt:

```toml
[[image]]
provider = "local-libvirt"
name = "local-rootfs"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = []
source = { kind = "staged-path", path = "/var/lib/kdive/rootfs/local-rootfs.qcow2" }
```

- [ ] **Step 2:** In the local-libvirt walkthrough README, replace any `ls /var/lib/kdive/rootfs/...` discovery step with: discover via `fixtures.list` / `systems.profile_examples`, then provision `rootfs = { kind = "catalog", provider = "local-libvirt", name = "local-rootfs" }`. State the operator stages the file at the declared `path` once.

- [ ] **Step 3:** If a contract test parses `systems.toml.example`, run it: `uv run python -m pytest tests/inventory/test_image_catalog_contract.py -q`. If none exists, add a small parse test that `load_inventory(<example>)` succeeds and the staged-path image is present.

- [ ] **Step 4: Run** the doc guards: `just docs-links docs-paths check-mermaid config-docs-check`. Grep to confirm no `ls /var/lib/kdive/rootfs` remains in the walkthrough.

- [ ] **Step 5: Commit**

```bash
git add systems.toml.example examples/local-libvirt/ tests/inventory/
git commit -m "docs(local-libvirt): discover rootfs via catalog, drop host ls

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** criteria 1 (Task 1 parse + Task 8 round-trip), 2 (Tasks 2-3), 3 (Task 7), 4 (Task 1 + existing `validate_rootfs_reference` passes a declared image — add a regression test in Task 6 if absent), 5 (Tasks 5-6), 6 (`profile_examples` unchanged — add an assertion in Task 9/contract that a declared public staged-path image makes the local example `uses_real_reference: true`), 7 (Task 9), 8 (Task 6 s3 branch + a `test_provisioning`/`test_fetch` s3 case), 9 (Task 1), 10 (Task 4 arch tests + a `test_fetch` arch case). **Gap to close during execution:** add the `profile_examples` + arch multi-arch corner test (spec next-step) — fold into Task 7 or Task 9.

**Placeholder scan:** none — every code step shows code.

**Type consistency:** `_realize` 6-tuple (Task 3) consumed nowhere else; `CatalogFetch(ref, arch)` (Task 6) consumed by `rootfs_catalog_fetch_from_env` and `_materialize_catalog_rootfs`; `MaterializeRootfs(rootfs, system_id, arch)` (Task 6) consumed by `provision` and the test doubles; `resolve_public_rootfs_sync(conn, provider, name, arch)` (Task 4) consumed by `fetch_registered_rootfs_sync` (Task 5); `fetch_registered_rootfs_sync(...)` consumed by `rootfs_catalog_fetch_from_env` (Task 6). Consistent.
