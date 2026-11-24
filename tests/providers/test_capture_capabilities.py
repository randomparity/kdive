"""Provider-runtime capture capability tests."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry


def test_local_libvirt_supports_kdump_fadump_and_host_dump_capture() -> None:
    # ADR-0208/0211/0349: local advertises the core-producing capture methods it can fetch a
    # vmcore for — KDUMP (overlay harvest), FADUMP (the pseries firmware-assisted variant sharing
    # that harvest), and HOST_DUMP (libvirt domain core dump, B4). The non-core CONSOLE/GDBSTUB
    # members are dropped.
    runtime = (
        ProviderComposition(secret_registry=SecretRegistry())
        .build_provider_resolver(
            enable_local_libvirt=True,
            enable_remote_libvirt=False,
            enable_fault_inject=False,
            enable_byo_host=False,
        )
        .resolve(ResourceKind.LOCAL_LIBVIRT)
    )
    assert runtime.support.capture_methods == frozenset(
        {CaptureMethod.KDUMP, CaptureMethod.FADUMP, CaptureMethod.HOST_DUMP}
    )
