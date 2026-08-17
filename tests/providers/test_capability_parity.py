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

Declaration parity is necessary and not sufficient: two providers can agree on a capability nothing
reads. The second half of this module (ADR-0563, #1942) therefore holds the component-source maps to
a stronger rule — every declared ``ComponentKind`` must be one some code path passes to
``reject_unsupported_component_source``, so a declaration cannot promise a rejection that never
happens.
"""

from __future__ import annotations

import ast
import dataclasses
from enum import Enum
from pathlib import Path

import kdive
from kdive.components import references
from kdive.components.references import ROOTFS_COMPONENT, ComponentKind
from kdive.components.validation import ComponentSourceCapabilities
from kdive.providers.assembly.composition import (
    build_fault_inject_runtime,
    build_local_runtime,
    build_remote_runtime,
)
from kdive.providers.core.runtime import ProviderRuntime, ProviderSupport
from kdive.security.secrets.secret_registry import SecretRegistry

# Remote now accepts a supplied ROOTFS from the worker-host `local` source kind (ADR-0440, #1433),
# so that gap is closed and unwaived. It deliberately does NOT accept `catalog`: a remote catalog
# image is the operator-staged base_image_volume the provider references by name, so a catalog
# component source would be a download-then-re-upload duplicate of a volume already on the host.
_ROOTFS_CATALOG_WAIVER = (
    "remote ROOTFS accepts a supplied `local` qcow2 (ADR-0440, #1433) but not `catalog`: a remote "
    "catalog image is the operator-staged base_image_volume referenced by name, so a catalog "
    "source would download-then-re-upload it to the host it already lives on."
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
        "remote validates a supplied `local` base image at staging time (allowlist + qcow2 magic, "
        "ADR-0440), like local's `upload` kind (ADR-0434) — not at admission; admission checks the "
        "source kind. So remote wires no admission-time rootfs validator."
    ),
    "support.capture_methods:fadump": (
        "FADUMP is a POWER-only (pseries firmware-assisted) mechanism, resolved only for a ppc64le "
        "System that also carries a crashkernel reservation (ADR-0349)."
    ),
    "support.component_sources.rootfs:catalog": _ROOTFS_CATALOG_WAIVER,
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


# --- Declared-vs-enforced component kinds (ADR-0563, #1942) --------------------------------------
#
# The parity guards above compare what two providers *declare*. That is the wrong question on its
# own: for five of the six `ComponentKind` values the maps agreed with each other while nothing
# read them, so "parity" meant "the two files match", not "the two providers behave the same"
# (#1942). A declared kind is only load-bearing if some code path passes it to
# `reject_unsupported_component_source` — the sole consumer of `accepted_component_sources`.
#
# `_enforced_component_kinds` finds that by parsing `src/kdive/` rather than by importing and
# calling, because enforcement is a property of the call graph, not of any one runtime value: a
# call site naming the kind is exactly what the removed declarations lacked. A rename of the helper
# empties the enforced set, which reddens `test_every_declared_component_kind_is_enforced` and
# `test_rootfs_is_declared_by_every_provider_and_enforced` rather than passing vacuously.
#
# What it proves and what it does not: a call site naming the kind exists somewhere in `src/kdive/`.
# It does not prove that site is reachable from a request, so a call in dead code would satisfy it.
# That is the weaker claim on purpose — deciding reachability needs a whole-program analysis, and
# the defect this exists to catch (#1942) was the absence of any call site at all, not an
# unreachable one. The stronger property is carried by the per-kind behaviour tests beside each
# provider.

_ENFORCEMENT_HELPER = "reject_unsupported_component_source"

# `ComponentKind` members reachable by name from `kdive.components.references` — the
# `ROOTFS_COMPONENT`-style aliases every call site actually writes.
_KIND_ALIASES: dict[str, ComponentKind] = {
    name: value for name, value in vars(references).items() if isinstance(value, ComponentKind)
}


def _kind_from_node(node: ast.expr) -> ComponentKind | None:
    """Resolve a ``component_kind=`` argument to a member, or ``None`` if it is not a literal."""
    if isinstance(node, ast.Name):
        return _KIND_ALIASES.get(node.id)
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == ComponentKind.__name__
    ):
        return ComponentKind.__members__.get(node.attr)
    return None


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _scan_enforcement_call_sites() -> tuple[set[ComponentKind], list[str]]:
    """Return the kinds ``src/kdive/`` enforces, plus one label per unresolvable call site.

    An unresolvable site (``component_kind`` supplied by a variable, a call, or ``**kwargs``)
    is reported rather than skipped: silently dropping it would shrink the enforced set and let
    a declaration pass as enforced, or as inert, on no evidence either way.
    """
    source_root = Path(kdive.__file__).resolve().parent
    enforced: set[ComponentKind] = set()
    unresolvable: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee_name(node) != _ENFORCEMENT_HELPER:
                continue
            argument = next(
                (kw.value for kw in node.keywords if kw.arg == "component_kind"),
                None,
            )
            kind = None if argument is None else _kind_from_node(argument)
            if kind is None:
                unresolvable.append(f"{path.relative_to(source_root.parent)}:{node.lineno}")
            else:
                enforced.add(kind)
    return enforced, unresolvable


def _enforced_component_kinds() -> set[ComponentKind]:
    return _scan_enforcement_call_sites()[0]


def _declared_component_sources() -> dict[str, ComponentSourceCapabilities]:
    """Every provider's component-source declaration, keyed by provider name.

    Enumerated rather than discovered, so a fourth provider is not silently skipped: the provider
    set is pinned in :func:`test_rootfs_is_declared_by_every_provider_and_enforced`, which reddens
    until whoever adds one adds it here too.
    """
    registry = SecretRegistry()
    runtimes = (
        build_local_runtime(secret_registry=registry),
        build_remote_runtime(secret_registry=registry),
        build_fault_inject_runtime(),
    )
    return {
        runtime.support.component_sources.provider: runtime.support.component_sources
        for runtime in runtimes
    }


def test_every_enforcement_call_site_names_a_literal_component_kind() -> None:
    _, unresolvable = _scan_enforcement_call_sites()
    assert not unresolvable, (
        f"{_ENFORCEMENT_HELPER} is called with a `component_kind` this guard cannot resolve to a "
        "ComponentKind member, so it cannot tell which declared kinds are enforced. Pass the "
        "member directly, or teach _kind_from_node in this file to resolve the new form:\n"
        + "\n".join(f"  - {site}" for site in unresolvable)
    )


def test_every_declared_component_kind_is_enforced() -> None:
    declarations = _declared_component_sources()
    assert declarations, "no provider component-source declarations were collected"

    enforced = _enforced_component_kinds()
    assert enforced, (
        f"no call site in src/kdive/ passes a literal component kind to {_ENFORCEMENT_HELPER}, so "
        "no declared kind is enforced anywhere. Was the helper renamed?"
    )

    inert = sorted(
        f"{provider}:{kind.value}"
        for provider, caps in declarations.items()
        for kind in caps.accepted_component_sources
        if kind not in enforced
    )
    assert not inert, (
        "a provider declares accepted component sources for kinds that no code path passes to "
        f"{_ENFORCEMENT_HELPER}, so the declaration promises a rejection that never happens "
        "(#1942, ADR-0563). Either add the enforcing call site where the ref enters a System or "
        "Run, or remove the kind from the provider's map:\n"
        + "\n".join(f"  - {entry}" for entry in inert)
    )


def test_rootfs_is_declared_by_every_provider_and_enforced() -> None:
    """The positive half: narrowing must not empty the maps or drop the one enforced kind."""
    assert ROOTFS_COMPONENT in _enforced_component_kinds()
    declarations = _declared_component_sources()
    assert set(declarations) == {"local-libvirt", "remote-libvirt", "fault-inject"}
    for provider, caps in declarations.items():
        accepted = caps.accepted_component_sources.get(ROOTFS_COMPONENT, frozenset())
        assert "local" in accepted, f"{provider} must still accept a `local` ROOTFS source"
