import json
from typing import Any, cast

from kdive.kernel_config.requirements import CRASH_CAPTURE
from kdive.mcp.tools.catalog.artifacts.feature_requirements import feature_config_requirements


def _features() -> list[dict[str, Any]]:
    resp = feature_config_requirements()
    assert resp.status == "ok"
    return cast(list[dict[str, Any]], resp.data["features"])


def test_returns_advisory_manifest_of_every_feature():
    ids = {f["feature"] for f in _features()}
    assert CRASH_CAPTURE in ids and "sysrq" in ids and "debuginfo" in ids


def test_crash_capture_entry_is_gated_and_advertises_kaslr_without_leaking_gate_set():
    crash = next(f for f in _features() if f["feature"] == CRASH_CAPTURE)
    assert crash["gated"] is True
    assert "RANDOMIZE_BASE" in json.dumps(crash["requirements"])  # advertised superset
    assert "gate_required" not in crash  # internal, not advertised


def test_response_is_advisory_no_adr_strings():
    resp = feature_config_requirements()
    assert "ADR" not in json.dumps(resp.data)
    assert resp.error_category is None
