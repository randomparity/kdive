"""A buildable BYO kind refuses every host operation (ADR-0813)."""

from __future__ import annotations

import asyncio
import socket
import subprocess
from pathlib import Path
from typing import cast

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.providers.assembly.composition import ProviderComposition
from kdive.providers.byo_host import composition as byo
from kdive.providers.core.runtime import ProviderSupport
from kdive.providers.ports.lifecycle import Booter, Connector, Controller, Installer, Provisioner
from kdive.providers.ports.retrieve import (
    CrashPostmortem,
    LiveIntrospector,
    Retriever,
    VmcoreIntrospector,
)


def test_every_required_port_and_policy_refuses_without_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("skeleton attempted host I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    runtime = byo.build_runtime()
    assert runtime.profile_policy.kind is ResourceKind.BYO_HOST
    for field, protocol in (
        ("profile_policy", ProfilePolicy),
        ("provisioner", Provisioner),
        ("installer", Installer),
        ("booter", Booter),
        ("connector", Connector),
        ("controller", Controller),
        ("retriever", Retriever),
        ("crash_postmortem", CrashPostmortem),
        ("vmcore_introspector", VmcoreIntrospector),
        ("live_introspector", LiveIntrospector),
    ):
        port = getattr(runtime, field)
        for name, member in vars(protocol).items():
            if name.startswith("_") or not callable(member):
                continue
            with pytest.raises(CategorizedError) as exc:
                getattr(port, name)()
            assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED, (field, name)
    assert runtime.platform_root_cmdline is None
    assert runtime.support == ProviderSupport()
    assert runtime.support.supports_build is False
    assert runtime.support.supports_boot is False
    for field in (
        "debug",
        "rootfs",
        "resource_details",
        "console",
        "bootstrap_key",
        "binding",
        "snapshot",
        "traffic_capturer",
        "traffic_capture_operation",
        "external_boot",
        "external_boot_preparation",
        "external_boot_recovery_objects",
        "authority",
    ):
        assert getattr(runtime, field) is None, field


def test_explicit_enable_builds_and_discovery_never_touches_pool() -> None:
    resolver = ProviderComposition().build_provider_resolver(
        enable_local_libvirt=False,
        enable_remote_libvirt=False,
        enable_fault_inject=False,
        enable_byo_host=True,
    )
    assert resolver.registered_kinds() == frozenset({ResourceKind.BYO_HOST})
    runtime = resolver.resolve(ResourceKind.BYO_HOST)
    assert byo.discovery_registration().creates is False
    asyncio.run(runtime.register_discovery(cast("AsyncConnectionPool", object())))


_VALID = """schema_version = 2
[[byo_host]]
name = "target-x"
arch = "x86_64"
ssh_target = "root@target-x.example"
ssh_key_ref = "target-ssh-key"
baseline_kernel = "6.17.0-baseline"
console_device = "ttyS0"
vcpus = 8
memory_mb = 32768
[byo_host.oob]
kind = "redfish"
endpoint = "https://bmc-x.example"
username_ref = "bmc-user"
password_ref = "bmc-password"  # pragma: allowlist secret
"""


@pytest.mark.parametrize("text,enabled", [(None, False), ("broken [[", False), (_VALID, True)])
def test_inventory_opt_in_preserves_existing_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None, enabled: bool
) -> None:
    path = tmp_path / "systems.toml"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr(byo, "systems_toml_path", lambda: path)
    assert byo.is_configured() is enabled
    resolver = ProviderComposition().build_provider_resolver(
        enable_local_libvirt=True,
        enable_remote_libvirt=False,
        enable_fault_inject=False,
    )
    expected = {ResourceKind.LOCAL_LIBVIRT}
    if enabled:
        expected.add(ResourceKind.BYO_HOST)
    assert resolver.registered_kinds() == frozenset(expected)
