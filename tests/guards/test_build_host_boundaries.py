"""Guard shared build-host dispatch against provider implementation imports."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "kdive"
DISALLOWED_REMOTE_PREFIX = "kdive.providers.remote_libvirt"
DISALLOWED_BUILD_HOST_PREFIX = "kdive.providers.shared.build_host"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _imports_prefix(path: Path, prefix: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for module in _imported_modules(path)
    )


def test_import_parser_ignores_comments_and_string_literals(tmp_path: Path) -> None:
    module = tmp_path / "sample.py"
    module.write_text(
        "\n".join(
            [
                "# import kdive.providers.remote_libvirt",
                'message = "from kdive.providers.remote_libvirt import console"',
                "from kdive.providers.shared.build_host import dispatch",
            ]
        ),
        encoding="utf-8",
    )

    assert not _imports_prefix(module, DISALLOWED_REMOTE_PREFIX)
    assert _imports_prefix(module, DISALLOWED_BUILD_HOST_PREFIX)


def test_build_host_dispatch_does_not_import_remote_libvirt() -> None:
    dispatch = SRC / "providers" / "shared" / "build_host" / "dispatch.py"
    assert not _imports_prefix(dispatch, DISALLOWED_REMOTE_PREFIX)


def test_provider_ports_do_not_import_build_host_implementation() -> None:
    offenders: list[str] = []
    for path in (SRC / "providers" / "ports").rglob("*.py"):
        if _imports_prefix(path, DISALLOWED_BUILD_HOST_PREFIX):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []
