"""Layering guards: what ``kdive.kernel_config`` may not import (ADR-0544)."""

from __future__ import annotations

import ast
from pathlib import Path

_KERNEL_CONFIG = Path(__file__).resolve().parents[2] / "src" / "kdive" / "kernel_config"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def _importers_of(package: str) -> dict[str, list[str]]:
    """Files under ``kernel_config`` importing ``package`` or a submodule of it, keyed by path."""
    scanned = sorted(_KERNEL_CONFIG.rglob("*.py"))
    assert scanned, f"no modules found under {_KERNEL_CONFIG}; the walk below would pass vacuously"
    offenders = {
        path.relative_to(_KERNEL_CONFIG).as_posix(): sorted(
            module
            for module in _imported_modules(path)
            if module == package or module.startswith(f"{package}.")
        )
        for path in scanned
    }
    return {path: modules for path, modules in offenders.items() if modules}


def test_kernel_config_never_imports_services() -> None:
    bad = _importers_of("kdive.services")
    assert not bad, f"kernel_config must not import kdive.services (layering inversion): {bad}"


def test_kernel_config_never_imports_domain_platform() -> None:
    # ADR-0544 §3: a clause carries `arches`, and the only list of arches kdive can provision is
    # `domain.platform.arch_traits.SUPPORTED_ARCHES`. Checking a clause's values against it is a
    # test-tree job precisely so this dependency does not become a runtime one - `kernel_config`
    # parses a `.config` and answers questions about symbols; it does not need to know which
    # arches kdive can boot, and a registry that imported the provisioning platform to validate a
    # tag would make a static data table depend on the provider layer.
    bad = _importers_of("kdive.domain.platform")
    assert not bad, (
        "kernel_config must not import kdive.domain.platform: the SUPPORTED_ARCHES check on "
        f"`Clause.arches` belongs to the test tree only (ADR-0544 §3): {bad}"
    )


def test_the_layering_guard_reports_an_import_it_should_reject() -> None:
    # Non-vacuity for both checks above: an empty result is what a walk that found no files, or a
    # prefix match that never fires, also returns. `kdive.kernel_config` is imported by every
    # module in the package, so feeding the same matcher that package proves it reports.
    assert _importers_of("kdive.kernel_config"), (
        "the import matcher found nothing even for the package's own internal imports, so the "
        "two assertions above prove nothing"
    )
