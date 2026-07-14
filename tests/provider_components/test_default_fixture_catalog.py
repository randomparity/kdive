"""The source-tree fixture catalog holds only profiles; rootfs moved to the DB (ADR-0112).

Image definitions left code entirely (the packaged ``seed_data`` YAML is gone): they now load
from ``systems.toml`` into ``image_catalog``. The file-based fixture catalog keeps only the
profiles half, which ``load_fixture_catalog`` still resolves.
"""

from pathlib import Path

import pytest

from kdive.components.catalog import DEFAULT_FIXTURE_CATALOG_PATH, load_fixture_catalog


def test_default_fixture_catalog_has_no_rootfs_entries() -> None:
    # The default fixture catalog keeps only profiles; rootfs definitions left code (ADR-0112).
    catalog = load_fixture_catalog(DEFAULT_FIXTURE_CATALOG_PATH)
    assert catalog.rootfs == []
    assert catalog.profiles != []


def test_default_fixture_catalog_resolves_both_arch_profiles() -> None:
    # Both console-ready profiles resolve; ppc64le carries arch=ppc64le so a System pointed at it
    # routes through the pseries arch traits (#1144, epic #1139).
    catalog = load_fixture_catalog(DEFAULT_FIXTURE_CATALOG_PATH)

    x86 = catalog.profile("local-libvirt", "console-ready_x86_64")
    ppc = catalog.profile("local-libvirt", "console-ready_ppc64le")
    assert x86 is not None and x86.arch == "x86_64"
    assert ppc is not None and ppc.arch == "ppc64le"


def test_catalog_path_can_be_overridden_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "catalog"
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        "  allowed_component_roots: [/tmp/rootfs]\n"
        "  cache_dir: /tmp/rootfs/cache\n"
        "  overlay_dir: /tmp/rootfs/overlays\n"
        "rootfs: [rootfs/custom.yaml]\n"
        "profiles: []\n",
        encoding="utf-8",
    )
    (fixture / "rootfs" / "custom.yaml").write_text(
        "provider: local-libvirt\n"
        "name: custom-rootfs\n"
        "arch: x86_64\n"
        "format: qcow2\n"
        "root_device: /dev/vda\n"
        "source:\n"
        "  kind: local\n"
        "  path: /tmp/rootfs/custom.qcow2\n"
        "visibility: public\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KDIVE_FIXTURE_CATALOG_PATH", str(fixture))

    catalog = load_fixture_catalog()

    assert catalog.rootfs_entry("local-libvirt", "custom-rootfs") is not None
