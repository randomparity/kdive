"""Detect whether the host QEMU implements pseries firmware-assisted dump (ADR-0349).

fadump needs the platform to export the ``ibm,configure-kernel-dump`` RTAS call, which QEMU's
``pseries`` machine implements only from **QEMU 10.2** (``hw/ppc/spapr_fadump.c``). Discovery
records the answer as a fail-closed bool so admission can reject a fadump-opted provision on a
host that cannot support it (never a hang). The signal is derived from the version of the same
ppc64le emulator ``guest_arches`` already discovered, compared against the documented floor — a
stable, PAPR-anchored fact, not a binary-string scan.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping

# The QEMU (major, minor) floor at which ``pseries`` exports ``ibm,configure-kernel-dump``.
PSERIES_FADUMP_QEMU_FLOOR = (10, 2)

_PPC64LE = "ppc64le"
_VERSION_RE = re.compile(r"QEMU emulator version (\d+)\.(\d+)")
# A version probe must not stall discovery; qemu --version returns immediately in practice.
_PROBE_TIMEOUT_SEC = 5.0

type VersionRunner = Callable[[list[str]], str]
"""Given an emulator argv, return its ``--version`` stdout; raises on spawn/timeout/failure."""


def _run_version(argv: list[str]) -> str:
    """Run ``<emulator> --version`` with a bounded timeout, returning stdout (the real seam)."""
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_SEC,
        check=True,
    )
    return completed.stdout


def detect_pseries_fadump(
    guest_arches: Mapping[str, Mapping[str, str]],
    *,
    run_version: VersionRunner = _run_version,
) -> bool:
    """Return whether the host's ppc64le emulator is a QEMU that implements pseries fadump.

    Reads the ppc64le emulator path from ``guest_arches`` (ADR-0338) and compares its reported
    QEMU version against :data:`PSERIES_FADUMP_QEMU_FLOOR`. **Fail-closed**: returns ``False``
    when no ppc64le arch is advertised (fadump is N/A — no subprocess is spawned), when the
    version is below the floor, or when the probe fails for any reason (missing binary, non-zero
    exit, timeout, unparseable output). A false positive would boot a guest that hangs or never
    captures, so uncertainty must deny.

    Args:
        guest_arches: ``{arch: {"accel", "emulator"}}`` as
            :meth:`ResourceCapabilities.guest_arches` returns.
        run_version: Injected ``<emulator> --version`` runner (a bounded subprocess by default;
            faked in tests).
    """
    entry = guest_arches.get(_PPC64LE)
    if entry is None:
        return False
    emulator = entry.get("emulator")
    if not emulator:
        return False
    try:
        output = run_version([emulator, "--version"])
    except OSError, subprocess.SubprocessError:
        return False
    match = _VERSION_RE.search(output)
    if match is None:
        return False
    version = (int(match.group(1)), int(match.group(2)))
    return version >= PSERIES_FADUMP_QEMU_FLOOR
