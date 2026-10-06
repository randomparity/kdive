"""Tests for the installed Ansible collection manifest guard (#2225)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.guards.check_ansible_collections import evaluate, main, missing

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


def test_missing_reports_only_absent_required_collections(tmp_path: Path) -> None:
    """A developer's collections directory may hold extra or off-version collections (#2782):

    the preflight only needs to know a required collection can be resolved at all, not that the
    tree is the exact single-purpose set CI verifies.
    """
    _write_manifest(tmp_path, "community.crypto", "3.2.1")  # wrong version -- not a violation
    _write_manifest(tmp_path, "community.general", "13.1.0")  # unrelated extra -- not a violation

    violations = missing(_REQUIREMENTS, tmp_path)

    assert violations == ["ansible.posix: required collection is missing"]


def test_missing_reports_nothing_when_all_required_collections_are_present(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path, "community.crypto", "3.2.1")
    _write_manifest(tmp_path, "ansible.posix", "2.2.0")

    assert missing(_REQUIREMENTS, tmp_path) == []


def _requirements_file(tmp_path: Path) -> Path:
    path = tmp_path / "requirements.yml"
    path.write_text(_REQUIREMENTS, encoding="utf-8")
    return path


def test_main_presence_only_exits_nonzero_and_names_the_recipe_on_a_missing_collection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    requirements = _requirements_file(tmp_path)
    collections_root = tmp_path / "collections"
    _write_manifest(collections_root, "community.crypto", "3.2.2")

    exit_code = main(["prog", "--presence-only", str(requirements), str(collections_root)])

    assert exit_code == 1
    assert "ansible.posix" in capsys.readouterr().err


def test_main_presence_only_exits_zero_despite_an_unrelated_extra_collection(
    tmp_path: Path,
) -> None:
    requirements = _requirements_file(tmp_path)
    collections_root = tmp_path / "collections"
    _write_manifest(collections_root, "community.crypto", "3.2.2")
    _write_manifest(collections_root, "ansible.posix", "2.2.0")
    _write_manifest(collections_root, "community.general", "13.1.0")

    assert main(["prog", "--presence-only", str(requirements), str(collections_root)]) == 0
