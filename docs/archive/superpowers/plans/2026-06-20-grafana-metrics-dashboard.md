# kdive Metrics Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a portable Grafana dashboard that visualizes all 29 operational metrics kdive emits, generated from a compact Python builder and guarded by a coverage test.

**Architecture:** A Python generator (`deploy/grafana/build_dashboard.py`) defines rows/panels in a compact DSL and emits the committed `deploy/grafana/kdive-overview.json`. A test-only catalog helper (`tests/deploy/grafana_catalog.py`) statically enumerates the 29-instrument catalog from the telemetry source modules via `ast`. Tests drift-guard the JSON against the generator, assert datasource portability, and assert every catalog series is referenced by a panel.

**Tech Stack:** Python 3.13, `ast` (stdlib), `json` (stdlib), pytest, OpenTelemetry SDK (`InMemoryMetricReader`, already a dependency), Grafana 10+ dashboard schema v39.

## Global Constraints

- Python: 3.13, managed with `uv`. Run tests with `uv run pytest -q`.
- Lint/format/types: `ruff check`, `ruff format`, `ty check` — zero warnings.
- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, 100-char lines.
- Absolute imports only (no relative `..` imports). Google-style docstrings on public APIs.
- Naming contract (from spec, verified against `src/kdive/health/metrics_text.py`): counters render with **no `_total` suffix** and **no unit suffix**; dots→underscores via `metrics_text._sanitize`; histograms split into `_bucket`/`_sum`/`_count` with an `le` label.
- The catalog is exactly **29 instruments** (25 literal `meter.create_*` names + 4 `reconciler/fleet.py:_INVENTORY` gauges; `kdive.errors` is one series across two modules).
- **Not metrics, never queried:** the five `kdive.config.*` / `kdive.providers.*.settings` strings (config module paths in `kdive/config/manifest.py:SETTING_MODULES`) and the meter scope names `kdive.mcp` / `kdive.worker` / `kdive.reconciler`.
- Every panel and target references the datasource as `{"type": "prometheus", "uid": "${datasource}"}` — no hardcoded UID.
- The committed JSON is generated. Hand-editing it is a test failure; edit the builder and regenerate.

---

## File Structure

- `tests/deploy/grafana_catalog.py` — **Create.** Static catalog enumerator. Public: `catalog_series() -> set[str]`, `TELEMETRY_MODULES`, `EXCLUDED_OTEL_NAMES`.
- `tests/deploy/test_grafana_catalog.py` — **Create.** Unit tests for the enumerator + a live-render anti-vacuity check.
- `deploy/grafana/build_dashboard.py` — **Create.** Dashboard generator. Public: `build_dashboard() -> dict`, `render_json() -> str`, `main() -> None`, `JSON_PATH`.
- `deploy/grafana/kdive-overview.json` — **Create (generated).** The importable dashboard.
- `deploy/grafana/README.md` — **Create.** Import/regenerate instructions.
- `tests/deploy/test_grafana_dashboard.py` — **Create.** Drift-guard + validity + portability + coverage tests.

`tests/deploy/` already exists with `__init__.py`. `ast-grep` is available but the enumerator uses stdlib `ast` (no new dependency).

---

### Task 1: Catalog enumerator helper

**Files:**
- Create: `tests/deploy/grafana_catalog.py`
- Test: `tests/deploy/test_grafana_catalog.py`

**Interfaces:**
- Consumes: `kdive.health.metrics_text._sanitize` (name normalization), `kdive.config.manifest.SETTING_MODULES` (exclusion set).
- Produces: `catalog_series() -> set[str]` returning the 29 rendered base series names (e.g. `kdive_mcp_requests`); `TELEMETRY_MODULES: tuple[str, ...]`; `EXCLUDED_OTEL_NAMES: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/deploy/test_grafana_catalog.py`:

```python
"""Tests for the static metric-catalog enumerator backing the dashboard coverage guard."""

from __future__ import annotations

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.health.metrics_text import render_prometheus
from tests.deploy.grafana_catalog import catalog_series


def test_catalog_has_29_series() -> None:
    assert len(catalog_series()) == 29


def test_catalog_includes_known_instruments() -> None:
    series = catalog_series()
    for expected in (
        "kdive_mcp_requests",
        "kdive_allocation_admission",
        "kdive_reconciler_repairs",
        "kdive_job_queue_depth",
        "kdive_provider_op_duration",
        "kdive_console_bytes",
        "kdive_allocations",
        "kdive_debug_sessions",
        "kdive_host_capacity_total",
    ):
        assert expected in series


def test_catalog_excludes_scope_names_and_config_paths() -> None:
    series = catalog_series()
    for excluded in (
        "kdive_mcp",
        "kdive_worker",
        "kdive_reconciler",
        "kdive_config_core_settings",
        "kdive_providers_local_libvirt_settings",
    ):
        assert excluded not in series


def test_renderer_emits_counter_without_total_suffix() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("kdive.test")
    meter.create_counter("kdive.mcp.requests").add(1, {"tool": "runs.create"})
    body = render_prometheus(reader.get_metrics_data())
    assert "kdive_mcp_requests{" in body
    assert "kdive_mcp_requests_total" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/deploy/test_grafana_catalog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.deploy.grafana_catalog'`.

