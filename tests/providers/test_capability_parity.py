"""Provider capability parity guard (#1428, epic #1423).

Every gap epic #1423 collected shares one root cause: a capability is added to ``ProviderRuntime``
and wired for one provider with nothing reporting the other's silence. ``ProviderSupport`` defaults
every flag fail-closed and the optional ``ProviderRuntime`` port fields default to ``None``, so an
omission produces no error, no warning, and no failing test (``providers/core/runtime.py``). That is
the correct safety property, but it also lets local↔remote divergence resume on the next capability.

This module reflects over both runtimes field by field and fails when local-libvirt advertises a
``ProviderRuntime`` port or ``ProviderSupport`` capability that remote-libvirt neither advertises
nor waives with a documented reason. The waiver table below is the record a future audit reads
instead of re-deriving each correct-and-deliberate difference. Both compositions are buildable
without operator config (ADR-0076), so this runs in the default ``just test`` gate with no libvirt
host.
"""

from __future__ import annotations

import dataclasses
from enum import Enum

from kdive.components.validation import ComponentSourceCapabilities
from kdive.providers.assembly.composition import build_local_runtime, build_remote_runtime
from kdive.providers.core.runtime import ProviderRuntime, ProviderSupport
from kdive.security.secrets.secret_registry import SecretRegistry

_ROOTFS_SOURCE_WAIVER = (
    "remote rootfs is fixed by the operator-staged base image; a supplied rootfs source is "
    "deferred to #1433 pending local supplied-rootfs #743 (parity-lockstep, maintainer decision)."
)

# Capability keys local-libvirt advertises that remote-libvirt deliberately does not, each mapped to
# the reason the difference is correct. A gap not listed here reddens the parity guard below; a
# key listed here whose gap no longer exists reddens the stale-waiver guard. Removing a real
# capability to silence the guard is the wrong fix — wire it into remote or waive it with a reason.
_WAIVERS: dict[str, str] = {
    "port:platform_root_cmdline": (
        "remote's in-guest bootloader owns the root device; injecting a platform root= would "
        "override the grub root=UUID= inherited via grubby --copy-default (ADR-0183, #587)."
    ),
    "port:bootstrap_key.customizer": (
        "bootstrap-key injection is a virt-customize overlay customizer that cannot reach a remote "
        "disk (ADR-0289); remote injects over the guest agent from its own provisioner (ADR-0291)."
    ),
    "port:rootfs.validator": (
        "RemoteLibvirtProfilePolicy.rootfs_source returns None unconditionally, so a rootfs "
        "validator is unreachable. " + _ROOTFS_SOURCE_WAIVER
    ),
    "support.capture_methods:fadump": (
        "FADUMP is a POWER-only (pseries firmware-assisted) mechanism, resolved only for a ppc64le "
        "System that also carries a crashkernel reservation (ADR-0349)."
    ),
    "support.component_sources.rootfs:catalog": _ROOTFS_SOURCE_WAIVER,
    "support.component_sources.rootfs:local": _ROOTFS_SOURCE_WAIVER,
    "support.component_sources.initrd:local": (
        "an initrd component source is future work on both providers, tracked by the parity epic "
        "(#1423); local declares it first."
    ),
}


def _member_key(member: object) -> str:
    """Return a stable lowercase key for a frozenset member (StrEnum value or literal string)."""
    if isinstance(member, Enum):
        return str(member.value)
    return str(member)


def _is_capability_group(value: object) -> bool:
    """Report whether ``value`` is a capability-group dataclass defined next to ``ProviderRuntime``.

    Capability groups (``DebugCapabilities``, ``RootfsCapabilities``, ...) live in the runtime
    module; concrete provider ports (the provisioner, installer, ...) live elsewhere. Reflecting one
    level into a group compares its sub-fields; a plain port is compared by presence alone.
    """
    return dataclasses.is_dataclass(value) and type(value).__module__ == ProviderRuntime.__module__


def _component_source_capabilities(sources: ComponentSourceCapabilities) -> set[str]:
    keys: set[str] = set()
    for kind, source_kinds in sources.accepted_component_sources.items():
        for source in source_kinds:
            keys.add(f"support.component_sources.{_member_key(kind)}:{_member_key(source)}")
    return keys


def _advertised_support_capabilities(support: ProviderSupport) -> set[str]:
    """Return ``support.<field>`` keys for each flag, frozenset member, and source kind."""
    caps: set[str] = set()
    for field in dataclasses.fields(support):
        value = getattr(support, field.name)
        if isinstance(value, bool):
            if value:
                caps.add(f"support.{field.name}")
        elif isinstance(value, frozenset):
            for member in value:
                caps.add(f"support.{field.name}:{_member_key(member)}")
    return caps | _component_source_capabilities(support.component_sources)


def _advertised_port_capabilities(runtime: ProviderRuntime) -> set[str]:
    """Return ``port:<field>`` / ``port:<field>.<sub>`` keys for each non-None runtime port.

    Reflecting one level into capability-group dataclasses means a new sub-field on a group is
    compared automatically. The ``support`` aggregate is handled by
    :func:`_advertised_support_capabilities`.
    """
    caps: set[str] = set()
    for field in dataclasses.fields(runtime):
        if field.name == "support":
            continue
        value = getattr(runtime, field.name)
        if value is None:
            continue
        if _is_capability_group(value):
            for sub in dataclasses.fields(value):
                if getattr(value, sub.name) is not None:
                    caps.add(f"port:{field.name}.{sub.name}")
        else:
            caps.add(f"port:{field.name}")
    return caps


def _advertised_capabilities(runtime: ProviderRuntime) -> set[str]:
    return _advertised_port_capabilities(runtime) | _advertised_support_capabilities(
        runtime.support
    )


def _local_and_remote_capabilities() -> tuple[set[str], set[str]]:
    registry = SecretRegistry()
    local = _advertised_capabilities(build_local_runtime(secret_registry=registry))
    remote = _advertised_capabilities(build_remote_runtime(secret_registry=registry))
    return local, remote


def test_remote_libvirt_advertises_or_waives_every_local_capability() -> None:
    local, remote = _local_and_remote_capabilities()
    unwaived = sorted(gap for gap in local - remote if gap not in _WAIVERS)
    assert not unwaived, (
        "local-libvirt advertises capabilities that remote-libvirt neither advertises nor waives.\n"
        "For each, either wire the capability into remote-libvirt's composition, or add a waiver "
        "with a documented reason to _WAIVERS in this file:\n"
        + "\n".join(f"  - {key}" for key in unwaived)
    )


def test_waiver_table_has_no_stale_entries() -> None:
    local, remote = _local_and_remote_capabilities()
    gaps = local - remote
    stale = sorted(key for key in _WAIVERS if key not in gaps)
    assert not stale, (
        "_WAIVERS lists capabilities that are no longer local-only gaps — remote now advertises "
        "them, or local dropped them. Remove the stale waivers:\n"
        + "\n".join(f"  - {key}" for key in stale)
    )
