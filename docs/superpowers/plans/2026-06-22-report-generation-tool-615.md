# Report Generation Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `reports.*` MCP tool plane that composes one cross-cutting report (inventory, active/stale leases, guest images, windowed activity, incurred costs) for a chosen scope and date window, returned both inline (agent-friendly) and as downloadable CSV/XLSX spreadsheet artifacts.

**Architecture:** A `services/reports/` domain layer gathers each section behind a `ReportSection` registry (reusing existing data-access) into a `Report`; a `render.py` serializes it to CSV/XLSX; `mcp/tools/reports/generate.py` exposes two tools mirroring the accounting RBAC split, captures one `as_of` snapshot, writes the spreadsheets to the object store, and assembles the envelope. A reconciler GC sweep reaps report artifacts so they do not leak.

**Tech Stack:** Python 3.14, FastMCP, psycopg (async), pydantic, openpyxl (new), pytest + testcontainers Postgres.

**Spec:** `docs/superpowers/specs/2026-06-22-report-generation-tool-615.md` · **ADR:** `docs/adr/0208-report-generation-tool.md`

## Global Constraints

- Python 3.14, managed with `uv`. Absolute imports only (no relative `..`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree: `src` + `tests`).
- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, Google-style docstrings on non-trivial public APIs.
- Every tool returns a `ToolResponse` (`mcp/responses.py`): category iff failure.
- Pick the most specific existing `ErrorCategory` (`domain/errors.py`); never invent strings.
- All free-text/untrusted output passes the redactor before persistence or response snippet.
- Doc-style guard (project-wide): use **Milestone** not "Sprint"; no "critical"/"crucial"/"essential"/"comprehensive"/"robust"/"elegant" in prose, commits, comments.
- Conventional Commits; imperative subject ≤72 chars; end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Pin dependencies with `==`; update `uv.lock`.
- Local guardrails before every commit: `just lint`, `just type`, and the relevant `just test` subset. Full gate is `just ci`.
- Single connection per report: the handler captures one `as_of` (`SELECT now()`) and threads it to every section so the report is internally consistent.

---

### Task 1: Dependency, config settings, env docs

**Files:**
- Modify: `pyproject.toml` (add `openpyxl==<current-stable>` to project dependencies)
- Modify: `uv.lock` (regenerated)
- Modify: `src/kdive/config/core_settings.py` (add two settings after `ARTIFACT_DOWNLOAD_TTL_SECONDS`, ~line 220)
- Modify: env documentation file consumed by `scripts/check_env_documented.py` (locate it first; see Step 4)
- Test: `tests/config/test_report_settings.py`

**Interfaces:**
- Produces: `REPORT_INLINE_MAX_BYTES` and `REPORT_ARTIFACT_RETENTION_DAYS` `Setting`s in `kdive.config.core_settings`, read via `config.require(...)`.

- [ ] **Step 1: Look up + add openpyxl**

Run `uv add 'openpyxl==<pin>'` where `<pin>` is the current stable release — look it up first (do not assume): `uv pip index versions openpyxl` or check PyPI. This edits `pyproject.toml` and `uv.lock`.

Run: `uv run python -c "import openpyxl; print(openpyxl.__version__)"`
Expected: prints the pinned version.

- [ ] **Step 2: Write the failing config test**

```python
# tests/config/test_report_settings.py
from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import (
    REPORT_ARTIFACT_RETENTION_DAYS,
    REPORT_INLINE_MAX_BYTES,
)


def test_report_inline_max_bytes_default() -> None:
    assert config.require(REPORT_INLINE_MAX_BYTES) == 64 * 1024


def test_report_artifact_retention_days_default() -> None:
    assert config.require(REPORT_ARTIFACT_RETENTION_DAYS) == 7
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run python -m pytest tests/config/test_report_settings.py -q`
Expected: FAIL — `ImportError: cannot import name 'REPORT_INLINE_MAX_BYTES'`.

- [ ] **Step 4: Locate the env-doc source**

Run: `uv run python scripts/check_env_documented.py` and read the script head to learn which file/section lists `KDIVE_*` vars (it cross-checks `Setting.name`s against a docs file). Note the path; you edit it in Step 6.

- [ ] **Step 5: Add the settings**

In `src/kdive/config/core_settings.py`, immediately after the `ARTIFACT_DOWNLOAD_TTL_SECONDS` block (~line 220), mirroring that block's style:

```python
REPORT_INLINE_MAX_BYTES = Setting(
    name="KDIVE_REPORT_INLINE_MAX_BYTES",
    parse=_int,
    default=str(64 * 1024),
    group="reports",
    processes=_SERVER,
    help=(
        "Total byte budget for the inline report payload `reports.generate_*` returns in "
        "`items[].data.rows_json`. A section whose serialized rows exceed its share degrades "
        "to a bounded preview plus `inline_truncated`; the full set is in the spreadsheet "
        "artifact (ADR-0208)."
    ),
    suggest="an integer number of bytes, e.g. 65536 (64 KiB)",
)

REPORT_ARTIFACT_RETENTION_DAYS = Setting(
    name="KDIVE_REPORT_ARTIFACT_RETENTION_DAYS",
    parse=_int,
    default="7",
    group="reports",
    processes=_STORE_USERS,
    help=(
        "Age in days after which the reconciler `gc_report_artifacts` sweep deletes a "
        "generated report's spreadsheet artifact (object + row). Reports are ephemeral and "
        "re-runnable (ADR-0208)."
    ),
    suggest="an integer number of days, e.g. 7",
)
```

If `_int` / `_SERVER` / `_STORE_USERS` are module-private and not in scope at that point, they already exist in this module (see `ARTIFACT_*` blocks and line 21); reuse them.