- [ ] **Step 3: Write the enumerator**

Create `tests/deploy/grafana_catalog.py`:

```python
"""Static enumeration of kdive's emitted metric instruments for the dashboard coverage guard.

Instruments are created inside methods via ``meter.create_*("kdive…")`` string literals, not
module constants, so this walks the AST of the telemetry modules and collects the first
positional argument of each ``create_*`` call. The lifecycle-inventory gauges are the one
family whose name is an f-string (``f"kdive.{table}"`` in ``reconciler/fleet.py``); those four
names are expanded explicitly. Meter *scope* names and config module paths are not instruments
and are excluded by construction (they are never ``create_*`` arguments) and asserted out.
"""

from __future__ import annotations

import ast
import pathlib

from kdive.config.manifest import SETTING_MODULES
from kdive.health.metrics_text import _sanitize

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

TELEMETRY_MODULES: tuple[str, ...] = (
    "src/kdive/mcp/middleware/telemetry.py",
    "src/kdive/mcp/tools/debug/debug_session_telemetry.py",
    "src/kdive/services/allocation/admission/metrics.py",
    "src/kdive/reconciler/fleet.py",
    "src/kdive/reconciler/loop_telemetry.py",
    "src/kdive/reconciler/build_host_fleet.py",
    "src/kdive/reconciler/console_telemetry.py",
    "src/kdive/jobs/worker_telemetry.py",
    "src/kdive/jobs/build_telemetry.py",
    "src/kdive/jobs/handlers/capture_telemetry.py",
)

_CREATE_ATTRS = frozenset(
    {
        "create_counter",
        "create_up_down_counter",
        "create_histogram",
        "create_observable_gauge",
        "create_observable_counter",
        "create_gauge",
    }
)

_INVENTORY_TABLES = ("allocations", "systems", "runs", "debug_sessions")

EXCLUDED_OTEL_NAMES: frozenset[str] = frozenset(
    {"kdive.mcp", "kdive.worker", "kdive.reconciler", *SETTING_MODULES}
)


def _otel_instrument_names() -> set[str]:
    names: set[str] = set()
    for rel in TELEMETRY_MODULES:
        tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _CREATE_ATTRS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.startswith("kdive.")
            ):
                names.add(first.value)
    names.update(f"kdive.{table}" for table in _INVENTORY_TABLES)
    if names & EXCLUDED_OTEL_NAMES:
        msg = f"excluded non-instruments leaked into the catalog: {names & EXCLUDED_OTEL_NAMES}"
        raise AssertionError(msg)
    return names


def catalog_series() -> set[str]:
    """Return the rendered Prometheus base series names for every emitted instrument."""
    return {_sanitize(name) for name in _otel_instrument_names()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/deploy/test_grafana_catalog.py -q`
Expected: PASS (4 tests). If `test_catalog_has_29_series` fails with a different count, print `sorted(catalog_series())` and reconcile against the spec catalog table before changing the literal `29` — the count is load-bearing.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format tests/deploy/grafana_catalog.py tests/deploy/test_grafana_catalog.py
uv run ruff check tests/deploy/grafana_catalog.py tests/deploy/test_grafana_catalog.py
uv run ty check tests/deploy/grafana_catalog.py
git add tests/deploy/grafana_catalog.py tests/deploy/test_grafana_catalog.py
git commit -m "test(grafana): static metric-catalog enumerator for dashboard coverage guard"
```

---

### Task 2: Dashboard generator scaffold + drift/validity/portability tests

**Files:**
- Create: `deploy/grafana/__init__.py` (empty — makes `deploy.grafana` an explicit, unambiguously importable package under `pythonpath = ["."]`).
- Create: `deploy/grafana/build_dashboard.py`
- Create: `deploy/grafana/kdive-overview.json` (generated)
- Test: `tests/deploy/test_grafana_dashboard.py`

**Interfaces:**
- Produces: `build_dashboard() -> dict` (full dashboard model), `render_json() -> str` (canonical serialization: `json.dumps(build_dashboard(), indent=2) + "\n"`), `main() -> None` (writes `JSON_PATH`), `JSON_PATH: pathlib.Path`, `DATASOURCE: dict`. Panel/grid helpers are added in Task 3.
- Consumes: nothing yet (rows added in Tasks 3–5).

**Import resolution note:** the repo sets `pythonpath = ["."]` (pyproject `[tool.pytest.ini_options]`) and `uv run ty check` type-checks the whole tree, so `from deploy.grafana.build_dashboard import ...` resolves once `deploy/grafana/__init__.py` exists. `deploy/` itself stays a namespace package (it holds non-Python compose/helm dirs and has no `__init__.py`); only `deploy/grafana/` becomes a regular package. If pytest or ty still cannot resolve the import, confirm `deploy/grafana/__init__.py` is committed before debugging further.

- [ ] **Step 1: Write the failing test**

Create `tests/deploy/test_grafana_dashboard.py`:

```python
"""Drift, validity, portability, and coverage tests for the generated Grafana dashboard."""

