"""Operator configuration for the remote-libvirt provider (ADR-0076, ADR-0077, ADR-0112).

The provider is opt-in: composition registers it only when a ``[[remote_libvirt]]`` instance is
declared in the ``systems.toml`` inventory (ADR-0112). The connection identity — URI, TLS client
cert/key/CA refs (secrets-by-reference, never material), gdbstub listen address, base image, and
per-host allocation cap — is resolved **per op** from that reconciled inventory instance, never
from the removed ``KDIVE_REMOTE_LIBVIRT_*`` singleton env vars (M2.6 Phase 3, #395). Reading the
config is deferred to discovery/connection time so the runtime stays buildable without it
(ADR-0076).

The libvirt host knobs that the v2 inventory model does not carry — storage pool, network, and
QEMU machine type — remain operational ``KDIVE_REMOTE_LIBVIRT_*`` env settings (they are host
topology, not declarative inventory).
"""

from __future__ import annotations

from dataclasses import dataclass

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import ImageEntry, InventoryDoc, RemoteLibvirtInstance, StagedSource
from kdive.inventory.path import systems_toml_path
from kdive.providers.remote_libvirt.connection.uri_validation import validate_remote_uri
from kdive.providers.remote_libvirt.settings import (
    REMOTE_LIBVIRT_MACHINE,
    REMOTE_LIBVIRT_NETWORK,
    REMOTE_LIBVIRT_STORAGE_POOL,
)

_DEFAULT_STORAGE_POOL = "default"
_DEFAULT_NETWORK = "default"
# i440fx by default: under q35, libvirt places each virtio device behind an
# auto-added pcie-root-port, and on QEMU 10.x those devices can come up in
# D3cold ("Unable to change power state from D3cold to D0, device inaccessible"),
# so the virtio root disk never appears and the guest hangs in the initramfs.
# i440fx puts virtio on the legacy PCI bus and sidesteps it. Operators who need
# q35 can set KDIVE_REMOTE_LIBVIRT_MACHINE=q35 once their host topology powers
# the root ports correctly.
_DEFAULT_MACHINE = "pc"


@dataclass(frozen=True, slots=True)
class TlsCertRefs:
    """Secret references (not material) for the mutual-TLS client identity + CA."""

    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str


@dataclass(frozen=True, slots=True)
class RemoteLibvirtConfig:
    """The resolved remote host: validated URI, cert refs, host-level knobs.

    ``uri`` / ``cert_refs`` / ``gdb_addr`` / the gdbstub port range / ``concurrent_allocation_cap``
    come from the declared ``[[remote_libvirt]]`` inventory instance (ADR-0112). ``storage_pool`` /
    ``network`` / ``machine`` are host topology not in the v2 model (ADR-0080 §5) and keep their
    operational env defaults. ``gdb_addr`` is the ACL'd security boundary (ADR-0079) and is always
    present when sourced from the inventory (the instance field is required).
    """

    uri: str
    cert_refs: TlsCertRefs
    concurrent_allocation_cap: int
    storage_pool: str = _DEFAULT_STORAGE_POOL
    network: str = _DEFAULT_NETWORK
    machine: str = _DEFAULT_MACHINE
    gdb_addr: str | None = None
    gdb_port_min: int = 47000
    gdb_port_max: int = 47099

    @property
    def acl_probe_port(self) -> int:
        """The lowest gdbstub port, reserved for the ACL probe and never assigned to a System.

        The ``gdbstub_acl`` diagnostic TCP-connects to this port (ADR-0184); reserving it keeps the
        port listener-free so the probe never attaches to — and pauses — a live System's gdbstub.
        """
        return self.gdb_port_min

    @property
    def assignable_gdb_port_min(self) -> int:
        """The lowest gdbstub port a System may be assigned (one above the reserved probe port)."""
        return self.gdb_port_min + 1


