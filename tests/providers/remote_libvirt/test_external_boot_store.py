"""The durable remote authority store is independent of coordinator orchestration."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_ORCHESTRATION = "kdive.providers.remote_libvirt.external_boot_authority"


def test_store_does_not_import_orchestration() -> None:
    path = _ROOT / "src/kdive/providers/remote_libvirt/external_boot_store.py"
    tree = ast.parse(path.read_text())
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert _ORCHESTRATION not in imports


def test_fresh_store_import_does_not_load_orchestration() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys; "
            "importlib.import_module('kdive.providers.remote_libvirt.external_boot_store'); "
            f"assert {_ORCHESTRATION!r} not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