from __future__ import annotations

import json
import re

from deploy.grafana.build_dashboard import JSON_PATH, build_dashboard, render_json
from tests.deploy.grafana_catalog import catalog_series


def test_committed_json_matches_generator() -> None:
    assert JSON_PATH.read_text(encoding="utf-8") == render_json()


def test_dashboard_has_expected_top_level_keys() -> None:
    dash = build_dashboard()
    for key in ("title", "uid", "schemaVersion", "templating", "panels"):
        assert key in dash


def test_committed_json_is_valid_json() -> None:
    json.loads(JSON_PATH.read_text(encoding="utf-8"))


def test_every_target_uses_templated_datasource() -> None:
    body = JSON_PATH.read_text(encoding="utf-8")
    uids = set(re.findall(r'"uid":\s*"([^"]+)"', body))
    assert uids == {"${datasource}"}, f"non-templated datasource uid present: {uids}"
```

(The coverage test is added in Task 6, once panels exist.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/deploy/test_grafana_dashboard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deploy.grafana.build_dashboard'`.

- [ ] **Step 3: Create the package marker and generator scaffold**

Create an empty `deploy/grafana/__init__.py`:

```python
```

Create `deploy/grafana/build_dashboard.py`:

```python
"""Generator for the kdive metrics-overview Grafana dashboard.

The committed ``kdive-overview.json`` is emitted by this module — never hand-edited. Run
``python -m deploy.grafana.build_dashboard`` (or ``uv run python deploy/grafana/build_dashboard.py``)
to regenerate after changing a row. A drift-guard test asserts the committed file matches.
"""

from __future__ import annotations

import json
import pathlib

JSON_PATH = pathlib.Path(__file__).with_name("kdive-overview.json")

DATASOURCE: dict[str, str] = {"type": "prometheus", "uid": "${datasource}"}


def _templating() -> dict:
    return {
        "list": [
            {
                "name": "datasource",
                "type": "datasource",
                "query": "prometheus",
                "label": "Data source",
                "hide": 0,
                "refresh": 1,
                "current": {},
            }
        ]
    }


def build_dashboard() -> dict:
    """Return the full Grafana dashboard model as a plain dict."""
    panels = _build_panels()
    return {
        "title": "kdive — Metrics Overview",
        "uid": "kdive-overview",
        "tags": ["kdive"],
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {},
        "annotations": {"list": []},
        "templating": _templating(),
        "panels": panels,
    }


def _build_panels() -> list[dict]:
    """Assemble every row's panels. Rows are appended in Tasks 3–5."""
    grid = _Grid()
    return grid.panels


def render_json() -> str:
    """Return the canonical JSON serialization committed to disk."""
    return json.dumps(build_dashboard(), indent=2) + "\n"


def main() -> None:
    JSON_PATH.write_text(render_json(), encoding="utf-8")


if __name__ == "__main__":
    main()
```

Add the grid accumulator to the same file (above `build_dashboard`):

