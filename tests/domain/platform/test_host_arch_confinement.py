"""Static guard: host-arch reads stay confined to accelerator/gdb-selection sites (ADR-0354).

The epic's symmetry invariant (#1155) is that *no code path derives guest-facing behavior from
the host architecture except accelerator/host-tooling selection*. Guest-facing facts flow from
``profile.arch`` + the libvirt-advertised accelerator, never from the host's own arch. This
module makes the read-site half of that invariant executable: it AST-walks ``src/kdive`` for the
``platform``/``os`` host-arch idioms and asserts every module that reads one is on a small
allowlist of legitimate accel/gdb binary-selection sites. A future module that newly reads the
host arch fails here at the source.

Detection is a pure function unit-tested with synthetic fixtures (a positive fixture proving the
walker matches a real read — non-vacuity — and a negative fixture proving a docstring/comment
mention is ignored, the reason AST is used over a text grep), so the guard never has to pin
*which* modules read the host today and stays tolerant of a read being removed (subset, not
equality). The whole-tree scan additionally asserts it enumerated a plausible file count, so an
empty/misrooted glob fails loudly rather than passing a vacuous subset.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kdive

# The ``platform``/``os`` attribute idioms that read the host architecture. ``platform.uname``
# covers the ``platform.uname().machine`` sibling (the inner ``platform.uname`` attribute node is
# what the walk matches); ``os.uname`` covers ``os.uname().machine``. None of ``uname``/
# ``processor``/``architecture`` occur in the tree today, so including them tightens the guard
# against the closest in-family alternatives without creating a new violation.
_PLATFORM_ARCH_ATTRS = frozenset({"machine", "uname", "processor", "architecture"})

# The only modules permitted to read the host arch — all accelerator/gdb binary-selection sites,
# the "except accelerator selection" carve-out of the invariant (ADR-0354). Keyed by the
# repo-relative ``kdive/...`` path the scan reports.
_HOST_ARCH_READ_ALLOWLIST = frozenset(
    {
        # per-arch guest-accelerator doctor probe (ADR-0352): reports KVM-native vs TCG-only.
        "kdive/diagnostics/guest_arch_accel.py",
        # cross-arch gdb doctor probe (ADR-0347): is a multiarch gdb present for foreign guests?
        "kdive/diagnostics/multiarch_gdb.py",
        # the gdb-engine's cross-arch binary selection (ADR-0347): guest arch comes from the
        # staged vmlinux ELF; the host arch only picks gdb vs gdb-multiarch.
        "kdive/providers/shared/debug_common/gdbmi/core/engine.py",
    }
)


def module_reads_host_arch(source: str) -> bool:
    """Return whether ``source`` reads the host architecture via a ``platform``/``os`` idiom.

    Parses ``source`` and returns ``True`` iff it contains an ``ast.Attribute`` reading one of
    ``platform.machine``/``uname``/``processor``/``architecture`` or ``os.uname``. Matching the
    attribute node (not a text scan) means ``platform.machine()``, the bare ``platform.machine``
    default-argument reference, and ``platform.uname().machine`` all count, while the same text
    inside a docstring or comment does not (those never become ``Attribute`` nodes).

    Does not resolve aliased imports (``from platform import machine``, ``import platform as p``);
    none exist in the tree and catching them would need an import-alias pass (documented in
    ADR-0354).

    Args:
        source: Python source text.

    Returns:
        ``True`` if the source reads the host arch through a covered idiom.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        module = node.value.id
        if module == "platform" and node.attr in _PLATFORM_ARCH_ATTRS:
            return True
        if module == "os" and node.attr == "uname":
            return True
    return False


def host_arch_reading_modules(package_root: Path) -> tuple[set[str], int]:
    """Scan every ``*.py`` under ``package_root`` for a host-arch read.

    Args:
        package_root: The package directory to walk (``.../src/kdive``).

    Returns:
        A ``(modules, files_scanned)`` pair: ``modules`` is the set of repo-relative
        ``kdive/...`` paths that read the host arch, and ``files_scanned`` is the total number of
        ``*.py`` files visited (used to prove the scan was not vacuous).
    """
    hits: set[str] = set()
    files_scanned = 0
    for path in sorted(package_root.rglob("*.py")):
        files_scanned += 1
        if module_reads_host_arch(path.read_text(encoding="utf-8")):
            hits.add(path.relative_to(package_root.parent).as_posix())
    return hits, files_scanned


def _package_root() -> Path:
    """Resolve ``src/kdive`` from the installed package; assert it exists so a bad base fails."""
    root = Path(kdive.__file__).parent
    assert root.is_dir(), f"kdive package root {root} is not a directory"
    return root


def test_detects_platform_machine_call_and_bare_reference() -> None:
    # Both real forms resolve to the same `platform.machine` Attribute node, so one predicate
    # branch catches both: the `platform.machine()` call (the diagnostics probes) and the bare
    # `= platform.machine` default-argument reference (the gdb engine, engine.py:128).
    assert module_reads_host_arch("import platform\nx = platform.machine()\n")
    assert module_reads_host_arch("import platform\ndef f(g=platform.machine):\n    return g\n")


def test_detects_platform_uname_dot_machine() -> None:
    # platform.uname().machine — matched via the inner platform.uname attribute node.
    assert module_reads_host_arch("import platform\nx = platform.uname().machine\n")


def test_detects_os_uname() -> None:
    assert module_reads_host_arch("import os\nx = os.uname().machine\n")


def test_detects_platform_processor_and_architecture() -> None:
    assert module_reads_host_arch("import platform\nx = platform.processor()\n")
    assert module_reads_host_arch("import platform\nx = platform.architecture()\n")


def test_ignores_docstring_and_comment_mention() -> None:
    # The reason AST is used over a text grep: a prose mention of platform.machine() in a
    # docstring or comment (mirroring provider_checks.py:409) is not a read.
    source = '''"""This probe uses platform.machine() to find the host arch."""
# platform.machine() would also read it, but this comment is not a read.
VALUE = 1
'''
    assert not module_reads_host_arch(source)


def test_ignores_unrelated_attributes() -> None:
    # A same-named attribute on a different object is not a host-arch read.
    assert not module_reads_host_arch("import shutil\nx = shutil.which('gdb')\n")
    assert not module_reads_host_arch("obj = object()\ny = obj.machine\n")


def test_host_arch_reads_confined_to_allowlist() -> None:
    modules, _ = host_arch_reading_modules(_package_root())
    unexpected = modules - _HOST_ARCH_READ_ALLOWLIST
    assert not unexpected, (
        "host-arch reads (platform.machine/uname/processor/architecture, os.uname) leaked into "
        f"modules outside the accel/gdb-selection allowlist: {sorted(unexpected)}. Guest-facing "
        "behavior must derive from profile.arch + the advertised accelerator, never the host "
        "arch (ADR-0354). If this is a legitimate accelerator/tooling-selection site, add it to "
        "_HOST_ARCH_READ_ALLOWLIST with a rationale."
    )


def test_scan_is_non_vacuous() -> None:
    # A subset assertion holds trivially over an empty scan; assert the walk saw a plausible file
    # count (the real tree is ~636) so an empty/misrooted glob fails loudly instead of green.
    _, files_scanned = host_arch_reading_modules(_package_root())
    assert files_scanned >= 100, f"scan enumerated only {files_scanned} files; glob misrooted?"