- [ ] **Step 6: Document the env vars**

Add `KDIVE_REPORT_INLINE_MAX_BYTES` and `KDIVE_REPORT_ARTIFACT_RETENTION_DAYS` to the env-doc file located in Step 4, matching the surrounding entry format.

- [ ] **Step 7: Run config test + env guard**

Run: `uv run python -m pytest tests/config/test_report_settings.py -q && uv run python scripts/check_env_documented.py`
Expected: PASS; env guard reports no undocumented settings.

- [ ] **Step 8: Lint, type, commit**

```bash
just lint && just type
git add pyproject.toml uv.lock src/kdive/config/core_settings.py tests/config/test_report_settings.py <env-doc-file>
git commit -m "feat(reports): add openpyxl dep and report config settings"
```

---

### Task 2: Domain core — scope, section protocol, registry, generate_report

**Files:**
- Create: `src/kdive/services/reports/__init__.py`
- Test: `tests/services/reports/test_generate_report.py`

**Interfaces:**
- Produces:
  - `ReportScope` (frozen dataclass): `projects: tuple[str, ...]`, `all_projects: bool`.
  - `SectionRows` (frozen dataclass): `rows: tuple[dict[str, object], ...]`, `truncated: bool`.
  - `ReportSection` (Protocol): `key: str`, `columns: tuple[str, ...]`, `async gather(conn, scope, window, as_of, *, cap) -> SectionRows`.
  - `Section` (frozen dataclass): `key: str`, `columns: tuple[str, ...]`, `rows: tuple[dict[str, object], ...]`, `truncated: bool`.
  - `Report` (frozen dataclass): `sections: tuple[Section, ...]`, `as_of: datetime`.
  - `DEFAULT_SECTION_CAP: int = 500`.
  - `async generate_report(conn, scope, window, as_of, *, sections, cap=DEFAULT_SECTION_CAP) -> Report`.
- Consumes (Task 3 provides the real `sections` tuple): `REGISTRY: tuple[ReportSection, ...]`.

- [ ] **Step 1: Write the failing test (with a fake section)**

```python
# tests/services/reports/test_generate_report.py
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kdive.services.reports import (
    Report,
    ReportScope,
    SectionRows,
    generate_report,
)

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


class _FakeSection:
    key = "fake"
    columns = ("a", "b")

    async def gather(self, conn, scope, window, as_of, *, cap):  # noqa: ANN001
        assert as_of == _AS_OF
        return SectionRows(rows=({"a": "1", "b": scope.projects[0]},), truncated=False)


@pytest.mark.asyncio
async def test_generate_report_runs_each_section_with_shared_as_of() -> None:
    scope = ReportScope(projects=("proj",), all_projects=False)
    report = await generate_report(
        None, scope, None, _AS_OF, sections=(_FakeSection(),)
    )
    assert isinstance(report, Report)
    assert report.as_of == _AS_OF
    assert len(report.sections) == 1
    section = report.sections[0]
    assert section.key == "fake"
    assert section.columns == ("a", "b")
    assert section.rows == ({"a": "1", "b": "proj"},)
    assert section.truncated is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/services/reports/test_generate_report.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.services.reports'`.

- [ ] **Step 3: Write the module**

```python
# src/kdive/services/reports/__init__.py
"""Report domain: section registry and the composed report (ADR-0208)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from psycopg import AsyncConnection

DEFAULT_SECTION_CAP = 500

Window = tuple[datetime | None, datetime | None] | None
Row = dict[str, object]


@dataclass(frozen=True, slots=True)
class ReportScope:
    """The authorized project set a report covers."""

    projects: tuple[str, ...]
    all_projects: bool


@dataclass(frozen=True, slots=True)
class SectionRows:
    """A section's gathered rows plus whether the per-section cap truncated them."""

    rows: tuple[Row, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class Section:
    """One rendered report section."""

    key: str
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class Report:
    """The composed point-in-time report."""

    sections: tuple[Section, ...]
    as_of: datetime


class ReportSection(Protocol):
    """A report section: a stable key, a column schema, and a gather coroutine."""

    key: str
    columns: tuple[str, ...]

    async def gather(
        self,
        conn: AsyncConnection,
        scope: ReportScope,
        window: Window,
        as_of: datetime,
        *,
        cap: int,
    ) -> SectionRows: ...


async def generate_report(
    conn: AsyncConnection,
    scope: ReportScope,
    window: Window,
    as_of: datetime,
    *,
    sections: tuple[ReportSection, ...],
    cap: int = DEFAULT_SECTION_CAP,
) -> Report:
    """Gather every section against one shared ``as_of`` into a :class:`Report`.

    Args:
        conn: Async connection; sections read through it (no transaction opened here).
        scope: The already-authorized project set.
        window: Half-open ``(start, end)`` bound for time-sensitive sections, or ``None``.
        as_of: The single point-in-time snapshot every section observes.
        sections: The ordered section registry to run.
        cap: Per-section row cap.

    Returns:
        A :class:`Report` with one :class:`Section` per registered section, in registry order.
    """
    gathered: list[Section] = []
    for section in sections:
        result = await section.gather(conn, scope, window, as_of, cap=cap)
        gathered.append(
            Section(
                key=section.key,
                columns=section.columns,
                rows=result.rows,
                truncated=result.truncated,
            )
        )
    return Report(sections=tuple(gathered), as_of=as_of)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/services/reports/test_generate_report.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/services/reports/__init__.py tests/services/reports/test_generate_report.py
git commit -m "feat(reports): add report domain core and section registry"
```

---