```python
class _Grid:
    """Sequential panel layout over Grafana's 24-column grid.

    ``row()`` starts a collapsible row marker (full width, height 1) and resets the cursor;
    ``add()`` places a panel at the cursor, wrapping to a new band when the row width is full.
    """

    _GRID_WIDTH = 24
    _PANEL_HEIGHT = 8

    def __init__(self) -> None:
        self.panels: list[dict] = []
        self._y = 0
        self._x = 0
        self._next_id = 1

    def _id(self) -> int:
        ident = self._next_id
        self._next_id += 1
        return ident

    def row(self, title: str) -> None:
        if self.panels:
            self._y += self._PANEL_HEIGHT
        self.panels.append(
            {
                "type": "row",
                "title": title,
                "collapsed": False,
                "id": self._id(),
                "gridPos": {"h": 1, "w": self._GRID_WIDTH, "x": 0, "y": self._y},
                "panels": [],
            }
        )
        self._y += 1
        self._x = 0

    def add(self, panel: dict, width: int) -> None:
        if self._x + width > self._GRID_WIDTH:
            self._x = 0
            self._y += self._PANEL_HEIGHT
        panel = dict(panel)
        panel["id"] = self._id()
        panel["gridPos"] = {"h": self._PANEL_HEIGHT, "w": width, "x": self._x, "y": self._y}
        self.panels.append(panel)
        self._x += width
```

- [ ] **Step 4: Generate the JSON**

Run: `uv run python deploy/grafana/build_dashboard.py`
Expected: writes `deploy/grafana/kdive-overview.json` (a valid dashboard with an empty `panels` list).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/deploy/test_grafana_dashboard.py -q`
Expected: PASS (4 tests). `test_every_target_uses_templated_datasource` passes because the only `uid` present is the template variable's own (there are no panel targets yet — the regex set equals `{"${datasource}"}`).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ruff check deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ty check deploy/grafana/build_dashboard.py
git add deploy/grafana/__init__.py deploy/grafana/build_dashboard.py deploy/grafana/kdive-overview.json tests/deploy/test_grafana_dashboard.py
git commit -m "feat(grafana): dashboard generator scaffold + drift/validity/portability tests"
```

---

### Task 3: Panel primitives + Rows 1–3 (MCP, Allocation, Lifecycle)

**Files:**
- Modify: `deploy/grafana/build_dashboard.py`
- Modify: `deploy/grafana/kdive-overview.json` (regenerate)
- Test: `tests/deploy/test_grafana_dashboard.py`

**Interfaces:**
- Produces panel helpers: `_timeseries(title, targets, *, unit="short", stacked=False) -> dict`, `_bargauge(title, targets, *, unit="short") -> dict`, `_target(expr, legend) -> dict`, `_rate(name, by) -> str`, `_quantile(name, q, by=()) -> str`. Consumed by Tasks 4–5.

- [ ] **Step 1: Write the failing test**

Append to `tests/deploy/test_grafana_dashboard.py`:

```python
def _all_exprs() -> list[str]:
    return [
        t["expr"]
        for p in build_dashboard()["panels"]
        for t in p.get("targets", [])
    ]


def test_rows_1_to_3_present() -> None:
    titles = [p["title"] for p in build_dashboard()["panels"] if p["type"] == "row"]
    for row in ("MCP request plane", "Allocation / admission", "Lifecycle inventory"):
        assert row in titles


def test_admission_reason_breakdown_panel_exists() -> None:
    exprs = " ".join(_all_exprs())
    assert "kdive_allocation_admission" in exprs
    assert "reason" in exprs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/deploy/test_grafana_dashboard.py::test_rows_1_to_3_present -q`
Expected: FAIL — `assert 'MCP request plane' in []`.

- [ ] **Step 3: Add panel primitives**

Insert into `deploy/grafana/build_dashboard.py` (above `_build_panels`):

```python
_RATE_WINDOW = "$__rate_interval"


def _target(expr: str, legend: str) -> dict:
    return {
        "datasource": DATASOURCE,
        "expr": expr,
        "legendFormat": legend,
        "refId": "A",
        "range": True,
    }


def _rate(name: str, by: str) -> str:
    return f"sum by ({by}) (rate({name}[{_RATE_WINDOW}]))"


def _quantile(name: str, quant: float, by: tuple[str, ...] = ()) -> str:
    grouping = ", ".join(("le", *by))
    return (
        f"histogram_quantile({quant}, "
        f"sum by ({grouping}) (rate({name}_bucket[{_RATE_WINDOW}])))"
    )


def _panel(kind: str, title: str, targets: list[dict], unit: str, stacked: bool) -> dict:
    stacking = {"mode": "normal"} if stacked else {"mode": "none"}
    return {
        "type": kind,
        "title": title,
        "datasource": DATASOURCE,
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": {"stacking": stacking}},
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "table", "placement": "bottom"}},
        "targets": targets,
    }


def _timeseries(title: str, targets: list[dict], *, unit: str = "short", stacked: bool = False) -> dict:
    return _panel("timeseries", title, targets, unit, stacked)


def _bargauge(title: str, targets: list[dict], *, unit: str = "short") -> dict:
    panel = _panel("bargauge", title, targets, unit, False)
    panel["options"] = {"displayMode": "gradient", "orientation": "horizontal"}
    return panel
```

