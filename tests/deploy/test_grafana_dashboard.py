"""Drift, validity, portability, and coverage tests for the generated Grafana dashboard."""

from __future__ import annotations

import json

from deploy.grafana.build_dashboard import JSON_PATH, build_dashboard, render_json
from tests.deploy.grafana_catalog import catalog_series  # noqa: F401 — used in Task 6 coverage test


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