### Task 3: Section implementations (inventory, leases, images, activity, costs)

**Files:**
- Create: `src/kdive/services/reports/sections.py`
- Modify: `src/kdive/services/reports/__init__.py` (export `REGISTRY`)
- Test: `tests/services/reports/test_sections.py` (DB-backed; testcontainers Postgres)

**Interfaces:**
- Consumes: `ReportScope`, `SectionRows`, `Window`, `Row` from Task 2; `accounting.ledger.report` (`from kdive.services.accounting import ledger`).
- Produces: `REGISTRY: tuple[ReportSection, ...]` = `(InventorySection(), LeasesSection(), ImagesSection(), ActivitySection(), CostsSection())`.

**Notes for the implementer (data-access grounding):**
- `systems(id, allocation_id, shape, domain_name, project, state, ...)`; `allocations(id, resource_id, project, principal, state, lease_expiry, ...)`; `resources(id, kind, ...)`; `system_shapes(name, vcpus, memory_mb, disk_gb)`; `image_catalog` (see `mcp/tools/catalog/images.py:_LIST_SQL`); `runs(id, project, system_id, state, created_at, ...)`.
- vCPU/RAM/disk come from `system_shapes` via **LEFT JOIN** on `systems.shape = system_shapes.name` — null when the shape is not a catalog row. `resources.capabilities` has **no** disk key; do not read it.
- Leases active/stale boundary compares `lease_expiry` against `as_of` (a bound `%s`), not SQL `now()`.
- Costs reuses `accounting.ledger.report(conn, projects=scope.projects, group_by="principal", window=window)` — do not write new ledger SQL.
- Every `gather` selects `cap + 1` rows; if more than `cap` come back, return the first `cap` and `truncated=True`.

- [ ] **Step 1: Write failing DB tests for each section**

```python
# tests/services/reports/test_sections.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kdive.services.reports import ReportScope
from kdive.services.reports.sections import (
    ActivitySection,
    CostsSection,
    ImagesSection,
    InventorySection,
    LeasesSection,
)

# Use the repo's existing DB fixture convention (testcontainers pool + migrated schema).
# Mirror tests/mcp/accounting/test_accounting_usage.py for the pool/migrate fixtures and
# row-insert helpers; reuse those helpers rather than re-rolling INSERTs.

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_inventory_null_specs_for_unknown_shape(db_conn) -> None:  # noqa: ANN001
    # Insert a resource + allocation + system whose `shape` is NOT in system_shapes.
    # Expect one inventory row with vcpus/memory_mb/disk_gb == None and resource_kind set.
    scope = ReportScope(projects=("proj",), all_projects=False)
    result = await InventorySection().gather(db_conn, scope, None, _AS_OF, cap=500)
    row = result.rows[0]
    assert row["vcpus"] is None and row["memory_mb"] is None and row["disk_gb"] is None
    assert row["resource_kind"] is not None


@pytest.mark.asyncio
async def test_leases_active_stale_boundary_against_as_of(db_conn) -> None:  # noqa: ANN001
    # One allocation expiring exactly at _AS_OF (stale), one one second later (active).
    scope = ReportScope(projects=("proj",), all_projects=False)
    result = await LeasesSection().gather(db_conn, scope, None, _AS_OF, cap=500)
    by_id = {r["allocation_id"]: r["status"] for r in result.rows}
    # the allocation with lease_expiry == _AS_OF is stale; > _AS_OF is active
    assert "stale" in by_id.values()
    assert "active" in by_id.values()


@pytest.mark.asyncio
async def test_activity_half_open_window(db_conn) -> None:  # noqa: ANN001
    # Runs created at start (included) and at end (excluded).
    start = _AS_OF - timedelta(hours=1)
    scope = ReportScope(projects=("proj",), all_projects=False)
    result = await ActivitySection().gather(db_conn, scope, (start, _AS_OF), _AS_OF, cap=500)
    times = [r["created_at"] for r in result.rows]
    assert all(start <= t < _AS_OF for t in times)


@pytest.mark.asyncio
async def test_section_cap_truncates(db_conn) -> None:  # noqa: ANN001
    scope = ReportScope(projects=("proj",), all_projects=False)
    result = await ActivitySection().gather(db_conn, scope, None, _AS_OF, cap=1)
    assert result.truncated is True
    assert len(result.rows) == 1


@pytest.mark.asyncio
async def test_images_visibility(db_conn) -> None:  # noqa: ANN001
    scope = ReportScope(projects=("proj",), all_projects=False)
    result = await ImagesSection().gather(db_conn, scope, None, _AS_OF, cap=500)
    # public images and private images owned by proj appear; other-owner private do not
    owners = {r["owner"] for r in result.rows}
    assert "other" not in owners


@pytest.mark.asyncio
async def test_costs_reuses_ledger_report(db_conn) -> None:  # noqa: ANN001
    scope = ReportScope(projects=("proj",), all_projects=False)
    result = await CostsSection().gather(db_conn, scope, None, _AS_OF, cap=500)
    assert result.rows  # one row per (project, principal) with ledger rows
    assert set(("project", "principal", "reserved", "reconciled", "variance")).issubset(
        result.rows[0].keys()
    )
```

> Before writing these, read `tests/mcp/accounting/test_accounting_usage.py` to copy the exact DB fixture names (pool, migration, `db_conn`/`conn`) and any row-insert helpers; align fixture names to the repo's convention.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/services/reports/test_sections.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.services.reports.sections'`.

- [ ] **Step 3: Implement the sections**

```python
# src/kdive/services/reports/sections.py
"""The v1 report sections (ADR-0208). Each gather composes existing data-access."""

from __future__ import annotations