- [ ] **Step 4: Add Rows 1–3 to `_build_panels`**

Replace the body of `_build_panels` in `deploy/grafana/build_dashboard.py`:

```python
def _build_panels() -> list[dict]:
    grid = _Grid()
    _row_mcp(grid)
    _row_allocation(grid)
    _row_lifecycle(grid)
    return grid.panels


def _row_mcp(grid: _Grid) -> None:
    grid.row("MCP request plane")
    grid.add(
        _timeseries(
            "Request rate by tool",
            [_target(_rate("kdive_mcp_requests", "tool"), "{{tool}}")],
            unit="reqps",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Request errors by tool",
            [_target(_rate("kdive_mcp_request_errors", "tool"), "{{tool}}")],
            unit="reqps",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Request latency (p50/p95/p99)",
            [
                _target(_quantile("kdive_mcp_request_duration", q), label)
                for q, label in ((0.50, "p50"), (0.95, "p95"), (0.99, "p99"))
            ],
            unit="s",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Debug-session duration (p95)",
            [_target(_quantile("kdive_debug_session_duration", 0.95), "p95")],
            unit="s",
        ),
        width=12,
    )


def _row_allocation(grid: _Grid) -> None:
    grid.row("Allocation / admission")
    grid.add(
        _timeseries(
            "Admission decisions by outcome",
            [_target(_rate("kdive_allocation_admission", "outcome"), "{{outcome}}")],
            unit="ops",
            stacked=True,
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Rejections by reason",
            [
                _target(
                    f'sum by (reason) (rate(kdive_allocation_admission{{outcome="rejected"}}[{_RATE_WINDOW}]))',
                    "{{reason}}",
                )
            ],
            unit="ops",
            stacked=True,
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Queue wait (p95)",
            [_target(_quantile("kdive_allocation_wait", 0.95), "p95")],
            unit="s",
        ),
        width=12,
    )


def _row_lifecycle(grid: _Grid) -> None:
    grid.row("Lifecycle inventory")
    for metric, title in (
        ("kdive_allocations", "Allocations by state"),
        ("kdive_systems", "Systems by state"),
        ("kdive_runs", "Runs by state"),
        ("kdive_debug_sessions", "Debug sessions by state"),
    ):
        grid.add(
            _timeseries(
                title,
                [_target(f"sum by (state) ({metric})", "{{state}}")],
                unit="short",
                stacked=True,
            ),
            width=12,
        )
```

- [ ] **Step 5: Regenerate and run tests**

```bash
uv run python deploy/grafana/build_dashboard.py
uv run pytest tests/deploy/test_grafana_dashboard.py -q
```
Expected: PASS (all tests, including the drift guard now that the JSON is regenerated and the new row tests).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ruff check deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ty check deploy/grafana/build_dashboard.py
git add deploy/grafana/build_dashboard.py deploy/grafana/kdive-overview.json tests/deploy/test_grafana_dashboard.py
git commit -m "feat(grafana): MCP, allocation, and lifecycle dashboard rows"
```

---

### Task 4: Rows 4–6 (Capacity, Reconciler loop, Jobs)

**Files:**
- Modify: `deploy/grafana/build_dashboard.py`
- Modify: `deploy/grafana/kdive-overview.json` (regenerate)
- Test: `tests/deploy/test_grafana_dashboard.py`

**Interfaces:**
- Consumes: `_timeseries`, `_bargauge`, `_target`, `_rate`, `_quantile`, `_Grid` (Task 3).

- [ ] **Step 1: Write the failing test**

Append to `tests/deploy/test_grafana_dashboard.py`:

```python
def test_rows_4_to_6_present() -> None:
    titles = [p["title"] for p in build_dashboard()["panels"] if p["type"] == "row"]
    for row in ("Capacity / saturation", "Reconciler loop", "Jobs / workers"):
        assert row in titles


def test_saturation_panel_divides_used_by_total() -> None:
    exprs = " ".join(_all_exprs())
    assert "kdive_host_capacity_used" in exprs
    assert "kdive_host_capacity_total" in exprs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/deploy/test_grafana_dashboard.py::test_rows_4_to_6_present -q`
Expected: FAIL — row titles absent.

- [ ] **Step 3: Add Rows 4–6**

Add to `_build_panels` (after `_row_lifecycle(grid)`):

```python
    _row_capacity(grid)
    _row_reconciler(grid)
    _row_jobs(grid)
