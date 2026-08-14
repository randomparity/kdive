"""Portable Linux pidfd boundary tests (ADR-0558)."""

from __future__ import annotations

import errno
import fcntl
import os
import signal

import pytest

from kdive.jobs.capture_operations import linux_pidfd


class _Function:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: list[object] = []
        self.restype: object = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


class _Libc:
    def __init__(self, *, open_result: int = 41, signal_result: int = 0) -> None:
        self.pidfd_open = _Function(open_result)
        self.pidfd_send_signal = _Function(signal_result)


def _without_python_wrappers(monkeypatch: pytest.MonkeyPatch, libc: _Libc) -> None:
    monkeypatch.delattr(linux_pidfd.os, "pidfd_open", raising=False)
    monkeypatch.delattr(linux_pidfd.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(linux_pidfd, "_LIBC", libc)


def test_libc_fallback_preserves_pidfd_arguments_and_signal_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = _Libc()
    _without_python_wrappers(monkeypatch, libc)

    assert linux_pidfd.open_pidfd(123, 0) == 41
    linux_pidfd.send_signal(41, signal.SIGTERM, None, 0)

    assert libc.pidfd_open.calls == [(123, 0)]
    assert libc.pidfd_send_signal.calls == [(41, signal.SIGTERM, None, 0)]
    assert libc.pidfd_open.argtypes == [linux_pidfd.ctypes.c_int, linux_pidfd.ctypes.c_uint]
    assert libc.pidfd_send_signal.argtypes == [
        linux_pidfd.ctypes.c_int,
        linux_pidfd.ctypes.c_int,
        linux_pidfd.ctypes.c_void_p,
        linux_pidfd.ctypes.c_uint,
    ]


def test_stdlib_wrappers_are_preferred_when_compiled_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        linux_pidfd.os,
        "pidfd_open",
        lambda pid, flags=0: calls.append(("open", pid, flags)) or 42,
        raising=False,
    )
    monkeypatch.setattr(
        linux_pidfd.signal,
        "pidfd_send_signal",
        lambda pidfd, sig, info=None, flags=0: calls.append(("signal", pidfd, sig, info, flags)),
        raising=False,
    )
    monkeypatch.setattr(linux_pidfd, "_LIBC", object())

    assert linux_pidfd.open_pidfd(123) == 42
    linux_pidfd.send_signal(42, signal.SIGTERM)

    assert calls == [("open", 123, 0), ("signal", 42, signal.SIGTERM, None, 0)]


@pytest.mark.parametrize("operation", ["open", "signal"])
def test_libc_fallback_preserves_errno(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    libc = _Libc(
        open_result=-1 if operation == "open" else 41,
        signal_result=-1 if operation == "signal" else 0,
    )
    _without_python_wrappers(monkeypatch, libc)
    monkeypatch.setattr(linux_pidfd.ctypes, "get_errno", lambda: errno.ESRCH)

    with pytest.raises(OSError) as raised:
        if operation == "open":
            linux_pidfd.open_pidfd(123)
        else:
            linux_pidfd.send_signal(41, signal.SIGKILL)

    assert raised.value.errno == errno.ESRCH


def test_missing_python_and_libc_pidfd_support_fails_actionably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_python_wrappers(monkeypatch, _Libc())
    monkeypatch.setattr(linux_pidfd, "_LIBC", object())

    with pytest.raises(RuntimeError, match="Linux pidfd support unavailable"):
        linux_pidfd.require_pidfd_support()


def test_real_libc_fallback_opens_cloexec_and_signals_exact_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(linux_pidfd.os, "pidfd_open", raising=False)
    monkeypatch.delattr(linux_pidfd.signal, "pidfd_send_signal", raising=False)

    pidfd = linux_pidfd.open_pidfd(os.getpid())
    try:
        assert fcntl.fcntl(pidfd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        linux_pidfd.send_signal(pidfd, 0)
    finally:
        os.close(pidfd)