from datetime import datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.services.accounting import ledger as accounting_ledger
from kdive.services.reports import ReportScope, SectionRows, Window

_ACTIVE_STATES = ("granted", "active")


def _cap(rows: list[dict[str, object]], cap: int) -> SectionRows:
    truncated = len(rows) > cap
    return SectionRows(rows=tuple(rows[:cap]), truncated=truncated)


def _window_clause(window: Window, column: str, params: list[object]) -> str:
    if not window:
        return ""
    start, end = window
    clause = ""
    if start is not None:
        clause += f" AND {column} >= %s"
        params.append(start)
    if end is not None:
        clause += f" AND {column} < %s"
        params.append(end)
    return clause


class InventorySection:
    key = "inventory"
    columns = (
        "system_id",
        "name",
        "project",
        "state",
        "resource_kind",
        "vcpus",
        "memory_mb",
        "disk_gb",
    )

    async def gather(
        self, conn: AsyncConnection, scope: ReportScope, window: Window,
        as_of: datetime, *, cap: int,
    ) -> SectionRows:
        params: list[object] = []
        where = ""
        if not scope.all_projects:
            where = " WHERE s.project = ANY(%s)"
            params.append(list(scope.projects))
        sql = (
            "SELECT s.id AS system_id, s.domain_name AS name, s.project, s.state, "
            "r.kind AS resource_kind, sh.vcpus, sh.memory_mb, sh.disk_gb "
            "FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "LEFT JOIN system_shapes sh ON sh.name = s.shape"
            + where
            + " ORDER BY s.created_at DESC, s.id DESC LIMIT %s"
        )
        params.append(cap + 1)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = [dict(r) for r in await cur.fetchall()]
        return _cap(rows, cap)


class LeasesSection:
    key = "leases"
    columns = ("allocation_id", "project", "principal", "state", "lease_expiry", "status")

    async def gather(
        self, conn: AsyncConnection, scope: ReportScope, window: Window,
        as_of: datetime, *, cap: int,
    ) -> SectionRows:
        params: list[object] = [list(_ACTIVE_STATES), as_of, list(_ACTIVE_STATES), as_of]
        scope_clause = ""
        if not scope.all_projects:
            scope_clause = " AND project = ANY(%s)"
            params.append(list(scope.projects))
        sql = (
            "SELECT id AS allocation_id, project, principal, state, lease_expiry, "
            "CASE WHEN state = ANY(%s) AND lease_expiry IS NOT NULL AND lease_expiry > %s "
            "THEN 'active' ELSE 'stale' END AS status "
            "FROM allocations "
            "WHERE (state = 'expired' OR (state = ANY(%s) AND lease_expiry IS NOT NULL)) "
            "AND (lease_expiry IS NOT NULL OR state = 'expired')"
            + scope_clause
            + " ORDER BY lease_expiry DESC NULLS LAST, id DESC LIMIT %s"
        )
        params.append(cap + 1)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = [dict(r) for r in await cur.fetchall()]
        return _cap(rows, cap)


class ImagesSection:
    key = "images"
    columns = ("provider", "name", "arch", "format", "visibility", "owner", "state")

    async def gather(
        self, conn: AsyncConnection, scope: ReportScope, window: Window,
        as_of: datetime, *, cap: int,
    ) -> SectionRows:
        # Mirror catalog/images.py visibility: public, or private owned by an in-scope project.
        params: list[object] = []
        if scope.all_projects:
            visibility = "TRUE"
        else:
            visibility = "(visibility = 'public' OR (visibility = 'private' AND owner = ANY(%s)))"
            params.append(list(scope.projects))
        sql = (
            "SELECT provider, name, arch, format, visibility, owner, state "
            "FROM image_catalog WHERE "
            + visibility
            + " ORDER BY provider, name, arch LIMIT %s"
        )
        params.append(cap + 1)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = [dict(r) for r in await cur.fetchall()]
        return _cap(rows, cap)


class ActivitySection:
    key = "activity"
    columns = ("run_id", "project", "system_id", "state", "created_at")

    async def gather(
        self, conn: AsyncConnection, scope: ReportScope, window: Window,
        as_of: datetime, *, cap: int,
    ) -> SectionRows:
        # Default the window end to as_of for point-in-time consistency.
        effective: Window = window
        if window is None:
            effective = (None, as_of)
        elif window[1] is None:
            effective = (window[0], as_of)
        params: list[object] = []
        scope_clause = ""
        if not scope.all_projects:
            scope_clause = " AND project = ANY(%s)"
            params.append(list(scope.projects))
        window_clause = _window_clause(effective, "created_at", params)
        sql = (
            "SELECT id AS run_id, project, system_id, state, created_at "
            "FROM runs WHERE TRUE"
            + scope_clause
            + window_clause
            + " ORDER BY created_at DESC, id DESC LIMIT %s"
        )
        params.append(cap + 1)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = [dict(r) for r in await cur.fetchall()]
        return _cap(rows, cap)


class CostsSection:
    key = "costs"
    columns = ("project", "principal", "reserved", "reconciled", "variance")

    async def gather(
        self, conn: AsyncConnection, scope: ReportScope, window: Window,
        as_of: datetime, *, cap: int,
    ) -> SectionRows:
        report = await accounting_ledger.report(
            conn, projects=list(scope.projects), group_by="principal", window=window
        )
        rows = [
            {
                "project": r.project,
                "principal": r.principal or "",
                "reserved": str(r.reserved),
                "reconciled": str(r.reconciled),
                "variance": str(r.variance),
            }
            for r in report.rows
        ]
        return _cap(rows, cap)
