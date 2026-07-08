"""Drift, validity, portability, and coverage tests for the generated Grafana dashboard."""

from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator

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
    division_exprs = [
        expr
        for expr in _all_exprs()
        if "kdive_host_capacity_used" in expr
        and "kdive_host_capacity_total" in expr
        and "/" in expr
    ]
    assert division_exprs, "no panel divides kdive_host_capacity_used by kdive_host_capacity_total"


def test_rows_7_to_9_present() -> None:
    titles = [p["title"] for p in build_dashboard()["panels"] if p["type"] == "row"]
    for row in ("Provider operations", "Capture"):
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


_DATASOURCE_SCHEMA = {
    "type": "object",
    "required": ["type", "uid"],
    "properties": {"type": {"const": "prometheus"}, "uid": {"type": "string", "minLength": 1}},
}

_GRIDPOS_SCHEMA = {
    "type": "object",
    "required": ["h", "w", "x", "y"],
    "properties": {key: {"type": "integer", "minimum": 0} for key in ("h", "w", "x", "y")},
}

_TARGET_SCHEMA = {
    "type": "object",
    "required": ["datasource", "expr", "refId"],
    "properties": {
        "datasource": _DATASOURCE_SCHEMA,
        "expr": {"type": "string", "minLength": 1},
        "refId": {"type": "string", "minLength": 1},
    },
}

# Fields a non-row (content) panel must carry to render in Grafana.
_CONTENT_PANEL_REQUIRES = {
    "required": ["datasource", "fieldConfig", "options", "targets"],
    "properties": {
        "datasource": _DATASOURCE_SCHEMA,
        "fieldConfig": {"type": "object"},
        "options": {"type": "object"},
        "targets": {"type": "array", "minItems": 1, "items": _TARGET_SCHEMA},
    },
}

# Structural contract every panel the generator emits must satisfy. Encodes what
# Grafana needs to render — not the full (CUE-based, permissive) Grafana schema, so
# it stays deterministic and non-flaky while still catching malformed panels.
_PANEL_SCHEMA = {
    "type": "object",
    "required": ["type", "id", "title", "gridPos"],
    "properties": {
        "type": {"enum": ["row", "timeseries", "bargauge", "stat", "table"]},
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "gridPos": _GRIDPOS_SCHEMA,
    },
    "allOf": [
        # Non-row panels must carry the render-bearing fields.
        {
            "if": {"required": ["type"], "properties": {"type": {"const": "row"}}},
            "else": _CONTENT_PANEL_REQUIRES,
        },
        # Single-value panels must declare how they reduce a series to a value.
        {
            "if": {"required": ["type"], "properties": {"type": {"enum": ["bargauge", "stat"]}}},
            "then": {"properties": {"options": {"required": ["reduceOptions"]}}},
        },
    ],
}

_DASHBOARD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["title", "uid", "schemaVersion", "templating", "panels"],
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "uid": {"type": "string", "minLength": 1},
        "schemaVersion": {"type": "integer"},
        "templating": {
            "type": "object",
            "required": ["list"],
            "properties": {
                "list": {"type": "array", "items": {"type": "object", "required": ["name", "type"]}}
            },
        },
        "panels": {"type": "array", "minItems": 1, "items": _PANEL_SCHEMA},
    },
}


def test_committed_json_matches_grafana_dashboard_schema() -> None:
    dash = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(_DASHBOARD_SCHEMA).iter_errors(dash),
        key=lambda error: list(error.absolute_path),
    )
    messages = [f"{list(error.absolute_path)}: {error.message}" for error in errors]
    assert not messages, "dashboard fails Grafana structural schema:\n" + "\n".join(messages)


def test_panel_gridpos_rectangles_do_not_overlap() -> None:
    rects = [
        (p["gridPos"]["x"], p["gridPos"]["y"], p["gridPos"]["w"], p["gridPos"]["h"])
        for p in build_dashboard()["panels"]
    ]
    for i, (ax, ay, aw, ah) in enumerate(rects):
        for bx, by, bw, bh in rects[i + 1 :]:
            overlap = ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
            assert not overlap, f"panels overlap at ({ax},{ay}) and ({bx},{by})"
