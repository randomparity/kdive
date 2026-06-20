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
