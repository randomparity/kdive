"""Minimal pre-gate seccomp installation for capture children (ADR-0558)."""

from __future__ import annotations

import ctypes
import errno
import platform
from dataclasses import dataclass

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_CMP_MASKED_EQ = 7
_CLONE_VM = 0x00000100
_CLONE_SIGHAND = 0x00000800
_CLONE_THREAD = 0x00010000
_REQUIRED_THREAD_BITS = (_CLONE_VM, _CLONE_SIGHAND, _CLONE_THREAD)
_SUPPORTED_ARCHITECTURES = frozenset({"x86_64", "ppc64le"})


class _ScmpArgCmp(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


@dataclass(slots=True)
class _Seccomp:
    library: ctypes.CDLL
    context: int

    @classmethod
    def create(cls) -> _Seccomp:
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        library.seccomp_init.argtypes = [ctypes.c_uint32]
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_release.argtypes = [ctypes.c_void_p]
        library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
        library.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_ScmpArgCmp),
        ]
        library.seccomp_rule_add_array.restype = ctypes.c_int
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_load.restype = ctypes.c_int
        context = library.seccomp_init(_SCMP_ACT_ALLOW)
        if not context:
            raise RuntimeError("seccomp_init failed")
        return cls(library=library, context=context)

    def close(self) -> None:
        self.library.seccomp_release(self.context)

    def deny(self, name: str, error_number: int, comparison: _ScmpArgCmp | None = None) -> None:
        syscall_number = self.library.seccomp_syscall_resolve_name(name.encode())
        if syscall_number < 0:
            raise RuntimeError(f"seccomp cannot resolve required syscall {name}")
        action = _SCMP_ACT_ERRNO | error_number
        if comparison is None:
            comparisons = (_ScmpArgCmp * 0)()
            count = 0
        else:
            comparisons = (_ScmpArgCmp * 1)(comparison)
            count = 1
        result = self.library.seccomp_rule_add_array(
            self.context, action, syscall_number, count, comparisons
        )
        if result != 0:
            raise RuntimeError(f"seccomp rule for {name} failed: {-result}")

    def load(self) -> None:
        result = self.library.seccomp_load(self.context)
        if result != 0:
            raise RuntimeError(f"seccomp_load failed: {-result}")


def install_capture_filter() -> None:
    """Install the single-process policy, allowing threads but no descendants or exec."""
    architecture = platform.machine().lower()
    if architecture not in _SUPPORTED_ARCHITECTURES:
        raise RuntimeError(f"unsupported audit architecture: {architecture}")
    seccomp = _Seccomp.create()
    try:
        for syscall_name in ("fork", "vfork", "execve", "execveat"):
            seccomp.deny(syscall_name, errno.EPERM)
        seccomp.deny("clone3", errno.ENOSYS)
        for required_bit in _REQUIRED_THREAD_BITS:
            seccomp.deny(
                "clone",
                errno.EPERM,
                _ScmpArgCmp(
                    arg=0,
                    op=_SCMP_CMP_MASKED_EQ,
                    datum_a=required_bit,
                    datum_b=0,
                ),
            )
        seccomp.load()
    finally:
        seccomp.close()
