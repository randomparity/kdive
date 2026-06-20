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


def _datasource_uids(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        datasource = node.get("datasource")
        if isinstance(datasource, dict):
            uid = datasource.get("uid")
            if isinstance(uid, str):
                found.append(uid)
        for value in node.values():
            found.extend(_datasource_uids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_datasource_uids(item))
    return found


def test_every_target_uses_templated_datasource() -> None:
    dash = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    uids = set(_datasource_uids(dash))
    leaked = uids - {"${datasource}"}
    assert not leaked, f"hardcoded datasource uid(s): {leaked}"


def _all_exprs() -> list[str]:
    return [t["expr"] for p in build_dashboard()["panels"] for t in p.get("targets", [])]


def test_rows_1_to_3_present() -> None:
    titles = [p["title"] for p in build_dashboard()["panels"] if p["type"] == "row"]
    for row in ("MCP request plane", "Allocation / admission", "Lifecycle inventory"):
        assert row in titles


def test_admission_reason_breakdown_panel_exists() -> None:
    exprs = " ".join(_all_exprs())
    assert "kdive_allocation_admission" in exprs
    assert "reason" in exprs


def test_rows_4_to_6_present() -> None:
    titles = [p["title"] for p in build_dashboard()["panels"] if p["type"] == "row"]
    for row in ("Capacity / saturation", "Reconciler loop", "Jobs / workers"):
        assert row in titles


def test_saturation_panel_divides_used_by_total() -> None:
    exprs = " ".join(_all_exprs())
    assert "kdive_host_capacity_used" in exprs
    assert "kdive_host_capacity_total" in exprs


def test_rows_7_to_9_present() -> None:
    titles = [p["title"] for p in build_dashboard()["panels"] if p["type"] == "row"]
    for row in ("Build plane", "Provider operations", "Capture"):
        assert row in titles


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