```

Add the row functions to `deploy/grafana/build_dashboard.py`:

```python
def _row_capacity(grid: _Grid) -> None:
    grid.row("Capacity / saturation")
    grid.add(
        _bargauge(
            "Host-cap saturation by provider",
            [
                _target(
                    "sum by (provider) (kdive_host_capacity_used) "
                    "/ sum by (provider) (kdive_host_capacity_total)",
                    "{{provider}}",
                )
            ],
            unit="percentunit",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Host-cap slots (used vs total)",
            [
                _target("sum by (provider) (kdive_host_capacity_used)", "used {{provider}}"),
                _target("sum by (provider) (kdive_host_capacity_total)", "total {{provider}}"),
            ],
            unit="short",
        ),
        width=12,
    )


def _row_reconciler(grid: _Grid) -> None:
    grid.row("Reconciler loop")
    grid.add(
        _timeseries(
            "Reconcile duration (p95)",
            [_target(_quantile("kdive_reconcile_duration", 0.95), "p95")],
            unit="s",
        ),
        width=8,
    )
    grid.add(
        _timeseries(
            "Reconcile lag (p95)",
            [_target(_quantile("kdive_reconcile_lag", 0.95), "p95")],
            unit="s",
        ),
        width=8,
    )
    grid.add(
        _timeseries(
            "Repairs by kind",
            [_target(_rate("kdive_reconciler_repairs", "repair_kind"), "{{repair_kind}}")],
            unit="ops",
            stacked=True,
        ),
        width=8,
    )
    grid.add(
        _timeseries(
            "Errors by category",
            [_target(_rate("kdive_errors", "error_category"), "{{error_category}}")],
            unit="ops",
            stacked=True,
        ),
        width=12,
    )


def _row_jobs(grid: _Grid) -> None:
    grid.row("Jobs / workers")
    grid.add(
        _timeseries(
            "Job duration p95 by kind",
            [_target(_quantile("kdive_job_duration", 0.95, ("job_kind",)), "{{job_kind}}")],
            unit="s",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Job queue depth",
            [_target("kdive_job_queue_depth", "depth")],
            unit="short",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Retries by kind",
            [_target(_rate("kdive_job_retries", "job_kind"), "{{job_kind}}")],
            unit="ops",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Time-to-claim p95 by kind",
            [_target(_quantile("kdive_job_time_to_claim", 0.95, ("job_kind",)), "{{job_kind}}")],
            unit="s",
        ),
        width=12,
    )
```

- [ ] **Step 4: Regenerate and run tests**

```bash
uv run python deploy/grafana/build_dashboard.py
uv run pytest tests/deploy/test_grafana_dashboard.py -q
```
Expected: PASS (drift guard + new row tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ruff check deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ty check deploy/grafana/build_dashboard.py
git add deploy/grafana/build_dashboard.py deploy/grafana/kdive-overview.json tests/deploy/test_grafana_dashboard.py
git commit -m "feat(grafana): capacity, reconciler, and jobs dashboard rows"
```

---

### Task 5: Rows 7–9 (Build, Provider operations, Capture)

**Files:**
- Modify: `deploy/grafana/build_dashboard.py`
- Modify: `deploy/grafana/kdive-overview.json` (regenerate)
- Test: `tests/deploy/test_grafana_dashboard.py`

**Interfaces:**
- Consumes: panel helpers from Task 3.

- [ ] **Step 1: Write the failing test**

Append to `tests/deploy/test_grafana_dashboard.py`:

```python
def test_rows_7_to_9_present() -> None:
    titles = [p["title"] for p in build_dashboard()["panels"] if p["type"] == "row"]
    for row in ("Build plane", "Provider operations", "Capture"):
        assert row in titles
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/deploy/test_grafana_dashboard.py::test_rows_7_to_9_present -q`
Expected: FAIL — row titles absent.

- [ ] **Step 3: Add Rows 7–9**

Add to `_build_panels` (after `_row_jobs(grid)`):

```python
    _row_build(grid)
    _row_provider(grid)
    _row_capture(grid)
```

Add the row functions:

