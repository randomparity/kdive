# Declarative Cost-Class Coefficients Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `[[cost_class]]` table to `systems.toml`, reconciled file-authoritatively into `cost_class_coefficients`, so the costing baseline is a reviewable artifact and a config host lands priced in the same reconcile that creates it.

**Architecture:** A neutral `domain/cost_class_rules` module owns the one name/coeff validation rule, shared by the inventory model and the `ops` tuning tool (each maps the neutral `ValueError` to its own error type). A new `inventory/reconcile_coefficients` pass upserts declared coeffs under a per-row `SELECT … FOR UPDATE` so drift is flagged atomically. Both resource-reconciling orchestrators (the background loop and `ops.reconcile_systems`) are collapsed onto one shared ordered pipeline that runs the coefficient pass **before** the resource pass. A read-only `ops.export_cost_classes` tool serializes the live table back to TOML for break-glass capture.

**Tech Stack:** Python 3.13, pydantic v2, psycopg (async), FastMCP, Postgres; `uv` / `ruff` / `ty` / `pytest`. Formal decision: [ADR-0115](../../adr/0115-declarative-cost-class-coefficients.md); design: [../../design/declarative-cost-class-coefficients.md](../../design/declarative-cost-class-coefficients.md).

**No migration:** `cost_class_coefficients` already exists (`0002`, with `remote` seeded in `0032`). This feature adds only a new write path into that table — **zero DDL**, so `tests/db/test_migrate.py` is untouched.

**Test conventions (load-bearing — match these exactly):**
- **DB-backed tests are sync `def test_…(migrated_url: str)` wrapping an inner `async def _run(): …` invoked via `asyncio.run(_run())`.** This repo does **not** use bare `async def test_` / pytest-asyncio for these suites. Every test that touches a connection follows the `_run()` + `asyncio.run` shape — copy it.
- The `migrated_url` fixture lives in `tests/db/conftest.py` and is re-exported only by `tests/integration/conftest.py`. A DB test therefore must live under `tests/integration/` (or `tests/db/`), **not** `tests/inventory/`.
- `ToolResponse` failures are asserted via `resp.status == "error"` and `resp.error_category == "authorization_denied"` (string), **not** `resp.error.category`. Success is `resp.status == "ok"`; payload is `resp.data["…"]` (string values).
- Reuse the existing per-file helpers verbatim: in `tests/mcp/ops/test_ops_tuning.py` — `_pool(url)`, `_OPERATOR`, `_ctx(platform_roles=…)`, `_platform_audit_rows(url)`; in `tests/integration/test_reconcile_inventory.py` — `_write_toml(tmp_path, body)`, `_connect(url)`, `_one(conn, name)`, `_resource_by_name(conn, name)`, `_remote_libvirt_toml(...)`, `_FakeImageStore`.

---

## File Structure

| File | Responsibility | New/Modify |
|------|----------------|------------|
| `src/kdive/domain/cost_class_rules.py` | The one name/coeff rule (non-blank name; finite `coeff > 0` via `Decimal(str(v))`); raises neutral `ValueError` | **Create** |
| `src/kdive/mcp/tools/ops/tuning.py` | `_validate_cost_class`/`_parse_positive_coeff` become thin wrappers over the shared rule; gains `export_cost_classes` + its registration | Modify |
| `src/kdive/inventory/model.py` | `CostClassEntry`; `InventoryDoc.cost_class`; duplicate-name check | Modify |
| `src/kdive/inventory/reconcile_coefficients.py` | Upsert declared coeffs file-authoritatively; flag drift via locked read; never delete | **Create** |
| `src/kdive/inventory/reconcile_pipeline.py` | The one ordered chain (`images → coefficients → resources → build_hosts`) both orchestrators call | **Create** |
| `src/kdive/reconciler/inventory.py` | `InventoryReconcilePass.run` calls the shared pipeline | Modify |
| `src/kdive/mcp/tools/ops/reconcile_systems.py` | `_run_pass` calls the shared pipeline | Modify |
| `systems.toml.example` | Document the `[[cost_class]]` block (committed scaffold; real `systems.toml` is gitignored) | Modify |
| `tests/domain/test_cost_class_rules.py` | Unit-test the shared rule (pure, no DB) | **Create** |
| `tests/inventory/test_model.py` | `[[cost_class]]` parse/reject cases (pure, no DB) | Modify |
| `tests/inventory/test_reconcile_pipeline.py` | Source-order assertion: coefficients before resources (pure, no DB) | **Create** |
| `tests/integration/test_reconcile_coefficients.py` | Upsert / drift / idempotent / no-delete (**DB** — needs the `migrated_url` fixture, which lives in `tests/db/conftest.py` and is only re-exported under `tests/integration/`) | **Create** |
| `tests/mcp/ops/test_ops_tuning.py` | `export_cost_classes` gate + determinism + round-trip; shared-rule still rejects | Modify |
| `tests/integration/test_reconcile_inventory.py` | Finding-1 regression on **both** orchestrators; Floor; drift-under-concurrency | Modify |
| `tests/mcp/core/test_tool_docs.py` | `TEST_INDEX` entry for `ops.export_cost_classes` | Modify |

---

## Task 1: Shared cost-class rule module

**Files:**
- Create: `src/kdive/domain/cost_class_rules.py`
- Test: `tests/domain/test_cost_class_rules.py`
- Modify: `src/kdive/mcp/tools/ops/tuning.py`

- [ ] **Step 1: Write the failing test**

Create `tests/domain/test_cost_class_rules.py`:

