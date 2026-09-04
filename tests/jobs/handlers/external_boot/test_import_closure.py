"""Charter criterion 10: the handler package reaches no libvirt provider module.

**This is a real transitive closure walk, and the existing gate is not one.**
``tests/services/external_boot/test_recovery_requests.py`` is a static, single-module check — its
``_reachable_names`` is an ``ast.walk`` over one module's source whose own docstring says "no walk
of the transitive import graph … is needed or wanted", and its ``_kdive_imports`` is a
direct-import allow-list compared against a frozen reviewed set. Mirroring it would catch a direct
``import kdive.providers.local_libvirt`` and miss a transitive reach through, say,
``kdive.providers.core.resolver`` — which is exactly what criterion 10 asks about.

So this imports every module in the package **in a subprocess** and inspects the resulting
``sys.modules``. A subprocess rather than the test process because ``sys.modules`` is process-wide:
an earlier test's imports would pollute an in-process assertion into a false pass. The existing
file is the precedent for pairing such a gate with a canary that proves it bites — that idea is
copied; its walk is not.
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
        name for name in loaded if name in FORBIDDEN_EXACT or name.startswith(FORBIDDEN_PREFIXES)
    ]


def test_the_package_has_modules_to_check() -> None:
    """A closure gate over an empty module list would pass vacuously forever."""
    modules = _package_modules()

    assert len(modules) > 1
    assert f"{external_boot_package.__name__}.router" in modules


def test_handler_package_import_closure_reaches_no_libvirt_module() -> None:
    loaded = _imported_modules(_package_modules())

    assert _forbidden(loaded) == []


def test_the_import_closure_gate_bites() -> None:
    """The canary: the same probe with a forbidden import added must report it.

    Without this, a gate that silently stopped importing anything — a renamed package, a probe
    whose output moved, a ``walk_packages`` that returned nothing — would keep passing.
    """
    loaded = _imported_modules(_package_modules(), extra="kdive.providers.local_libvirt")

    assert _forbidden(loaded) != []
