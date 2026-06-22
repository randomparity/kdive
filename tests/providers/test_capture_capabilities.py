"""Provider-runtime capture capability tests."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.providers.assembly.composition import build_local_runtime
from kdive.security.secrets.secret_registry import SecretRegistry


def test_local_libvirt_supports_only_kdump_capture() -> None:
    # ADR-0208 narrows local's advertised capture methods to the core-producing methods it can
    # actually fetch a vmcore for: {KDUMP} now (+HOST_DUMP after B4). The non-core CONSOLE/GDBSTUB
    # members are dropped, and HOST_DUMP's seam is still a stub until B4.
    runtime = build_local_runtime(secret_registry=SecretRegistry())
    assert runtime.supported_capture_methods == frozenset({CaptureMethod.KDUMP})