def _load_inventory_doc() -> InventoryDoc | None:
    """Load and validate the ``systems.toml`` document, or ``None`` when the file is absent.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the inventory file is present but
            unreadable/malformed/invalid (the parse error is surfaced verbatim).
    """
    try:
        return load_inventory_optional(systems_toml_path())
    except InventoryError as exc:
        raise CategorizedError(
            f"systems.toml is present but invalid: {exc}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc


def _load_remote_instances() -> list[RemoteLibvirtInstance]:
    """Load the ``[[remote_libvirt]]`` instances from ``systems.toml``."""
    doc = _load_inventory_doc()
    if doc is None:
        return []
    return list(doc.remote_libvirt)


def is_remote_libvirt_configured() -> bool:
    """True when ``systems.toml`` declares at least one ``[[remote_libvirt]]`` instance.

    This is the composition opt-in gate, invoked at app/CLI startup. It **degrades** rather than
    raises: a missing inventory file means "nothing declared" (not configured), and a
    present-but-malformed file is treated as not-configured here too — so a bad operator edit to
    the shared ``systems.toml`` cannot crash the whole MCP server or the unrelated providers
    (ADR-0112's fault-isolation contract). The precise parse error still surfaces fail-closed at
    op time via :func:`remote_config_for_resource`.
    """
    try:
        return bool(_load_remote_instances())
    except CategorizedError:
        return False


def unbound_remote_config() -> RemoteLibvirtConfig:
    """The default per-op ``config_factory``: a remote port used without a bound host.

    Every per-op remote-libvirt port resolves its host config from the granted Resource's name
    (the resolver binds it via ``ProviderRuntime.rebind_for_resource``, ADR-0187, #395). A port
    that reaches this default was used unbound — a wiring bug, not an operator error — so it fails
    loudly rather than guessing a host.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` always.
    """
    raise CategorizedError(
        "remote-libvirt port used without a bound host; the runtime must be rebound to the "
        "granted resource (ProviderRuntime.for_resource) before a per-op call",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def resolve_base_image_staged_volume_for(resource_name: str) -> str:
    """Return the staged base-image volume for the named ``[[remote_libvirt]]`` instance.

    Resolves the named instance's ``base_image`` cross-reference to its ``[[image]]`` entry and
    returns that image's operator-staged ``volume`` — the name the provider looks up on the host's
    storage pool at provision time, and the same name the base-image-staging diagnostic probes
    (ADR-0187, #395).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no instance named ``resource_name`` is
            declared, the inventory is malformed, the ``base_image`` names no declared
            ``[[image]]``, or that image's source is not ``staged``.
    """
    doc = _load_inventory_doc()
    images = doc.image if doc is not None else []
    instances = list(doc.remote_libvirt) if doc is not None else []
    instance = next((inst for inst in instances if inst.name == resource_name), None)
    if instance is None:
        names = sorted(inst.name for inst in instances)
        raise CategorizedError(
            f"no [[remote_libvirt]] instance named {resource_name!r} is declared in systems.toml "
            f"(declared: {names})",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return _staged_volume_for_instance(instance, images)


def _staged_volume_for_instance(instance: RemoteLibvirtInstance, images: list[ImageEntry]) -> str:
    image = next((img for img in images if img.name == instance.base_image), None)
    if image is None:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].base_image={instance.base_image!r} names no "
            "declared [[image]] in systems.toml",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if not isinstance(image.source, StagedSource):
        raise CategorizedError(
            f"image {instance.base_image!r} source is {image.source.kind!r}, not 'staged'; only a "
            "staged image has an operator-staged base volume to verify",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return image.source.volume


def _parse_gdbstub_range(instance: RemoteLibvirtInstance) -> tuple[int, int]:
    """Parse the instance ``gdbstub_range`` (``"min:max"``) into a validated ``(min, max)``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the range is not ``min:max`` of integers,
            a port is outside 1..65535, the range is inverted, or it spans fewer than two ports
            (the lowest is reserved for the ACL probe per ADR-0184, so a one-port range leaves
            nothing assignable to a System).
    """
    raw = instance.gdbstub_range
    parts = raw.split(":")
    if len(parts) != 2:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].gdbstub_range={raw!r} is not 'min:max'",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    try:
        low, high = int(parts[0]), int(parts[1])
    except ValueError:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].gdbstub_range={raw!r} has non-integer ports",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    for port in (low, high):
        if port < 1 or port > 65535:
            raise CategorizedError(
                f"remote_libvirt[{instance.name}].gdbstub_range port {port} is outside 1..65535",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
    if low > high:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].gdbstub_range={raw!r} is inverted",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if low == high:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].gdbstub_range={raw!r} must span at least 2 ports "
            "(the lowest is reserved for the ACL probe; the rest are assignable to Systems)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return low, high


def _build_config(instance: RemoteLibvirtInstance) -> RemoteLibvirtConfig:
    """Map one validated ``[[remote_libvirt]]`` instance onto :class:`RemoteLibvirtConfig`.

    Validates the URI (mutual-TLS-safe) and the gdbstub range, then fills the host-topology knobs
    (storage pool / network / machine) from the operational env settings (they are not in the v2
    inventory model).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the URI is not mutual-TLS-safe (wrong
            scheme, ``no_verify``, or an operator-set ``pkipath``) or the gdbstub range is
            malformed, out of range, or inverted.
    """
    validate_remote_uri(instance.uri)
    gdb_port_min, gdb_port_max = _parse_gdbstub_range(instance)
    return RemoteLibvirtConfig(
        uri=instance.uri,
        cert_refs=TlsCertRefs(
            client_cert_ref=instance.client_cert_ref,
            client_key_ref=instance.client_key_ref,
            ca_cert_ref=instance.ca_cert_ref,
        ),
        concurrent_allocation_cap=instance.concurrent_allocation_cap,
        storage_pool=config.get(REMOTE_LIBVIRT_STORAGE_POOL) or _DEFAULT_STORAGE_POOL,
        network=config.get(REMOTE_LIBVIRT_NETWORK) or _DEFAULT_NETWORK,
        machine=config.get(REMOTE_LIBVIRT_MACHINE) or _DEFAULT_MACHINE,
        gdb_addr=instance.gdb_addr,
        gdb_port_min=gdb_port_min,
        gdb_port_max=gdb_port_max,
    )


def remote_config_for_resource(resource_name: str) -> RemoteLibvirtConfig:
    """Resolve the remote-libvirt connection config for the resource named ``resource_name``.

    The reconcile keys config-owned resources on ``(kind, name)``, so a remote resource row's
    ``name`` is its ``[[remote_libvirt]]`` instance name (ADR-0112/0187). Selecting by name lets a
    per-op call resolve the *allocated* host instead of the lone declared one (#395).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no instance named ``resource_name`` is
            declared, the inventory is malformed, or the selected instance fails validation.
    """
    instances = _load_remote_instances()
    instance = next((inst for inst in instances if inst.name == resource_name), None)
    if instance is None:
        names = sorted(inst.name for inst in instances)
        raise CategorizedError(
            f"no [[remote_libvirt]] instance named {resource_name!r} is declared in systems.toml "
            f"(declared: {names})",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return _build_config(instance)


def all_remote_configs() -> list[RemoteLibvirtConfig]:
    """Resolve every declared ``[[remote_libvirt]]`` instance's config (fleet-wide callers).

    For host-agnostic callers that operate over the whole fleet — discovery enumeration, the
    console-hosting bootstrap, and fan-out diagnostics/reapers (ADR-0187) — rather than a single
    allocated host.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the inventory is malformed or any declared
            instance fails validation.
    """
    return [_build_config(inst) for inst in _load_remote_instances()]


def remote_instance_names() -> list[str]:
    """The declared ``[[remote_libvirt]]`` instance names, parsed but not connection-validated.

    For fan-out callers (the doctor) that enumerate the fleet at assembly time and resolve each
    host's config lazily at op/probe time via :func:`remote_config_for_resource` — so a single
    malformed instance surfaces as that host's per-check error, not an assembly crash (ADR-0187).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the inventory file is present but
            unparseable (the same fail-closed contract as the other resolvers).
    """
    return [inst.name for inst in _load_remote_instances()]


def all_remote_configs_by_name() -> list[tuple[str, RemoteLibvirtConfig]]:
    """Resolve every declared instance's ``(name, config)`` for name-keyed fan-out callers.

    The fan-out doctor pairs each host's config with its instance name so a per-host probe can
    resolve the by-name staged base-image volume (ADR-0187, #395).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the inventory is malformed or any declared
            instance fails validation.
    """
    return [(inst.name, _build_config(inst)) for inst in _load_remote_instances()]