```python
def _row_build(grid: _Grid) -> None:
    grid.row("Build plane")
    grid.add(
        _timeseries(
            "Build-phase duration p95",
            [_target(_quantile("kdive_build_phase_duration", 0.95, ("build_phase",)), "{{build_phase}}")],
            unit="s",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Build-host capacity by host",
            [_target("sum by (build_host) (kdive_build_host_capacity)", "{{build_host}}")],
            unit="short",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Build-host leases by host",
            [_target("sum by (build_host) (kdive_build_host_leases)", "{{build_host}}")],
            unit="short",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Build-host reachability by host",
            [_target("sum by (build_host) (kdive_build_host_reachable)", "{{build_host}}")],
            unit="short",
        ),
        width=12,
    )


def _row_provider(grid: _Grid) -> None:
    grid.row("Provider operations")
    grid.add(
        _timeseries(
            "Provider-op duration p95",
            [
                _target(
                    _quantile("kdive_provider_op_duration", 0.95, ("provider", "job_kind")),
                    "{{provider}}/{{job_kind}}",
                )
            ],
            unit="s",
        ),
        width=12,
    )
    grid.add(
        _timeseries(
            "Provider-op errors",
            [_target(_rate("kdive_provider_op_errors", "provider"), "{{provider}}")],
            unit="ops",
        ),
        width=12,
    )


def _row_capture(grid: _Grid) -> None:
    grid.row("Capture")
    grid.add(
        _timeseries(
            "vmcore capture duration p95",
            [_target(_quantile("kdive_vmcore_capture_duration", 0.95), "p95")],
            unit="s",
        ),
        width=8,
    )
    grid.add(
        _timeseries(
            "vmcore capture size p95",
            [_target(_quantile("kdive_vmcore_capture_bytes", 0.95), "p95")],
            unit="bytes",
        ),
        width=8,
    )
    grid.add(
        _timeseries(
            "Console bytes rate",
            [_target("sum (rate(kdive_console_bytes[$__rate_interval]))", "bytes/s")],
            unit="Bps",
        ),
        width=8,
    )
```

- [ ] **Step 4: Regenerate and run tests**

```bash
uv run python deploy/grafana/build_dashboard.py
uv run pytest tests/deploy/test_grafana_dashboard.py -q
```
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ruff check deploy/grafana/build_dashboard.py tests/deploy/test_grafana_dashboard.py
uv run ty check deploy/grafana/build_dashboard.py
git add deploy/grafana/build_dashboard.py deploy/grafana/kdive-overview.json tests/deploy/test_grafana_dashboard.py
git commit -m "feat(grafana): build, provider-op, and capture dashboard rows"
```

---

### Task 6: Coverage guard + README

**Files:**
- Modify: `tests/deploy/test_grafana_dashboard.py`
- Create: `deploy/grafana/README.md`

**Interfaces:**
- Consumes: `catalog_series()` (Task 1), `build_dashboard()` (Task 2).

- [ ] **Step 1: Write the coverage test**

Append to `tests/deploy/test_grafana_dashboard.py`:

```python
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def _referenced_base_series() -> set[str]:
    found: set[str] = set()
    for expr in _all_exprs():
        for match in re.findall(r"kdive_[a-z0-9_]+", expr):
            base = match
            for suffix in _HISTOGRAM_SUFFIXES:
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            found.add(base)
    return found


def test_dashboard_covers_full_catalog() -> None:
    assert _referenced_base_series() == catalog_series()


_KNOWN_PANEL_TYPES = {"row", "timeseries", "bargauge", "stat", "table"}


def _content_panels() -> list[dict]:
    return [p for p in build_dashboard()["panels"] if p["type"] != "row"]


def test_every_panel_type_is_known() -> None:
    types = {p["type"] for p in build_dashboard()["panels"]}
    assert types <= _KNOWN_PANEL_TYPES, f"unknown panel type(s): {types - _KNOWN_PANEL_TYPES}"


def test_every_content_panel_has_renderable_targets() -> None:
    for panel in _content_panels():
        targets = panel.get("targets", [])
        assert targets, f"panel {panel['title']!r} has no targets"
        for target in targets:
            assert target.get("datasource"), f"target in {panel['title']!r} missing datasource"
            assert target.get("expr", "").strip(), f"target in {panel['title']!r} has empty expr"


def test_panel_gridpos_rectangles_do_not_overlap() -> None:
    rects = [
        (p["gridPos"]["x"], p["gridPos"]["y"], p["gridPos"]["w"], p["gridPos"]["h"])
        for p in build_dashboard()["panels"]
    ]
    for i, (ax, ay, aw, ah) in enumerate(rects):
        for bx, by, bw, bh in rects[i + 1 :]:
            overlap = ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
            assert not overlap, f"panels overlap at ({ax},{ay}) and ({bx},{by})"
```

- [ ] **Step 2: Run tests to verify coverage and structure**

Run: `uv run pytest tests/deploy/test_grafana_dashboard.py -q`
Expected: PASS. `test_dashboard_covers_full_catalog` prints the symmetric difference on failure — series in the catalog but not the dashboard (missing panel) or a stray series (e.g. an accidental `_total`). The three structural tests catch a typo'd panel `type`, a target with no `datasource`/`expr`, and overlapping `gridPos` — the "imports but renders broken" failures that JSON-validity alone misses.

- [ ] **Step 3: Write the README**

Create `deploy/grafana/README.md`:

```markdown
# kdive — Grafana metrics dashboard