```python
"""Unit tests for the neutral cost-class name/coeff rule (ADR-0115 §1)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kdive.domain.cost_class_rules import parse_positive_coeff, validate_cost_class_name


def test_valid_name_returned_unchanged() -> None:
    assert validate_cost_class_name("remote") == "remote"


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_blank_name_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        validate_cost_class_name(bad)


@pytest.mark.parametrize("value", ["2.5", 2.5, 1, Decimal("0.25")])
def test_positive_coeff_parsed_to_decimal(value: object) -> None:
    parsed = parse_positive_coeff(value)
    assert isinstance(parsed, Decimal)
    assert parsed > 0


def test_coeff_uses_string_construction_no_float_drift() -> None:
    # Decimal(str(0.1)) == Decimal("0.1"), not the binary-float expansion.
    assert parse_positive_coeff(0.1) == Decimal("0.1")


@pytest.mark.parametrize("bad", [0, -1, "0", "-2.5"])
def test_non_positive_coeff_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="> 0"):
        parse_positive_coeff(bad)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", float("nan"), float("inf")])
def test_non_finite_coeff_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_positive_coeff(bad)


@pytest.mark.parametrize("bad", ["abc", None, object()])
def test_non_numeric_coeff_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="not a number"):
        parse_positive_coeff(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/domain/test_cost_class_rules.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.domain.cost_class_rules'`

- [ ] **Step 3: Write the module**

Create `src/kdive/domain/cost_class_rules.py`:

```python
"""The single name/coeff validation rule for a cost class (ADR-0115 §1).

Both the inventory model (``[[cost_class]]`` in ``systems.toml``) and the imperative
``ops.set_cost_class_coeff`` tool validate a cost class the same way. To keep the two
surfaces from diverging — and without ``inventory/`` importing ``mcp/tools/ops`` (a
core→tool layering inversion) — the rule lives here, neutral. It raises a bare
:class:`ValueError`; each caller maps it to its own error type (``InventoryError`` at file
load, ``CONFIGURATION_ERROR`` for the tool).
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, InvalidOperation


def validate_cost_class_name(name: str) -> str:
    """Return ``name`` if non-blank; raise ``ValueError`` otherwise (fail closed).

    A blank class would seed an unreachable junk row no host can carry.
    """
    if not name.strip():
        raise ValueError(f"cost_class name {name!r} must be non-blank")
    return name


def parse_positive_coeff(value: object) -> Decimal:
    """Parse ``value`` into a finite, positive coefficient (fail closed).

    Uses ``Decimal(str(value))`` so a TOML float does not introduce binary-float drift.
    A coefficient is a price multiplier; ``0`` or negative would price work as free or as a
    budget credit, so both are rejected, as is a non-finite (``nan``/``inf``) value.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, DecimalException, ValueError, TypeError):
        raise ValueError(f"coeff {value!r} is not a number") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"coeff {value!r} must be a finite number > 0")
    return parsed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/domain/test_cost_class_rules.py -q`
Expected: PASS

- [ ] **Step 5: Refactor `ops/tuning.py` to call the shared rule (keep existing tests green)**

In `src/kdive/mcp/tools/ops/tuning.py`, add the import near the other `kdive.domain` imports:

```python
from kdive.domain.cost_class_rules import parse_positive_coeff, validate_cost_class_name
```

Replace the body of `_validate_cost_class` so it delegates to the shared rule and maps the neutral `ValueError` to the tool's `CategorizedError`:

```python
def _validate_cost_class(cost_class: str) -> None:
    """Reject a blank cost class (fail closed); delegates to the shared rule (ADR-0115 §1)."""
    try:
        validate_cost_class_name(cost_class)
    except ValueError as exc:
        raise CategorizedError(
            str(exc),
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "cost_class", "value": cost_class},
        ) from None
```

Replace the body of `_parse_positive_coeff` the same way:

```python
def _parse_positive_coeff(value: object) -> Decimal:
    """Parse ``value`` into a finite, positive coefficient (the shared rule; ADR-0115 §1)."""
    try:
        return parse_positive_coeff(value)
    except ValueError as exc:
        raise CategorizedError(
            str(exc),
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "coeff", "value": str(value)},
        ) from None
```

Remove the now-unused top-level imports if they are no longer referenced elsewhere in the file: `from decimal import Decimal, DecimalException, InvalidOperation` becomes `from decimal import Decimal` (the type annotation still needs `Decimal`; `DecimalException`/`InvalidOperation` move into the shared module). Verify with the lint step.

- [ ] **Step 6: Run the tuning tests + lint to verify the refactor preserves behavior**

