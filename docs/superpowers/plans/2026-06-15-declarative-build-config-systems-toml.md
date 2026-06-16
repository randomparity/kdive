# Declarative `[[build_config]]` in `systems.toml` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give build-config fragments a declarative, file-authoritative home in `systems.toml` (`[[build_config]]`), reconciled into `build_config_catalog` + the reserved object key, with `source='config'` beating `operator`, upsert-only prune, and inline content.

**Architecture:** A new `inventory/reconcile_build_configs.py` pass (appended last in `reconcile_all`) publishes each declared fragment's inline `content` to the existing reserved object key under the shared per-name `BUILD_CONFIG` advisory lock, writing `source='config'`. Validation rules (`name` charset, non-empty, byte cap) are shared with `buildconfig.set` via a neutral `build_configs/rules.py`. The byte cap is enforced at the two config-reading layers (`reconcile-systems --check` at deploy, the reconcile pass at runtime), keeping the inventory loader pure. A third `source='config'` value is allowed by migration `0035`.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `psycopg` (async), pydantic v2, Postgres advisory locks, S3-compatible object store. Source of truth for commands is the `justfile`.

**Spec:** `docs/design/declarative-build-config-systems-toml.md` · **ADR:** `docs/adr/0122-declarative-build-config-systems-toml.md`

**Project conventions every task must honor:**
- Run guardrails before each commit: `just lint` (ruff check + format), `just type` (ty, whole tree), and the focused test(s). Zero warnings.
- Ruff line length 100; absolute imports only (no relative `..`); Google-style docstrings on non-trivial public APIs.
- `inventory/` must NOT import `mcp/`. `build_configs/` is neutral (importable by both), like `domain/cost_class_rules.py`.
- Conventional-commit messages, imperative ≤72-char subject, ending with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Tests mirror the package tree under `tests/`. The db/reconcile tests need a reachable Docker daemon (disposable Postgres); they skip without it.

---

## File structure

| File | Responsibility | Task |
|------|----------------|------|
| `src/kdive/db/schema/0035_build_config_catalog_source_config.sql` | Widen `source` CHECK to allow `'config'` | 1 |
| `scripts/m2_portability_gate.py` | Add `0035` to `ALLOWED_FILES` | 1 |
| `tests/scripts/test_m2_portability_gate.py` | Add `0035` to the meta-test frozenset | 1 |
| `src/kdive/build_configs/rules.py` | Neutral shared validators (name charset, non-empty, byte-cap predicate) | 2 |
| `src/kdive/mcp/tools/catalog/build_configs.py` | Refactor `buildconfig.set` to call `rules.py` | 2 |
| `src/kdive/build_configs/catalog.py` | `upsert_config_build_config`; `read_build_config_provenance` returning `(sha256, source, description)` | 3 |
| `src/kdive/build_configs/seed.py` | Use the shared reader; widen skip to `{operator, config}` | 3, 6 |
| `src/kdive/inventory/model.py` | `BuildConfigDecl` + `InventoryDoc.build_config` + uniqueness check | 4 |
| `src/kdive/inventory/reconcile_cli.py` | `validate_systems` enforces the byte cap (deploy-time) | 5 |
| `src/kdive/inventory/reconcile_build_configs.py` | The reconcile pass + publish-capable store protocol | 7 |
| `src/kdive/inventory/reconcile_pipeline.py` | Append the build-config pass to `reconcile_all` | 7 |
| `systems.toml.example` | Document the `[[build_config]]` section | 8 |

---

### Task 1: Migration 0035 — widen `source` CHECK to allow `'config'`

**Files:**
- Create: `src/kdive/db/schema/0035_build_config_catalog_source_config.sql`
- Modify: `scripts/m2_portability_gate.py` (the `ALLOWED_FILES` frozenset, near line 51-70)
- Modify: `tests/scripts/test_m2_portability_gate.py` (the expected frozenset, near line 164-168)

The migration runner (`src/kdive/db/migrate.py`) auto-discovers `schema/NNNN_*.sql` in ascending order, so creating the file is enough to apply it. `build_config_catalog` is provider-agnostic core (`db/schema/`), so the M2 portability gate would flag the new file unless it is added to both the gate script and its meta-test (the same as `0034`).

- [ ] **Step 1: Write the failing meta-test assertion**

In `tests/scripts/test_m2_portability_gate.py`, add the new migration to the expected frozenset (it sits next to the existing build-config entries near line 165):

```python
                "src/kdive/db/schema/0034_build_config_catalog_source.sql",
                "src/kdive/db/schema/0035_build_config_catalog_source_config.sql",
                "src/kdive/mcp/tools/catalog/build_configs.py",
```

- [ ] **Step 2: Run the meta-test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_m2_portability_gate.py -q`
Expected: FAIL — the expected frozenset no longer equals `ALLOWED_FILES` (the script does not yet list `0035`).

- [ ] **Step 3: Add the migration to the gate script**

In `scripts/m2_portability_gate.py`, inside `ALLOWED_FILES`, add next to the existing migration entries (with a brief comment matching the file's style):

```python
    # ADR-0122: declarative [[build_config]] source='config' CHECK widen (provider-agnostic core).
    "src/kdive/db/schema/0035_build_config_catalog_source_config.sql",