```

> The `LeasesSection` WHERE is intentionally inclusive of both active and stale rows; the `CASE` labels each. Verify the predicate against the spec: active = `state IN active_states AND lease_expiry > as_of`; everything else selected (expired, or active-state with `lease_expiry <= as_of`) is stale. Simplify the WHERE if the test shows a row class is wrongly dropped — the test is the contract.

- [ ] **Step 4: Export the registry**

In `src/kdive/services/reports/__init__.py`, append at the end:

```python
def _registry() -> tuple[ReportSection, ...]:
    from kdive.services.reports.sections import (
        ActivitySection,
        CostsSection,
        ImagesSection,
        InventorySection,
        LeasesSection,
    )

    return (
        InventorySection(),
        LeasesSection(),
        ImagesSection(),
        ActivitySection(),
        CostsSection(),
    )


REGISTRY: tuple[ReportSection, ...] = _registry()
```

(The deferred import avoids a circular import: `sections.py` imports from `__init__`.)

- [ ] **Step 5: Run section tests**

Run: `uv run python -m pytest tests/services/reports/test_sections.py -q`
Expected: PASS. If Docker is absent these skip; run where Docker is available (or `KDIVE_REQUIRE_DOCKER=1` in CI).

- [ ] **Step 6: For-loop the costs all-projects scope universe**

For `scope.all_projects`, the costs section must report the `ledger ∪ budgets` universe, not an empty `projects` set. The tool handler (Task 5) resolves the all-projects set once and passes it as `scope.projects` with `all_projects=True`, so `CostsSection` receives a populated `projects`; no change here, but assert it in Task 5's test.

- [ ] **Step 7: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/services/reports/sections.py src/kdive/services/reports/__init__.py tests/services/reports/test_sections.py
git commit -m "feat(reports): implement the five v1 report sections"
```

---

### Task 4: Rendering — CSV and XLSX

**Files:**
- Create: `src/kdive/services/reports/render.py`
- Test: `tests/services/reports/test_render.py`

**Interfaces:**
- Consumes: `Report`, `Section` from Task 2.
- Produces:
  - `render_csv(report: Report) -> dict[str, bytes]` — one entry per section, key = section key.
  - `render_xlsx(report: Report) -> bytes` — one workbook, one sheet per section.
  - Each rendering prepends a truncation note when `section.truncated`.

- [ ] **Step 1: Write the failing render tests**

```python
# tests/services/reports/test_render.py
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import openpyxl

from kdive.services.reports import Report, Section
from kdive.services.reports.render import render_csv, render_xlsx

_AS_OF = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


def _report() -> Report:
    inv = Section(
        key="inventory",
        columns=("system_id", "vcpus"),
        rows=({"system_id": "s1", "vcpus": 4}, {"system_id": "s2", "vcpus": None}),
        truncated=False,
    )
    leases = Section(
        key="leases", columns=("allocation_id",),
        rows=({"allocation_id": "a1"},), truncated=True,
    )
    return Report(sections=(inv, leases), as_of=_AS_OF)


def test_render_csv_one_file_per_section_with_header() -> None:
    out = render_csv(_report())
    assert set(out) == {"inventory", "leases"}
    rows = list(csv.reader(io.StringIO(out["inventory"].decode("utf-8"))))
    assert rows[0] == ["system_id", "vcpus"]
    assert rows[1] == ["s1", "4"]
    assert rows[2] == ["s2", ""]  # None -> empty cell


def test_render_csv_marks_truncation() -> None:
    out = render_csv(_report())
    assert b"truncated" in out["leases"].lower()


def test_render_xlsx_sheet_per_section() -> None:
    wb = openpyxl.load_workbook(io.BytesIO(render_xlsx(_report())))
    assert wb.sheetnames == ["inventory", "leases"]
    assert [c.value for c in wb["inventory"][1]] == ["system_id", "vcpus"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/services/reports/test_render.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the renderers**

```python
# src/kdive/services/reports/render.py
"""CSV and XLSX rendering of a Report (ADR-0208). openpyxl is imported here only."""

from __future__ import annotations

import csv
import io

from openpyxl import Workbook

from kdive.services.reports import Report, Section

_TRUNCATED_NOTE = "# truncated: section row cap reached; full data in the spreadsheet"


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def _section_csv(section: Section) -> bytes:
    buffer = io.StringIO()
    if section.truncated:
        buffer.write(f"{_TRUNCATED_NOTE}\n")
    writer = csv.writer(buffer)
    writer.writerow(section.columns)
    for row in section.rows:
        writer.writerow([_cell(row.get(col)) for col in section.columns])
    return buffer.getvalue().encode("utf-8")


def render_csv(report: Report) -> dict[str, bytes]:
    """Render each section to its own CSV file keyed by section key."""
    return {section.key: _section_csv(section) for section in report.sections}