Run: `uv run pytest tests/mcp/ops/test_ops_tuning.py -q`
Expected: PASS (the tool's error type and messages are unchanged)

Run: `uv run ruff check src/kdive/mcp/tools/ops/tuning.py && uv run ty check src/kdive/mcp/tools/ops/tuning.py`
Expected: no warnings (fix any unused-import warning from Step 5)

- [ ] **Step 7: Commit**

```bash
git add src/kdive/domain/cost_class_rules.py tests/domain/test_cost_class_rules.py src/kdive/mcp/tools/ops/tuning.py
git commit -m "refactor: extract shared cost-class name/coeff rule"
```

---

## Task 2: `CostClassEntry` model + `InventoryDoc.cost_class`

**Files:**
- Modify: `src/kdive/inventory/model.py`
- Test: `tests/inventory/test_model.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/inventory/test_model.py`:

```python
from decimal import Decimal

from kdive.inventory.model import CostClassEntry


def test_cost_class_block_parses() -> None:
    d = _doc(cost_class=[{"name": "premium", "coeff": 2.5}])
    doc = InventoryDoc.parse(d)
    assert doc.cost_class[0].name == "premium"
    assert doc.cost_class[0].coeff == Decimal("2.5")


def test_cost_class_coeff_uses_decimal_string_construction() -> None:
    # A TOML float 0.1 must land as Decimal("0.1"), not the binary-float expansion.
    doc = InventoryDoc.parse(_doc(cost_class=[{"name": "c", "coeff": 0.1}]))
    assert doc.cost_class[0].coeff == Decimal("0.1")


def test_cost_class_absent_defaults_empty() -> None:
    assert InventoryDoc.parse(_doc()).cost_class == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_cost_class_blank_name_rejected(bad: str) -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(cost_class=[{"name": bad, "coeff": 1.0}]))


@pytest.mark.parametrize("bad", [0, -1, "0", "-2"])
def test_cost_class_non_positive_coeff_rejected(bad: object) -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(cost_class=[{"name": "c", "coeff": bad}]))


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_cost_class_non_finite_coeff_rejected(bad: str) -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(cost_class=[{"name": "c", "coeff": bad}]))


def test_duplicate_cost_class_name_rejected() -> None:
    d = _doc(cost_class=[{"name": "dup", "coeff": 1.0}, {"name": "dup", "coeff": 2.0}])
    with pytest.raises(InventoryError) as excinfo:
        InventoryDoc.parse(d)
    assert excinfo.value.entry == "cost_class"
    assert excinfo.value.field == "name"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/inventory/test_model.py -q -k cost_class`
Expected: FAIL with `ImportError: cannot import name 'CostClassEntry'`

- [ ] **Step 3: Add the model + field + uniqueness check**

In `src/kdive/inventory/model.py`, add `from decimal import Decimal` to the stdlib imports, extend the existing pydantic import to include `field_validator`, and add the shared-rule import. The pydantic line becomes:

```python
from pydantic import BaseModel, Field, ValidationError, field_validator
```

and add, with the other `kdive.*` imports:

```python
from decimal import Decimal  # with the stdlib imports, above the third-party ones

from kdive.domain.cost_class_rules import parse_positive_coeff, validate_cost_class_name
```

Add the `CostClassEntry` model after `BuildHostInstance`:

```python
class CostClassEntry(BaseModel):
    """A single ``[[cost_class]]`` declaration: a pricing coefficient for a cost class.

    Validation delegates to ``domain/cost_class_rules`` — the same rule
    ``ops.set_cost_class_coeff`` applies — so the file and the tool cannot diverge. A
    field-validator raising ``ValueError`` surfaces as a pydantic ``ValidationError`` that
    :meth:`InventoryDoc.parse` maps to :class:`InventoryError` (ADR-0115 §1, §6).
    """

    name: str
    coeff: Decimal

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return validate_cost_class_name(value)

    @field_validator("coeff", mode="before")
    @classmethod
    def _check_coeff(cls, value: object) -> Decimal:
        return parse_positive_coeff(value)
```

Add the field to `InventoryDoc` (after `build_host`):

```python
    cost_class: list[CostClassEntry] = Field(default_factory=list)
```

Add the uniqueness check method to `InventoryDoc`:

```python
    def _check_cost_class_uniqueness(self) -> None:
        names = [c.name for c in self.cost_class]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise InventoryError("cost_class", "name", f"duplicate cost_class names {dupes}")
```

Call it in `parse`, after `_check_remote_libvirt_singleton()`:

```python
        doc._check_remote_libvirt_singleton()
        doc._check_cost_class_uniqueness()
        return doc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/inventory/test_model.py -q`
Expected: PASS (new + all existing model tests)

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src/kdive/inventory/model.py && uv run ty check src/kdive/inventory/model.py`
Expected: no warnings

- [ ] **Step 6: Commit**

```bash
git add src/kdive/inventory/model.py tests/inventory/test_model.py
git commit -m "feat: parse [[cost_class]] coefficient declarations in systems.toml"
```

---

## Task 3: `reconcile_coefficients` pass (file-authoritative upsert + atomic drift)

**Files:**
- Create: `src/kdive/inventory/reconcile_coefficients.py`
- Test: `tests/integration/test_reconcile_coefficients.py` (DB-backed — needs `migrated_url`)

This pass runs under the shared `inventory_pass_lock` (consistency with the sibling passes) and handles each declared `(name, coeff)` in its **own** transaction taking `SELECT coeff … FOR UPDATE` before the write, so a concurrent `ops.set_cost_class_coeff` cannot slip between the read and the clobber unlogged (ADR-0115 §3).

- [ ] **Step 1: Write the failing test (DB-backed, `asyncio.run` idiom)**

Create `tests/integration/test_reconcile_coefficients.py` (note: `migrated_url` resolves via `tests/integration/conftest.py`; the sync-test + inner-`_run()` shape is mandatory here):

```python
"""DB-backed tests for the cost-class coefficient reconcile pass (ADR-0115 §2/§3)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile_coefficients import reconcile_coefficients


def _doc(*classes: tuple[str, str]) -> InventoryDoc:
    return InventoryDoc.parse(
        {
            "schema_version": 2,
            "cost_class": [{"name": n, "coeff": c} for n, c in classes],
        }
    )


async def _coeff(pool: AsyncConnectionPool, name: str) -> Decimal | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s", (name,)
        )
        row = await cur.fetchone()
    return Decimal(row[0]) if row else None


def test_upserts_a_new_declared_coefficient(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            assert await _coeff(pool, "premium") == Decimal("2.5")
            assert [r.name for r in diff.created] == ["premium"]
            assert diff.warned == []

    asyncio.run(_run())


def test_file_value_overrides_existing_row_and_flags_drift(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            # Seed a runtime override that differs from the file.
            async with await psycopg.AsyncConnection.connect(
                migrated_url, autocommit=True
            ) as seed:
                await seed.execute(
                    "INSERT INTO cost_class_coefficients (cost_class, coeff) "
                    "VALUES ('remote', 9.0) "
                    "ON CONFLICT (cost_class) DO UPDATE SET coeff = EXCLUDED.coeff"
                )
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc(("remote", "1.0")))
            assert await _coeff(pool, "remote") == Decimal("1.0")
            assert [r.name for r in diff.updated] == ["remote"]
            drift = [r for r in diff.warned if r.name == "remote"]
            assert drift and "was 9.0" in drift[0].detail and "now 1.0" in drift[0].detail

    asyncio.run(_run())


def test_idempotent_rerun_is_a_clean_no_op(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            assert diff.created == [] and diff.updated == [] and diff.warned == []

    asyncio.run(_run())


def test_removed_block_does_not_delete(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            # A later pass with no [[cost_class]] block leaves the row untouched.
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, _doc())
            assert await _coeff(pool, "premium") == Decimal("2.5")
            assert diff.pruned == []

    asyncio.run(_run())


def test_undeclared_seed_floor_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_coefficients(conn, _doc(("premium", "2.5")))
            # 'local' is seeded by 0002 and never declared here; it must keep its value.
            assert await _coeff(pool, "local") == Decimal("1.0")

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_reconcile_coefficients.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.inventory.reconcile_coefficients'`

- [ ] **Step 3: Write the pass**

Create `src/kdive/inventory/reconcile_coefficients.py`:

```python
"""The cost-class coefficient merge-reconcile (ADR-0115 §2/§3).

Upserts each ``[[cost_class]]`` declaration into ``cost_class_coefficients``
**file-authoritatively**: a declared class is re-asserted to the file value on every pass,
including the continuous reconciler loop. It is **upsert-only** — a class removed from the
file simply stops being re-asserted (its last value persists); reconcile never deletes a
coefficient, so an in-flight host can never be mispriced by a reconcile-driven delete.

Drift detection is **atomic with the write**: each row is taken under ``SELECT coeff …
FOR UPDATE`` in its own transaction, then written, so a concurrent
``ops.set_cost_class_coeff`` cannot slip between a separate read and the clobber and be
reverted unlogged. (Plain ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` returns the
*post*-update row, not the prior ``coeff``, so it cannot supply the "was Y" — the locked
read is required.) When the prior value differs from the file value, the pass records a
``warned`` entry (the one behavior that *changes* a value is never silent) **and** logs the
drift line; the on-demand ``ops.reconcile_systems`` path folds ``warned`` into its
``platform_audit_log`` row. An idempotent re-run (file == DB) produces no diff and no log
noise.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from psycopg import AsyncConnection

from kdive.inventory.model import CostClassEntry, InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff, ReconcileRecord, inventory_pass_lock

_log = logging.getLogger(__name__)


async def reconcile_coefficients(conn: AsyncConnection, doc: InventoryDoc) -> ReconcileDiff:
    """Upsert ``doc``'s ``[[cost_class]]`` declarations file-authoritatively; flag drift.

    Held under the same session-scoped inventory lock as the sibling passes, so a
    multi-row pass never races a second pass. Each declared class is handled in its own
    transaction (a brief ``FOR UPDATE`` hold), so drift is detected atomically with the
    write.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened per row).
        doc: The parsed inventory document.

    Returns:
        The :class:`ReconcileDiff` for the coefficient pass (``created`` for new rows,
        ``updated`` + ``warned`` for a value the file overrides; empty on a no-op).
    """
    diff = ReconcileDiff()
    async with inventory_pass_lock(conn):
        for entry in doc.cost_class:
            await _upsert_one(conn, entry, diff)
    return diff


async def _upsert_one(conn: AsyncConnection, entry: CostClassEntry, diff: ReconcileDiff) -> None:
    """Create or change-detectingly re-assert one coefficient under a per-row lock."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s FOR UPDATE",
            (entry.name,),
        )
        row = await cur.fetchone()
        if row is None:
            # Race-safe create: a concurrent ops insert between the SELECT and here would
            # otherwise abort the pass on the PK; the next pass reconciles any value diff.
            await cur.execute(
                "INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES (%s, %s) "
                "ON CONFLICT (cost_class) DO NOTHING",
                (entry.name, entry.coeff),
            )
            diff.created.append(_record(entry.name, f"priced at {entry.coeff}"))
            return
        prior = Decimal(row[0])
        if prior == entry.coeff:
            return  # idempotent: no write, no diff, no log noise
        await cur.execute(
            "UPDATE cost_class_coefficients SET coeff = %s WHERE cost_class = %s",
            (entry.coeff, entry.name),
        )
        detail = f"re-asserted from file: was {prior}, now {entry.coeff}"
        diff.updated.append(_record(entry.name, detail))
        diff.warned.append(_record(entry.name, detail))
        _log.warning("inventory: cost_class %r %s", entry.name, detail)


def _record(name: str, detail: str) -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=f"cost_class[{name}]", detail=detail)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_reconcile_coefficients.py -q`
Expected: PASS

- [ ] **Step 5: Lint + type-check**

Run: `uv run ruff check src/kdive/inventory/reconcile_coefficients.py && uv run ty check src/kdive/inventory/reconcile_coefficients.py`
Expected: no warnings

- [ ] **Step 6: Commit**

```bash
git add src/kdive/inventory/reconcile_coefficients.py tests/integration/test_reconcile_coefficients.py
git commit -m "feat: reconcile [[cost_class]] coefficients file-authoritatively"
```

---

## Task 4: Shared ordered pipeline (coefficients before resources, both orchestrators)

**Files:**
- Create: `src/kdive/inventory/reconcile_pipeline.py`
- Modify: `src/kdive/reconciler/inventory.py`
- Modify: `src/kdive/mcp/tools/ops/reconcile_systems.py`

The coefficient pass must run **before** `reconcile_resources` in **both** resource-reconciling orchestrators, or the on-demand path silently skips pricing. Collapse the duplicated chain into one helper both call.

- [ ] **Step 1: Write the failing test**

Create `tests/inventory/test_reconcile_pipeline.py`:

```python
"""The shared ordered reconcile pipeline runs coefficients before resources (ADR-0115 §2)."""

from __future__ import annotations

import inspect

from kdive.inventory import reconcile_pipeline


def test_pipeline_orders_coefficients_before_resources() -> None:
    src = inspect.getsource(reconcile_pipeline.reconcile_all)
    assert src.index("reconcile_coefficients") < src.index("reconcile_resources"), (
        "coefficients must be priced before the resource rows are created"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/inventory/test_reconcile_pipeline.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.inventory.reconcile_pipeline'`

- [ ] **Step 3: Write the pipeline module**

Create `src/kdive/inventory/reconcile_pipeline.py`:

```python
"""The one ordered resource-reconciling chain (ADR-0115 §2).

Two orchestrators reconcile resources from ``systems.toml`` — the background reconciler
loop (``reconciler/inventory.py``) and the on-demand ``ops.reconcile_systems`` MCP tool.
Both call :func:`reconcile_all` so the ordering is defined once: the **coefficient pass runs
before the resource pass**, so a config host that declares both a ``cost_class`` and a
matching ``[[cost_class]]`` block is priced in the same pass that creates its row — closing
the unpriced-cost_class admission wall. The images-only CLI (``inventory/reconcile_cli.py``)
reconciles no resources and does not use this helper.
"""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff
from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts
from kdive.inventory.reconcile_coefficients import reconcile_coefficients
from kdive.inventory.reconcile_images import ImageHeadStore, reconcile_images
from kdive.inventory.reconcile_resources import reconcile_resources


async def reconcile_all(
    conn: AsyncConnection, doc: InventoryDoc, store: ImageHeadStore
) -> ReconcileDiff:
    """Reconcile ``doc`` into the catalog in dependency order; return one merged diff.

    Order: images → **coefficients → resources** → build hosts. Coefficients precede
    resources so a host lands priced (ADR-0115 §2). Each sub-pass owns its own locks and
    transactions; this helper only sequences them and folds the per-entity diffs.
    """
    merged = ReconcileDiff()
    _extend(merged, await reconcile_images(conn, doc, store))
    _extend(merged, await reconcile_coefficients(conn, doc))
    _extend(merged, await reconcile_resources(conn, doc))
    _extend(merged, await reconcile_build_hosts(conn, doc))
    return merged


def _extend(into: ReconcileDiff, part: ReconcileDiff) -> None:
    """Fold one per-entity diff into the merged diff."""
    into.created.extend(part.created)
    into.updated.extend(part.updated)
    into.pruned.extend(part.pruned)
    into.cordoned.extend(part.cordoned)
    into.warned.extend(part.warned)
```

- [ ] **Step 4: Wire the background loop onto the pipeline**

In `src/kdive/reconciler/inventory.py`, replace the four reconcile imports:

```python
from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts
from kdive.inventory.reconcile_images import ImageHeadStore, reconcile_images
from kdive.inventory.reconcile_resources import reconcile_resources
```

with:

```python
from kdive.inventory.reconcile_images import ImageHeadStore
from kdive.inventory.reconcile_pipeline import reconcile_all
```

(`reconcile.ReconcileDiff` and `_changes` stay.) Then replace the body of `InventoryReconcilePass.run` after the absent-file guard:

```python
        path = _systems_toml_path()
        doc = self._load(path)
        if doc is None:
            return 0
        diff = await reconcile_all(conn, doc, store)
        return _changes(diff)
```

Update `_changes` is unchanged — note it counts `created+updated+pruned+cordoned`, so a coefficient drift (which appends to both `updated` and `warned`) is counted once via `updated`, and a clean coefficient create is counted via `created`. No change to `_changes` needed.

- [ ] **Step 5: Wire `ops.reconcile_systems` onto the pipeline**

In `src/kdive/mcp/tools/ops/reconcile_systems.py`, replace the three reconcile imports:

```python
from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts
from kdive.inventory.reconcile_images import ImageHeadStore, reconcile_images
from kdive.inventory.reconcile_resources import reconcile_resources
```

with:

```python
from kdive.inventory.reconcile_images import ImageHeadStore
from kdive.inventory.reconcile_pipeline import reconcile_all
```

Replace `_run_pass` so it delegates to the pipeline (and delete the now-unused local `_extend`):

```python
async def _run_pass(pool: AsyncConnectionPool, store: ImageHeadStore) -> ReconcileDiff:
    """Reconcile the inventory file into the catalog and return one merged ``ReconcileDiff``.

    An absent default file is a quiet no-op (an empty diff); a present-but-malformed file
    raises :class:`~kdive.inventory.InventoryError`, surfaced as a categorized failure.
    """
    doc = _load()
    if doc is None:
        return ReconcileDiff()
    async with pool.connection() as conn:
        return await reconcile_all(conn, doc, store)
```

Delete the local `_extend` function (now in `reconcile_pipeline`). Keep `_names`, `_audit_*`, `_response`, etc. The existing `_audit_args` already folds `diff.warned` names into the audit row, so coefficient drift on this path is persisted to `platform_audit_log` with no further change.

- [ ] **Step 6: Run the affected suites + lint**

Run: `uv run pytest tests/inventory/test_reconcile_pipeline.py tests/mcp/ops/test_reconcile_systems.py tests/integration/test_reconcile_inventory.py -q`
Expected: PASS (existing reconcile-systems and integration tests still green — the chain is the same plus the coefficient pass, which is a no-op when no `[[cost_class]]` is declared)

Run: `uv run ruff check src/kdive/inventory/reconcile_pipeline.py src/kdive/reconciler/inventory.py src/kdive/mcp/tools/ops/reconcile_systems.py && uv run ty check src/kdive/inventory/reconcile_pipeline.py src/kdive/reconciler/inventory.py src/kdive/mcp/tools/ops/reconcile_systems.py`
Expected: no warnings (remove any now-unused imports flagged)

- [ ] **Step 7: Commit**

```bash
git add src/kdive/inventory/reconcile_pipeline.py src/kdive/reconciler/inventory.py src/kdive/mcp/tools/ops/reconcile_systems.py tests/inventory/test_reconcile_pipeline.py
git commit -m "refactor: run both reconcile orchestrators through one ordered pipeline"
```

---

## Task 5: `ops.export_cost_classes` capture tool

**Files:**
- Modify: `src/kdive/mcp/tools/ops/tuning.py`
- Modify: `tests/mcp/ops/test_ops_tuning.py`
- Modify: `tests/mcp/core/test_tool_docs.py`

A read-only `PLATFORM_OPERATOR` tool that serializes the live table to a deterministic, name-sorted `[[cost_class]]` TOML fragment so a break-glass override can be captured back into the file. It returns text and writes no file. The coeff is emitted as a **quoted decimal string** so an exported fragment round-trips through `Decimal(str(...))` with no float drift.

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/ops/test_ops_tuning.py` (reuse this file's `_pool`, `_OPERATOR`, `_ctx`, and the `asyncio.run(_run())` idiom; add `import tomllib` and `from kdive.inventory.model import InventoryDoc` to the existing imports if absent):

```python
def test_export_cost_classes_requires_platform_operator(migrated_url: str) -> None:
    # No platform role → denied (the gate), no table read amplification.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.export_cost_classes(pool, _ctx())
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


def test_export_cost_classes_returns_deterministic_sorted_toml(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="zeta", coeff="3.0")
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="alpha", coeff="0.5")
            resp = await tuning.export_cost_classes(pool, _OPERATOR)
            assert resp.status == "ok"
            toml_text = resp.data["toml"]
        # 'local' is seeded; the export is name-sorted, so alpha < local < zeta.
        assert toml_text.index("alpha") < toml_text.index("local") < toml_text.index("zeta")

    asyncio.run(_run())


def test_export_round_trips_through_the_model(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="premium", coeff="2.5")
            resp = await tuning.export_cost_classes(pool, _OPERATOR)
            toml_text = resp.data["toml"]
        parsed = tomllib.loads("schema_version = 2\n" + toml_text)
        doc = InventoryDoc.parse(parsed)
        by_name = {c.name: c.coeff for c in doc.cost_class}
        assert by_name["premium"] == Decimal("2.5")

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mcp/ops/test_ops_tuning.py -q -k export`
Expected: FAIL with `AttributeError: module 'kdive.mcp.tools.ops.tuning' has no attribute 'export_cost_classes'`

- [ ] **Step 3: Add the export handler + registration**

In `src/kdive/mcp/tools/ops/tuning.py`, add tool constants near the existing ones:

```python
_EXPORT_OBJECT_ID = "cost_class_export"
_EXPORT_TOOL = "ops.export_cost_classes"
```

Add the handler (place it after `set_cost_class_coeff`):

```python
async def export_cost_classes(pool: AsyncConnectionPool, ctx: RequestContext) -> ToolResponse:
    """Serialize the live ``cost_class_coefficients`` table to a ``[[cost_class]]`` fragment.

    Read-only (``PLATFORM_OPERATOR``; audited as a platform read). Returns a deterministic,
    name-sorted TOML fragment so a break-glass override can be captured back into
    ``systems.toml`` (override → export → commit → reconcile re-asserts from the file). It
    returns text and does **not** write any file. Reliable only for an **ops-owned** class
    (one not yet in the file); an override on an already-declared class is transient and may
    be re-asserted by the reconciler before the export runs (ADR-0115 §5).
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(pool, ctx, tool=_EXPORT_TOOL, scope="all-cost-classes")
            return _denied(_EXPORT_OBJECT_ID, _EXPORT_TOOL)
        async with pool.connection() as conn:
            rows = await _all_coefficients(conn)
            await _audit_read(conn, ctx)
        return ToolResponse.success(
            _EXPORT_OBJECT_ID,
            "ok",
            suggested_next_actions=[_EXPORT_TOOL],
            data={"toml": _render_toml(rows)},
        )


async def _all_coefficients(conn: AsyncConnection) -> list[tuple[str, Decimal]]:
    """Read every ``(cost_class, coeff)`` row, name-sorted for a deterministic export."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT cost_class, coeff FROM cost_class_coefficients ORDER BY cost_class"
        )
        return [(name, Decimal(coeff)) for name, coeff in await cur.fetchall()]


