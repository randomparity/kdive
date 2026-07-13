"""Default fixture bundle tests."""

from __future__ import annotations

import yaml

from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES
from kdive.components.catalog import DEFAULT_FIXTURE_CATALOG_PATH


def test_local_libvirt_fixtures_declare_manifest_and_profiles() -> None:
    manifest = yaml.safe_load(LOCAL_LIBVIRT_FIXTURES["manifest.yaml"])

    assert manifest["schema_version"] == 1
    assert manifest["provider"] == "local-libvirt"
    assert manifest["rootfs"] == []
    assert manifest["profiles"] == [
        "profiles/console-ready_x86_64.yaml",
        "profiles/console-ready_ppc64le.yaml",
    ]
    assert manifest["storage"]["allowed_component_roots"] == ["/var/lib/kdive/rootfs"]


def test_console_ready_x86_64_profile_has_no_requires_block() -> None:
    profile = yaml.safe_load(LOCAL_LIBVIRT_FIXTURES["profiles/console-ready_x86_64.yaml"])

    assert profile == {
        "provider": "local-libvirt",
        "name": "console-ready_x86_64",
        "arch": "x86_64",
    }


def test_console_ready_ppc64le_profile_has_no_requires_block() -> None:
    # The ppc64le sibling mirrors the x86_64 profile shape: just the (provider, name, arch)
    # triple, no kernel-config requirements (ADR-0316/0319). arch=ppc64le is what routes the
    # provisioner through the pseries arch traits (#1144, epic #1139).
    profile = yaml.safe_load(LOCAL_LIBVIRT_FIXTURES["profiles/console-ready_ppc64le.yaml"])

    assert profile == {
        "provider": "local-libvirt",
        "name": "console-ready_ppc64le",
        "arch": "ppc64le",
    }


def test_embedded_fixture_bundle_matches_the_on_disk_files() -> None:
    # The install-fixtures bundle (embedded LOCAL_LIBVIRT_FIXTURES) and the on-disk
    # fixtures/local-libvirt/ files are two copies of the same data; a drift between them would
    # ship an install-fixtures output that disagrees with load_fixture_catalog's source. Assert
    # they parse equal so neither copy can be edited without the other (#1144).
    for relative, embedded in LOCAL_LIBVIRT_FIXTURES.items():
        on_disk = (DEFAULT_FIXTURE_CATALOG_PATH / relative).read_text(encoding="utf-8")
        assert yaml.safe_load(embedded) == yaml.safe_load(on_disk), relative
