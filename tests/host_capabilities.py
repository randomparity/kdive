# tests/host_capabilities.py
"""Skip gates for tests that shell out to GNU/Linux-targeted project scripts.

Some scripts under ``scripts/`` and ``deploy/`` use GNU bash >= 4 builtins (``mapfile``,
``local -n`` namerefs, ``wait -n``) or GNU ``find -printf``. They run in CI (Linux) and in
production hosts that have the GNU toolchain, but a developer host without it — notably
macOS, which still ships bash 3.2 and BSD coreutils — cannot exercise them. These markers
skip such tests cleanly when the prerequisite is absent, mirroring the suite's existing
Docker/promtool/OIDC skips, instead of reporting a host limitation as a failure.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile

import pytest

BASH = shutil.which("bash")

# The capture-vmcore helper test pins PATH to the system directories, so its ``find`` is
# resolved from here too — probe GNU ``-printf`` support against the same search path.
_SYSTEM_PATH = "/usr/bin:/bin"


def _bash_versinfo() -> tuple[int, int] | None:
    """Return the ``(major, minor)`` of the ``bash`` tests invoke, or None if unknown."""
    if BASH is None:
        return None
    proc = subprocess.run(
        [BASH, "-c", 'printf "%s %s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


_BASH_VERSION = _bash_versinfo()


def requires_bash(major: int, minor: int, feature: str) -> pytest.MarkDecorator:
    """Skip when the resolved ``bash`` predates the version that introduced ``feature``."""
    have = ".".join(str(n) for n in _BASH_VERSION) if _BASH_VERSION is not None else "no bash"
    insufficient = _BASH_VERSION is None or (major, minor) > _BASH_VERSION
    return pytest.mark.skipif(
        insufficient,
        reason=f"script needs bash >= {major}.{minor} for {feature} (host has {have})",
    )


def _find_supports_printf(path: str) -> bool:
    """Whether the ``find`` resolved from ``path`` supports the GNU ``-printf`` primary."""
    find = shutil.which("find", path=path)
    if find is None:
        return False
    proc = subprocess.run(
        [find, tempfile.gettempdir(), "-maxdepth", "0", "-printf", ""],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


_GNU_FIND_AVAILABLE = _find_supports_printf(_SYSTEM_PATH)


def requires_gnu_find(feature: str) -> pytest.MarkDecorator:
    """Skip when system ``find`` lacks GNU ``-printf`` (e.g. BSD/macOS find)."""
    return pytest.mark.skipif(
        not _GNU_FIND_AVAILABLE,
        reason=f"script needs GNU find -printf for {feature} (system find lacks it)",
    )