def _render_toml(rows: list[tuple[str, Decimal]]) -> str:
    """Render rows as ``[[cost_class]]`` blocks; coeff is a quoted string (exact round-trip)."""
    blocks = [
        f'[[cost_class]]\nname = "{name}"\ncoeff = "{coeff}"\n' for name, coeff in rows
    ]
    return "\n".join(blocks)


async def _audit_read(conn: AsyncConnection, ctx: RequestContext) -> None:
    """Audit the platform read to ``platform_audit_log`` (no row mutated)."""
    async with conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=_EXPORT_TOOL,
                scope="all-cost-classes",
                args={"tool": _EXPORT_TOOL},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )
```

Confirm `_denied`, `held_platform_roles`, `actor_for`, and `audit` are already imported in `tuning.py` (they are used by the existing handlers / imports). If `record_platform`'s `audit` module alias is not yet imported, add `from kdive.security import audit` (the file already imports `audit`); reuse it.

Register the tool inside the existing `register(app, pool)` function (append after `ops_set_host_capacity`):

```python
    @app.tool(
        name=_EXPORT_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def ops_export_cost_classes() -> ToolResponse:
        """Export the cost-class coefficient table as a systems.toml fragment. Operator."""
        return await export_cost_classes(pool, current_context())
```

- [ ] **Step 4: Add the tool-docs coverage index entry**

In `tests/mcp/core/test_tool_docs.py`, add to the `TEST_INDEX` mapping (keep it alphabetically grouped near the other `ops.` entries):

```python
    "ops.export_cost_classes": ("tests/mcp/ops/test_ops_tuning.py",),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/mcp/ops/test_ops_tuning.py tests/mcp/core/test_tool_docs.py -q`
Expected: PASS (export tests + `test_active_tools_have_a_covering_test` + `test_every_tool_has_a_description`/`_valid_maturity`)

- [ ] **Step 6: Lint + type-check**

Run: `uv run ruff check src/kdive/mcp/tools/ops/tuning.py && uv run ty check src/kdive/mcp/tools/ops/tuning.py`
Expected: no warnings

- [ ] **Step 7: Commit**

```bash
git add src/kdive/mcp/tools/ops/tuning.py tests/mcp/ops/test_ops_tuning.py tests/mcp/core/test_tool_docs.py
git commit -m "feat: add ops.export_cost_classes capture tool"
```

---

## Task 6: Finding-1 regression (both orchestrators) + Floor + drift-under-concurrency

**Files:**
- Modify: `tests/integration/test_reconcile_inventory.py`

The load-bearing acceptance test: a config host with a novel `cost_class` plus its `[[cost_class]]` block is admitted (no `configuration_error{cost_class}`) after reconcile via **each** resource-reconciling path. The on-demand path is the one that silently skipped pricing before Task 4, so it must be pinned explicitly.

- [ ] **Step 1: Extend `_remote_libvirt_toml` to take a `cost_class` kwarg**

The existing helper hardcodes `cost_class = "remote"`. Add an optional param so a test can declare a novel class. In `tests/integration/test_reconcile_inventory.py`, change the signature and the interpolated line:

```python
def _remote_libvirt_toml(
    *,
    name: str,
    base_image: str = "base",
    vcpus: int = 8,
    memory_mb: int = 16384,
    cost_class: str = "remote",
) -> str:
```

and replace the hardcoded line `'cost_class = "remote"\n'` with:

```python
        f'cost_class = "{cost_class}"\n'
```

- [ ] **Step 2: Write the failing tests (`asyncio.run` idiom, reuse the file's helpers)**

Append to `tests/integration/test_reconcile_inventory.py`. The imports `asyncio`, `dict_row`, `AsyncConnectionPool`, `load_inventory`, `_FakeImageStore`, `_write_toml`, `_remote_libvirt_toml`, `_resource_by_name` already exist in this module; add `from decimal import Decimal`, `from kdive.inventory.model import InventoryDoc`, `from kdive.inventory.reconcile_pipeline import reconcile_all`, `from kdive.inventory.reconcile_coefficients import reconcile_coefficients`, `from kdive.mcp.auth import RequestContext`, and `from kdive.security.authz.rbac import PlatformRole` if absent:

```python
def _priced_remote_toml(coeff: str) -> str:
    # A remote host on a novel cost_class plus its matching [[cost_class]] block.
    return _remote_libvirt_toml(name="h1", cost_class="premium") + (
        f'[[cost_class]]\nname = "premium"\ncoeff = {coeff}\n'
    )


async def _coeff_row(pool: AsyncConnectionPool, name: str) -> Decimal | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s", (name,)
        )
        row = await cur.fetchone()
    return Decimal(row[0]) if row else None