`kdive-overview.json` is a portable Grafana dashboard (Grafana 10+, schema v39) showing all 29
operational metrics kdive emits, grouped into nine collapsible subsystem rows.

## Import

1. In Grafana: **Dashboards → New → Import**, upload `kdive-overview.json`.
2. When prompted, pick the Prometheus datasource that scrapes your kdive deployment. The
   dashboard uses a `${datasource}` variable, so no UID editing is needed.

## What Prometheus must scrape

kdive runs three processes (server, worker, reconciler), each exposing its own
`/metrics` aux endpoint (ADR-0090 §5). Point Prometheus at **all three** — the reference
compose stack does this under the `obs` profile:

    docker compose --profile obs up -d prometheus

Metrics are served by a hand-rolled exposition renderer (`src/kdive/health/metrics_text.py`),
**not** the OpenTelemetry Prometheus exporter: counters have **no `_total` suffix** and there
are no unit suffixes. Off-the-shelf OTel dashboards will not match these series names.

## Empty panels

On a freshly started stack many counters read zero until traffic flows. Exercise a run
(allocate a system, start a build/debug session) to populate the request, admission, job,
and provider rows.

## Regenerating

The JSON is generated — do not hand-edit it. Edit `build_dashboard.py` and run:

    uv run python deploy/grafana/build_dashboard.py

A test (`tests/deploy/test_grafana_dashboard.py`) drift-guards the committed JSON against the
generator and asserts every emitted instrument has a panel.
```

- [ ] **Step 4: Run the full deploy test module**

Run: `uv run pytest tests/deploy/test_grafana_dashboard.py tests/deploy/test_grafana_catalog.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff format tests/deploy/test_grafana_dashboard.py
uv run ruff check tests/deploy/test_grafana_dashboard.py
git add tests/deploy/test_grafana_dashboard.py deploy/grafana/README.md
git commit -m "feat(grafana): coverage guard + structural-validity tests + README"
```

---

### Task 7: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Confirm the committed JSON is regenerable and clean**

```bash
uv run python deploy/grafana/build_dashboard.py
git diff --exit-code deploy/grafana/kdive-overview.json
```
Expected: no diff (the committed file already matches a fresh generate).

- [ ] **Step 2: Run the project guardrails the way CI does**

Run: `just lint && just type && just test` (per `ci-runs-justfile-recipes-individually` — CI calls these separately, not `just ci`).
Expected: PASS with zero warnings. If `just test` is too broad for a quick loop, scope to `uv run pytest tests/deploy -q` first, then run the full suite once before opening the PR.

- [ ] **Step 3: Manual smoke (optional, documented not automated)**

Bring up the compose `obs` profile, import `kdive-overview.json`, select the Prometheus datasource, exercise a run, and confirm the MCP/admission/jobs rows populate. This is the README's manual step; it is not part of the automated suite.

---

## Self-Review Notes

- **Spec coverage:** Naming contract → Task 1 render test + Global Constraints. 29-instrument catalog → Task 1. Generator + drift guard → Task 2. All 9 rows / 29 series → Tasks 3–5 + Task 6 coverage guard. Datasource portability → Task 2. Excluded non-metrics → Task 1 enumerator exclusion + assertion. README/manual smoke → Task 6 + Task 7.
- **Coverage-guard completeness:** Task 6's `test_dashboard_covers_full_catalog` asserts set equality, so a missing panel OR a stray series (e.g. an accidental `_total`) fails. Task 1's `test_catalog_has_29_series` is the anti-vacuity count.
- **Render validity (not just JSON validity):** Task 6's structural tests (`test_every_panel_type_is_known`, `test_every_content_panel_has_renderable_targets`, `test_panel_gridpos_rectangles_do_not_overlap`) catch the "imports but renders broken/blank" failures — typo'd panel types, targets missing `datasource`/`expr`, overlapping `gridPos` — that JSON-validity and coverage alone miss. The manual Grafana smoke (Task 7) is an additional, not the only, gate.
- **Series-name consistency:** every `expr` uses bare counter names (`kdive_mcp_requests`, not `_total`) and `_bucket` for histogram quantiles; `kdive_host_capacity_total` is a real gauge name and is intentionally not suffix-stripped (only `_bucket`/`_sum`/`_count` are stripped).
- **Label fidelity:** group-by labels match the verified catalog — repairs by `repair_kind` only (no `outcome`), provider-op by `provider`/`job_kind`, admission rejections by `reason`.
