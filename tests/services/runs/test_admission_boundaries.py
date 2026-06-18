"""Run admission service boundary tests."""

from __future__ import annotations

import ast
from pathlib import Path


def test_run_admission_service_does_not_import_mcp_modules() -> None:
    module = Path("src/kdive/services/runs/admission.py")
    tree = ast.parse(module.read_text(), filename=str(module))

    imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]

    assert not [name for name in imports if name.startswith("kdive.mcp")]
