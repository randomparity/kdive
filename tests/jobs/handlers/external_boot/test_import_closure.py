"""The served authority client loads configuration, never concrete libvirt execution.

The subprocess checks the entire transitive closure without prior-test import pollution.
ADR-0087's registry loads declarative provider settings; the fixed authority sender also needs
remote inventory configuration and URI validation. Only those exact modules and their namespace
packages are allowed. Concrete provider operations and the libvirt C extension remain forbidden.
"""

from __future__ import annotations

import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import kdive.jobs.handlers.external_boot as external_boot_package

FORBIDDEN_PREFIXES = ("kdive.providers.local_libvirt", "kdive.providers.remote_libvirt")
FORBIDDEN_EXACT = ("libvirt",)
CONFIGURATION_MODULES = frozenset(
    {
        "kdive.providers.local_libvirt",
        "kdive.providers.local_libvirt.settings",
        "kdive.providers.remote_libvirt",
        "kdive.providers.remote_libvirt.settings",
        "kdive.providers.remote_libvirt.config",
        "kdive.providers.remote_libvirt.connection",
        "kdive.providers.remote_libvirt.connection.uri_validation",
    }
)

# Every import happens before the snapshot is taken. An earlier draft appended the canary's extra
# import *after* the print, so the canary could not bite — which is the exact failure mode a canary
# exists to catch, and it caught it.
_PROBE = """
import importlib, json, sys
for name in json.loads(sys.argv[1]):
    importlib.import_module(name)
print(json.dumps(sorted(sys.modules)))
"""


def _package_modules() -> list[str]:
    """Every module in the handler package, including the package itself."""
    modules = [external_boot_package.__name__]
    modules.extend(
        info.name
        for info in pkgutil.walk_packages(
            external_boot_package.__path__, prefix=f"{external_boot_package.__name__}."
        )
    )
    return modules


def _imported_modules(names: list[str], *, extra: str | None = None) -> list[str]:
    requested = names if extra is None else [*names, extra]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-local probe source
        [sys.executable, "-c", _PROBE, json.dumps(requested)],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(external_boot_package.__file__).parents[5],
    )
    return json.loads(completed.stdout.splitlines()[-1])


def _forbidden(loaded: list[str]) -> list[str]:
    return [
        name
        for name in loaded
        if name not in CONFIGURATION_MODULES
        and (name in FORBIDDEN_EXACT or name.startswith(FORBIDDEN_PREFIXES))
    ]


def test_the_package_has_modules_to_check() -> None:
    """A closure gate over an empty module list would pass vacuously forever."""
    modules = _package_modules()

    assert len(modules) > 1
    assert f"{external_boot_package.__name__}.router" in modules


def test_handler_package_import_closure_reaches_no_libvirt_execution() -> None:
    loaded = _imported_modules(_package_modules())

    assert _forbidden(loaded) == []


def test_the_import_closure_gate_bites() -> None:
    """The canary: the same probe with a forbidden import added must report it.

    Without this, a gate that silently stopped importing anything — a renamed package, a probe
    whose output moved, a ``walk_packages`` that returned nothing — would keep passing.
    """
    assert _forbidden(_imported_modules(_package_modules())) == []
    loaded = _imported_modules(
        _package_modules(), extra="kdive.providers.local_libvirt.lifecycle.boot.external_boot"
    )

    assert _forbidden(loaded) != []