def render_xlsx(report: Report) -> bytes:
    """Render the report to one workbook with a sheet per section."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for section in report.sections:
        sheet = workbook.create_sheet(title=section.key[:31])
        if section.truncated:
            sheet.append([_TRUNCATED_NOTE])
        sheet.append(list(section.columns))
        for row in section.rows:
            sheet.append([_cell(row.get(col)) for col in section.columns])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
```

> Sheet titles cap at 31 chars (an Excel limit) and must be unique; v1 section keys are short and distinct, so `[:31]` is safe. If a future section key collides, suffix it — not a v1 concern.

- [ ] **Step 4: Run render tests**

Run: `uv run python -m pytest tests/services/reports/test_render.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/services/reports/render.py tests/services/reports/test_render.py
git commit -m "feat(reports): add CSV and XLSX report rendering"
```

---

### Task 5: Tool handlers — generate_granted_set / generate_all_projects

**Files:**
- Create: `src/kdive/services/reports/artifacts.py` (write + presign helper, store seam)
- Create: `src/kdive/mcp/tools/reports/__init__.py`
- Create: `src/kdive/mcp/tools/reports/generate.py`
- Test: `tests/mcp/tools/reports/test_generate.py`

**Interfaces:**
- Consumes: Task 2 `generate_report`, `REGISTRY`, `ReportScope`; Task 4 `render_csv`/`render_xlsx`; `ToolResponse`; `parse_timestamptz_window`; `_resolve_granted_set`-equivalent logic (copy the shape from `accounting/reports.py`, do not import its private helper); `require_platform_role`, `require_role`, `audit_platform_denial`, `ALL_PROJECTS_SCOPE`, `actor_for`, `held_platform_roles`; object store (`object_store_from_env`, `put_artifact`, `register_artifact_row`, `presign_get`, `delete`); config `REPORT_INLINE_MAX_BYTES`, `ARTIFACT_DOWNLOAD_TTL_SECONDS`; `Sensitivity`, `ArtifactWriteRequest`.
- Produces: `register(app, pool)`; `async generate_granted_set(pool, ctx, *, projects, window, formats)`; `async generate_all_projects(pool, ctx, *, window, formats)`.

**Notes:**
- `as_of`: `SELECT now()` on the same connection that runs the sections, before gathering.
- Redaction: pass each free-text cell through the redaction registry before it enters the inline envelope or a rendered artifact. Locate the redactor used elsewhere (`security/` redaction registry — grep `redact` in `mcp/tools` and `services`) and apply it to string cells in the gathered rows (a small `_redact_rows(report)` step). If the report contains no secret-bearing free text in v1 (inventory/lease/cost columns are ids/enums/numbers), still route string cells through the redactor so the invariant holds structurally.
- Inline byte budget: serialize each section's rows to `rows_json`; track cumulative bytes against `REPORT_INLINE_MAX_BYTES`; when a section would exceed the remaining budget, emit a bounded preview (first K rows that fit) plus `inline_truncated="true"`.
- Spreadsheet: for each requested format render bytes, `put_artifact` (`Sensitivity.REDACTED`, retention class `"report"`, `owner_kind="reports"`, a fresh report UUID as `owner_id`), `register_artifact_row`, then `presign_get` via `asyncio.to_thread`. A store/`CategorizedError` outage sets `data["spreadsheet_unavailable"]` and drops the refs; the inline report still returns.
- Store I/O is synchronous boto3 — wrap `put_artifact`/`presign_get`/`delete` in `await asyncio.to_thread(...)` (the `artifacts/reads.py` pattern).

- [ ] **Step 1: Write the failing handler tests (injected store seam)**

```python
# tests/mcp/tools/reports/test_generate.py
from __future__ import annotations

import pytest

from kdive.mcp.tools.reports.generate import generate_all_projects, generate_granted_set
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role

# Reuse the DB pool fixture + insert helpers from tests/mcp/accounting/test_accounting_usage.py.


def _ctx(*, projects=("proj",), role=Role.VIEWER, platform=frozenset()):  # noqa: ANN001,ANN202
    roles = {p: role for p in projects} if role is not None else {}
    return RequestContext(
        principal="u1", agent_session="s", projects=projects, roles=roles,
        platform_roles=platform,
    )


@pytest.mark.asyncio
async def test_granted_set_viewer_returns_collection_with_sections(pool, seed) -> None:  # noqa: ANN001
    resp = await generate_granted_set(
        pool, _ctx(), projects=None, window=None, formats=["csv", "xlsx"]
    )
    assert resp.status == "ok"
    keys = {item.data["section"] for item in resp.items}
    assert keys == {"inventory", "leases", "images", "activity", "costs"}
    assert "xlsx" in resp.refs or "spreadsheet_unavailable" in resp.data


@pytest.mark.asyncio
async def test_granted_set_role_less_project_denied(pool, seed) -> None:  # noqa: ANN001
    resp = await generate_granted_set(
        pool, _ctx(role=None), projects=["proj"], window=None, formats=["csv"]
    )
    assert resp.status == "error"
    assert resp.error_category == "authorization_denied"


@pytest.mark.asyncio
async def test_all_projects_requires_platform_auditor(pool, seed) -> None:  # noqa: ANN001
    denied = await generate_all_projects(pool, _ctx(), window=None, formats=["csv"])
    assert denied.error_category == "authorization_denied"
    ok = await generate_all_projects(
        pool, _ctx(platform=frozenset({PlatformRole.PLATFORM_AUDITOR})),
        window=None, formats=["csv"],
    )
    assert ok.status == "ok"


@pytest.mark.asyncio
async def test_bad_formats_is_config_error(pool, seed) -> None:  # noqa: ANN001
    resp = await generate_granted_set(pool, _ctx(), projects=None, window=None, formats=[])
    assert resp.error_category == "configuration_error"


@pytest.mark.asyncio
async def test_store_outage_degrades_to_inline(pool, seed, monkeypatch) -> None:  # noqa: ANN001
    # Patch the store factory used by the handler to raise; assert inline still returns
    # and data["spreadsheet_unavailable"] is set, status stays "ok".
    ...
```

> Fill the `monkeypatch` body using the handler's store-factory seam (Step 3 exposes `store_factory=object_store_from_env` as a parameter so tests inject a failing/recording fake). Build `seed` to insert a resource, allocation, system (catalog shape), an active + a stale lease, a public image + a private `proj` image + a private `other` image, a run, and ledger rows — reuse accounting test helpers.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/tools/reports/test_generate.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the artifact helper + handlers**

Write `src/kdive/services/reports/artifacts.py` with a `write_report_artifacts(report, formats, *, store, tenant, report_id, ttl) -> dict[str, str]` that renders, puts, registers, and presigns — returning `{ref_key: presigned_url}` — and raises `CategorizedError` on store failure (caller degrades). Define a `ReportArtifactStore(Protocol)` with `put_artifact`, `presign_get`, `delete` so tests inject a fake.

Write `src/kdive/mcp/tools/reports/generate.py`:
- `generate_granted_set(pool, ctx, *, projects, window, formats, store_factory=object_store_from_env)`:
  1. Parse `formats` (subset of `{"csv","xlsx"}`, non-empty) and `window` (`parse_timestamptz_window(window, timestamp_column="report")`); on error return `ToolResponse.failure_from_error(... configuration_error ...)`.
  2. Resolve the granted set with the `accounting/reports.py:_resolve_granted_set` shape: `projects is None` → `[p for p in ctx.projects if ctx.roles.get(p) is not None]`; named → `require_role(ctx, p, Role.VIEWER)` for each (catch `AuthorizationError` → `authorization_denied`).
  3. Open one pool connection; `as_of = SELECT now()`; `report = await generate_report(conn, ReportScope(tuple(targets), all_projects=False), window, as_of, sections=REGISTRY)`.
  4. Redact string cells; build inline items honoring `REPORT_INLINE_MAX_BYTES`.
  5. `refs/data = write spreadsheets` via `asyncio.to_thread`, degrading on `CategorizedError`.
  6. Audit when the read shape warrants it (mirror `accounting/reports.py` granted-set audit).
  7. Return `ToolResponse.collection("report", "ok", items, refs=refs, data=top_level)`.
- `generate_all_projects(pool, ctx, *, window, formats, store_factory=...)`:
  1. Parse args (as above).
  2. `require_platform_role(ctx, PlatformRole.PLATFORM_AUDITOR)`; on `AuthorizationError`, `audit_platform_denial(...)` then return `authorization_denied`.
  3. Resolve the all-projects universe: `SELECT project FROM ledger UNION SELECT project FROM budgets ... UNION SELECT project FROM systems ... UNION SELECT project FROM allocations` (the report spans systems/leases too, not only ledger). Pass `ReportScope(tuple(universe), all_projects=True)`.
  4. Same gather/render/envelope path; always audit (mirror `report_all_projects`).

`register(app, pool)` exposes both as `reports.generate_granted_set` / `reports.generate_all_projects` with `_docmeta.read_only()`, `meta={"maturity": "implemented"}`, `Annotated` params (`projects`, `window`, `formats`), calling `current_context()` — copy the wrapper shape from `accounting/reports.py:register`.

Keep each handler ≤100 lines / complexity ≤8 by extracting `_parse_args`, `_inline_items`, `_resolve_granted_targets`, `_spreadsheet_refs` helpers.

- [ ] **Step 4: Run handler tests**

Run: `uv run python -m pytest tests/mcp/tools/reports/test_generate.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/services/reports/artifacts.py src/kdive/mcp/tools/reports/ tests/mcp/tools/reports/test_generate.py
git commit -m "feat(reports): add reports.generate_* tool handlers"
```

---

### Task 6: Register the reports plane

**Files:**
- Modify: `src/kdive/mcp/app.py` (import + append to `_PLANE_REGISTRARS`)
- Test: `tests/mcp/test_app.py` (or the existing tool-registration test) — assert both tool names register.

**Interfaces:**
- Consumes: `register` from `kdive.mcp.tools.reports`.

- [ ] **Step 1: Write/extend the failing registration test**

Find the existing test that builds the app and asserts registered tool names (grep `build_app` in `tests/mcp`). Add:

```python
def test_reports_tools_registered(registered_tool_names) -> None:  # noqa: ANN001
    assert "reports.generate_granted_set" in registered_tool_names
    assert "reports.generate_all_projects" in registered_tool_names
```

Match the fixture/helper the existing registration test uses.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_app.py -q -k reports`
Expected: FAIL.

- [ ] **Step 3: Register**

In `src/kdive/mcp/app.py`: add import near the other tool imports (~line 41):

```python
from kdive.mcp.tools.reports import register as register_report_tools
```

Append to `_PLANE_REGISTRARS` (using the existing `_pool_only_plane_registrar` wrapper):

```python
    _pool_only_plane_registrar(register_report_tools),
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_app.py -q -k reports`
Expected: PASS.

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/mcp/app.py tests/mcp/test_app.py
git commit -m "feat(reports): register the reports tool plane"
```

---

### Task 7: Reconciler GC sweep for report artifacts

**Files:**
- Modify: `src/kdive/reconciler/cleanup/gc.py` (add `gc_report_artifacts`)
- Modify: `src/kdive/reconciler/loop.py` (register the sweep + config field + wire the store)
- Test: `tests/reconciler/test_gc_report_artifacts.py`

**Interfaces:**
- Produces: `async gc_report_artifacts(conn, store, retention) -> int` where `store` is a `ReportArtifactStore` Protocol with `delete(key: str) -> None`.

- [ ] **Step 1: Write the failing GC test**

```python
# tests/reconciler/test_gc_report_artifacts.py
from __future__ import annotations

from datetime import timedelta

import pytest

from kdive.reconciler.cleanup.gc import gc_report_artifacts


class _RecordingStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


@pytest.mark.asyncio
async def test_gc_deletes_only_old_report_artifacts(db_conn) -> None:  # noqa: ANN001
    # Insert: an old report artifact, a fresh report artifact, an old 'systems' artifact.
    # (use owner_kind='reports'/'systems', created_at via explicit timestamps)
    store = _RecordingStore()
    deleted = await gc_report_artifacts(db_conn, store, timedelta(days=7))
    assert deleted == 1  # only the old report artifact
    assert len(store.deleted) == 1  # its object removed
    # assert the fresh report row and the systems row still exist
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/reconciler/test_gc_report_artifacts.py -q`
Expected: FAIL — `ImportError: cannot import name 'gc_report_artifacts'`.

- [ ] **Step 3: Implement the sweep**

In `src/kdive/reconciler/cleanup/gc.py`, mirroring `gc_idempotency_keys` and the per-object resilience of `reap_orphaned_dump_volumes`:

```python
from typing import Protocol


class ReportArtifactStore(Protocol):
    def delete(self, key: str) -> None: ...


async def gc_report_artifacts(
    conn: AsyncConnection, store: ReportArtifactStore, retention: timedelta
) -> int:
    """Delete report artifacts (object + row) older than ``retention`` (ADR-0208).

    Scoped strictly to ``owner_kind = 'reports'`` so System-owned evidence is never touched.
    A per-object store failure is logged and retried next pass, not fatal.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, object_key FROM artifacts "
            "WHERE owner_kind = 'reports' AND created_at < now() - %s",
            (retention,),
        )
        candidates = [(row[0], row[1]) for row in await cur.fetchall()]
    deleted = 0
    for artifact_id, object_key in candidates:
        try:
            await asyncio.to_thread(store.delete, object_key)
        except Exception:  # noqa: BLE001 - one object failure must not starve the rest
            _log.warning(
                "reconciler: deleting report artifact object %s failed; retry next pass",
                object_key, exc_info=True,
            )
            continue
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute("DELETE FROM artifacts WHERE id = %s", (artifact_id,))
        deleted += 1
    if deleted:
        _log.info("reconciler: GC'd %d report artifact(s) past retention", deleted)
    return deleted
```

Add `import asyncio` at the top of `gc.py` if absent.

- [ ] **Step 4: Wire into the reconciler loop**

In `src/kdive/reconciler/loop.py`:
- Add a config field on `ReconcileConfig` (near `idempotency_retention`, ~line 217):
  ```python
  report_artifact_retention: timedelta = timedelta(days=7)
  report_store: ReportArtifactStore | None = None
  ```
- Add the alias near line 92: `_gc_report_artifacts = gc_repairs.gc_report_artifacts`.
- Where the assembly builds the runtime `ReconcileConfig` (find where `idempotency_retention` and `upload_store` are populated from config/env), set `report_artifact_retention=timedelta(days=config.require(REPORT_ARTIFACT_RETENTION_DAYS))` and `report_store=object_store_from_env()` for the reconciler process.
- Append a repair spec guarded on the store being present (the `upload_store` pattern, ~line 297):
  ```python
  if config.report_store is not None:
      report_store = config.report_store
      report_retention = config.report_artifact_retention
      repairs.append(
          _RepairSpec(
              "report_artifacts_gc_count",
              lambda conn: _gc_report_artifacts(conn, report_store, report_retention),
          )
      )
  ```

- [ ] **Step 5: Run GC test + reconciler tests**

Run: `uv run python -m pytest tests/reconciler/test_gc_report_artifacts.py -q`
Expected: PASS. Then run the broader reconciler suite to catch loop-wiring regressions: `uv run python -m pytest tests/reconciler -q`.

- [ ] **Step 6: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/reconciler/cleanup/gc.py src/kdive/reconciler/loop.py tests/reconciler/test_gc_report_artifacts.py
git commit -m "feat(reports): reap report artifacts via reconciler GC sweep"
```

