"""Shared authority orchestration depends on neutral ports, not provider implementation."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED_MODULES = (
    "jobs/authority_sender.py",
    "jobs/external_boot_authority_client.py",
    "jobs/handlers/external_boot/runner.py",
    "jobs/handlers/external_boot/lifecycle.py",
    "services/external_boot/routing.py",
    "services/remote_module_authority_preparation.py",
    "services/remote_module_volume_preparation.py",
)
FORBIDDEN = (
    "libvirt",
    "kdive.providers.remote_libvirt",
    "kdive.providers.local_libvirt",
    "kdive.providers.external_boot_authority.local_client",
    "kdive.providers.external_boot_authority.network_client",
    "kdive.services.remote_module_operation",
    "kdive.services.remote_module_phases",
)


@pytest.mark.parametrize("module", SHARED_MODULES)
def test_shared_authority_import_boundary(module: str) -> None:
    tree = ast.parse((ROOT / "src/kdive" / module).read_text())
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN):
                violations.append(f"{module}:{node.lineno}: {name}")
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("name", ["remote_module_operation", "remote_module_phases"])
def test_unused_service_facades_are_removed(name: str) -> None:
    assert not (ROOT / "src/kdive/services" / f"{name}.py").exists()
