"""Shared authority contracts must not depend on concrete remote provider implementation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "relative_path",
    [
        "external_boot_authority/protocol.py",
        "external_boot_authority/module_protocol.py",
        "external_boot_authority/module_documents.py",
        "external_boot_authority/service.py",
        "external_boot_authority/transport.py",
        "shared/module_volume_names.py",
    ],
)
def test_common_authority_has_no_concrete_remote_import(relative_path: str) -> None:
    path = _ROOT / "src/kdive/providers" / relative_path
    tree = ast.parse(path.read_text())
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not [name for name in imports if name.startswith("kdive.providers.remote_libvirt")]
