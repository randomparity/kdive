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

from kdive.admin.fixtures import install_fixtures
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

_PROFILE_TEMPLATE = """provider: {provider}
name: {name}
arch: {arch}
"""


def _write_catalog(root: Path, triples: list[tuple[str, str, str]]) -> None:
    """Write a manifest plus one profile yaml per ``(provider, name, arch)`` triple."""
    profiles_dir = root / "profiles"
    profiles_dir.mkdir(parents=True)
    rel_paths = []
    for index, (provider, name, arch) in enumerate(triples):
        rel = f"profiles/p{index}.yaml"
        (root / rel).write_text(_PROFILE_TEMPLATE.format(provider=provider, name=name, arch=arch))
        rel_paths.append(rel)
    listed = "\n".join(f"  - {rel}" for rel in rel_paths)
    (root / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        "  allowed_component_roots:\n"
        "    - /var/lib/kdive/rootfs\n"
        "  cache_dir: /var/lib/kdive/rootfs/cache\n"
        "  overlay_dir: /var/lib/kdive/rootfs/overlays\n"
        "rootfs: []\n"
        f"profiles:\n{listed}\n"
    )


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
    # On success the verb points the operator at the listing of the same catalog.
    assert resp.suggested_next_actions == ["fixtures.list"]


def test_profiles_are_sorted_by_provider_name_arch(tmp_path: Path) -> None:
    # Manifest lists profiles out of sorted order; the response must sort them by the
    # (provider, name, arch) triple, not echo manifest order.
    dest = tmp_path / "catalog"
    _write_catalog(
        dest,
        [
            ("local-libvirt", "zeta", "x86_64"),
            ("local-libvirt", "alpha", "x86_64"),
            ("local-libvirt", "alpha", "aarch64"),
        ],
    )
    resp = asyncio.run(fixtures.validate_fixtures_tool(dest))
    assert resp.status == "valid", resp
    rows = [json_mapping(r) for r in data_sequence(resp, "profiles")]
    triples = [(r["provider"], r["name"], r["arch"]) for r in rows]
    assert triples == [
        ("local-libvirt", "alpha", "aarch64"),
        ("local-libvirt", "alpha", "x86_64"),
        ("local-libvirt", "zeta", "x86_64"),
    ]


def test_absent_path_is_configuration_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    resp = asyncio.run(fixtures.validate_fixtures_tool(missing))
    assert resp.status != "valid"
    assert resp.error_category == "configuration_error"
    assert data_str(resp, "path") == str(missing)
    # The bounded reason is the chained cause's type name (the loader chains the OSError),
    # not the wrapper CategorizedError nor a "NoneType" placeholder.
    assert data_str(resp, "reason") == "FileNotFoundError"
    # A failure steers the operator back to re-run this validation after fixing the path.
    assert resp.suggested_next_actions == ["fixtures.validate"]


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
