"""Import boundaries for neutral authority and durable external-boot contracts."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("relative_path", "forbidden"),
    [
        ("providers/external_boot_authority/protocol.py", "kdive.providers.remote_libvirt"),
        ("providers/external_boot_authority/module_protocol.py", "kdive.providers.remote_libvirt"),
        ("providers/external_boot_authority/module_documents.py", "kdive.providers.remote_libvirt"),
        ("providers/external_boot_authority/service.py", "kdive.providers.remote_libvirt"),
        ("providers/external_boot_authority/transport.py", "kdive.providers.remote_libvirt"),
        ("providers/shared/module_volume_names.py", "kdive.providers.remote_libvirt"),
        ("domain/external_boot_activation.py", "kdive.providers"),
        ("domain/external_boot_values.py", "kdive.providers"),
    ],
)
def test_declared_contract_import_boundary(relative_path: str, forbidden: str) -> None:
    path = _ROOT / "src/kdive" / relative_path
    package = "kdive." + Path(relative_path).parent.as_posix().replace("/", ".")
    tree = ast.parse(path.read_text())
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            imports.extend(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not [name for name in imports if name.startswith(forbidden)]


def test_external_boot_port_module_owns_only_operation_protocols() -> None:
    path = _ROOT / "src/kdive/providers/ports/external_boot.py"
    tree = ast.parse(path.read_text())
    declarations = [
        node
        for node in tree.body
        if isinstance(
            node,
            (
                ast.ClassDef,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.TypeAlias,
                ast.Assign,
                ast.AnnAssign,
            ),
        )
    ]
    assert all(isinstance(node, ast.ClassDef) for node in declarations)
    assert {node.name for node in declarations if isinstance(node, ast.ClassDef)} == {
        "ExternalBootArtifactStager",
        "ExternalBootPreparationPorts",
        "ExternalBootRecoveryObjectPorts",
        "ExternalBootPorts",
    }
    assert all(
        isinstance(node, ast.ClassDef)
        and [ast.unparse(base) for base in node.bases] == ["Protocol"]
        for node in declarations
    )