```

- [ ] **Step 4: Create the migration file**

`src/kdive/db/schema/0035_build_config_catalog_source_config.sql`:

```sql
-- Declarative [[build_config]] home in systems.toml (ADR-0122): a third provenance value.
-- 'config' = published by the systems.toml reconcile (file-authoritative, beats 'operator').
-- Drop and re-add the CHECK so 'config' is accepted; no new column. The reconcile pass writes
-- 'config'; the seed's WHERE source='seed' guard already refuses any non-seed row, so it
-- refuses 'config' too.
ALTER TABLE build_config_catalog DROP CONSTRAINT build_config_catalog_source_check;
ALTER TABLE build_config_catalog
    ADD CONSTRAINT build_config_catalog_source_check
        CHECK (source IN ('seed', 'operator', 'config'));
```

Note: confirm the existing constraint name. Migration `0034` added the column with an inline `CHECK`, which Postgres auto-names `build_config_catalog_source_check`. If a `\d build_config_catalog` in a scratch DB shows a different name, use that name in the `DROP CONSTRAINT`.

- [ ] **Step 5: Run the meta-test + migration discovery test to verify green**

Run: `uv run python -m pytest tests/scripts/test_m2_portability_gate.py tests/db -q`
Expected: PASS (the meta-test matches; migration discovery accepts the new sequential file).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/schema/0035_build_config_catalog_source_config.sql scripts/m2_portability_gate.py tests/scripts/test_m2_portability_gate.py
git commit -m "feat(db): allow source='config' for build_config_catalog (#443)"
```

---

### Task 2: Shared validation rule `build_configs/rules.py`

**Files:**
- Create: `src/kdive/build_configs/rules.py`
- Create/extend: `tests/build_configs/test_rules.py`
- Modify: `src/kdive/mcp/tools/catalog/build_configs.py` (replace the inline `_NAME_RE` / cap checks in `set_build_config` with calls to `rules.py`)

Extract the `name` charset, non-empty, and byte-cap checks `buildconfig.set` enforces into a neutral module both the tool and the inventory model can call, so the two surfaces cannot diverge (mirrors `domain/cost_class_rules.py`). The helpers raise a bare `ValueError`; callers map it. The byte-cap is a pure predicate taking the cap as an argument (no config import in `rules.py`).

- [ ] **Step 1: Write the failing tests**

`tests/build_configs/test_rules.py`:

```python
"""Shared build-config validation rules (ADR-0122)."""

from __future__ import annotations

import pytest

from kdive.build_configs.rules import (
    exceeds_build_config_cap,
    validate_build_config_content,
    validate_build_config_name,
)


@pytest.mark.parametrize("name", ["kdump", "a", "k1", "kdump-debug", "kdump_debug", "a" * 64])
def test_valid_names_pass(name: str) -> None:
    assert validate_build_config_name(name) == name


@pytest.mark.parametrize("name", ["", "Kdump", "-kdump", "kdump!", "a" * 65, "kd/ump", "kd ump"])
def test_invalid_names_raise(name: str) -> None:
    with pytest.raises(ValueError):
        validate_build_config_name(name)


def test_nonempty_content_passes() -> None:
    assert validate_build_config_content("CONFIG_KEXEC=y\n") == "CONFIG_KEXEC=y\n"


def test_empty_content_raises() -> None:
    with pytest.raises(ValueError):
        validate_build_config_content("")


def test_cap_predicate() -> None:
    assert exceeds_build_config_cap(b"x" * 11, 10) is True
    assert exceeds_build_config_cap(b"x" * 10, 10) is False
    assert exceeds_build_config_cap(b"", 10) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/build_configs/test_rules.py -q`
Expected: FAIL — `kdive.build_configs.rules` does not exist.

- [ ] **Step 3: Write `build_configs/rules.py`**

```python
"""Neutral build-config validation rules shared by the tool and the inventory model (ADR-0122).

`buildconfig.set` (`mcp/tools/catalog/build_configs.py`) and the `systems.toml` `[[build_config]]`
inventory model validate a fragment the same way. To keep the two surfaces from diverging — and
without `inventory/` importing `mcp/` (a core->tool layering inversion) — the rules live here,
neutral. Each raises a bare `ValueError`; callers map it (`InventoryError` at file load,
`CONFIGURATION_ERROR` for the tool). The byte cap is a pure predicate taking the cap as an
argument, so this module imports no config singleton (mirrors `domain/cost_class_rules.py`).
"""

from __future__ import annotations

import re

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_build_config_name(name: str) -> str:
    """Return `name` if it matches `^[a-z0-9][a-z0-9_-]{0,63}$`; raise `ValueError` otherwise.

    The name folds into the reserved object key, so a strict charset is enforced before it
    reaches the key builder (which blocks only `/` and control chars, not `..`/whitespace/case).
    """
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"build-config name {name!r} must match ^[a-z0-9][a-z0-9_-]{{0,63}}$"
        )
    return name


def validate_build_config_content(content: str) -> str:
    """Return `content` if it is non-empty; raise `ValueError` otherwise (fail closed).

    The byte cap is NOT checked here — it is config-dependent and enforced by the caller that
    has config access (`reconcile-systems --check` and the reconcile pass), via
    `exceeds_build_config_cap`.
    """
    if not content:
        raise ValueError("build-config content must be non-empty")
    return content


def exceeds_build_config_cap(data: bytes, cap: int) -> bool:
    """Return True iff `data` is larger than `cap` bytes (the shared byte-cap predicate)."""
    return len(data) > cap
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `uv run python -m pytest tests/build_configs/test_rules.py -q`
Expected: PASS.

- [ ] **Step 5: Refactor `buildconfig.set` to call the shared rules**

In `src/kdive/mcp/tools/catalog/build_configs.py`:

Remove the module-level `_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")` and the `import re` if now unused. Add the import:

```python
from kdive.build_configs.rules import (
    exceeds_build_config_cap,
    validate_build_config_name,
)
```

In `set_build_config`, replace the inline name check:

```python
        if not _NAME_RE.match(name):
            return ToolResponse.failure(
                name,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=[_SET_TOOL],
                data={"field": "name"},
            )
```

with:

```python
        try:
            validate_build_config_name(name)
        except ValueError:
            return ToolResponse.failure(
                name,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=[_SET_TOOL],
                data={"field": "name"},
            )
```

And replace the cap branch's `len(data) > cap` with the shared predicate (keep the empty check, since `not data` is the byte-level non-empty guard the tool already uses):

```python
        cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
        if not data or exceeds_build_config_cap(data, cap):
            return ToolResponse.failure(
                name,
                ErrorCategory.CONFIGURATION_ERROR,
                suggested_next_actions=[_SET_TOOL],
                data={"field": "content", "limit": cap, "actual": len(data)},
            )
```

- [ ] **Step 6: Run the existing tool tests + rules tests**

Run: `uv run python -m pytest tests/build_configs/test_rules.py tests/mcp/catalog/test_build_configs_tool.py -q`
Expected: PASS (behavior unchanged; the tool now delegates).

- [ ] **Step 7: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/build_configs/rules.py tests/build_configs/test_rules.py src/kdive/mcp/tools/catalog/build_configs.py
git commit -m "refactor(build-config): share name/content/cap rules with the tool (#443)"
```

---

### Task 3: Catalog writer + provenance reader

**Files:**
- Modify: `src/kdive/build_configs/catalog.py`
- Modify: `src/kdive/build_configs/seed.py` (use the shared reader in place of `_stored_row`)
- Extend: `tests/build_configs/test_catalog.py`

Add `upsert_config_build_config` (writes `source='config'` unconditionally — the file is authoritative) and `read_build_config_provenance(conn, name) -> (sha256, source, description) | None` for change-detection + drift. Refactor the seed's private `_stored_row` to call the shared reader (it currently reads `(sha256, source)`; widen to include `description`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/build_configs/test_catalog.py` (follow the file's existing fixture style for a Postgres connection; mirror the patterns already used to insert/read a `build_config_catalog` row):

```python
async def test_upsert_config_writes_source_config(db_conn) -> None:
    from kdive.build_configs.catalog import (
        read_build_config_provenance,
        upsert_config_build_config,
    )

    await upsert_config_build_config(db_conn, "kdump", "system/build-configs/kdump/kdump.config", "abc123", "desc")
    prov = await read_build_config_provenance(db_conn, "kdump")
    assert prov == ("abc123", "config", "desc")


async def test_upsert_config_clobbers_operator(db_conn) -> None:
    from kdive.build_configs.catalog import (
        read_build_config_provenance,
        upsert_config_build_config,
        upsert_operator_build_config,
    )

    await upsert_operator_build_config(db_conn, "kdump", "k", "op_sha", "op desc")
    await upsert_config_build_config(db_conn, "kdump", "k2", "cfg_sha", "cfg desc")
    assert await read_build_config_provenance(db_conn, "kdump") == ("cfg_sha", "config", "cfg desc")


async def test_provenance_absent_returns_none(db_conn) -> None:
    from kdive.build_configs.catalog import read_build_config_provenance

    assert await read_build_config_provenance(db_conn, "nope") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/build_configs/test_catalog.py -q`
Expected: FAIL — `upsert_config_build_config` / `read_build_config_provenance` do not exist.

- [ ] **Step 3: Add the writer + reader to `catalog.py`**

Append to `src/kdive/build_configs/catalog.py`:

```python
async def read_build_config_provenance(
    conn: AsyncConnection, name: str
) -> tuple[str, str, str] | None:
    """Return `(sha256, source, description)` for `name`, or `None` if absent.

    The inventory reconcile pass uses this for change-detection (sha256 + description) and
    drift attribution (source), and the seed reuses it for its source-aware skip (ADR-0122).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sha256, source, description FROM build_config_catalog WHERE name = %(name)s",
            {"name": name},
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return (str(row["sha256"]), str(row["source"]), str(row["description"]))


async def upsert_config_build_config(
    conn: AsyncConnection, name: str, object_key: str, sha256: str, description: str
) -> None:
    """Upsert a config-declared fragment row (`source='config'`), unconditionally (ADR-0122).

    The systems.toml file is authoritative, so this clobbers a `seed` or `operator` row AND
    writes `description` **verbatim** (the file fully specifies the fragment each reconcile).
    It deliberately does NOT use the `COALESCE`-preserve pattern the operator writer uses: that
    pattern would make a file declaring an empty description un-converge against the reconcile
    pass's `(sha256, source, description)` change-detection key (the stored description would
    never blank, so the pass would re-assert every cycle). Verbatim keeps the pass idempotent.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description, source) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s, 'config') "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = EXCLUDED.description, source = 'config', updated_at = now()",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": description},
        )
```

- [ ] **Step 4: Run to verify the new tests pass**

Run: `uv run python -m pytest tests/build_configs/test_catalog.py -q`
Expected: PASS.

- [ ] **Step 5: Refactor the seed to reuse the shared reader (no behavior change yet)**

In `src/kdive/build_configs/seed.py`, replace the private `_stored_row` function with a call to the shared reader. Change the import:

```python
from kdive.build_configs.catalog import read_build_config_provenance, upsert_seed_build_config
```

Delete the `_stored_row` function and its `dict_row` import if now unused. In `seed_build_configs`, the existing read returns `(sha256, source)`; adapt it to the 3-tuple (the skip condition is unchanged in this task — Task 6 widens it):

```python
        stored = await read_build_config_provenance(conn, _KDUMP_NAME)
        if stored is not None and (stored[0] == sha256 and stored[1] == "seed" or stored[1] == "operator"):
            return 0
```

(`stored` is now `(sha256, source, description)`; the original compared `stored == (sha256, "seed")` — preserve that meaning: skip when the seed-owned row already matches, or the row is operator-owned.)

- [ ] **Step 6: Run the seed tests to verify unchanged behavior**

Run: `uv run python -m pytest tests/build_configs/test_seed.py tests/build_configs/test_seed_db.py -q`
Expected: PASS.

- [ ] **Step 7: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/build_configs/catalog.py src/kdive/build_configs/seed.py tests/build_configs/test_catalog.py
git commit -m "feat(build-config): add config-source upsert + provenance reader (#443)"
```

---

### Task 4: Inventory model — `BuildConfigDecl` + `InventoryDoc.build_config`

**Files:**
- Modify: `src/kdive/inventory/model.py`
- Extend: `tests/inventory/test_model.py`

Add the `[[build_config]]` model with name + content + optional description, config-free validation (name charset via `rules.py`, non-empty content), and a name-uniqueness semantic check (mirrors `_check_cost_class_uniqueness`). The byte cap is NOT checked here (Task 5/7 enforce it).

- [ ] **Step 1: Write the failing tests**

Add to `tests/inventory/test_model.py`:

```python
def test_build_config_valid() -> None:
    doc = InventoryDoc.parse(
        {
            "schema_version": 2,
            "build_config": [
                {"name": "kdump", "content": "CONFIG_KEXEC=y\n", "description": "kdump frag"}
            ],
        }
    )
    assert len(doc.build_config) == 1
    assert doc.build_config[0].name == "kdump"
    assert doc.build_config[0].content == "CONFIG_KEXEC=y\n"
    assert doc.build_config[0].description == "kdump frag"


def test_build_config_absent_is_empty_list() -> None:
    doc = InventoryDoc.parse({"schema_version": 2})
    assert doc.build_config == []


def test_build_config_duplicate_name_raises() -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(
            {
                "schema_version": 2,
                "build_config": [
                    {"name": "kdump", "content": "a"},
                    {"name": "kdump", "content": "b"},
                ],
            }
        )


@pytest.mark.parametrize("name", ["", "Kdump", "kd/ump"])
def test_build_config_bad_name_raises(name: str) -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(
            {"schema_version": 2, "build_config": [{"name": name, "content": "a"}]}
        )


def test_build_config_empty_content_raises() -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(
            {"schema_version": 2, "build_config": [{"name": "kdump", "content": ""}]}
        )
```

(`InventoryError` and `InventoryDoc` are already imported in this test module; confirm and add the import if not.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/inventory/test_model.py -q -k build_config`
Expected: FAIL — `InventoryDoc` has no `build_config` field.

- [ ] **Step 3: Add the model**

In `src/kdive/inventory/model.py`, add the import near the other domain-rule import:

```python
from kdive.build_configs.rules import validate_build_config_content, validate_build_config_name
```

Add the model class (next to `CostClassEntry`):

```python
class BuildConfigDecl(BaseModel):
    """A single `[[build_config]]` declaration: a named kernel-config fragment (ADR-0122).

    `content` is the inline fragment text; the reconcile pass publishes it to the reserved
    object key. `name`/`content` validation delegates to the neutral `build_configs/rules`
    the `buildconfig.set` tool shares; the byte cap is enforced where config is available
    (`reconcile-systems --check` and the reconcile pass), not here, so the loader stays pure.
    """

    name: str
    content: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return validate_build_config_name(value)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        return validate_build_config_content(value)
```

Add the field to `InventoryDoc` (next to `cost_class`):

```python
    build_config: list[BuildConfigDecl] = Field(default_factory=list)
```

Add the uniqueness check method (next to `_check_cost_class_uniqueness`):

```python
    def _check_build_config_uniqueness(self) -> None:
        names = [b.name for b in self.build_config]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise InventoryError("build_config", "name", f"duplicate build_config names {dupes}")
```

Call it in `parse`, after `_check_cost_class_uniqueness()`:

```python
        doc._check_cost_class_uniqueness()
        doc._check_build_config_uniqueness()
        return doc
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `uv run python -m pytest tests/inventory/test_model.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/inventory/model.py tests/inventory/test_model.py
git commit -m "feat(inventory): parse [[build_config]] declarations (#443)"
```

---

### Task 5: Deploy-time byte-cap enforcement in `validate_systems`

**Files:**
- Modify: `src/kdive/inventory/reconcile_cli.py` (the `validate_systems` function, near line 66)
- Extend: `tests/inventory/test_validate_systems.py`

`reconcile-systems --check` (`validate_systems`) is the `pre-install`/`pre-upgrade` fail-fast Helm gate. It already reads `kdive.config` (to resolve `SYSTEMS_TOML`), so reading the byte cap is within its no-DB/no-S3 contract. After a successful parse, fail (exit non-zero) when any declared fragment's UTF-8 content exceeds the cap, so an oversized fragment aborts the upgrade instead of deploying green and silently not publishing.

- [ ] **Step 1: Write the failing tests**

Add to `tests/inventory/test_validate_systems.py` (follow the file's existing pattern for writing a temp `systems.toml` and asserting the exit code; set the cap env var with `monkeypatch`):

```python
def test_validate_rejects_over_cap_build_config(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KDIVE_MAX_BUILD_CONFIG_BYTES", "10")
    path = tmp_path / "systems.toml"
    path.write_text(
        'schema_version = 2\n'
        '[[build_config]]\nname = "kdump"\ncontent = "CONFIG_KEXEC=y_way_too_long"\n'
    )
    assert validate_systems(path) != 0
    assert "kdump" in capsys.readouterr().err


def test_validate_accepts_in_cap_build_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_MAX_BUILD_CONFIG_BYTES", "4096")
    path = tmp_path / "systems.toml"
    path.write_text('schema_version = 2\n[[build_config]]\nname = "kdump"\ncontent = "y\\n"\n')
    assert validate_systems(path) == 0
```

(`validate_systems` is already imported in this test module; confirm/add. The cap setting name is `KDIVE_MAX_BUILD_CONFIG_BYTES`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/inventory/test_validate_systems.py -q -k build_config`
Expected: FAIL — the over-cap fragment currently returns exit 0 (no cap check).

- [ ] **Step 3: Add the cap check to `validate_systems`**

In `src/kdive/inventory/reconcile_cli.py`, add imports at the top (config is currently imported lazily inside `_load_doc`; a module-level import of the rule + setting is fine here):

```python
import kdive.config as config
from kdive.build_configs.rules import exceeds_build_config_cap
from kdive.config.core_settings import MAX_BUILD_CONFIG_BYTES
```

In `validate_systems`, after the doc loads successfully, add the cap check before `return _EXIT_OK`:

```python
    try:
        doc = _load_doc(path)
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_INVENTORY_ERROR
    if doc is not None:
        cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
        for frag in doc.build_config:
            if exceeds_build_config_cap(frag.content.encode("utf-8"), cap):
                print(
                    f"error: build_config[{frag.name}]: content exceeds "
                    f"KDIVE_MAX_BUILD_CONFIG_BYTES ({cap} bytes)",
                    file=sys.stderr,
                )
                return _EXIT_INVENTORY_ERROR
    return _EXIT_OK
```

(Keep `_load_doc`'s lazy `import kdive.config as config`, or hoist it to the new module-level import and remove the local one — but do not import config inside `model.py`/the loader; `reconcile_cli.py` is a CLI entry point, not the pure loader.)

- [ ] **Step 4: Run to verify the tests pass**

Run: `uv run python -m pytest tests/inventory/test_validate_systems.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/inventory/reconcile_cli.py tests/inventory/test_validate_systems.py
git commit -m "feat(inventory): fail --check on an over-cap build_config (#443)"
```

---

### Task 6: Widen the seed skip to `{operator, config}`

**Files:**
- Modify: `src/kdive/build_configs/seed.py` (the skip condition in `seed_build_configs`)
- Extend: `tests/build_configs/test_seed.py` (or `test_seed_db.py`, whichever exercises the DB skip)

The seed must skip a `config`-owned row (not just `operator`), so a `seed-build-configs` run never reverts a config-declared fragment to the packaged default. The seed's `WHERE source='seed'` SQL guard already enforces this on the row; this widens the Python pre-read skip to match (a cheap early-out + defence in depth).

- [ ] **Step 1: Write the failing test**

Add to the DB-backed seed test (e.g. `tests/build_configs/test_seed_db.py`):

```python
async def test_seed_skips_config_owned_row(db_conn, object_store) -> None:
    from kdive.build_configs.catalog import upsert_config_build_config
    from kdive.build_configs.seed import seed_build_configs

    await upsert_config_build_config(db_conn, "kdump", "k", "cfg_sha", "cfg desc")
    published = await seed_build_configs(db_conn, object_store)
    assert published == 0  # the config-owned row is left untouched
```

(Reuse the test module's existing fixtures for `db_conn` and an object store; match their names.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/build_configs/test_seed_db.py -q -k config`
Expected: FAIL — the current skip only matches `source == "operator"`, so a `config` row is not skipped and the seed publishes (returns 1).

- [ ] **Step 3: Widen the skip condition**

In `src/kdive/build_configs/seed.py`, update the skip so it covers both non-seed sources:

```python
        stored = await read_build_config_provenance(conn, _KDUMP_NAME)
        if stored is not None and (
            (stored[0] == sha256 and stored[1] == "seed") or stored[1] in {"operator", "config"}
        ):
            return 0
```

- [ ] **Step 4: Run to verify the test passes**

Run: `uv run python -m pytest tests/build_configs/test_seed_db.py tests/build_configs/test_seed.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/build_configs/seed.py tests/build_configs/test_seed_db.py
git commit -m "feat(build-config): seed skips a config-owned row (#443)"
```

---

### Task 7: The reconcile pass + pipeline wiring

**Files:**
- Create: `src/kdive/inventory/reconcile_build_configs.py`
- Modify: `src/kdive/inventory/reconcile_pipeline.py` (append the pass to `reconcile_all`)
- Create: `tests/inventory/test_reconcile_build_configs.py`
- Extend: `tests/adversarial/test_build_config_concurrency.py` (reconcile-vs-set serialization)

The pass publishes each declared fragment's inline content to the reserved object key under the shared `BUILD_CONFIG` lock, change-detecting on `(sha256, description)` and warning only on a real clobber. It consumes the same `store` the reconcile already threads, narrowed via a `runtime_checkable` publish-capable protocol; when the store cannot publish (no S3) it warns and skips. The byte cap is enforced here (runtime authority).

- [ ] **Step 1: Write the failing tests**

`tests/inventory/test_reconcile_build_configs.py` (use the Postgres + object-store fixtures the other inventory/reconcile tests use; mirror `test_reconcile_pipeline.py` for the store double):

```python
"""The [[build_config]] reconcile pass (ADR-0122)."""

from __future__ import annotations

import hashlib

import pytest

from kdive.build_configs.catalog import (
    read_build_config_provenance,
    upsert_operator_build_config,
    upsert_seed_build_config,
)
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile_build_configs import reconcile_build_configs


def _doc(name: str, content: str, description: str = "") -> InventoryDoc:
    return InventoryDoc.parse(
        {
            "schema_version": 2,
            "build_config": [{"name": name, "content": content, "description": description}],
        }
    )


async def test_create_publishes_and_writes_config(db_conn, object_store) -> None:
    diff = await reconcile_build_configs(db_conn, _doc("kdump", "y\n", "d"), object_store)
    assert [r.name for r in diff.created] == ["kdump"]
    prov = await read_build_config_provenance(db_conn, "kdump")
    assert prov is not None and prov[1] == "config"
    assert prov[0] == hashlib.sha256(b"y\n").hexdigest()


async def test_identical_reassert_is_noop(db_conn, object_store) -> None:
    await reconcile_build_configs(db_conn, _doc("kdump", "y\n", "d"), object_store)
    diff = await reconcile_build_configs(db_conn, _doc("kdump", "y\n", "d"), object_store)
    assert diff.created == [] and diff.updated == [] and diff.warned == []


async def test_description_only_edit_reasserts_without_warn(db_conn, object_store) -> None:
    await reconcile_build_configs(db_conn, _doc("kdump", "y\n", "old"), object_store)
    diff = await reconcile_build_configs(db_conn, _doc("kdump", "y\n", "new"), object_store)
    assert [r.name for r in diff.updated] == ["kdump"]
    assert diff.warned == []
    assert (await read_build_config_provenance(db_conn, "kdump"))[2] == "new"


async def test_reassert_over_operator_warns(db_conn, object_store) -> None:
    await upsert_operator_build_config(db_conn, "kdump", "k", "opsha", "od")
    diff = await reconcile_build_configs(db_conn, _doc("kdump", "y\n", "d"), object_store)
    assert [r.name for r in diff.warned] == ["kdump"]
    assert (await read_build_config_provenance(db_conn, "kdump"))[1] == "config"


async def test_benign_seed_adoption_does_not_warn(db_conn, object_store) -> None:
    # Seed row with the SAME bytes + description the file declares -> adoption, not a clobber.
    sha = hashlib.sha256(b"y\n").hexdigest()
    await upsert_seed_build_config(db_conn, "kdump", "k", sha, "d")
    diff = await reconcile_build_configs(db_conn, _doc("kdump", "y\n", "d"), object_store)
    assert diff.warned == []
    assert (await read_build_config_provenance(db_conn, "kdump"))[1] == "config"


async def test_over_cap_skips_with_warn(db_conn, object_store, monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_MAX_BUILD_CONFIG_BYTES", "5")
    diff = await reconcile_build_configs(db_conn, _doc("kdump", "way too long content"), object_store)
    assert [r.name for r in diff.warned] == ["kdump"]
    assert await read_build_config_provenance(db_conn, "kdump") is None  # never published


async def test_store_cannot_publish_degrades(db_conn) -> None:
    class _HeadOnly:
        def head_present(self, key: str) -> bool:
            return False

    diff = await reconcile_build_configs(db_conn, _doc("kdump", "y\n"), _HeadOnly())
    assert [r.name for r in diff.warned] == ["kdump"]
    assert await read_build_config_provenance(db_conn, "kdump") is None
```

(Confirm the fixture names `db_conn` and `object_store` against `tests/inventory/conftest.py` / `tests/build_configs/conftest.py`; use whatever the sibling reconcile tests use for a write-capable store double — it must implement `head_present` + `put_artifact`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/inventory/test_reconcile_build_configs.py -q`
Expected: FAIL — `kdive.inventory.reconcile_build_configs` does not exist.

- [ ] **Step 3: Write the reconcile pass**

`src/kdive/inventory/reconcile_build_configs.py`:

```python
"""The [[build_config]] merge-reconcile (#443, ADR-0122).

Publishes each declared `[[build_config]]` fragment's inline `content` to the reserved
build-config object key and upserts its `build_config_catalog` row `source='config'`,
file-authoritatively. Each fragment is handled under the shared per-name `BUILD_CONFIG`
advisory lock (the same lock the seed and `buildconfig.set` take), so reconcile, seed, and
set never interleave a row sha256 that describes another writer's bytes (the ADR-0119
row-vs-object contract). Upsert-only — never prunes (ADR-0122 §2).

The pass needs a publish-capable store. The reconciler loop and the on-demand path (with S3
configured) pass a concrete `ObjectStore`; the on-demand `_AbsentImageStore` (no S3) cannot
publish, so the pass warns and skips — the same degrade the image pass uses. The byte cap is
enforced here off the same `MAX_BUILD_CONFIG_BYTES` the tool and `--check` read.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Protocol, runtime_checkable

from psycopg import AsyncConnection

import kdive.config as config
from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.build_configs.catalog import (
    read_build_config_provenance,
    upsert_config_build_config,
)
from kdive.build_configs.rules import exceeds_build_config_cap
from kdive.config.core_settings import MAX_BUILD_CONFIG_BYTES
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.models import Sensitivity
from kdive.inventory.model import BuildConfigDecl, InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff, ReconcileRecord

_log = logging.getLogger(__name__)

_TENANT = "system"
_OWNER_KIND = "build-configs"
_RETENTION_CLASS = "build-config"


@runtime_checkable
class BuildConfigPublishStore(Protocol):
    """The publish-capable store port the build-config reconcile needs (write + presence)."""

    def head_present(self, key: str) -> bool: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> object: ...


def _record(name: str, detail: str = "") -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=f"build_config[{name}]", detail=detail)


async def reconcile_build_configs(
    conn: AsyncConnection, doc: InventoryDoc, store: object
) -> ReconcileDiff:
    """Publish each `[[build_config]]` declaration file-authoritatively; return the diff.

    Args:
        conn: A transaction-free pooled connection (each fragment opens its own transaction
            to hold the per-name advisory lock).
        doc: The parsed inventory document.
        store: The reconcile store. Must satisfy `BuildConfigPublishStore` (head + put) to
            publish; a head-only store degrades every declared fragment to `warned`.

    Returns:
        The `ReconcileDiff` for the build-config pass (`created`/`updated` per published row,
        `warned` for an over-cap skip, a store that cannot publish, or a real clobber).
    """
    diff = ReconcileDiff()
    if not doc.build_config:
        return diff
    if not isinstance(store, BuildConfigPublishStore):
        for frag in doc.build_config:
            diff.warned.append(_record(frag.name, "object store cannot publish; row untouched"))
        _log.warning("inventory: build_config pass has no publish-capable store; skipped")
        return diff
    cap = int(config.require(MAX_BUILD_CONFIG_BYTES))
    for frag in doc.build_config:
        await _reconcile_one(conn, frag, store, cap, diff)
    return diff


async def _reconcile_one(
    conn: AsyncConnection,
    frag: BuildConfigDecl,
    store: BuildConfigPublishStore,
    cap: int,
    diff: ReconcileDiff,
) -> None:
    """Publish one fragment under its per-name lock, change-detecting and warning on a clobber."""
    data = frag.content.encode("utf-8")
    if exceeds_build_config_cap(data, cap):
        diff.warned.append(_record(frag.name, f"content exceeds {cap} bytes; skipped"))
        _log.warning("inventory: build_config %r over cap (%d bytes); skipped", frag.name, cap)
        return
    sha256 = hashlib.sha256(data).hexdigest()
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.BUILD_CONFIG, frag.name):
        prior = await read_build_config_provenance(conn, frag.name)
        if prior is not None and prior == (sha256, "config", frag.description):
            return  # idempotent: no write, no diff, no log noise
        written = store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=frag.name,
                name=f"{frag.name}.config",
                data=data,
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION_CLASS,
            )
        )
        await upsert_config_build_config(conn, frag.name, _key_of(written), sha256, frag.description)
        if prior is None:
            diff.created.append(_record(frag.name))
            return
        diff.updated.append(_record(frag.name))
        # Warn only on a real clobber: an operator override reverted, or the bytes changed.
        # A benign seed->config adoption at identical bytes, or a description-only edit on an
        # already-config row, is `updated`, not `warned` (mirrors reconcile_coefficients).
        if prior[1] == "operator" or prior[0] != sha256:
            detail = f"re-asserted from file over {prior[1]} (was sha {prior[0][:12]})"
            diff.warned.append(_record(frag.name, detail))
            _log.warning("inventory: build_config %r %s", frag.name, detail)


def _key_of(written: object) -> str:
    """Read the object key off the put_artifact result (`.key`)."""
    key = getattr(written, "key", None)
    assert isinstance(key, str)
    return key
```

Note on the warn rule: warn iff the prior row's `source == 'operator'` (a live override reverted) OR the prior `sha256` differs from the file (real content change). A benign `seed`→`config` adoption at identical bytes (prior `source='seed'`, `prior[0] == sha256`) is `updated`, not `warned`. A description-only change on an already-`config` row is `updated`, not `warned`.

- [ ] **Step 4: Wire the pass into `reconcile_all`**

In `src/kdive/inventory/reconcile_pipeline.py`, add the import and the call (last in the chain):

```python
from kdive.inventory.reconcile_build_configs import reconcile_build_configs
```

```python
    _extend(merged, await reconcile_build_hosts(conn, doc))
    _extend(merged, await reconcile_build_configs(conn, doc, store))
    return merged
```

The `store` parameter of `reconcile_all` is currently typed `ImageHeadStore`. Widen its annotation so a head-only store still type-checks (the build-config pass runtime-narrows via `isinstance(store, BuildConfigPublishStore)`):

```python
async def reconcile_all(
    conn: AsyncConnection, doc: InventoryDoc, store: ImageHeadStore
) -> ReconcileDiff:
```

stays as-is — `ImageHeadStore` is the common floor both callers satisfy, and `reconcile_build_configs(conn, doc, store)` takes `store: object` and narrows at runtime, so no call-site or caller type changes are needed. Confirm `just type` is green; if ty complains that `ImageHeadStore` is incompatible with `reconcile_build_configs`'s `object` param, it will not (any type is assignable to `object`).

- [ ] **Step 5: Run the pass tests + pipeline tests**

Run: `uv run python -m pytest tests/inventory/test_reconcile_build_configs.py tests/inventory/test_reconcile_pipeline.py -q`
Expected: PASS.

- [ ] **Step 6: Add the adversarial serialization test**

Extend `tests/adversarial/test_build_config_concurrency.py` with a test that runs `reconcile_build_configs` for a name concurrently with `buildconfig.set` for the same name and asserts the stored row's sha256 always matches the object bytes the row points at (no interleave). Follow the file's existing concurrency-harness pattern (it already exercises `buildconfig.set` concurrency on `BUILD_CONFIG`); add the reconcile path as the second contender. Keep it under the existing markers.

```python
async def test_reconcile_and_set_serialize_on_name(pool, object_store) -> None:
    # Run a reconcile (config bytes) and a buildconfig.set (operator bytes) for the SAME name
    # concurrently; whichever lands last, the row sha256 must describe the object's bytes.
    # (Mirror the existing harness in this file for spawning the two coroutines on the pool.)
    ...
```

- [ ] **Step 7: Run the adversarial test**

Run: `uv run python -m pytest tests/adversarial/test_build_config_concurrency.py -q`
Expected: PASS.

- [ ] **Step 8: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/inventory/reconcile_build_configs.py src/kdive/inventory/reconcile_pipeline.py tests/inventory/test_reconcile_build_configs.py tests/adversarial/test_build_config_concurrency.py
git commit -m "feat(inventory): reconcile [[build_config]] into the catalog (#443)"
```

---

### Task 8: Document the `[[build_config]]` section in `systems.toml.example`

**Files:**
- Modify: `systems.toml.example`

Add a commented `[[build_config]]` section (after the `[[cost_class]]` block, before the campaign knobs) explaining inline content, config-authoritative precedence, and no-prune-on-removal.

- [ ] **Step 1: Add the documented section**

Insert after the `[[cost_class]]` block in `systems.toml.example`:

```toml
# ---------------------------------------------------------------------------------------------
# BUILD-CONFIG FRAGMENTS — reconciled into build_config_catalog (ADR-0122).
#
# Each [[build_config]] declares a named kernel-config fragment inline. reconcile-systems
# publishes the `content` bytes to the reserved object key and upserts the catalog row with
# source='config'. The file is AUTHORITATIVE: a declared fragment overrides a live
# `buildconfig.set` (which writes source='operator') and is re-asserted on every reconcile, so
# edit this file to change a declared fragment — a buildconfig.set on a declared name is
# transient (break-glass only). A name NOT declared here keeps buildconfig.set's durability.
#
# Removing a [[build_config]] block is a no-op: the catalog row persists at its last bytes
# (upsert-only, never pruned). `content` is capped at KDIVE_MAX_BUILD_CONFIG_BYTES (256 KiB);
# an over-cap fragment fails `reconcile-systems --check` at deploy.
# ---------------------------------------------------------------------------------------------

# [[build_config]]
# name = "kdump"
# description = "kdump/debuginfo kernel-config fragment"
# content = """
# CONFIG_KEXEC=y
# CONFIG_CRASH_DUMP=y
# CONFIG_DEBUG_INFO=y
# """
```

- [ ] **Step 2: Run the doc guardrails**

Run: `just docs-check && just docs-links && just docs-paths`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add systems.toml.example
git commit -m "docs(inventory): document the [[build_config]] section (#443)"
```

---

## Final verification (after all tasks)

- [ ] **Full local gate.** Run `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, docs-*, config-*, test). Expected: green. If `just ci` invokes a recipe needing services you lack, run at minimum `just lint`, `just type`, and `just test`; note any locally-unrunnable gate in the PR body.
- [ ] **Behavior sweep.** Confirm an end-to-end `reconcile_all` over a doc containing a `[[build_config]]` publishes the fragment and `buildconfig.get` serves the bytes with `source='config'` (the integration test in `tests/integration/`, added if a suitable home exists, or asserted via the pass test + a `buildconfig.get` call).
- [ ] **Self-check the warn semantics** against the spec: benign seed-adoption at identical bytes must NOT warn; an operator-override revert MUST warn.

## Rollback / cleanup

Each task is an independent commit. The migration `0035` is additive and forward-only (it only widens a CHECK); there is no down-migration in this forward-only runner (ADR-0015). If the feature is reverted before release, revert the commits in reverse order; the widened CHECK is harmless to leave (no row will carry `'config'` once the reconcile pass is gone, and the constraint still admits `seed`/`operator`).