---

### Task 8: Redaction integration test + docs

**Files:**
- Test: `tests/mcp/tools/reports/test_redaction.py`
- Modify: any tool-catalog/surface doc that enumerates MCP tools (grep `accounting.report_granted_set` in `docs/` to find the doc to extend)

**Interfaces:** none new.

- [ ] **Step 1: Write the redaction test**

```python
# tests/mcp/tools/reports/test_redaction.py
# Seed a row whose free-text field carries a registered secret value; generate the report;
# assert the secret does not appear in any inline rows_json nor in the rendered CSV/XLSX bytes.
```

Locate the redaction registry seam used by other tools (grep `redact` under `src/kdive/security`); register a secret in the test the same way the redaction unit tests do, then assert absence in inline + artifact bytes.

- [ ] **Step 2: Run to verify it fails (if redaction not yet wired in Task 5), then wire + pass**

Run: `uv run python -m pytest tests/mcp/tools/reports/test_redaction.py -q`
If FAIL because redaction is not applied, add `_redact_rows` in the Task 5 handler path and re-run to PASS.

- [ ] **Step 3: Document the tools**

Add `reports.generate_granted_set` and `reports.generate_all_projects` to the MCP tool-surface doc found by the grep, matching the existing entry style; reference ADR-0208.

- [ ] **Step 4: Doc + full guardrails, commit**

```bash
just lint && just type && just docs-links && just adr-status-check
git add tests/mcp/tools/reports/test_redaction.py <tool-surface-doc>
git commit -m "test(reports): verify redaction; document the reports tools"
```

---

## Final verification (before pushing — see workflow steps 5–7)

- [ ] Run the full local gate: `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test). Fix everything before pushing.
- [ ] Confirm `KDIVE_REQUIRE_DOCKER=1 just test` passes the DB-backed sections/handlers/GC tests (or run where Docker is available).
- [ ] Confirm no `live_vm` / `live_stack` gates were widened.
