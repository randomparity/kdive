"""Provider-runtime capture capability tests."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.providers.assembly.composition import build_local_runtime
from kdive.security.secrets.secret_registry import SecretRegistry


def test_local_libvirt_supports_kdump_and_host_dump_capture() -> None:
    # ADR-0208/0211: local advertises the core-producing capture methods it can fetch a vmcore
    # for — KDUMP (overlay harvest) and HOST_DUMP (libvirt domain core dump, B4). The non-core
    # CONSOLE/GDBSTUB members are dropped.
    runtime = build_local_runtime(secret_registry=SecretRegistry())
    assert runtime.supported_capture_methods == frozenset(
        {CaptureMethod.KDUMP, CaptureMethod.HOST_DUMP}
    )
