"""Drift, validity, portability, and coverage tests for the generated Grafana dashboard."""

from __future__ import annotations

import json
import re

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


def test_every_target_uses_templated_datasource() -> None:
    body = JSON_PATH.read_text(encoding="utf-8")
    uids = set(re.findall(r'"uid":\s*"([^"]+)"', body))
    assert uids == {"${datasource}"}, f"non-templated datasource uid present: {uids}"
