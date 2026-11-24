"""Provider boundary regression tests."""

from __future__ import annotations

import ast
from pathlib import Path


def test_only_composition_imports_local_libvirt_provider_implementation() -> None:
    src_root = Path("src/kdive")
    allowed = {
        Path("src/kdive/providers/assembly/composition.py"),
        Path("src/kdive/providers/local_libvirt"),
        # ADR-0623 owns its provider-specific assembly and per-Resource route here.
        Path("src/kdive/providers/system_authority/composition.py"),
        Path("src/kdive/providers/system_authority/routing.py"),
    }
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if any(path == item or item in path.parents for item in allowed):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "kdive.providers.local_libvirt.settings":
                    continue  # ADR-0087 declarations contain no provider implementation.
                if node.module.startswith("kdive.providers.local_libvirt"):
                    offenders.append(f"{path}:{node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "kdive.providers.local_libvirt.settings":
                        continue
                    if alias.name.startswith("kdive.providers.local_libvirt"):
                        offenders.append(f"{path}:{node.lineno}: import {alias.name}")

    assert offenders == []


def test_providers_do_not_import_application_services() -> None:
    src_root = Path("src/kdive/providers")
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "kdive.services" or node.module.startswith("kdive.services."):
                    offenders.append(f"{path}:{node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "kdive.services" or alias.name.startswith("kdive.services."):
                        offenders.append(f"{path}:{node.lineno}: import {alias.name}")

    assert offenders == []


def test_generic_operation_callers_do_not_construct_provider_targets() -> None:
    modules = (
        "jobs/handlers/systems.py",
        "jobs/handlers/devices.py",
        "jobs/handlers/control/control.py",
        "jobs/handlers/control/diagnostic_sysrq.py",
        "jobs/handlers/control/capture_traffic.py",
        "jobs/handlers/connectivity/ssh_reachable.py",
        "jobs/handlers/connectivity/ssh_authorize.py",
        "jobs/handlers/runs/boot.py",
        "jobs/handlers/runs/boot_evidence.py",
        "mcp/tools/lifecycle/systems/ssh_access.py",
        "mcp/tools/debug/sessions/lifecycle.py",
    )
    offenders: list[str] = []
    for module in modules:
        path = Path("src/kdive") / module
        tree = ast.parse(path.read_text(), filename=str(path))
        constructors = {"SystemHandle"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "SystemHandle":
                        constructors.add(alias.asname or alias.name)
                    if alias.name in {"domain_name_for", "libvirt_system_handle"}:
                        offenders.append(f"{path}:{node.lineno}: naming fallback import")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in constructors
            ):
                offenders.append(f"{path}:{node.lineno}: target construction")
    assert offenders == []
