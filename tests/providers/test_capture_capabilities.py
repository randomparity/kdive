"""Provider-runtime capture capability tests."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.providers.assembly.composition import build_local_runtime
from kdive.security.secrets.secret_registry import SecretRegistry


def test_local_libvirt_supports_three_methods_now_not_kdump() -> None:
    # kdump joins via #115; it is in the vocabulary but not yet supported.
    runtime = build_local_runtime(secret_registry=SecretRegistry())
    assert runtime.supported_capture_methods == frozenset(
        {
            CaptureMethod.CONSOLE,
            CaptureMethod.HOST_DUMP,
            CaptureMethod.GDBSTUB,
        }
    )
