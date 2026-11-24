"""ProviderRuntime capability-descriptor default tests (ADR-0208).

The descriptor (``supported_capture_methods`` + ``supported_debug_transports`` +
``supported_introspection``) is fail-closed: an unconfigured runtime advertises *no*
capability, so the surface can never report a stubbed plane as working.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from kdive.providers.core.runtime import ConsoleCapabilities, ProviderRuntime, ProviderSupport
from kdive.providers.shared.runtime_paths import libvirt_system_handle


def _unconfigured_runtime() -> ProviderRuntime:
    """Build a runtime with only the required ports, none of the descriptor fields set."""
    port = cast(Any, object())
    return ProviderRuntime(
        system_handle=libvirt_system_handle,
        profile_policy=port,
        provisioner=port,
        installer=port,
        booter=port,
        connector=port,
        controller=port,
        retriever=port,
        crash_postmortem=port,
        vmcore_introspector=port,
        live_introspector=port,
    )


def test_unconfigured_runtime_reports_empty_for_every_capability_field() -> None:
    runtime = _unconfigured_runtime()

    assert runtime.support.capture_methods == frozenset()
    assert runtime.support.debug_transports == frozenset()
    assert runtime.support.introspection == frozenset()


def test_runtime_rejects_advertised_snapshots_without_snapshotter() -> None:
    support = ProviderSupport(supports_snapshots=True)

    with pytest.raises(ValueError, match=r"support\.supports_snapshots.*snapshot"):
        replace(_unconfigured_runtime(), support=support)


@pytest.mark.parametrize("capability", ["supports_diagnostic_sysrq", "supports_crash_watch"])
@pytest.mark.parametrize("missing_bundle", [False, True])
def test_runtime_rejects_advertised_console_operation_without_reader(
    capability: str, missing_bundle: bool
) -> None:
    support = replace(ProviderSupport(), **{capability: True})
    console = None if missing_bundle else ConsoleCapabilities(snapshotter=cast(Any, object()))
    with pytest.raises(ValueError, match="console.*reader"):
        replace(_unconfigured_runtime(), support=support, console=console)


def test_runtime_rejects_snapshotter_without_advertised_snapshots() -> None:
    with pytest.raises(ValueError, match=r"support\.supports_snapshots.*snapshot"):
        replace(_unconfigured_runtime(), snapshot=cast(Any, object()))


def test_runtime_rejects_advertised_traffic_capture_without_capturer() -> None:
    support = ProviderSupport(supports_traffic_capture=True)

    with pytest.raises(ValueError, match=r"support\.supports_traffic_capture.*traffic_capturer"):
        replace(_unconfigured_runtime(), support=support)


def test_runtime_rejects_traffic_capturer_without_advertised_support() -> None:
    with pytest.raises(ValueError, match=r"support\.supports_traffic_capture.*traffic_capturer"):
        replace(_unconfigured_runtime(), traffic_capturer=cast(Any, object()))


def test_runtime_accepts_advertised_capabilities_with_matching_ports() -> None:
    port = cast(Any, object())
    support = ProviderSupport(supports_snapshots=True, supports_traffic_capture=True)

    runtime = replace(
        _unconfigured_runtime(),
        support=support,
        snapshot=port,
        traffic_capturer=port,
    )

    assert runtime.support == support


@pytest.mark.parametrize("provider", ["local", "remote", "fault", "byo"])
@pytest.mark.parametrize("recorded_name", [None, "", "custom opaque target"])
def test_runtime_owns_operation_target_after_rebinding(
    provider: str, recorded_name: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.providers.byo_host.composition import build_runtime as byo
    from kdive.providers.fault_inject.composition import build_runtime as fault
    from kdive.providers.local_libvirt.composition import build_runtime as local
    from kdive.providers.remote_libvirt import composition as remote_composition
    from kdive.providers.remote_libvirt.composition import build_runtime as remote
    from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
    from kdive.security.secrets.secret_registry import SecretRegistry

    monkeypatch.setattr(
        remote_composition,
        "remote_config_for_resource",
        lambda _name: RemoteLibvirtConfig(
            uri="qemu+tls://target.example/system",
            cert_refs=TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="ca"),
            concurrent_allocation_cap=1,
        ),
    )
    runtime = {
        "local": lambda: local(secret_registry=SecretRegistry()),
        "remote": lambda: remote(secret_registry=SecretRegistry()),
        "fault": fault,
        "byo": byo,
    }[provider]()
    system_id = uuid4()
    for bound in (runtime, runtime.for_resource("target-resource")):
        if provider == "byo":
            with pytest.raises(CategorizedError) as exc:
                bound.system_handle(system_id, recorded_name)
            assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED
        else:
            prefix = "fault-inject" if provider == "fault" else "kdive"
            assert bound.system_handle(system_id, recorded_name) == (
                recorded_name or f"{prefix}-{system_id}"
            )


def test_runtime_requires_system_handle_policy() -> None:
    port = cast(Any, object())
    with pytest.raises(TypeError, match="system_handle"):
        ProviderRuntime(  # ty: ignore[missing-argument] - exercise the required assembly contract.
            profile_policy=port,
            provisioner=port,
            installer=port,
            booter=port,
            connector=port,
            controller=port,
            retriever=port,
            crash_postmortem=port,
            vmcore_introspector=port,
            live_introspector=port,
        )
