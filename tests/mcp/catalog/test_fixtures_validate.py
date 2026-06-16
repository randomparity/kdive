"""``fixtures.validate`` — read-only validation of the resolved fixture catalog (ADR-0120).

Drives the handler directly with an injected catalog path (no DB, no transport). Covers:
* a valid catalog (the packaged default written by ``install_fixtures``) → ``valid`` + its
  ``(provider, name, arch)`` profile triples;
* an absent path → ``configuration_error`` naming the resolved path;
* a malformed manifest → ``configuration_error`` (no raw file content in the reason);
* an empty profile list (valid manifest, ``profiles: []``) → ``valid`` with ``profiles == []``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kdive.admin.bootstrap import install_fixtures
from kdive.mcp.tools.catalog import fixtures
from tests.mcp.json_data import data_sequence, data_str, json_mapping

_MIN_MANIFEST = """schema_version: 1
provider: local-libvirt
storage:
  allowed_component_roots:
    - /var/lib/kdive/rootfs
  cache_dir: /var/lib/kdive/rootfs/cache
  overlay_dir: /var/lib/kdive/rootfs/overlays
rootfs: []
profiles: []
"""


def test_valid_catalog_reports_profiles(tmp_path: Path) -> None:
    # install_fixtures refuses a pre-existing dest (force=False), and tmp_path already
    # exists; write into a fresh subdir it creates.
    dest = tmp_path / "catalog"
    install_fixtures(dest)
    resp = asyncio.run(fixtures.validate_fixtures_tool(dest))
    assert resp.status == "valid", resp
    assert resp.error_category is None
    rows = [json_mapping(r) for r in data_sequence(resp, "profiles")]
    triples = {(r["provider"], r["name"], r["arch"]) for r in rows}
    assert ("local-libvirt", "console-ready_x86_64", "x86_64") in triples
    assert data_str(resp, "path") == str(dest)


def test_absent_path_is_configuration_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    resp = asyncio.run(fixtures.validate_fixtures_tool(missing))
    assert resp.status != "valid"
    assert resp.error_category == "configuration_error"
    assert data_str(resp, "path") == str(missing)


def test_malformed_manifest_is_configuration_error_without_content(tmp_path: Path) -> None:
    (tmp_path / "manifest.yaml").write_text("schema_version: 2\nSEKRIT_TOKEN: leakme\n")
    resp = asyncio.run(fixtures.validate_fixtures_tool(tmp_path))
    assert resp.error_category == "configuration_error"
    assert "leakme" not in data_str(resp, "reason"), "bounded reason must not echo file content"


def test_empty_profile_list_is_valid(tmp_path: Path) -> None:
    (tmp_path / "manifest.yaml").write_text(_MIN_MANIFEST)
    resp = asyncio.run(fixtures.validate_fixtures_tool(tmp_path))
    assert resp.status == "valid", resp
    assert data_sequence(resp, "profiles") == []
