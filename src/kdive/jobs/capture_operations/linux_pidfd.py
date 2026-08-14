"""Portable libc-backed Linux pidfd operations (ADR-0558)."""

from __future__ import annotations

import ctypes
import os
import signal
from typing import Any

_LIBC = ctypes.CDLL(None, use_errno=True)


def _libc_symbol(name: str, argument_types: list[Any]) -> Any:
    symbol = getattr(_LIBC, name, None)
    if symbol is None:
        raise RuntimeError(f"Linux pidfd support unavailable: Python and libc omit {name}")
    symbol.argtypes = argument_types
    symbol.restype = ctypes.c_int
    return symbol


def _raise_errno(operation: str) -> None:
    error_number = ctypes.get_errno()
    raise OSError(error_number, f"{operation} failed: {os.strerror(error_number)}")


def require_pidfd_support() -> None:
    """Fail readiness unless open and exact signaling operations are callable."""
    if not callable(getattr(os, "pidfd_open", None)):
        _libc_symbol("pidfd_open", [ctypes.c_int, ctypes.c_uint])
    if not callable(getattr(signal, "pidfd_send_signal", None)):
        _libc_symbol(
            "pidfd_send_signal",
            [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint],
        )


def open_pidfd(pid: int, flags: int = 0) -> int:
    """Open a close-on-exec descriptor for one process, preserving kernel errno."""
    wrapper = getattr(os, "pidfd_open", None)
    if callable(wrapper):
        return wrapper(pid, flags)
    function = _libc_symbol("pidfd_open", [ctypes.c_int, ctypes.c_uint])
    ctypes.set_errno(0)
    descriptor = int(function(pid, flags))
    if descriptor < 0:
        _raise_errno("pidfd_open")
    return descriptor


def send_signal(pidfd: int, sig: int, info: None = None, flags: int = 0) -> None:
    """Signal exactly one pidfd, preserving stdlib argument and errno semantics."""
    wrapper = getattr(signal, "pidfd_send_signal", None)
    if callable(wrapper):
        wrapper(pidfd, int(sig), info, flags)
        return
    function = _libc_symbol(
        "pidfd_send_signal",
        [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint],
    )
    ctypes.set_errno(0)
    if int(function(pidfd, int(sig), info, flags)) < 0:
        _raise_errno("pidfd_send_signal")
