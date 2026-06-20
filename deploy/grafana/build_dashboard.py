"""Generator for the kdive metrics-overview Grafana dashboard.

The committed ``kdive-overview.json`` is emitted by this module — never hand-edited. Run
``uv run python deploy/grafana/build_dashboard.py`` to regenerate after changing a row.
A drift-guard test asserts the committed file matches.
"""

from __future__ import annotations

import json
import pathlib

JSON_PATH = pathlib.Path(__file__).with_name("kdive-overview.json")

DATASOURCE: dict[str, str] = {"type": "prometheus", "uid": "${datasource}"}


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
    return f"histogram_quantile({quant}, sum by ({grouping}) (rate({name}_bucket[{_RATE_WINDOW}])))"


def _panel(kind: str, title: str, targets: list[dict], unit: str, stacked: bool) -> dict:
    stacking = {"mode": "normal"} if stacked else {"mode": "none"}
    for index, target in enumerate(targets):
        target["refId"] = chr(ord("A") + index)
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


def _timeseries(
    title: str, targets: list[dict], *, unit: str = "short", stacked: bool = False
) -> dict:
    return _panel("timeseries", title, targets, unit, stacked)


def _bargauge(title: str, targets: list[dict], *, unit: str = "short") -> dict:
    panel = _panel("bargauge", title, targets, unit, False)
    panel["options"] = {"displayMode": "gradient", "orientation": "horizontal"}
    return panel


def _build_panels() -> list[dict]:
    """Assemble every dashboard row's panels in display order."""
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
                    "sum by (reason) (rate("
                    f'kdive_allocation_admission{{outcome="rejected"}}[{_RATE_WINDOW}]))',
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


def render_json() -> str:
    """Return the canonical JSON serialization committed to disk."""
    return json.dumps(build_dashboard(), indent=2) + "\n"


def main() -> None:
    JSON_PATH.write_text(render_json(), encoding="utf-8")


if __name__ == "__main__":
    main()