def test_pipeline_prices_before_creating_the_host(migrated_url: str, tmp_path: Path) -> None:
    # The shared pipeline (the loop's path): the coefficient exists and the host is priced
    # after one reconcile_all — no unpriced-cost_class wall.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _priced_remote_toml("3.0")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_all(conn, doc, _FakeImageStore())
            assert await _coeff_row(pool, "premium") == Decimal("3.0")
            async with pool.connection() as conn:
                row = await _resource_by_name(conn, "h1")
            assert row["cost_class"] == "premium"

    asyncio.run(_run())


def test_on_demand_reconcile_systems_also_prices(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The path that silently skipped pricing before Task 4 — pin it explicitly.
    from kdive.config.core_settings import SYSTEMS_TOML
    from kdive.mcp.tools.ops import reconcile_systems as rs

    async def _run() -> None:
        path = _write_toml(tmp_path, _priced_remote_toml("4.0"))
        monkeypatch.setattr(
            rs.config, "get", lambda key: str(path) if key is SYSTEMS_TOML else None
        )
        ctx = RequestContext(
            principal="admin-1",
            agent_session="s",
            projects=(),
            roles={},
            platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            resp = await rs.reconcile_systems(pool, ctx, image_store=None)
            assert resp.status == "ok"
            assert await _coeff_row(pool, "premium") == Decimal("4.0")

    asyncio.run(_run())


def test_absent_file_leaves_seed_floor_priced(migrated_url: str) -> None:
    # Floor: the 0002/0032 seeds survive with no file, so resolve_coeff succeeds for them.
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            assert await _coeff_row(pool, "local") == Decimal("1.0")
            assert await _coeff_row(pool, "remote") == Decimal("1.0")

    asyncio.run(_run())


def test_drift_detected_under_concurrent_ops_override(migrated_url: str) -> None:
    # A reconcile clobbering a differing prior value (the ops-override case) emits drift.
    async def _run() -> None:
        doc = InventoryDoc.parse(
            {"schema_version": 2, "cost_class": [{"name": "premium", "coeff": 1.0}]}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "INSERT INTO cost_class_coefficients (cost_class, coeff) "
                    "VALUES ('premium', 8.0) "
                    "ON CONFLICT (cost_class) DO UPDATE SET coeff = EXCLUDED.coeff"
                )
                await seed.commit()
            async with pool.connection() as conn:
                diff = await reconcile_coefficients(conn, doc)
            assert [r.name for r in diff.warned] == ["premium"]
            assert await _coeff_row(pool, "premium") == Decimal("1.0")

    asyncio.run(_run())
```

Notes for the implementer:
- `_connect(url)` returns a non-autocommit `AsyncConnection`, so the seed insert above commits explicitly. If you prefer, use `psycopg.AsyncConnection.connect(migrated_url, autocommit=True)` instead — match whichever the surrounding tests already use for one-off seeds.
- `_resource_by_name(conn, "h1")` returns the `resources` row as a dict; `_remote_libvirt_toml` names the host `h1`. The remote host's `vcpus`/`memory_mb` ceiling defaults satisfy admission's size check, so the only gate this test exercises is the coefficient.

- [ ] **Step 3: Run the new tests to verify they pass**

Run: `uv run pytest tests/integration/test_reconcile_inventory.py -q -k "prices or floor or drift or on_demand"`
Expected: the four new tests PASS (Task 4 already wired the pipeline). If `test_on_demand_reconcile_systems_also_prices` fails because the coefficient is absent, the pipeline wiring in Task 4 Step 5 is incomplete — fix there, not here.

- [ ] **Step 4: Run the full integration reconcile suite (no regression from the helper change)**

Run: `uv run pytest tests/integration/test_reconcile_inventory.py -q`
Expected: PASS (new + all existing; the `cost_class` kwarg defaults to `"remote"`, so existing callers are unaffected)

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_reconcile_inventory.py
git commit -m "test: pin coefficient pricing across both reconcile orchestrators"
```

---

## Task 7: Document the `[[cost_class]]` block in the committed scaffold

**Files:**
- Modify: `systems.toml.example`

The real `systems.toml` is gitignored (operator-local). Only the committed `.example` scaffold changes.

- [ ] **Step 1: Add a documented `[[cost_class]]` section to `systems.toml.example`**

Add, after the provider-instances section (anchor on the last existing line of the file so the insert does not split an existing table — see the markdown-table insert gotcha):

```toml

# ---------------------------------------------------------------------------------------------
# COST CLASSES — reconciled into cost_class_coefficients (ADR-0115).
#
# Each [[cost_class]] declares the pricing coefficient for a cost_class label a host carries
# (the [[remote_libvirt]]/[[fault_inject]] `cost_class` field). The file is authoritative:
# a declared coefficient is re-asserted on every reconcile, so an `ops.set_cost_class_coeff`
# override on a declared class is transient — edit this file to change a declared price.
#
# A host whose cost_class has no matching block here (and is not a seeded `local`/`remote`)
# is admitted into the catalog but DENIED every allocation (configuration_error). Declare the
# block to price it. `coeff` may be a number (2.5) or a quoted decimal string ("2.5"); the
# latter is what `ops.export_cost_classes` emits for an exact round-trip.
# ---------------------------------------------------------------------------------------------

[[cost_class]]
name = "remote"
coeff = "1.0"
```

- [ ] **Step 2: Verify the example still parses**

Run: `uv run python -c "from pathlib import Path; from kdive.inventory.loader import load_inventory; d = load_inventory(Path('systems.toml.example')); print('cost_class:', [(c.name, str(c.coeff)) for c in d.cost_class])"`
Expected: prints `cost_class: [('remote', '1.0')]` (and no exception)

- [ ] **Step 3: Commit**

```bash
git add systems.toml.example
git commit -m "docs: document the [[cost_class]] block in systems.toml.example"
```

---

## Final verification

- [ ] **Run the full suite (boundary/arch tests live outside the touched dirs)**

Run: `just test` (or `uv run pytest -q` if `just` is unavailable)
Expected: PASS. Watch specifically for: the inventory layering/import-boundary test (confirms `inventory/` still imports nothing from `mcp/` — the shared rule in `domain/` is what keeps that true), and the generated tool-docs / `test_tool_docs` suite (the new tool must appear with a description, valid maturity, `read_only` hint, and a covering-test index entry).

- [ ] **Run lint + types across the whole change**

Run: `just lint && just type` (or `uv run ruff check . && uv run ty check`)
Expected: zero warnings.

- [ ] **Confirm no migration crept in**

Run: `git diff --name-only main... -- src/kdive/db/schema/`
Expected: empty (this feature adds no DDL).

- [ ] **Update the ADR status if the team convention flips Proposed→Accepted on merge.** (M2 ADRs in this repo stay `Proposed` after merge — leave as-is unless told otherwise.)
