"""Cross-arch gdb binary selection for the debug plane (ADR-0347, #1149).

QEMU's gdbstub speaks the *guest* architecture. When the guest arch differs from the host's
(a ppc64le guest under TCG on an x86_64 host), the host-side gdb must be multiarch-capable —
``gdb-multiarch`` on distros that split it, plain ``gdb`` where it is built multiarch. These
pure helpers pick that binary and name the guest arch, keyed off the ``vmlinux`` the engine
already stages: its ELF header *is* the guest arch. They are unit-tested here; the engine's
``attach`` that consumes them is ``live_vm``-only.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from pathlib import Path

from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES

_ELF_MAGIC = b"\x7fELF"
_ELFDATA2LSB = 1
_ELF_HEADER_PREFIX_LEN = 20  # through e_machine (bytes 18-19)

# ELF ``e_machine`` -> kdive arch string, for the arches kdive supports (little-endian only;
# big-endian ppc64 is out of scope, ADR-0347). The endianness is checked separately so a
# big-endian EM_PPC64 maps to nothing.
_EM_X86_64 = 62
_EM_PPC64 = 21
_MACHINE_TO_ARCH: dict[int, str] = {_EM_X86_64: "x86_64", _EM_PPC64: "ppc64le"}

# kdive arch string -> the gdb ``set architecture`` (bfd) name.
_ARCH_TO_GDB_NAME: dict[str, str] = {
    "x86_64": "i386:x86-64",
    "ppc64le": "powerpc:common64",
}


def arch_from_elf(path: Path) -> str | None:
    """Return the kdive arch of an ELF file from its header, or ``None`` if undeterminable.

    Reads only the fixed 20-byte header prefix (magic, ``EI_DATA`` endianness, and the 2-byte
    ``e_machine`` at offset 18). A non-ELF, truncated, or unreadable file, a big-endian object,
    or a machine outside ``SUPPORTED_ARCHES`` returns ``None`` — the engine treats that as
    "assume native", a safe fallback that never blocks a same-arch attach.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(_ELF_HEADER_PREFIX_LEN)
    except OSError:
        return None
    if len(header) < _ELF_HEADER_PREFIX_LEN or header[:4] != _ELF_MAGIC:
        return None
    if header[5] != _ELFDATA2LSB:  # EI_DATA — only little-endian is in scope
        return None
    (e_machine,) = struct.unpack_from("<H", header, 18)
    arch = _MACHINE_TO_ARCH.get(e_machine)
    return arch if arch in SUPPORTED_ARCHES else None


def select_gdb_binary(
    host_arch: str, guest_arch: str | None, which: Callable[[str], str | None]
) -> str | None:
    """Resolve the gdb binary to spawn for a guest on ``host_arch``, or ``None`` if none found.

    A native attach (``guest_arch`` is ``None`` or equals ``host_arch``) uses plain ``gdb``. A
    cross-arch attach prefers ``gdb-multiarch`` (the split-package build) and falls back to plain
    ``gdb``, which *is* multiarch on build-multiarch distros. ``which`` is injected
    (``shutil.which`` in production) so selection is unit-tested without a real gdb.
    """
    if guest_arch is None or guest_arch == host_arch:
        return which("gdb")
    return which("gdb-multiarch") or which("gdb")


def gdb_target_arch_name(arch: str) -> str | None:
    """Return the gdb ``set architecture`` name for a kdive arch, or ``None`` if unmapped."""
    return _ARCH_TO_GDB_NAME.get(arch)
