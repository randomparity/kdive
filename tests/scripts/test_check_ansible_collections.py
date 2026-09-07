"""Tests for the installed Ansible collection manifest guard (#2225)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.guards.check_ansible_collections import evaluate

_REQUIREMENTS = """\
---
collections:
  - name: community.crypto
    version: "==3.2.2"
  - name: ansible.posix
    version: "==2.2.0"
"""


def _write_manifest(root: Path, name: str, version: str) -> None:
    namespace, collection = name.split(".")
    directory = root / "ansible_collections" / namespace / collection
    directory.mkdir(parents=True)
    (directory / "MANIFEST.json").write_text(
        json.dumps(
            {
                "collection_info": {
                    "namespace": namespace,
                    "name": collection,
                    "version": version,
                }
            }
        ),
        encoding="utf-8",
    )


def test_exact_installed_collection_set_passes(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "community.crypto", "3.2.2")
    _write_manifest(tmp_path, "ansible.posix", "2.2.0")

    assert evaluate(_REQUIREMENTS, tmp_path) == []


def test_missing_extra_and_wrong_versions_are_named(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "community.crypto", "3.2.1")
    _write_manifest(tmp_path, "community.general", "13.1.0")

    violations = evaluate(_REQUIREMENTS, tmp_path)

    assert any("community.crypto" in item and "3.2.1" in item for item in violations)
    assert any("ansible.posix" in item and "missing" in item for item in violations)
    assert any("community.general" in item and "unexpected" in item for item in violations)


def test_collection_directory_without_manifest_fails_closed(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "community.crypto", "3.2.2")
    missing_manifest = tmp_path / "ansible_collections" / "ansible" / "posix"
    missing_manifest.mkdir(parents=True)

    violations = evaluate(_REQUIREMENTS, tmp_path)

    assert any("ansible.posix" in item and "MANIFEST.json" in item for item in violations)
